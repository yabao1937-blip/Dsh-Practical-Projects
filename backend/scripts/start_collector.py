"""Start the configured read-only collector in the background, without a console."""
import argparse
from pathlib import Path
import sys

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app.telemetry.control import CollectorStartError, ensure_collector_started


def main():
    parser = argparse.ArgumentParser(description="后台启动 KEPServer 只读采集")
    parser.add_argument("--config", type=Path, default=BACKEND / "config/kepserver.local.json")
    args = parser.parse_args()
    try:
        result = ensure_collector_started(args.config)
    except CollectorStartError as exc:
        print(exc.message)
        return 1
    action = {"started": "后台采集已启动", "already_running": "后台采集已在运行", "already_starting": "后台采集正在启动"}
    print(f"{action[result['action']]}：source={result['source_id']}")
    print("请通过 /api/v1/telemetry/latest 核对 connected 状态；进程启动不等于已连接。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
