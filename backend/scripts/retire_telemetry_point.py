"""Retire configured telemetry points without deleting retained history.

Run this on the production host when a meter is out of service. It removes the
matching ``item_id``/``node_id`` entries from the collector JSON, backs the file
up, validates the remaining configuration and then tells the operator to restart
collection (the collector marks points missing from its config as disabled).
"""
import argparse
import json
from datetime import datetime
from pathlib import Path
import sys

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

DEFAULT_CONFIG = BACKEND / "config/kepserver.local.json"
BACKUP_DIR = BACKEND / "data/backups"


def remove_points(data, items):
    points = data.get("points")
    if not isinstance(points, list):
        raise ValueError("采集配置缺少 points 列表")
    kept, removed = [], []
    for point in points:
        identities = {point.get("item_id"), point.get("node_id")}
        if identities & items:
            removed.append(point)
        else:
            kept.append(point)
    data["points"] = kept
    return removed


def main(argv=None):
    parser = argparse.ArgumentParser(description="停采指定采集点并保留历史数据")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--item", action="append", required=True, help="item_id 或 node_id，可重复")
    parser.add_argument("--apply", action="store_true", help="写入配置；缺省只预览")
    args = parser.parse_args(argv)

    from app.telemetry.config import CollectorConfig, load_config

    path = args.config.resolve()
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    removed = remove_points(data, set(args.item))
    if not removed:
        print("未找到指定点位，配置未改动：" + "、".join(args.item))
        return 0
    CollectorConfig.model_validate(data)
    print(f"将移除 {len(removed)} 个点位，剩余 {len(data['points'])} 个：")
    for point in removed:
        print(f"  - {point.get('item_id')} ({point.get('key')})")
    if not args.apply:
        print("预览结束；确认后加 --apply 写入。")
        return 0

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    backup = BACKUP_DIR / f"{path.name}.{datetime.now():%Y%m%dT%H%M%S}.bak"
    backup.write_bytes(path.read_bytes())
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
    config = load_config(path)
    print(f"已写入 {path}；备份 {backup}；有效点位 {len(config.points)} 个。")
    print("下一步：停止采集进程并重新启动（或重启系统），使该点位在库中标记为停用；")
    print("历史 readings 不删除，/api/v1/telemetry/latest 将不再返回该点位。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
