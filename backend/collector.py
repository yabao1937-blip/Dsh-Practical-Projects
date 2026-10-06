"""python collector.py --config config/kepserver.local.json [--validate|--once]"""
import argparse
import asyncio
import logging
import os
from pathlib import Path

from pydantic import ValidationError

from app.telemetry.config import load_config


def main():
    parser = argparse.ArgumentParser(description="KEPServer OPC UA 只读采集；不会写入 PLC")
    parser.add_argument("--config", type=Path, default=Path(__file__).parent / "config/kepserver.local.json")
    parser.add_argument("--validate", action="store_true", help="只校验配置，不连接、不写数据库")
    parser.add_argument("--once", action="store_true", help="读取并持久化一批后退出")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        cfg = load_config(args.config.resolve())
    except ValidationError as exc:
        for error in exc.errors(include_input=False, include_url=False):
            print("配置错误：" + ".".join(str(p) for p in error["loc"]) + " (" + error["type"] + ")")
        return 2
    except (OSError, ValueError):
        print("配置文件不可读取，或证书文件/配置不完整。请核对配置；没有连接设备。")
        return 2
    if args.validate or not cfg.enabled:
        print(f"配置有效：source={cfg.source_id} enabled={cfg.enabled} points={len(cfg.points)} read_only=True")
        return 0
    try:
        from app.database import Base, engine
        from app.telemetry import models  # noqa: F401
        from app.telemetry.repository import TelemetryRepository
        from app.telemetry.spool import DurableSpool
        from app.telemetry.worker import run_collector
        from app.telemetry.views import ensure_sqlite_views
        for name in ("telemetry_sources", "telemetry_points", "telemetry_readings"):
            Base.metadata.tables[name].create(engine, checkfirst=True)
        ensure_sqlite_views(engine)
        spool_root = Path(os.environ.get("DMCS_TELEMETRY_SPOOL") or Path(__file__).parent / "data/telemetry-spool")
        return asyncio.run(run_collector(cfg, TelemetryRepository(), DurableSpool(spool_root, cfg.source_id), once=args.once))
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        logging.getLogger("dmcs.collector").error("collector_startup_failed source=%s code=%s", cfg.source_id, type(exc).__name__)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
