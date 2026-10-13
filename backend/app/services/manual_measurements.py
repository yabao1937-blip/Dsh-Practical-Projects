"""Manual process measurements, separate from instruments and training records."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import re

METRICS = {
    "heavy_clean_coal_ash": {"name": "重介精煤灰分", "unit": "%", "maximum": 100},
    "raw_coal_slime_content": {"name": "原煤煤泥含量", "unit": "%", "maximum": 100},
    "coarse_slime_amount": {"name": "粗精煤泥量", "unit": "t/h", "maximum": None},
    "coarse_slime_ash": {"name": "粗精煤泥灰分", "unit": "%", "maximum": 100, "archive_only": True},
}
# Retain the old amount definition for historical reads and legacy point users.
ENTRY_METRICS = {key: value for key, value in METRICS.items() if key != "coarse_slime_amount"}
POINT_METRICS = {key: value for key, value in METRICS.items() if not value.get("archive_only")}
BEIJING = timezone(timedelta(hours=8))
TIME_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:\d{2})?$")


def timestamp_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def normalize_time(value):
    if value is None or not value.strip():
        return None
    text = value.strip()
    if not TIME_PATTERN.fullmatch(text):
        raise ValueError("时间须包含有效日期和时分，例如 2026-10-07T14:30:00")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=BEIJING)
        return parsed.astimezone(timezone.utc).isoformat(timespec="milliseconds")
    except (ValueError, OverflowError) as exc:
        raise ValueError("补录时间无效") from exc


def request_hash(metric, value, observed_at):
    # Null time must remain null here, so retries reuse the original server time.
    content = {"metric": metric, "value": float(value) if value else 0.0, "observed_at": observed_at}
    return hashlib.sha256(json.dumps(content, sort_keys=True, allow_nan=False).encode()).hexdigest()


def latest_points(db):
    """One stable display point per metric, ordered by measurement time."""
    from ..models import ManualMeasurement

    now = timestamp_now()
    items = []
    for metric, definition in POINT_METRICS.items():
        record = (db.query(ManualMeasurement)
                  .filter(ManualMeasurement.metric == metric, ManualMeasurement.observed_at <= now)
                  .order_by(ManualMeasurement.observed_at.desc(), ManualMeasurement.id.desc()).first())
        valid = bool(record and record.value is not None and math.isfinite(record.value)
                     and record.value >= 0 and (definition["maximum"] is None or record.value <= definition["maximum"]))
        items.append({
            "source_id": "manual-measurements", "key": metric, "kind": "manual_measurement",
            "name": definition["name"], "device": "手工补录", "tag_name": definition["name"],
            "description": "SQLite · 按测量时间取最新", "unit": definition["unit"],
            "available": valid, "status": "good" if valid else "invalid_value" if record else "no_data",
            "sample": {"record_id": record.id if record else None,
                       "value": record.value if valid else None,
                       "observed_at": record.observed_at if record else None,
                       "created_at": record.created_at if record else None},
        })
    return {"items": items, "checked_at": now}
