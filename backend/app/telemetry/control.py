"""启动固定配置的独立采集进程；启动锁与数据库租约分别防止重复启动和重复采集。"""
from contextlib import contextmanager
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from sqlalchemy import inspect
from sqlalchemy.exc import SQLAlchemyError

from .config import load_config
from .domain import iso_time, parse_time, utc_now
from .models import TelemetrySource

BACKEND = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = BACKEND / "config/kepserver.local.json"
DEFAULT_RUNTIME = BACKEND / "data/telemetry-runtime"


class CollectorStartError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


@contextmanager
def _startup_lock(path):
    with path.open("a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        deadline = time.monotonic() + 3
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise CollectorStartError("startup_busy", "已有连接请求正在处理，请稍后重试。")
                time.sleep(0.05)
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def _process_identity(pid):
    """同时确认 PID 存活及创建时间，防止 PID 被其他进程复用。"""
    if not isinstance(pid, int) or pid <= 0:
        return None
    if os.name == "nt":
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return None
        try:
            code = wintypes.DWORD()
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != 259:
                return None
            times = [wintypes.FILETIME() for _ in range(4)]
            if not kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in times)):
                return None
            return str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        stat = Path(f"/proc/{pid}/stat")
        return stat.read_text().split(") ", 1)[1].split()[19] if stat.exists() else "alive"
    except (OSError, ValueError, IndexError):
        return None


def _live_source(cfg, session_factory, engine):
    if not inspect(engine).has_table("telemetry_sources"):
        return None
    with session_factory() as db:
        source = db.get(TelemetrySource, cfg.source_id)
        if source and source.owner and source.lease_until and parse_time(source.lease_until) > utc_now():
            return {"action": "already_running", "state": source.state, "lease_until": source.lease_until}
    return None


def _pending_process(path, cfg, config_path):
    try:
        metadata = json.loads(path.read_text(encoding="utf-8"))
        if metadata.get("source_id") != cfg.source_id or metadata.get("config") != str(config_path):
            return False
        identity = _process_identity(metadata.get("pid"))
        if not identity:
            return False
        age = (utc_now() - parse_time(metadata["started_at"])).total_seconds()
        expected = metadata.get("process_identity")
        if expected and identity != expected:
            return False
        # 兼容旧启动器：只有刚启动的元数据可用；旧 PID 不能用来回收租约或终止进程。
        if not expected and age > cfg.timeout_seconds * 3 + 15:
            return False
        if age > cfg.timeout_seconds * 3 + 15:
            raise CollectorStartError("startup_timeout", "采集进程未就绪，请检查采集日志后重试。")
        return True
    except (OSError, ValueError, KeyError, TypeError):
        return False


def _save_metadata(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def ensure_collector_started(config_path=DEFAULT_CONFIG, runtime_dir=DEFAULT_RUNTIME, *, session_factory=None, engine=None):
    try:
        config_path = Path(config_path).resolve()
        cfg = load_config(config_path)
    except (OSError, ValueError):
        raise CollectorStartError("configuration_invalid", "采集配置缺失或无效，请联系维护人员核对。") from None
    if not cfg.enabled:
        raise CollectorStartError("collector_disabled", "采集配置未启用，请联系维护人员核对。")
    if session_factory is None or engine is None:
        from ..database import SessionLocal, engine as configured_engine
        session_factory = session_factory or SessionLocal
        engine = engine or configured_engine
    requested_at = iso_time(utc_now())
    runtime_dir = Path(runtime_dir)
    metadata_path = runtime_dir / f"{cfg.source_id}.json"
    try:
        runtime_dir.mkdir(parents=True, exist_ok=True)
        with _startup_lock(runtime_dir / f"{cfg.source_id}.start.lock"):
            result = _live_source(cfg, session_factory, engine)
            if result:
                return {"ok": True, "source_id": cfg.source_id, "requested_at": requested_at, **result}
            if _pending_process(metadata_path, cfg, config_path):
                return {"ok": True, "source_id": cfg.source_id, "requested_at": requested_at,
                    "action": "already_starting", "state": "starting"}
            stamp = utc_now().strftime("%Y%m%dT%H%M%S%fZ")
            stdout_path = runtime_dir / f"{cfg.source_id}-{stamp}.stdout.log"
            stderr_path = runtime_dir / f"{cfg.source_id}-{stamp}.stderr.log"
            metadata = {"source_id": cfg.source_id, "config": str(config_path), "started_at": requested_at,
                "stdout": str(stdout_path), "stderr": str(stderr_path), "pid": None}
            _save_metadata(metadata_path, metadata)
            environment = os.environ.copy()
            environment["PYTHONIOENCODING"] = "utf-8"
            # 使子进程与 HTTP 服务使用同一数据库（包括显式测试库），不退回默认库。
            environment["DMCS_DATABASE_URL"] = engine.url.render_as_string(hide_password=False)
            options = {"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
            with stdout_path.open("ab") as stdout, stderr_path.open("ab") as stderr:
                process = subprocess.Popen([sys.executable, "-u", str(BACKEND / "collector.py"), "--config", str(config_path)],
                    cwd=BACKEND, env=environment, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                    close_fds=True, **options)
            time.sleep(0.05)
            if process.poll() is not None:
                raise CollectorStartError("process_exited", "采集进程启动后退出，请检查采集配置和日志。")
            metadata.update(pid=process.pid, process_identity=_process_identity(process.pid))
            _save_metadata(metadata_path, metadata)
            return {"ok": True, "source_id": cfg.source_id, "requested_at": requested_at,
                "action": "started", "state": "starting"}
    except CollectorStartError:
        raise
    except OSError:
        raise CollectorStartError("process_start_failed", "后台采集启动失败，请检查服务权限和运行环境。") from None
    except SQLAlchemyError:
        raise CollectorStartError("storage_unavailable", "采集数据库暂时不可用，请稍后重试。") from None
