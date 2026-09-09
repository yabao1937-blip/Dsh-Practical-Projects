"""P4 推测简报 golden 对拍：与前端 buildHourlyBrief 在种子数据上的输出逐值一致。"""
import json
from pathlib import Path

from app.services.brief import BRIEF_HEADERS, build_hourly_brief

SEED = json.loads((Path(__file__).resolve().parent.parent / "data" / "seed_store.json").read_text(encoding="utf-8"))

# 前端 verify_web2_brief.js 在种子数据 + 默认设置(ashTarget=8.50,tol=0.1,总灰分版)下的实测输出
FIRST_ROW = ["2026-06-16 09:00", "268.5", "235.2", "40.0", "8.50", "503.7",
             "8.52", "8.10", "53.0", "8.22", "27.0", "8.94", "1.451", "", "14.27", "14.27"]
LAST_ROW = ["2026-07-30 21:00", "268.5", "235.2", "40.0", "8.50", "503.7",
            "8.52", "8.10", "50.0", "8.22", "27.0", "8.83", "1.462", "", "12.83", "12.83"]


def test_headers():
    assert build_hourly_brief(SEED)["headers"] == BRIEF_HEADERS


def test_row_count():
    assert len(build_hourly_brief(SEED)["rows"]) == 1069


def test_first_row():
    assert build_hourly_brief(SEED)["rows"][0] == FIRST_ROW


def test_last_row():
    assert build_hourly_brief(SEED)["rows"][-1] == LAST_ROW


def test_heavyash_invalid_manual_rejected():
    """GLM 发现的 bug：负数/布尔/NaN/inf 的重介精煤灰分 manual 不应进入简报，回退 8.50。"""
    for bad in (-5, True, float("nan"), float("inf")):
        s = json.loads(json.dumps(SEED))
        s["heavyAshInput"] = {"manual": bad}
        first = build_hourly_brief(s)["rows"][0]
        assert first[4] == "8.50", f"heavy_ash leaked for manual={bad!r}: {first[4]}"


def test_full_golden_diff():
    """全量逐行 diff：后端 Python 与前端 JS(brief_js.json) 逐值一致。"""
    js_path = Path(__file__).resolve().parent.parent / "data" / "brief_js.json"
    if not js_path.exists():
        return  # golden 文件缺失时跳过（由 dump_brief_js.js 生成）
    js = json.loads(js_path.read_text(encoding="utf-8"))
    py = build_hourly_brief(SEED)
    assert py["headers"] == js["headers"]
    assert len(py["rows"]) == len(js["rows"])
    for i, (jr, pr) in enumerate(zip(js["rows"], py["rows"])):
        assert jr == pr, f"row {i} mismatch:\n  JS={jr}\n  PY={pr}"
