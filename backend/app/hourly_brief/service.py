"""Immutable hourly calculations. No training, EMA update or device writes."""
from copy import deepcopy
from datetime import timedelta

from sqlalchemy import select, text

from ..database import SessionLocal
from ..models import CoarseModel, Setting
from ..services.density import calc_total_ash, compute_density_guidance, DENSITY_GUIDE
from ..services.guidance import density_guard_state
from ..services.modeling import MLR_FEATURES, predict_coarse_ash
from ..services.state import SETTING_TO_STORE
from ..telemetry.domain import iso_time, parse_time, utc_now
from .columns import COLUMNS, cell
from .inputs import Inputs, LOCAL, business_time, numeric
from .models import HourlyBriefRow, HourlyBriefSchedule


def next_hour(now):
    return now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)


def settings_and_model(db, hour, inputs):
    store = deepcopy(inputs.auto)
    for setting in db.scalars(select(Setting)):
        if setting.key in SETTING_TO_STORE:
            if business_time(setting.updated_at) <= hour:
                store[SETTING_TO_STORE[setting.key]] = setting.value
            else:
                store.setdefault("_unavailable_settings", []).append(setting.key)
    row = db.scalar(select(CoarseModel).where(CoarseModel.is_current.is_(True)))
    if not row:
        return store, None
    if row.trained_at and business_time(row.trained_at) > hour:
        return store, None
    model = dict(type=row.method, intercept=row.intercept, coefs=row.coefs,
                 imputeMeans=row.impute_means, means=row.means, featureNames=row.feature_names or MLR_FEATURES)
    if (row.metrics or {}).get("inputPolicy") == "clip-training-range":
        model.update(inputPolicy=row.metrics["inputPolicy"], inputBounds=row.metrics.get("inputBounds"))
    metadata = dict(id=row.id, method=row.method, trained_at=row.trained_at, train_run_id=row.train_run_id,
                    n=row.n, model=model)
    return store, metadata


def calculate(cells, store, model_metadata, hour):
    features = {k: cells[k]["value"] for k in ("raw_ash", "coal_amount", "level")}
    for key, group, label in (
        ("sysA", "systems", "A"), ("sysB", "systems", "B"),
        ("sys401", "systems", "401"), ("sys402", "systems", "402"),
        ("desliming473", "desliming", "473"), ("desliming474", "desliming", "474")
    ):
        value = (cells[group]["value"] or {}).get(label)
        features[key] = int(value) if isinstance(value, bool) else None
    raw_amount = cells["coal_amount"].get("raw_value", features["coal_amount"])
    features["is_stoppage"] = int(raw_amount <= 10) if numeric(raw_amount) and raw_amount >= 0 else None
    absent = [k for k in MLR_FEATURES if not numeric(features[k])]
    model = (model_metadata or {}).get("model") or {}
    valid_model = (model.get("type") in ("mlr", "pls") and numeric(model.get("intercept"))
                   and len(model.get("coefs") or []) == len(MLR_FEATURES)
                   and all(numeric(v) for v in model["coefs"])
                   and model.get("featureNames") == MLR_FEATURES)
    if absent:
        feature_labels = dict(raw_ash="原煤灰分", coal_amount="入洗原煤量", level="精磁尾液位",
            sysA="A系统状态", sysB="B系统状态", sys401="401系统状态", sys402="402系统状态",
            desliming473="473脱粉状态", desliming474="474脱粉状态", is_stoppage="停机状态")
        cells["coarse_ash"] = cell(reason="模型输入缺失：" + "、".join(feature_labels[k] for k in absent))
    elif not valid_model:
        cells["coarse_ash"] = cell(reason="无有效的当前保存模型或模型因子不匹配")
    else:
        prediction = predict_coarse_ash(features, model)
        cells["coarse_ash"] = (cell(prediction, "当前保存模型预测", iso_time(hour), "predicted",
            reason="含停机沿用值" if any(cells[k]["status"] == "held" for k in ("raw_ash", "coal_amount")) else "")
            if numeric(prediction) and 0 <= prediction <= 100 else cell(reason="模型预测超出有效灰分范围"))
    required = ("heavy_ash", "heavy_amount", "float_ash", "float_amount", "coarse_ash", "coarse_amount")
    missing = [key for key in required if not numeric(cells[key]["value"])]
    labels = dict((key, title) for key, title, _ in COLUMNS)
    values = {k: c["value"] for k, c in cells.items()}
    valid_amounts = all(numeric(values[k]) and values[k] >= 0 for k in ("heavy_amount", "float_amount", "coarse_amount"))
    valid_ashes = all(numeric(values[k]) and 0 <= values[k] <= 100 for k in ("heavy_ash", "float_ash", "coarse_ash"))
    total = None
    if not missing and valid_amounts and valid_ashes:
        if values["heavy_amount"] + values["float_amount"] + values["coarse_amount"] > 0:
            computed = calc_total_ash(*(values[key] for key in required))
            total = computed if numeric(computed) else None
    cells["total_ash"] = cell(total, "三组分加权公式", iso_time(hour),
        "calculated" if total is not None else "missing",
        ("缺少：" + "、".join(labels[k] for k in missing)) if missing else
        "灰分或煤量无效，或总煤量为零" if total is None else "",
        formula="(重介灰分×重介量 + 浮精灰分×浮精量 + 粗泥灰分×粗泥量) ÷ (重介量 + 浮精量 + 粗泥量)")
    rho = values["density"]
    if total is None or not numeric(rho) or rho <= 0:
        cells["suggested_density"] = cell(reason="缺少可计算的总灰分或有效分选密度")
        return features, None
    if set(store.get("_unavailable_settings", [])) & {"ash_target", "ash_target_tol", "guide_scheme", "density_guide"}:
        cells["suggested_density"] = cell(reason="建议参数在整点后发生更新，无法还原当时配置")
        return features, None
    scheme = "heavy" if store.get("guideScheme") == "heavy" else "total"
    guard_store = deepcopy(store)
    # Use this snapshot's actual inputs for guard identity; never advance live latches.
    driver_times = [business_time(cells[key]["observed_at"]) for key in ("raw_ash", "coal_amount", "level")
                    if cells[key].get("observed_at")]
    driver_time = max(driver_times) if driver_times else hour
    guard_store.update(coarseCoal=[{"timestamp": driver_time.astimezone(LOCAL).strftime("%Y-%m-%d %H:%M:%S")}],
                       totalAshManualOn=False)
    heavy = cells["heavy_ash"]
    guard_store["heavyAshManualOn"] = heavy["status"] == "manual"
    guard_store["heavyAshInput"] = (dict(manual=heavy["value"],
        manualAt=business_time(heavy["created_at"]).timestamp() * 1000) if heavy["status"] == "manual" else {})
    guard = density_guard_state(guard_store, total, values["heavy_ash"], scheme, hour.timestamp() * 1000)
    max_step = (store.get("densityGuide") or {}).get("maxStep")
    if not numeric(max_step) or max_step <= 0:
        max_step = DENSITY_GUIDE["maxStep"]
    target = store.get("ashTarget", 8.5)
    tol = store.get("ashTargetTol", .1)
    if not numeric(target) or not 0 <= target <= 100 or not numeric(tol) or tol < 0:
        cells["suggested_density"] = cell(reason="目标灰分或容差配置无效")
        return features, None
    guide = compute_density_guidance(dict(
        rho_cur=rho, heavy_ash=values["heavy_ash"], actual_total=total, scheme=scheme, tol=tol,
        max_step=max_step, placeholder_manual=guard["placeholder"], hold=guard["hold"],
        hold_reason=guard["holdReason"], drive_key=guard["driveKey"], actual_total_is_constant=False,
        heavy_amt=values["heavy_amount"], float_amt=values["float_amount"], coarse_amt=values["coarse_amount"],
        float_ash=values["float_ash"], coarse_ash=cells["coarse_ash"]["value"]), target)
    cells["suggested_density"] = cell(guide["rhoNew"] if guide["valid"] else None,
        "原建议密度算法", iso_time(hour), "calculated" if guide["valid"] else "missing", guide["reason"])
    return features, guide


def build_snapshot(db, hour, unavailable_reason=None):
    if unavailable_reason:
        return dict(cells={key: cell(reason=unavailable_reason) for key, _, _ in COLUMNS},
                    reason=unavailable_reason, policy="hour-boundary-v2")
    from ..services.decision_sources import shared_snapshot
    result = shared_snapshot(db, hour)
    return dict(cells={key:result['cells'][key] for key, _, _ in COLUMNS},
                model=result['model'], model_inputs={k:result['inputs'][k]['value'] for k in MLR_FEATURES},
                guidance=result['guidance'], sources=result['sources'], target=result['target'],
                density_label=result['density_label'], policy='hour-boundary-shared-v3')


def tick(now=None, sessions=SessionLocal):
    """Single transaction per due hour. Primary key + row lock handle multiple workers."""
    now = now or utc_now()
    with sessions() as db:
        schedule = db.get(HourlyBriefSchedule, "main")
        if schedule and parse_time(schedule.next_hour) > now:
            return
        db.rollback()
        if db.bind.dialect.name == "sqlite":
            db.execute(text("BEGIN IMMEDIATE"))
        schedule = db.get(HourlyBriefSchedule, "main", with_for_update=True)
        if not schedule:
            db.add(HourlyBriefSchedule(key="main", activated_at=iso_time(now), next_hour=iso_time(next_hour(now))))
            db.commit()
            return
        hour = parse_time(schedule.next_hour)
        # After downtime retain missing hour rows, without applying today's model/data to the past.
        for _ in range(24):
            if hour > now:
                break
            if not db.get(HourlyBriefRow, iso_time(hour)):
                reason = "后台服务未在该整点完成快照，未回算历史数据" if (now-hour).total_seconds() > 90 else None
                snapshot = build_snapshot(db, hour, reason)
                db.add(HourlyBriefRow(hour=iso_time(hour), generated_at=iso_time(now), snapshot=snapshot))
            hour += timedelta(hours=1)
        schedule.next_hour = iso_time(hour)
        db.commit()
