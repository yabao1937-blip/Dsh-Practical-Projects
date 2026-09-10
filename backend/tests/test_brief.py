# -*- coding: utf-8 -*-
"""P4 推测简报：行存在规则(三表齐全才成行/24h窗口)单测 + golden 对拍。

golden(brief_js.json)由 scripts/dump_brief_js.js 生成:向页面注入与
FIXTURE 完全相同的合成 store 后导出前端 buildHourlyBrief 结果,
后端在同一 FIXTURE 上逐行 diff。种子数据三表时间窗不重叠(表1=6-7月、
表2/3=5月),按新规则全程无行——本身即是一条规则回归。
"""
import json
from pathlib import Path

from app.services.brief import BRIEF_HEADERS, build_hourly_brief

SEED = json.loads((Path(__file__).resolve().parent.parent / "data" / "seed_store.json").read_text(encoding="utf-8"))

ZERO_MODEL_NODE = {
    "type": "pls", "intercept": 10.0,
    "coefs": [0.0] * 10, "means": [0.0] * 10, "stds": [1.0] * 10,
    "stdCoef": [0.0] * 10, "imputeMeans": [0.0] * 10,
    "A": 1, "metrics": {}, "n": 3, "tolerance": 0.8, "range": "fixture",
}


def fixture_store() -> dict:
    """与 dump_brief_js.js 中 FIXTURE 完全一致(改任一侧必须同步另一侧)。

    覆盖:三表重叠成行 / 浮精 8-02 07:30 前断档 24.5h(>24h 窗口)缺行 /
    实测密度变化(8-01 22:00 → 1.46) / 粗精煤泥三次新采样重新锚定。
    """
    return {
        "coarseCoal": [
            {"timestamp": "2026-08-01 08:10:00", "ash_content": 12.5, "coal_amount": 800, "level": 55, "raw_ash": 40.0},
            {"timestamp": "2026-08-01 12:00:00", "ash_content": 13.1, "coal_amount": 810, "level": 57, "raw_ash": 40.0},
            {"timestamp": "2026-08-02 08:00:00", "ash_content": 12.8, "coal_amount": 790, "level": 54, "raw_ash": 40.0},
        ],
        "floatCoal": [
            {"timestamp": "2026-08-01 07:30:00", "ash_content": 9.5, "coal_amount": 30.0},
            {"timestamp": "2026-08-02 12:00:00", "ash_content": 9.8, "coal_amount": 32.0},
        ],
        "calcLogs": [
            {"timestamp": "2026-08-01 08:05:00", "calc_type": "ash_density",
             "input_json": json.dumps({"system": "A", "belt": "502", "ash_content": 8.5, "density": 1.45})},
            {"timestamp": "2026-08-01 22:00:00", "calc_type": "ash_density",
             "input_json": json.dumps({"system": "A", "belt": "502", "ash_content": 8.6, "density": 1.46})},
            {"timestamp": "2026-08-02 09:00:00", "calc_type": "ash_density",
             "input_json": json.dumps({"system": "A", "belt": "502", "ash_content": 8.4, "density": 1.47})},
        ],
        "coarseModel": {
            "production": "pls",
            "pls": dict(ZERO_MODEL_NODE),
            "mlr": dict(ZERO_MODEL_NODE, type="mlr"),
            "trainedAt": "2026-08-01 00:00:00", "n": 3, "tolerance": 0.8, "range": "fixture",
        },
    }


def test_headers():
    assert build_hourly_brief(fixture_store())["headers"] == BRIEF_HEADERS


def test_seed_no_overlap_no_rows():
    """种子数据三表时间窗不重叠 → 全程无一行(旧行为会续传凑出 1069 行)。"""
    r = build_hourly_brief(SEED)
    assert r["headers"] == BRIEF_HEADERS
    assert r["rows"] == []


def test_fixture_row_count_and_gap():
    r = build_hourly_brief(fixture_store())
    rows = r["rows"]
    # 8-01 08:00 ~ 8-02 12:00 共 29 小时;浮精 8-01 07:30 → 8-02 12:00 断档,
    # 8-02 07:00~11:00 五个小时浮精超 24h 窗口 → 缺行;共 24 行
    assert len(rows) == 24
    assert rows[0][0] == "2026-08-01 08:00"
    assert rows[-1][0] == "2026-08-02 12:00"
    times = [x[0] for x in rows]
    for missing in ("2026-08-02 07:00", "2026-08-02 09:00", "2026-08-02 11:00"):
        assert missing not in times
    assert "2026-08-02 06:00" in times          # 22.5h ≤ 24h 边界内保留
    assert "2026-08-02 12:00" in times          # 新浮精落桶后恢复


def test_fixture_measured_and_anchor():
    r = build_hourly_brief(fixture_store())
    rows = r["rows"]
    by_h = {x[0]: x for x in rows}
    # 8-01 22:00 表3新密度 1.46 → 实测密度列有值
    assert by_h["2026-08-01 22:00"][13] == "1.460"
    # 8-01 08:00 首个采样:预测列=实测列(锚定 12.5)
    assert by_h["2026-08-01 08:00"][14] == "12.50" and by_h["2026-08-01 08:00"][15] == "12.50"
    # 8-01 12:00 新采样 → 重新锚定 13.1(实测列仅采样时刻有值)
    assert by_h["2026-08-01 12:00"][14] == "13.10" and by_h["2026-08-01 12:00"][15] == "13.10"
    assert by_h["2026-08-01 13:00"][15] == ""


def test_one_table_stale_no_rows():
    """任一表断档超 24h → 全部行跳过。"""
    s = fixture_store()
    s["floatCoal"] = [{"timestamp": "2026-07-30 00:00:00", "ash_content": 9.5, "coal_amount": 30.0}]
    assert build_hourly_brief(s)["rows"] == []


def test_heavyash_invalid_manual_rejected():
    """负数/布尔/NaN/inf 的重介精煤灰分 manual 不应进入简报,回退 502 在线链(夹具最新 502 灰分=8.4)。"""
    for bad in (-5, True, float("nan"), float("inf")):
        s = fixture_store()
        s["heavyAshInput"] = {"manual": bad}
        first = build_hourly_brief(s)["rows"][0]
        assert first[4] == "8.40", f"heavy_ash leaked for manual={bad!r}: {first[4]}"


def test_full_golden_diff():
    """全量逐行 diff：后端 Python 与前端 JS(brief_js.json) 在同一 FIXTURE 上逐值一致。"""
    js_path = Path(__file__).resolve().parent.parent / "data" / "brief_js.json"
    if not js_path.exists():
        return  # golden 文件缺失时跳过（由 dump_brief_js.js 生成）
    js = json.loads(js_path.read_text(encoding="utf-8"))
    py = build_hourly_brief(fixture_store())
    assert py["headers"] == js["headers"]
    assert len(py["rows"]) == len(js["rows"]), f"行数不一致: py={len(py['rows'])} js={len(js['rows'])}"
    for i, (jr, pr) in enumerate(zip(js["rows"], py["rows"])):
        assert jr == pr, f"row {i} mismatch:\n  JS={jr}\n  PY={pr}"
