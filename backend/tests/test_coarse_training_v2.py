"""实际 JS/Python 对拍及时间泄漏、重复采样、日级比例的回归。"""
import copy
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from app.services.coarse_training import aggregate_daily, fit, folds, prepare_rows, train_models
from app.services.modeling import MLR_FEATURES, predict_coarse_ash

ROOT = Path(__file__).resolve().parents[2]


def records():
    rows = []
    for day in range(1, 25):
        for hour in (9, 15):
            row = {"timestamp": f"2026-06-{day:02d} {hour:02d}:00:00", "raw_ash": 30 + day / 3,
                   "coal_amount": 900 + hour, "level": 50 + day % 7,
                   "sysA": 1, "sysB": day % 2, "sys401": 1, "sys402": int(day % 3 == 0),
                   "desliming473": 1, "desliming474": int(hour == 9), "is_stoppage": 0,
                   "ash_content": 8 + day / 9 + .3 * (hour == 9)}
            if day % 5 == 0:
                row["raw_ash"] = None
            rows.append(row)
    return rows


def assert_close(a, b):
    if isinstance(a, dict):
        for key, value in a.items():
            assert key in b, key
            assert_close(value, b[key])
    elif isinstance(a, list):
        assert len(a) == len(b)
        for x, y in zip(a, b):
            assert_close(x, y)
    elif isinstance(a, (int, float)) and not isinstance(a, bool):
        assert a == pytest.approx(b, abs=1e-6)
    else:
        assert a == b


@pytest.mark.parametrize("daily", [False, True])
def test_js_python_training_parity(daily):
    if not shutil.which("node"):
        pytest.skip("需要 Node 执行前端 oracle")
    rows = records()
    args = ["node", str(ROOT / "backend/scripts/dump_coarse_v2_js.js"), "--stdin"]
    if daily:
        args.append("--daily")
    js = json.loads(subprocess.check_output(args, input=json.dumps(rows), text=True, encoding="utf-8"))
    py = train_models(aggregate_daily(rows) if daily else rows)
    assert py["production"] == js["production"]
    for kind in ("mlr", "pls"):
        assert_close(py[kind], js[kind])


def test_whole_days_and_forward_only_folds():
    rows = records()
    for outer in (False, True):
        for s, e in folds(rows, outer):
            train_days = {r["timestamp"][:10] for r in rows[:s]}
            test_days = {r["timestamp"][:10] for r in rows[s:e]}
            assert max(train_days) < min(test_days)
            assert train_days.isdisjoint(test_days)


def test_deduplicate_and_keep_switch_proportion():
    rows = records()
    duplicate = dict(rows[0], system="401", ash_content=11)
    clean = prepare_rows(rows + [duplicate])
    assert len(clean) == len(rows)
    assert clean[0]["ash_content"] == 11
    days = aggregate_daily(rows + [dict(rows[0])])
    assert days[0]["n"] == 2
    assert days[0]["desliming474"] == .5
    assert days[0]["raw_ash"] == rows[0]["raw_ash"]


def test_missing_values_use_only_training_and_prediction_never_uses_target():
    rows = records()[:20]
    m = fit(rows, "mlr", {"subset": "expert", "alpha": .1, "penalty": 1})
    valid = [r["raw_ash"] for r in rows if r["raw_ash"] is not None]
    assert m["imputeMeans"][0] == pytest.approx(sum(valid) / len(valid))
    a = dict(rows[-1], raw_ash=None, ash_content=1)
    b = dict(a, ash_content=40)
    assert predict_coarse_ash(a, m) == predict_coarse_ash(b, m)
    assert m["coefs"][1] == 0  # 专家候选不使用带煤量；保留兼容的10列
    assert m["coefs"][6] == 0  # 本训练窗口473恒开，没有可学习的开关效应


def test_distinct_days_required_and_constant_target_is_finite():
    assert train_models(records()[:12]) is None  # 6天，即使已有12条也不冒充充分样本
    rows = [dict(r, ash_content=12) for r in records()]
    result = train_models(rows)
    assert result is not None
    json.dumps(result, allow_nan=False)
    assert predict_coarse_ash(rows[0], result[result["production"]]) == pytest.approx(12)


def test_holdout_targets_do_not_change_first_outer_fold_choice():
    rows = records()
    first_start = folds(rows, True)[0][0]
    changed = copy.deepcopy(rows)
    for i in range(first_start, len(rows)):
        changed[i]["ash_content"] = 35
    a = train_models(rows)["mlr"]["metrics"]["pipelineValidation"]["folds"][0]
    b = train_models(changed)["mlr"]["metrics"]["pipelineValidation"]["folds"][0]
    assert a == b
