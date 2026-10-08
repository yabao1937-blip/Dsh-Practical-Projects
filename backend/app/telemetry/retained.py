"""Bounded display memories; raw readings and live availability remain unchanged."""
from collections import OrderedDict
from copy import deepcopy
import json
from threading import Lock
from weakref import WeakKeyDictionary

from sqlalchemy import select
from sqlalchemy.orm import aliased

from .domain import availability, definition_id, parse_time
from .derived import DENSITY_AVERAGE_MEMBERS, density_average_points
from .models import TelemetryPoint, TelemetryReading

MEMORY = "_display_memory"
DENSITY_MEMORY = "_density_display_memory"
_legacy_cache = WeakKeyDictionary()
_cache_lock = Lock()


def _bootstrap(db, kind, points, read):
    # A stopped legacy collector cannot persist memories yet. Avoid scanning its
    # history on every HTTP poll; point/config/history changes invalidate this key.
    engine = db.get_bind()
    key = (kind, tuple((p.source_id, p.key, p.last_history_at,
        json.dumps(p.config, sort_keys=True), json.dumps(raw_sample(p.latest), sort_keys=True)) for p in points))
    with _cache_lock:
        cache = _legacy_cache.setdefault(engine, OrderedDict())
        if key in cache:
            cache.move_to_end(key)
            return deepcopy(cache[key])
    result = read()
    with _cache_lock:
        cache[key] = deepcopy(result)
        cache.move_to_end(key)
        while len(cache) > 512:
            cache.popitem(last=False)
    return result


def raw_sample(sample):
    return {k: v for k, v in sample.items() if k not in (MEMORY, DENSITY_MEMORY)} if sample else None


def usable_at_receipt(sample, config):
    if not sample or sample.get("definition_id") != definition_id(config) or sample.get("quality") != "good":
        return False
    try:
        return availability(sample, config, True, parse_time(sample["received_at"]))[0]
    except (KeyError, TypeError, ValueError):
        return False


def newest(*samples):
    return max((s for s in samples if s), key=lambda s: parse_time(s["received_at"]), default=None)


def point_memory(db, point):
    """Bootstrap old installations from history once; new snapshots carry both memories."""
    memory = (point.latest or {}).get(MEMORY)
    if isinstance(memory, dict) and memory.get("definition_id") == definition_id(point.config):
        return {key: memory.get(key) for key in ("last_valid", "last_nonzero")}
    return _bootstrap(db, "point", [point], lambda: _point_history(db, point))


def _point_history(db, point):
    result = {"last_valid": None, "last_nonzero": None}
    if point.config.get("value_type", "number") != "number":
        return result
    current = raw_sample(point.latest)
    for key in result:
        if usable_at_receipt(current, point.config) and (key == "last_valid" or current["value"] != 0):
            result[key] = current
            continue
        query = select(TelemetryReading.sample).where(
            TelemetryReading.source_id == point.source_id, TelemetryReading.point_key == point.key,
            TelemetryReading.sample["definition_id"].as_string() == definition_id(point.config),
            TelemetryReading.sample["quality"].as_string() == "good",
            TelemetryReading.sample["issue"].as_string().is_(None))
        if key == "last_nonzero":
            query = query.where(TelemetryReading.sample["value"].as_float() != 0)
        for sample in db.scalars(query.order_by(TelemetryReading.received_at.desc()).execution_options(yield_per=64)):
            if usable_at_receipt(sample, point.config):
                result[key] = raw_sample(sample)
                break
    return result


def remember_point(db, point, sample):
    memory = point_memory(db, point)
    if point.config.get("value_type", "number") == "number" and usable_at_receipt(sample, point.config):
        memory["last_valid"] = newest(memory["last_valid"], raw_sample(sample))
        if sample["value"] != 0:
            memory["last_nonzero"] = newest(memory["last_nonzero"], raw_sample(sample))
    result = {**raw_sample(sample), MEMORY: {"definition_id": definition_id(point.config), **memory}}
    if point.latest and DENSITY_MEMORY in point.latest:
        result[DENSITY_MEMORY] = point.latest[DENSITY_MEMORY]
    return result


def density_rows(db, source_id):
    # Use the configured item identifiers, exactly as the live average does.
    by_id = {p.config.get("item_id"): p for p in db.scalars(select(TelemetryPoint).where(
        TelemetryPoint.source_id == source_id, TelemetryPoint.enabled.is_(True)))}
    return [by_id.get(item_id) for _, item_id in DENSITY_AVERAGE_MEMBERS]


def density_definition(rows):
    return "|".join(f"{p.key}:{definition_id(p.config)}" for p in rows)


def density_candidate(rows, samples):
    if not all(samples) or not samples[0].get("batch_id") or samples[0].get("batch_id") != samples[1].get("batch_id"):
        return None
    items = [{"source_id": p.source_id, "key": p.key, "item_id": p.config["item_id"],
              "unit": p.config["unit"], "sample": sample,
              "available": usable_at_receipt(sample, p.config), "status": "good"}
             for p, sample in zip(rows, samples)]
    candidate = density_average_points(items)[0]
    if candidate["available"]:
        return {**candidate["sample"], "unit": candidate["unit"], "definition_id": density_definition(rows)}
    return None


def density_memory(db, rows):
    if not all(rows):
        return None
    signature = density_definition(rows)
    cached = (rows[0].latest or {}).get(DENSITY_MEMORY)
    current = density_candidate(rows, [p.latest for p in rows])
    if cached and cached.get("definition_id") == signature:
        return newest(current, cached.get("sample"))
    return _bootstrap(db, "density", rows, lambda: _density_history(db, rows, current))


def _density_history(db, rows, current):
    # Pair historical readings by source AND batch. Never average two unrelated last values.
    left, right = aliased(TelemetryReading), aliased(TelemetryReading)
    query = select(left.sample, right.sample, left.batch_id).join(right,
        (left.source_id == right.source_id) & (left.batch_id == right.batch_id)).where(
        left.source_id == rows[0].source_id, left.point_key == rows[0].key, right.point_key == rows[1].key)
    for table, point in ((left, rows[0]), (right, rows[1])):
        query = query.where(table.sample["definition_id"].as_string() == definition_id(point.config),
            table.sample["quality"].as_string() == "good", table.sample["issue"].as_string().is_(None))
    if current:
        query = query.where(left.received_at > current["received_at"])
    for left_sample, right_sample, batch_id in db.execute(query.order_by(left.received_at.desc()).execution_options(yield_per=64)):
        candidate = density_candidate(rows, [{**s, "batch_id": batch_id} for s in (left_sample, right_sample)])
        if candidate:
            return newest(current, candidate)
    return current


def remember_density(db, source_id):
    rows = density_rows(db, source_id)
    if all(rows) and rows[0].latest:
        sample = density_memory(db, rows)
        rows[0].latest = {**rows[0].latest, DENSITY_MEMORY: {
            "definition_id": density_definition(rows), "sample": sample}}
