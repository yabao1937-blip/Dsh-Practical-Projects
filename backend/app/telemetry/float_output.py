"""Flotation concentrate output derived from filter-press plate counters.

2026-10-26 现场口径（用户确认）：浮精压滤机 A415/A416/A417/A418 的 PLATE_CNT 是
累计板数计数器（会周期性归零，实测 A416 有 27→13 回落），一板 = 13 t。
小时浮精量 = 四台计数器在该小时内相邻 5 秒样本的**正增量**之和 × 13：
正增量规则对计数器归零天然稳健（归零步贡献 0，不丢产量、不出负值）。

即时链路取「上一完整小时」的折算值（整点更新）；数据断档覆盖率不足、
单步跳变超阈值或四台全无数据时不采用，由调用方回退到沿用来源链。
计数为 0 且覆盖充分是有效产量（压滤机确实没卸板），不视为缺数据。
"""
from datetime import timedelta
from math import isfinite
from time import monotonic

from sqlalchemy import select, tuple_

from .models import TelemetryPoint, TelemetryReading
from .domain import iso_time, parse_time
from .retained import raw_sample

PLATE_MEMBERS = (
    ("A415", "TIA_26_1.TIA_26_1.A415/PLATE_CNT"),
    ("A416", "TIA_27_1.TIA_27_1.A416/PLATE_CNT"),
    ("A417", "TIA_4_2.TIA_4_2.A417/PLATE_CNT"),
    ("A418", "TIA_4_2.TIA_4_2.A418/PLATE_CNT"),
)
PLATE_TONNES = 13.0        # 一板 = 13 t（2026-10-26 现场给定常量）
MAX_STEP_PLATES = 60       # 单个采样间隔的跳变上限，超过判定信号异常
MIN_COVERAGE = 0.80        # 小时采样覆盖率下限，低于则不采用折算值
CADENCE = timedelta(seconds=5)
DERIVATION = "float-plate-count-v1"

_cache = {}                # (db_url, window_end) -> (deadline, stats)
_CACHE_TTL = 25.0          # 秒；整点键自动失效，避免每次 5 秒轮询重扫一小时读数


def floor_hour(moment):
    return moment.replace(minute=0, second=0, microsecond=0)


def _sample_value(sample):
    if not sample or sample.get("quality", "good") != "good":
        return None
    value = sample.get("value")
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    if not isfinite(value) or value < 0:
        return None
    return float(value)


def _plate_points(db):
    """The four counter points as {(source_id, point_key): label}."""
    wanted = {item for _, item in PLATE_MEMBERS}
    found = {}
    for p in db.scalars(select(TelemetryPoint).where(TelemetryPoint.enabled.is_(True))):
        item = p.config.get("item_id") if isinstance(p.config, dict) else None
        if item in wanted:
            for label, member in PLATE_MEMBERS:
                if item == member:
                    found[(p.source_id, p.key)] = label
    return found


def _series(db, points, start, end):
    """Ordered per-press (moment, value) samples, plus one pre-window baseline each."""
    from collections import defaultdict

    series = defaultdict(list)
    if not points:
        return series
    lookback = iso_time(start - CADENCE * 12)
    rows = db.scalars(select(TelemetryReading).where(
        tuple_(TelemetryReading.source_id, TelemetryReading.point_key).in_(list(points)),
        TelemetryReading.received_at >= lookback,
        TelemetryReading.received_at < iso_time(end)).order_by(TelemetryReading.received_at))
    for r in rows:
        value = _sample_value(raw_sample(r.sample))
        if value is None:
            continue
        try:
            moment = parse_time(r.received_at)
        except (TypeError, ValueError):
            continue
        series[points[(r.source_id, r.point_key)]].append((moment, value))
    return series


def _hour_stats(series, start, end):
    expected = max(1, round((end - start).total_seconds() / CADENCE.total_seconds()))
    plates, counts, anomaly = {}, {}, None
    for label in (label for label, _ in PLATE_MEMBERS):
        samples = series.get(label) or []
        inside = [(t, v) for t, v in samples if start <= t < end]
        counts[label] = len(inside)
        total, last = 0.0, None
        # The latest pre-window sample seeds the boundary-crossing delta.
        before = [(t, v) for t, v in samples if t < start]
        if before:
            last = before[-1][1]
        for _, value in inside:
            if last is not None:
                delta = value - last
                if delta > MAX_STEP_PLATES:
                    anomaly = f"{label} 板数单步跳变 {delta:+.0f} 超过 {MAX_STEP_PLATES}"
                elif delta > 0:
                    total += delta
            last = value
        plates[label] = total
    active = [label for label in counts if counts[label]]
    coverage = min((counts[label] / expected for label in active), default=0.0)
    coverage = max(0.0, min(1.0, coverage))
    usable = bool(active) and anomaly is None and coverage >= MIN_COVERAGE
    return dict(plates={k: v for k, v in plates.items() if v}, total_plates=sum(plates.values()),
                tonnes=sum(plates.values()) * PLATE_TONNES, coverage=coverage,
                active=sorted(active), samples=counts, anomaly=anomaly, usable=usable,
                start=start, end=end)


def float_rate_stats(db, moment):
    """Plate-derived output of the last complete hour ending at floor_hour(moment).

    Returns None (caller falls back to the legacy chain) when the window has no
    usable counter data. Results are cached briefly per window boundary.
    """
    end = floor_hour(moment)
    start = end - timedelta(hours=1)
    if end <= start:
        return None
    try:  # Session has get_bind(); a bare Connection exposes .engine.
        key = (str(db.get_bind().url), iso_time(end))
    except AttributeError:
        key = (str(db.engine.url), iso_time(end))
    now = monotonic()
    cached = _cache.get(key)
    if cached and cached[0] > now:
        return cached[1]
    stats = _hour_stats(_series(db, _plate_points(db), start, end), start, end)
    result = stats if stats["usable"] else None
    _cache[key] = (now + _CACHE_TTL, result)
    if len(_cache) > 8:
        for stale in [k for k, v in _cache.items() if v[0] <= now]:
            _cache.pop(stale, None)
    return result
