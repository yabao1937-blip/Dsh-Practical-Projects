"""Read-only composite points calculated from a single latest-query snapshot."""
from .domain import parse_time

FOUR_SYSTEM_MEMBERS = (
    ("401", "TIA_3_3.TIA_3_3.478/if_prod"),
    ("402", "TIA_3_3.TIA_3_3.479/if_prod"),
    ("A", "TIA_4_2.TIA_4_2.A345/if_prod"),
    ("B", "TIA_4_2.TIA_4_2.A346/if_prod"),
)


def system_status_points(items):
    result = []
    for source_id in sorted({item["source_id"] for item in items}):
        by_id = {item.get("item_id"):item for item in items if item["source_id"] == source_id}
        if not any(item_id in by_id for _, item_id in FOUR_SYSTEM_MEMBERS):
            continue
        components = []
        for label, item_id in FOUR_SYSTEM_MEMBERS:
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
        complete = len(times) == 4 and all(c["point_key"] for c in components)
        same_batch = complete and len(set(times)) == 1
        available = same_batch and all(c["available"] for c in components)
        issue = None if available else "incomplete_components" if not complete else "member_unavailable" if not all(c["available"] for c in components) else "mixed_batches"
        qualities = [c["quality"] for c in components]
        quality = "bad" if "bad" in qualities else "uncertain" if "uncertain" in qualities else "good" if all(q == "good" for q in qualities) else "unknown"
        received_at = min(times).isoformat(timespec="milliseconds") if complete else None
        sample = {"value":{c["label"]:c["value"] for c in components} if available else None,
            "quality":quality, "issue":issue, "received_at":received_at, "source_at":None, "server_at":None,
            "components":components}
        result.append({"source_id":source_id, "key":"four_system_status", "name":"四系统运行状态（组合）",
            "device":"四系统组合", "tag_name":"四系统运行状态", "description":"401、402、A、B 四路状态组合",
            "item_id":"", "unit":"", "kind":"state_group", "available":available,
            "status":"good" if available else issue, "sample":sample})
    return result
