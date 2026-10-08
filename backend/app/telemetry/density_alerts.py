"""Read-only MD/MD_SET checks. Held business values must not mask meter faults."""
from decimal import Decimal
import math

from .derived import DENSITY_AVERAGE_MEMBERS
from .domain import parse_time

DENSITY_DEVIATION_LIMIT = Decimal("0.3")


def density_alarm_checks(items, now):
    checks = []
    for source in sorted({p["source_id"] for p in items}):
        scoped = [p for p in items if p["source_id"] == source]
        for label, actual_id in DENSITY_AVERAGE_MEMBERS:
            set_id = actual_id + "_SET"
            actual = [p for p in scoped if p.get("item_id") == actual_id]
            target = [p for p in scoped if p.get("item_id") == set_id]
            if not actual and not target:
                continue
            check = dict(id=f"density-deviation:{source}:{label}", source_id=source, device=label,
                status="unknown", actual=None, setpoint=None, deviation=None,
                threshold=float(DENSITY_DEVIATION_LIMIT), received_at=None,
                actual_item_id=actual_id, setpoint_item_id=set_id,
                message=f"{label} 密度偏差无法判断：实际或设定密度测点缺失、配置不唯一")
            checks.append(check)
            if len(actual) != 1 or len(target) != 1:
                continue
            pair = [actual[0], target[0]]
            samples = [p.get("sample") or {} for p in pair]
            usable = True
            for p, sample in zip(pair, samples):
                value = sample.get("value")
                try:
                    usable = usable and (p.get("available") and sample.get("quality") == "good"
                        and not sample.get("issue") and isinstance(value, (int,float))
                        and not isinstance(value, bool) and math.isfinite(value)
                        and sample.get("definition_id") == p.get("definition_id")
                        and parse_time(sample["received_at"]) <= now)
                except (KeyError, TypeError, ValueError, OverflowError):
                    usable = False
            check["message"] = f"{label} 密度偏差无法判断：实际或设定密度缺失、过期或采集异常"
            if not usable:
                continue
            check["message"] = f"{label} 密度偏差无法判断：实际与设定密度的单位或采集批次不一致"
            if (str(pair[0].get("unit") or "").strip() != str(pair[1].get("unit") or "").strip()
                    or not samples[0].get("batch_id") or samples[0].get("batch_id") != samples[1].get("batch_id")
                    or parse_time(samples[0]["received_at"]) != parse_time(samples[1]["received_at"])):
                continue
            measured, setpoint = [s["value"] for s in samples]
            # Decimal protects the exact 0.3 boundary (e.g. 1.6 - 1.3).
            deviation = abs(Decimal(str(measured)) - Decimal(str(setpoint)))
            abnormal = deviation > DENSITY_DEVIATION_LIMIT
            check.update(status="abnormal" if abnormal else "normal", actual=measured, setpoint=setpoint,
                deviation=float(deviation), received_at=samples[0]["received_at"],
                message=(f"{label} 密度异常：实际 {measured:.3f}，设定 {setpoint:.3f}，"
                         f"偏差 {deviation:.3f} > 阈值 0.300") if abnormal else f"{label} 实际与设定密度偏差正常")
    return checks
