"""Start the configured read-only collector in the background, without a console."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from datetime import datetime, timezone

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app.telemetry.config import load_config


def main():
    parser = argparse.ArgumentParser(description="后台启动 KEPServer 只读采集")
    parser.add_argument("--config", type=Path, default=BACKEND / "config/kepserver.local.json")
    args = parser.parse_args()
    config_path = args.config.resolve()
    cfg = load_config(config_path)
    if not cfg.enabled:
        raise SystemExit("采集配置未启用，没有启动进程。")

    from sqlalchemy import inspect
    from app.database import SessionLocal, engine
    from app.telemetry.models import TelemetrySource
    from app.telemetry.domain import parse_time, utc_now
    if inspect(engine).has_table("telemetry_sources"):
        with SessionLocal() as db:
            source = db.get(TelemetrySource, cfg.source_id)
            if source and source.owner and source.lease_until and parse_time(source.lease_until) > utc_now():
                print(f"来源 {cfg.source_id} 已有采集进程，未重复启动。")
                return 0

    runtime = BACKEND / "data/telemetry-runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    stdout_path = runtime / f"{cfg.source_id}-{stamp}.stdout.log"
    stderr_path = runtime / f"{cfg.source_id}-{stamp}.stderr.log"
    environment = os.environ.copy()
    environment["PYTHONIOENCODING"] = "utf-8"
    kwargs = {"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
    # File handles, rather than parent stdout pipes, let the child survive launcher exit.
    with stdout_path.open("ab") as stdout, stderr_path.open("ab") as stderr:
        process = subprocess.Popen([sys.executable, "-u", str(BACKEND / "collector.py"), "--config", str(config_path)],
            cwd=BACKEND, env=environment, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, close_fds=True, **kwargs)
    metadata = {"pid": process.pid, "source_id": cfg.source_id, "config": str(config_path),
        "started_at": datetime.now(timezone.utc).isoformat(), "stdout": str(stdout_path), "stderr": str(stderr_path)}
    (runtime / f"{cfg.source_id}.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已启动采集进程：source={cfg.source_id} pid={process.pid} points={len(cfg.points)}")
    print(f"日志：{stderr_path}")
    print("请通过 /api/v1/telemetry/latest 核对 connected 状态；进程启动不等于已连接。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
