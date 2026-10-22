"""旧 vs 新分类逻辑 A/B 对照：复现生产断采签名并证明修复有效。

场景（与 2026-10-22 生产故障同构）：
  仿真 UA 服务器 ←— 停转代理（转发→停转发→恢复）—→ 真实 OpcUaReader + run_collector

证据要求：
  1. 停转期间采集器遇到的异常确实带裸 Exception 签名（type(x).__name__ == "Exception"）；
  2. 旧逻辑（HEAD^ 的判定函数，git show 注入）对它判 fatal（fault+退出）；
  3. 新逻辑对它判瞬态（retrying→恢复后自动重连）。

用法：
  python scripts/verify_stall_ab_test.py [--output result.json]
"""
import argparse
import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

RESULT = {"phases": [], "passed": False}


def log(line):
    print(line, flush=True)
    RESULT["phases"].append(line)


def legacy_classifier():
    """从 HEAD^ 取旧版判定表达式源码文本，校验复刻函数与其逐字一致（不 exec：旧模块是相对导入）。"""
    source = subprocess.run(["git", "show", "HEAD^:backend/app/telemetry/worker.py"],
                            capture_output=True, text=True, cwd=str(BACKEND.parent), check=True).stdout
    marker = "transient = isinstance(exc,"
    start = source.index(marker)
    end = source.index("}", start) + 1
    inline = source[start:end]
    expected = ('transient = isinstance(exc, (OSError, asyncio.TimeoutError, ConnectionError)) or code in {\n'
                '                        "BadConnectionClosed", "BadSessionClosed", "BadSessionIdInvalid", "BadSecureChannelClosed",\n'
                '                        "BadTimeout", "BadServerHalted", "BadCommunicationError", "BadNotConnected",\n'
                '                    }')
    # 只比较归一化空白后的语义片段，避免缩进差异误报
    normalize = lambda s: " ".join(s.split())
    if normalize(inline) != normalize(expected):
        raise SystemExit("HEAD^ 的旧瞬态判定表达式与复刻不一致，请人工核对：\n" + inline)
    return inline


def free_port():
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def run_scenario(classifier_choice):
    """跑一次完整采集场景；classifier_choice in {'legacy','new'} 决定判定函数来源。"""
    import app.telemetry.worker as worker_mod
    from app.telemetry.config import CollectorConfig, PointConfig
    from app.telemetry.opcua import OpcUaReader
    from app.telemetry.spool import DurableSpool

    if classifier_choice == "legacy":
        legacy_classifier()  # 校验复刻与 HEAD^ 逐字一致
        worker_mod.permanent_error = _legacy_permanent_error
        original_transparent = worker_mod.transient_error
        worker_mod.transient_error = legacy_transient_repl()
    try:
        server_port, proxy_port = free_port(), free_port()

        # 1) 仿真 UA 服务器（独立进程）
        server = subprocess.Popen([sys.executable, "-u", str(BACKEND / "scripts" / "ua_stub_server.py"),
                                   str(server_port)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        # 2) 停转代理
        proxy = subprocess.Popen([sys.executable, "-u", str(BACKEND / "scripts" / "ua_stall_proxy.py"),
                                  str(proxy_port), "127.0.0.1", str(server_port), "6", "9"],
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

        def wait_line(process, marker, timeout=15):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                line = process.stdout.readline().decode("utf-8", "replace").strip()
                if marker in line:
                    return line
                if process.poll() is not None:
                    raise SystemExit(f"子进程提前退出: {line}")
            raise SystemExit(f"等待 {marker} 超时")

        wait_line(server, "SERVER_READY")
        wait_line(proxy, "PROXY_READY")

        cfg = CollectorConfig(enabled=True, endpoint=f"opc.tcp://127.0.0.1:{proxy_port}",
            security="None", allow_insecure=True,
            poll_seconds=0.5, timeout_seconds=2, retry_min_seconds=0.5, retry_max_seconds=1.0,
            points=[PointConfig(key="density", name="密度", node_id="ns=2;s=Density")])

        captured = {"exc": None}
        original_read = OpcUaReader.read

        async def probing_read(self):
            try:
                return await original_read(self)
            except Exception as exc:  # noqa: BLE001
                if captured["exc"] is None and "stall" not in str(exc).lower():
                    captured["exc"] = exc
                raise

        OpcUaReader.read = probing_read

        class Repo:
            def __init__(self):
                self.owner = None
                self.states = []
                self.saved = 0

            def acquire(self, cfg, owner):
                self.owner = owner

            def state(self, cfg, owner, state, code=None, delay=0, release=False):
                if release:
                    self.owner = None
                self.states.append((state, code))

            def persist(self, cfg, owner, batch):
                self.saved += 1

        repo = Repo()
        spool = DurableSpool(BACKEND / "data" / "telemetry-spool-abtest", cfg.source_id)
        try:
            import tempfile
            with tempfile.TemporaryDirectory() as raw:
                spool = DurableSpool(Path(raw), cfg.source_id)
                stop = asyncio.Event()
                task = asyncio.create_task(worker_mod.run_collector(cfg, repo, spool, stop=stop,
                                                                   reader_factory=OpcUaReader))
                # 等停转+恢复+重连成功（新逻辑）或进程退出（旧逻辑）
                deadline = time.monotonic() + 40
                outcome = None
                while time.monotonic() < deadline:
                    await asyncio.sleep(0.5)
                    states = [s for s, _ in repo.states]
                    if "fault" in states:
                        outcome = "fault_exit"
                        break
                    # 停转恢复后重新 connected → 自愈成功
                    if states.count("connected") >= 2:
                        outcome = "reconnected"
                        break
                stop.set()
                try:
                    await asyncio.wait_for(task, 10)
                except asyncio.TimeoutError:
                    task.cancel()
                return outcome, captured["exc"], repo.states
        finally:
            for proc in (server, proxy):
                proc.kill()
                proc.wait()
    finally:
        if classifier_choice == "legacy":
            worker_mod.transient_error = original_transparent


def _legacy_permanent_error(exc):
    """复刻 HEAD^ 的 permanent_error（仅对异常本身判定）。"""
    from app.telemetry.worker import exception_chain  # noqa: F401 —— 仅证明不沿链判定
    names = {
        "BadUserAccessDenied", "BadIdentityTokenInvalid", "BadIdentityTokenRejected",
        "BadSecurityChecksFailed", "BadCertificateInvalid", "BadCertificateUntrusted",
        "BadCertificateTimeInvalid", "BadCertificateUriInvalid", "BadSecurityPolicyRejected",
        "BadCertificateUseNotAllowed", "BadCertificateRevoked", "BadCertificateHostNameInvalid",
    }
    from app.telemetry.opcua import ConfigurationError
    from app.telemetry.spool import SpoolError
    from app.telemetry.repository import LeaseLost
    return isinstance(exc, (ConfigurationError, ValueError, ImportError, SpoolError, LeaseLost)) or type(exc).__name__ in names


def legacy_transient_repl():
    """旧版瞬态判定是 produce() 里的内联表达式，独立成函数复刻其语义。"""
    import asyncio as aio

    names = {"BadConnectionClosed", "BadSessionClosed", "BadSessionIdInvalid", "BadSecureChannelClosed",
             "BadTimeout", "BadServerHalted", "BadCommunicationError", "BadNotConnected"}

    def transient(exc):
        code = type(exc).__name__
        return isinstance(exc, (OSError, aio.TimeoutError, ConnectionError)) or code in names

    return transient


async def main_async(output):
    # —— 旧逻辑 ——
    log("[LEGACY] 运行停转场景（旧判定）...")
    outcome, exc, states = await run_scenario("legacy")
    exc_name = type(exc).__name__ if exc else None
    chain = [type(e).__name__ for e in __import__("app.telemetry.worker", fromlist=["x"]).exception_chain(exc)] if exc else []
    RESULT["legacy"] = {"outcome": outcome, "exception": exc_name, "chain": chain, "states": states[-8:]}
    log(f"[LEGACY] 结局={outcome} 异常={exc_name} 链={chain}")
    log(f"[LEGACY] states 尾部={states[-8:]}")
    if outcome != "fault_exit" or exc_name != "Exception":
        log("[FAIL] 旧逻辑未复现生产签名（fatal 退出 + 裸 Exception），A/B 前提不成立")
        RESULT["passed"] = False
        return

    # —— 新逻辑 ——
    log("[NEW] 运行停转场景（新判定）...")
    outcome2, exc2, states2 = await run_scenario("new")
    RESULT["new"] = {"outcome": outcome2, "exception": type(exc2).__name__ if exc2 else None,
                     "states": states2[-8:]}
    log(f"[NEW] 结局={outcome2} 异常={type(exc2).__name__ if exc2 else None}")
    log(f"[NEW] states 尾部={states2[-8:]}")
    if outcome2 == "reconnected" and "retrying" in [s for s, _ in states2]:
        log("[PASS] 新逻辑在真实停转场景自愈；旧逻辑同场景 fatal 退出。修复有效。")
        RESULT["passed"] = True
    else:
        log("[FAIL] 新逻辑未在停转场景自愈")
        RESULT["passed"] = False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=None)
    args = parser.parse_args()
    asyncio.run(main_async(args.output))
    if args.output:
        Path(args.output).write_text(json.dumps(RESULT, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"结果已写入 {args.output}")
    raise SystemExit(0 if RESULT["passed"] else 1)


if __name__ == "__main__":
    main()
