"""Read-only composite points calculated from a single latest-query snapshot."""
import math
from .domain import parse_time

FOUR_SYSTEM_MEMBERS = (
    ("401", "TIA_3_3.TIA_3_3.478/if_prod"),
    ("402", "TIA_3_3.TIA_3_3.479/if_prod"),
    ("A", "TIA_4_2.TIA_4_2.A345/if_prod"),
    ("B", "TIA_4_2.TIA_4_2.A346/if_prod"),
)
DESLIMING_MEMBERS = (
    ("473", "TIA_3_3.TIA_3_3.473/if_prod"),
    ("474", "TIA_3_3.TIA_3_3.474/if_prod"),
)
FLOAT_PRESS_MEMBERS = tuple(
    (label, f"TIA_4_2.TIA_4_2.{label}/YL_STATE") for label in ("A415", "A416", "A417", "A418")
)
STATUS_GROUPS = (
    {"key":"four_system_status", "name":"四系统运行状态（组合）", "device":"四系统组合",
     "tag_name":"四系统运行状态", "description":"401、402、A、B 四路状态组合", "members":FOUR_SYSTEM_MEMBERS},
    {"key":"desliming_status", "name":"脱粉系统运行状态（组合）", "device":"脱粉系统组合",
     "tag_name":"脱粉系统运行状态", "description":"473、474 两路状态组合", "members":DESLIMING_MEMBERS},
    {"key":"float_press_status", "name":"浮精压滤机运行状态（组合）", "device":"浮精压滤机组合",
     "tag_name":"浮精压滤机运行状态", "description":"A415、A416、A417、A418 四台运行状态组合",
     "member_label":"压滤机", "members":FLOAT_PRESS_MEMBERS},
)
DENSITY_AVERAGE_MEMBERS = (
    ("A303", "TIA_4_1.TIA_4_1.A303/MD"),
    ("A304", "TIA_4_1.TIA_4_1.A304/MD"),
)


def density_average_points(items):
    """User-confirmed equal-weight mean of the two current-density tags, not MD_SET."""
    result = []
    for source_id in sorted({item["source_id"] for item in items}):
        by_id = {item.get("item_id"): item for item in items if item["source_id"] == source_id}
        if not any(item_id in by_id for _, item_id in DENSITY_AVERAGE_MEMBERS):
            continue
        components, times = [], []
        for label, item_id in DENSITY_AVERAGE_MEMBERS:
            item = by_id.get(item_id)
            sample = (item.get("sample") or {}) if item else {}
            value = sample.get("value")
            try:
                numeric = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
            except OverflowError:
                numeric = False
            usable = bool(item and item.get("available") and numeric and sample.get("quality") == "good" and not sample.get("issue"))
            component = {"label": label, "item_id": item_id, "point_key": item.get("key") if item else None,
                "value": value if numeric else None, "weight": 0.5, "unit": str(item.get("unit") or "").strip() if item else "",
                "available": usable, "status": item.get("status") if item and numeric else "invalid_value_type" if item else "no_data",
                "quality": sample.get("quality"), "received_at": sample.get("received_at"),
                "source_at": sample.get("source_at"), "server_at": sample.get("server_at")}
            components.append(component)
            if isinstance(component["received_at"], str):
                try:
                    times.append(parse_time(component["received_at"]))
                except (TypeError, ValueError):
                    pass
        complete = len(times) == 2 and all(c["point_key"] for c in components)
        same_batch = complete and len(set(times)) == 1
        batch_ids = [(by_id.get(item_id, {}).get("sample") or {}).get("batch_id") for _, item_id in DENSITY_AVERAGE_MEMBERS]
        if any(batch_ids):
            same_batch = same_batch and bool(batch_ids[0]) and len(set(batch_ids)) == 1
        same_unit = len({c["unit"] for c in components}) == 1
        available = same_batch and same_unit and all(c["available"] for c in components)
        issue = None if available else "incomplete_components" if not complete else "member_unavailable" if not all(c["available"] for c in components) else "mixed_batches" if not same_batch else "unit_mismatch"
        qualities = [c["quality"] for c in components]
        quality = "bad" if "bad" in qualities else "uncertain" if "uncertain" in qualities else "good" if all(q == "good" for q in qualities) else "unknown"
        # Halving each finite operand first avoids overflowing their sum.
        value = sum(c["value"] / 2 for c in components) if available else None
        result.append({"source_id": source_id, "key": "a303_a304_density_average",
            "name": "A303/A304 当前密度（平均）", "device": "A303/A304", "tag_name": "当前密度（平均）",
            "description": "本系统计算：(A303/MD + A304/MD) ÷ 2；两路各占 50%",
            "item_id": "", "kind": "numeric_average", "member_count": 2,
            "unit": components[0]["unit"] if same_unit else "", "available": available,
            "status": "good" if available else issue,
            "sample": {"value": value, "quality": quality, "issue": issue,
                "received_at": min(times).isoformat(timespec="milliseconds") if complete else None,
                "source_at": None, "server_at": None, "components": components,
                "formula": "(A303/MD + A304/MD) ÷ 2"}})
    return result


def system_status_points(items):
    result = []
    for source_id in sorted({item["source_id"] for item in items}):
        by_id = {item.get("item_id"):item for item in items if item["source_id"] == source_id}
        for group in STATUS_GROUPS:
            if any(item_id in by_id for _, item_id in group["members"]):
                result.append(_status_group(source_id, by_id, group))
    return result


def _status_group(source_id, by_id, group):
    members = group["members"]
    components = []
    for label, item_id in members:
        item = by_id.get(item_id)
        sample = (item.get("sample") or {}) if item else {}
        value = sample.get("value")
        boolean = isinstance(value, bool)
        usable = bool(item and item.get("available") and boolean and sample.get("quality") == "good" and not sample.get("issue"))
        components.append({"label":label, "item_id":item_id, "point_key":item.get("key") if item else None,
            "value":value if boolean else None, "available":usable,
            "status":item.get("status") if item and boolean else "invalid_value_type" if item else "no_data",
            "quality":sample.get("quality"), "received_at":sample.get("received_at"),
            "source_at":sample.get("source_at"), "server_at":sample.get("server_at")})
    times = []
    for component in components:
        if not isinstance(component["received_at"], str):
            continue
        try:
            times.append(parse_time(component["received_at"]))
        except (TypeError, ValueError):
            pass
    complete = len(times) == len(members) and all(c["point_key"] for c in components)
    same_batch = complete and len(set(times)) == 1
    available = same_batch and all(c["available"] for c in components)
    issue = None if available else "incomplete_components" if not complete else "member_unavailable" if not all(c["available"] for c in components) else "mixed_batches"
    qualities = [c["quality"] for c in components]
    quality = "bad" if "bad" in qualities else "uncertain" if "uncertain" in qualities else "good" if all(q == "good" for q in qualities) else "unknown"
    received_at = min(times).isoformat(timespec="milliseconds") if complete else None
    sample = {"value":{c["label"]:c["value"] for c in components} if available else None,
        "quality":quality, "issue":issue, "received_at":received_at, "source_at":None, "server_at":None,
        "components":components}
    return {"source_id":source_id, **{key:group[key] for key in ("key", "name", "device", "tag_name", "description")},
        "item_id":"", "unit":"", "kind":"state_group", "member_count":len(members),
        "member_label":group.get("member_label", "系统"), "available":available,
        "status":"good" if available else issue, "sample":sample}
