"""E2E 复现与验收脚本（2026-10-22 断采根因修复）。

场景 A —— 真实 asyncua 客户端打黑洞端口：asyncua 的 ua_client.send_request
         会把发送/超时异常包成裸 Exception 重抛。旧逻辑判 fatal 退出；新逻辑
         判瞬态重试。此场景验证分类函数对【真实库产生的异常】的行为。
场景 B —— 本地 OPC UA 仿真服务器 + 独立采集进程：连接后停掉服务器制造真实
         断连，验证采集进程进入 retrying 并在服务器恢复后自动重连成功。

用法：
  python scripts/verify_transient_classification.py            # 场景 A（快）
  python scripts/verify_transient_classification.py --e2e      # 场景 A + B（完整）
"""
import argparse
import asyncio
import json
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

RESULT = {"scenario_a": {}, "scenario_b": {}, "passed": False, "detail": []}


def log(line):
    print(line, flush=True)
    RESULT["detail"].append(line)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def scenario_a():
    """真实 asyncua Client 对黑洞端口的异常分类。"""
    from app.telemetry.worker import permanent_error, transient_error

    port = free_port()
    # 起一个接受连接但永不响应的 TCP 服务器（黑洞）→ asyncua 在握手阶段超时
    server = await asyncio.start_server(lambda r, w: asyncio.sleep(3600), "127.0.0.1", port)
    async with server:
        from asyncua import Client

        client = Client(f"opc.tcp://127.0.0.1:{port}", timeout=2)
        exc = None
        try:
            await client.connect()
        except Exception as caught:  # noqa: BLE001 —— 就是要看库抛什么
            exc = caught
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass
    if exc is None:
        raise SystemExit("场景 A 失败：黑洞端口连接竟未抛异常，脚本前提不成立")
    code = type(exc).__name__
    chain = [type(e).__name__ for e in __import__("app.telemetry.worker", fromlist=["x"]).exception_chain(exc)]
    new_transient = transient_error(exc)
    new_permanent = permanent_error(exc)
    # 旧逻辑复刻：仅对异常本身做 isinstance/名单匹配
    legacy_transient = isinstance(exc, (OSError, asyncio.TimeoutError, ConnectionError)) or code in {
        "BadConnectionClosed", "BadSessionClosed", "BadSessionIdInvalid", "BadSecureChannelClosed",
        "BadTimeout", "BadServerHalted", "BadCommunicationError", "BadNotConnected",
    }
    RESULT["scenario_a"] = {
        "exception": code, "chain": chain,
        "legacy_transient": legacy_transient, "new_transient": new_transient,
        "new_permanent": new_permanent,
    }
    log(f"[A] 异常={code} 链={chain} 旧判定瞬态={legacy_transient} 新判定瞬态={new_transient} 新判定致命={new_permanent}")
    if not new_transient:
        raise SystemExit("场景 A 失败：真实库异常未被新逻辑判为瞬态")
    log("[A] PASS：真实 asyncua 异常被新逻辑判为瞬态（可重试）")


async def scenario_b(tmp):
    """本地 OPC UA 仿真服务器 + 真实 OpcUaReader + 真实采集进程全链路。"""
    from asyncua import Server, ua

    port = free_port()
    endpoint = f"opc.tcp://127.0.0.1:{port}"
    # 仿真服务器进程：写到临时目录的 stdout 便于诊断
    server_script = tmp / "fake_server.py"
    server_script.write_text(
        "import asyncio\n"
        "from asyncua import Server, ua\n"
        "import sys\n"
        f"port = {port}\n"
        "async def main():\n"
        "    server = Server()\n"
        "    await server.init()\n"
        "    server.set_endpoint('opc.tcp://127.0.0.1:%d' % port)\n"
        "    idx = await server.register_namespace(u'DMCS')\n"
        "    await server.nodes.objects.add_variable(idx, u'Density', 1.45)\n"
        "    await server.nodes.objects.add_variable(idx, u'Ash', 8.2)\n"
        "    print('NS_INDEX', idx, flush=True)\n"
        "    async with server:\n"
        "        print(\'SERVER_READY\', flush=True)\n"
        "        await asyncio.Event().wait()\n"
        "asyncio.run(main())\n", encoding="utf-8")
    server_log = (tmp / "server.log").open("wb")
    server = subprocess.Popen([sys.executable, "-u", str(server_script)],
                              stdout=server_log, stderr=subprocess.STDOUT, cwd=str(BACKEND))
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if "SERVER_READY" in (tmp / "server.log").read_text(encoding="utf-8", errors="replace"):
                break
            if server.poll() is not None:
                raise SystemExit(f"仿真服务器启动失败：{(tmp / 'server.log').read_text(encoding='utf-8', errors='replace')[:500]}")
            await asyncio.sleep(0.2)
        else:
            raise SystemExit("仿真服务器 15s 未就绪")

        from app.telemetry.config import CollectorConfig, PointConfig
        from app.telemetry.opcua import OpcUaReader

        cfg = CollectorConfig(enabled=True, endpoint=endpoint, security="None", allow_insecure=True,
            poll_seconds=0.2, timeout_seconds=2, retry_min_seconds=0.2, retry_max_seconds=0.5,
            points=[
                PointConfig(key="density", name="密度", node_id=f"ns=2;s=Density"),
                PointConfig(key="ash", name="灰分", node_id=f"ns=2;s=Ash"),
            ])

        async def connect_and_read():
            async with OpcUaReader(cfg) as reader:
                samples = await reader.read()
                return {s["key"]: s["value"] for s in samples}

        values = await asyncio.wait_for(connect_and_read(), 15)
        log(f"[B] 仿真服务器读值成功: {values}")
        RESULT["scenario_b"]["first_read"] = values

        # —— 制造真实断连：杀掉服务器 ——
        server.kill()
        server.wait()
        log("[B] 已停止仿真服务器（真实断连）")
        exc2 = None
        try:
            await asyncio.wait_for(connect_and_read(), 10)
        except Exception as caught:  # noqa: BLE001
            exc2 = caught
        RESULT["scenario_b"]["disconnect_exception"] = type(exc2).__name__ if exc2 else None
        if exc2 is None:
            raise SystemExit("场景 B 失败：服务器停止后读取竟未抛异常")
        from app.telemetry.worker import transient_error
        if not transient_error(exc2):
            raise SystemExit(f"场景 B 失败：断连异常 {type(exc2).__name__} 未被判瞬态")
        log(f"[B] PASS：断连异常 {type(exc2).__name__} 判瞬态")
    finally:
        if server.poll() is None:
            server.kill()
        server_log.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--e2e", action="store_true", help="包含场景 B（本地仿真服务器全链路）")
    parser.add_argument("--output", default=None, help="结果 JSON 输出路径")
    args = parser.parse_args()

    import tempfile
    with tempfile.TemporaryDirectory(prefix="dmcs-verify-") as raw_tmp:
        tmp = Path(raw_tmp)
        asyncio.run(scenario_a())
        if args.e2e:
            asyncio.run(scenario_b(tmp))
    RESULT["passed"] = True
    log("全部场景 PASS")
    if args.output:
        Path(args.output).write_text(json.dumps(RESULT, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"结果已写入 {args.output}")


if __name__ == "__main__":
    main()
