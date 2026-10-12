"""Read inputs known at an hour boundary; never rewrite telemetry or measurements."""
import json
import math
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from ..models import AutoState, CalcLog, CoalRecord, ManualMeasurement, Setting
from ..services.resolvers import INSTRUMENT_DEFAULT, measurement_number, manual_valid
from ..telemetry.derived import FOUR_SYSTEM_MEMBERS, DESLIMING_MEMBERS, FLOAT_PRESS_MEMBERS, DENSITY_AVERAGE_MEMBERS
from ..telemetry.domain import availability, definition_id, iso_time, parse_time
from ..telemetry.models import TelemetryPoint, TelemetryReading, TelemetrySource
from ..telemetry.retained import MEMORY, density_history, raw_sample, usable_at_receipt
from ..telemetry.density_effective import effective_average
from .columns import cell

LOCAL = timezone(timedelta(hours=8))
RAW_ASH = "TIA_3_1.TIA_3_1.202/ASH_R_INS"
COAL_AMOUNT = "TIA_3_1.TIA_3_1.301/COAL_CAP"
LEVEL = "TIA_3_5.TIA_3_5.JMCWC/LEVEL_B"
HEAVY_AMOUNT = "TIA_3_4.TIA_3_4.502/COAL_CAP"


def numeric(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def business_time(value):
    if value is None or isinstance(value, bool):
        raise ValueError("missing business timestamp")
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, timezone.utc)
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt.replace(tzinfo=dt.tzinfo or LOCAL).astimezone(timezone.utc)


class Inputs:
    def __init__(self, db, hour, live=False, authoritative=False):
        self.db, self.hour = db, hour
        self.live = live
        self.authoritative = authoritative
        self.cutoff = iso_time(hour)
        self.points = {}
        for p in db.scalars(select(TelemetryPoint).where(TelemetryPoint.enabled.is_(True))):
            self.points.setdefault(p.config.get("item_id"), []).append(p)
        self.sources = {s.source_id: s for s in db.scalars(select(TelemetrySource))}
        auto_rows = list(db.scalars(select(AutoState)))
        self.auto = {r.key: r.value for r in auto_rows
                     if business_time(r.updated_at) <= hour}
        # A later edit to one instrument must not hide other older manual
        # sub-values in the same JSON row; each has its own manualAt gate.
        self.manual_auto = {r.key: r.value for r in auto_rows if r.key in
            ("instrumentInputs", "amountInputs", "heavyAshInput", "floatAshInput")}
        self.settings = {r.key: r.value for r in db.scalars(select(Setting))
                         if business_time(r.updated_at) <= hour}

    def point(self, item, source_id=None):
        points = self.points.get(item, [])
        if source_id is not None:
            points = [p for p in points if p.source_id == source_id]
        return points[0] if len(points) == 1 else None

    def before(self, sample):
        try:
            return bool(sample and parse_time(sample["received_at"]) <= self.hour)
        except (TypeError, KeyError, ValueError):
            return False

    def latest(self, point):
        candidates = [raw_sample(point.latest)] if not self.authoritative and self.before(point.latest) else []
        row = self.db.scalar(select(TelemetryReading).where(
            TelemetryReading.source_id == point.source_id, TelemetryReading.point_key == point.key,
            TelemetryReading.received_at <= self.cutoff,
            TelemetryReading.sample["definition_id"].as_string() == definition_id(point.config)
        ).order_by(TelemetryReading.received_at.desc(), TelemetryReading.id.desc()).limit(1))
        if row:
            candidates.append({**raw_sample(row.sample), "batch_id": row.batch_id})
        return max(candidates, key=lambda s: parse_time(s["received_at"]), default=None)

    def usable(self, point, sample):
        if not usable_at_receipt(sample, point.config):
            return False
        if self.live:
            source = self.sources.get(point.source_id)
            if not source or source.state != 'connected' or not source.lease_until or parse_time(source.lease_until) <= self.hour:
                return False
        # The current connection can change just AFTER the boundary. Judge the
        # already received sample at the hour, not a future source-state update.
        return availability(sample, point.config, True, self.hour)[0]

    def nonzero(self, point):
        cached = ((point.latest or {}).get(MEMORY) or {}).get("last_nonzero")
        candidates = [cached] if not self.authoritative and self.before(cached) and usable_at_receipt(cached, point.config) else []
        query = select(TelemetryReading).where(
            TelemetryReading.source_id == point.source_id, TelemetryReading.point_key == point.key,
            TelemetryReading.received_at <= self.cutoff,
            TelemetryReading.sample["definition_id"].as_string() == definition_id(point.config),
            TelemetryReading.sample["quality"].as_string() == "good",
            TelemetryReading.sample["value"].as_float() != 0
        ).order_by(TelemetryReading.received_at.desc()).execution_options(yield_per=64)
        for row in self.db.scalars(query):
            if usable_at_receipt(row.sample, point.config):
                candidates.append({**row.sample, "batch_id": row.batch_id})
                break
        return max(candidates, key=lambda s: parse_time(s["received_at"]), default=None)

    def telemetry(self, item, zero_hold=False, unit=None, source_id=None, point=None):
        p = point or self.point(item, source_id)
        if not p:
            return cell(reason="未配置测点或同名测点来源不唯一", item_id=item)
        s = self.latest(p)
        meta = dict(source="KEPServer", item_id=item, point_key=p.key, source_id=p.source_id,
                    sample=s, unit=unit or p.config.get("unit", ""))
        if not s or not self.usable(p, s):
            return cell(reason="整点前无有效读数，或采集离线、质量异常、读数过期", **meta)
        if zero_hold and s["value"] == 0:
            held = self.nonzero(p)
            return cell(held["value"] if held else None, observed_at=held["received_at"] if held else None,
                        status="held", reason="设备停机 · 沿用上次非零读数" if held else "设备停机 · 无上次非零读数",
                        raw_value=0, retained_sample=held, **meta)
        return cell(s["value"], observed_at=s["received_at"], status="good", **meta)

    def density(self):
        points = [self.point(item) for _, item in DENSITY_AVERAGE_MEMBERS]
        if not all(points) or points[0].source_id != points[1].source_id:
            return cell(reason="A303/A304 密度测点缺失或来源不一致")
        items = []
        for p in points:
            sample = self.latest(p)
            items.append(dict(source_id=p.source_id, key=p.key, item_id=p.config["item_id"],
                unit=p.config.get("unit", ""), definition_id=definition_id(p.config), sample=sample, as_of=self.cutoff,
                available=self.usable(p, sample), status="good",
                last_density_sample=density_history(self.db, p, self.cutoff, authoritative=self.authoritative)))
        effective = effective_average(items)
        return cell(effective["value"], "KEPServer · A303/A304 平均", effective["received_at"],
                    effective["status"], effective["reason"], sample=effective,
                    components=effective["components"], policy=effective["policy"])

    def group(self, members, source_id=None):
        parts = {label: self.telemetry(item, source_id=source_id) for label, item in members}
        values = {label: c["value"] if isinstance(c["value"], bool) else None for label, c in parts.items()}
        missing = [label for label, v in values.items() if v is None]
        samples = [c.get("sample") or {} for c in parts.values()]
        same_batch = (len({c.get("source_id") for c in parts.values()}) == 1
                      and all(s.get("batch_id") for s in samples)
                      and len({s.get("batch_id") for s in samples}) == 1
                      and len({s.get("received_at") for s in samples}) == 1)
        if not missing and not same_batch:
            return cell({label: None for label in values}, "KEPServer", status="missing",
                        reason="各设备状态不属于同一采集批次", components=parts)
        return cell(values, "KEPServer", status="missing" if missing else "good",
                    reason="无有效状态：" + "、".join(missing) if missing else "", components=parts)

    def manual(self, metric=None, auto_paths=(), record=None, calc=None):
        """Latest manual measurement by observation time, gated by availability time."""
        candidates = []
        if metric:
            row = self.db.scalar(select(ManualMeasurement).where(
                ManualMeasurement.metric == metric, ManualMeasurement.observed_at <= self.cutoff,
                ManualMeasurement.created_at <= self.cutoff
            ).order_by(ManualMeasurement.observed_at.desc(), ManualMeasurement.id.desc()).limit(1))
            if row:
                candidates.append(cell(row.value, "手工补录", row.observed_at, "manual",
                                       record_id=row.id, created_at=row.created_at, metric=metric))
        for path in auto_paths:
            cfg = self.manual_auto
            for part in path:
                cfg = cfg.get(part, {}) if isinstance(cfg, dict) else {}
            value = measurement_number(cfg.get("manual")) if isinstance(cfg, dict) else None
            validity_key = "heavyAsh" if path == ("heavyAshInput",) else path[-1] if path[0] == "instrumentInputs" else None
            if validity_key and not manual_valid(self.auto, cfg, validity_key):
                continue
            if path == ("heavyAshInput",) and self.settings.get("heavy_ash_manual_on") is False:
                continue
            try:
                entered = business_time(cfg.get("manualAt"))
                observed = business_time(cfg.get("sampleAt") or cfg["manualAt"])
            except (ValueError, TypeError, KeyError, OverflowError):
                continue
            if value is not None and value >= 0 and entered <= self.hour and observed <= self.hour:
                candidates.append(cell(value, "在线仪表手工值", iso_time(observed), "manual",
                                       created_at=iso_time(entered), input_path=".".join(path)))
        if record:
            category, field, belt = record
            query = select(CoalRecord).where(CoalRecord.category == category, CoalRecord.source == "manual",
                    CoalRecord.ts <= self.hour.astimezone(LOCAL).strftime("%Y-%m-%d %H:%M:%S"),
                    CoalRecord.created_at <= self.hour.astimezone(LOCAL).replace(tzinfo=None),
                    getattr(CoalRecord, field).is_not(None))
            if belt:
                query = query.where(CoalRecord.belt == belt)
            row = self.db.scalar(query.order_by(CoalRecord.ts.desc(), CoalRecord.id.desc()).limit(1))
            if row and numeric(getattr(row, field)):
                candidates.append(cell(getattr(row, field), "手工煤样记录", iso_time(business_time(row.ts)),
                                       "manual", record_id=row.id, created_at=iso_time(business_time(row.created_at))))
        if calc:
            kind, field, belt = calc
            query = select(CalcLog).where(CalcLog.calc_type == kind,
                    CalcLog.ts <= self.hour.astimezone(LOCAL).strftime("%Y-%m-%d %H:%M:%S"),
                    CalcLog.created_at <= self.hour.astimezone(LOCAL).replace(tzinfo=None)
            ).order_by(CalcLog.ts.desc(), CalcLog.id.desc())
            for row in self.db.scalars(query):
                try:
                    data = json.loads(row.input_json)
                    value = measurement_number(data.get(field))
                except (ValueError, TypeError, AttributeError):
                    continue
                if value is not None and (belt is None or str(data.get("belt")) == belt):
                    candidates.append(cell(value, "在线仪表补录", iso_time(business_time(row.ts)), "manual",
                                           record_id=row.id, created_at=iso_time(business_time(row.created_at))))
                    break
        return max(candidates, key=lambda c: (business_time(c["observed_at"]),
                   business_time(c["created_at"])), default=None)

    @staticmethod
    def default(key, reason=""):
        return cell(INSTRUMENT_DEFAULT[key], "在线仪表默认值", status="default", reason=reason, default_key=key)

    def collect(self):
        result = {
            "raw_ash": self.telemetry(RAW_ASH, zero_hold=True, unit="%"),
            "coal_amount": self.telemetry(COAL_AMOUNT, zero_hold=True, unit="t/h"),
            "systems": self.group(FOUR_SYSTEM_MEMBERS),
            "desliming": self.group(DESLIMING_MEMBERS),
            "float_press": self.group(FLOAT_PRESS_MEMBERS),
            "density": self.density(),
            "reagent": cell(reason="尚未配置药剂量测点、手工录入或默认值"),
        }
        result["level"] = self.telemetry(LEVEL, unit="%")
        if result["level"]["value"] is None:
            result["level"] = self.manual(auto_paths=[("instrumentInputs", "level_tail")],
                record=("coarse", "level", None)) or self.default("level_tail", result["level"]["reason"])
        result["coarse_amount"] = self.manual("coarse_slime_amount", [("amountInputs", "coarseAmount")])
        if not result["coarse_amount"]:
            cc = self.auto.get("coarseCalc") or {}
            a, b = cc.get("screen315"), cc.get("waterUnder")
            result["coarse_amount"] = (cell(round(a-b, 1), "在线仪表计算", self.cutoff, "calculated",
                formula="315筛上量 − 筛下水量", inputs=cc) if numeric(a) and numeric(b) and a >= b >= 0
                else cell(reason="无粗精煤泥量手工补录，在线仪表计算参数不完整"))
        result["raw_slime"] = self.manual("raw_coal_slime_content") or cell(reason="无原煤煤泥含量手工补录或默认值")
        result["heavy_ash"] = self.manual("heavy_clean_coal_ash",
            [("heavyAshInput",), ("instrumentInputs", "ash_502")],
            record=("ash_density", "ash_content", "502"), calc=("ash_meter", "value", "502")) or self.default("ash_502")
        result["heavy_amount"] = self.manual(auto_paths=[("amountInputs", "denseAmount"),
            ("instrumentInputs", "scale_502")], calc=("belt_scale", "value", "502"))
        if not result["heavy_amount"]:
            kep = self.telemetry(HEAVY_AMOUNT)
            if kep["value"] is not None and kep.get("unit") == "t/h":
                result["heavy_amount"] = kep
            else:
                result["heavy_amount"] = self.default("scale_502",
                    "502 点位单位尚未确认，使用默认值" if self.point(HEAVY_AMOUNT) else "未接入已确认单位的重介量测点")
        result["float_ash"] = self.manual(auto_paths=[("floatAshInput",)],
            record=("float", "ash_content", None)) or self.default("float_ash")
        result["float_amount"] = self.manual(auto_paths=[("amountInputs", "floatAmount")],
            record=("float", "coal_amount", None)) or cell(reason="无浮精量手工记录或默认值")
        return result
