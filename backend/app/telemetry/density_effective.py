"""Business density values; raw OPC samples are never changed."""
import math

from .derived import DENSITY_AVERAGE_MEMBERS, density_average_points
from .domain import parse_time

POLICY = "density-per-channel-nonone-v1"
DENSITY_ITEMS = {item for _, item in DENSITY_AVERAGE_MEMBERS}


def valid_density(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value > 0 and value != 1)


def effective_component(item):
    def in_time(sample):
        try:
            received = parse_time(sample["received_at"])
            return not item.get("as_of") or received <= parse_time(item["as_of"])
        except (KeyError, TypeError, ValueError):
            return False

    raw = item.get("sample") or {}
    live = bool(item.get("available") and raw.get("quality") == "good"
                and not raw.get("issue") and valid_density(raw.get("value")) and in_time(raw))
    held = item.get("last_density_sample") or {}
    # The repository validates quality, timestamp and definition when remembering.
    # Check again here so callers cannot accidentally feed an old definition.
    usable_held = (valid_density(held.get("value")) and held.get("quality") == "good"
                   and not held.get("issue") and in_time(held)
                   and held.get("definition_id") == item.get("definition_id")
                   and held.get("unit", "") == item.get("unit", ""))
    selected = raw if live else held if usable_held else {}
    cause = "读数为 1.000" if raw.get("value") == 1 else "读数缺失或采集异常"
    status = "good" if live else "held" if selected else "missing"
    return dict(policy=POLICY, value=selected.get("value"), status=status,
                reason="" if live else cause + (" · 沿用本路最近非 1 有效值" if selected else " · 无本路非 1 有效历史"),
                received_at=selected.get("received_at"), source_at=selected.get("source_at"),
                unit=item.get("unit", ""), source_id=item["source_id"], point_key=item["key"],
                item_id=item.get("item_id"), raw_value=raw.get("value"), raw_sample=raw or None,
                sample=selected or None)


def effective_average(items):
    """Resolve each channel first. Mixed timestamps are explicit holds, never live."""
    by_id = {p.get("item_id"): p for p in items}
    parts = []
    for label, item_id in DENSITY_AVERAGE_MEMBERS:
        item = by_id.get(item_id)
        matches = sum(p.get("item_id") == item_id for p in items)
        if matches != 1:
            parts.append(dict(label=label, value=None, status="missing", reason="测点未配置" if not matches else "测点配置不唯一"))
        else:
            parts.append(dict(effective_component(item), label=label))
    result = dict(policy=POLICY, value=None, status="missing", received_at=None,
                  unit="", components=parts, formula="(A303/MD + A304/MD) ÷ 2")
    reasons = [p["label"] + "：" + p["reason"] for p in parts if p["reason"]]
    result["reason"] = "；".join(reasons)
    if any(p["value"] is None for p in parts):
        return result
    if len({p["source_id"] for p in parts}) != 1 or len({p["unit"] for p in parts}) != 1:
        result["reason"] = "A303/A304 来源或单位不一致"
        return result
    if all(p["status"] == "good" for p in parts):
        raw_average = density_average_points(items)[0]
        if not raw_average["available"]:
            result["reason"] = "A303/A304 实时读数不属于同一有效采集批次"
            return result
    result.update(value=sum(p["value"] / 2 for p in parts), unit=parts[0]["unit"],
                  status="held" if reasons else "good",
                  received_at=min((p["received_at"] for p in parts), key=parse_time))
    return result
