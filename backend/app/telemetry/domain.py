"""纯数据规则：质量、量纲换算与时间；不导入数据库或 OPC UA 库。"""
import math
import hashlib
import json
from datetime import datetime, timezone
from numbers import Real

from .config import PointConfig


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_time(value: datetime | None) -> str | None:
    if value is None or value.year <= 1601:
        return None
    # OPC UA 时间戳以 UTC 定义；部分客户端版本返回不带 tzinfo 的 UTC。
    return value.replace(tzinfo=value.tzinfo or timezone.utc).astimezone(timezone.utc).isoformat(timespec="milliseconds")


def parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp requires timezone")
    return parsed.astimezone(timezone.utc)


def definition_id(config: dict) -> str:
    fields = {key: config.get(key) for key in ("node_id", "unit", "scale", "offset", "value_type", "minimum", "maximum")}
    return hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()


def normalize(point: PointConfig, raw, status_code: int, source_at: datetime | None,
              server_at: datetime | None, received_at: datetime) -> dict:
    severity = status_code & 0xC0000000
    quality = "good" if severity == 0 else "uncertain" if severity == 0x40000000 else "bad"
    issue = None if quality == "good" else quality + "_quality"
    value = None
    safe_raw = raw if isinstance(raw, (str, bool, int, float)) else None
    if isinstance(safe_raw, str):
        safe_raw = safe_raw[:256]
    if isinstance(safe_raw, float) and not math.isfinite(safe_raw):
        safe_raw = None
    if point.value_type == "boolean":
        if isinstance(raw, bool):
            value = raw
        else:
            issue = issue or "invalid_value_type"
    elif isinstance(raw, Real) and not isinstance(raw, bool):
        numeric = float(raw)
        if math.isfinite(numeric):
            value = numeric * point.scale + point.offset
            if not math.isfinite(value):
                value, issue = None, issue or "non_finite_value"
            elif (point.minimum is not None and value < point.minimum) or (point.maximum is not None and value > point.maximum):
                issue = issue or "out_of_range"
        else:
            issue = issue or "non_finite_value"
    else:
        issue = issue or "invalid_value_type"
    source = iso_time(source_at)
    server = iso_time(server_at)
    if source and (parse_time(source) - received_at).total_seconds() > 5:
        issue = issue or "future_source_time"
    return {"key": point.key, "name": point.name, "node_id": point.node_id, "unit": point.unit,
            "raw_value": safe_raw, "value": value, "status_code": status_code,
            "quality": quality, "issue": issue, "source_at": source, "server_at": server,
            "received_at": iso_time(received_at), "definition_id": definition_id(point.model_dump())}


def availability(sample: dict | None, point: dict, source_online: bool, now: datetime) -> tuple[bool, str]:
    if not sample:
        return False, "no_data"
    if sample.get("issue"):
        return False, sample["issue"]
    if not source_online:
        return False, "collector_offline"
    age = (now - parse_time(sample["received_at"])).total_seconds()
    if age < -5 or age > point["max_age_seconds"]:
        return False, "stale"
    source_limit = point.get("max_source_age_seconds")
    if source_limit and (not sample.get("source_at") or (now - parse_time(sample["source_at"])).total_seconds() > source_limit):
        return False, "source_stale"
    return True, "good"
