"""P3 导入解析单测：M.DD 日期补偿 + 时间解析 + normalizeTs + 多因素列提取"""
from app.services.importer import (
    extract_date, extract_time, fix_day, normalize_ts, parse_coarse_factors,
)


def test_fix_day():
    assert fix_day(6, 10) == {"m": 6, "d": 1}    # 6.1 → 6月1日
    assert fix_day(6, 16) == {"m": 6, "d": 16}   # 6.16
    assert fix_day(6, 30) == {"m": 6, "d": 30}   # 6.3 → 6月30日
    assert fix_day(7, 40) == {"m": 7, "d": 4}    # 7.4 → 7月4日


def test_extract_date_string():
    assert extract_date("6.16") == {"m": 6, "d": 16}
    assert extract_date("6.3") == {"m": 6, "d": 30}   # 个位天数省略尾零
    assert extract_date("7.4") == {"m": 7, "d": 4}
    assert extract_date("2026-06-16") == {"m": 6, "d": 16}
    assert extract_date("6/16") == {"m": 6, "d": 16}


def test_extract_date_number():
    assert extract_date(6.16) == {"m": 6, "d": 16}
    assert extract_date(6.3) == {"m": 6, "d": 30}
    assert extract_date(7.4) == {"m": 7, "d": 4}
    assert extract_date(6.1) == {"m": 6, "d": 1}


def test_extract_time():
    assert extract_time("09:31:00") == "09:31:00"
    assert extract_time("9:31") == "09:31:00"
    assert extract_time(0.0) == "00:00:00"


def test_normalize_ts():
    assert normalize_ts("2026-06-16 08:23:00") == "2026-06-16 08:23:00"
    assert normalize_ts("2026/6/16 8:23") == "2026-06-16 08:23:00"
    assert normalize_ts("6.16 08:23") == "2026-06-16 08:23:00"
    assert normalize_ts("6.3 08:23") == "2026-06-30 08:23:00"
    assert normalize_ts("6/16 08:23") == "2026-06-16 08:23:00"


# ---------------- 多因素表解析（与前端 import.js parseCoarseFactors 对齐） ----------------

COARSE_SHEET_ROWS = [
    ["粗精煤泥的灰分影响因素"],
    ["日期", "序号", "时间", "入洗工作面", "原煤灰分（%）", "小时带煤量（t/h）",
     "开启的系统", None, None, None, "脱粉", None, "315灰分（%）", "315全水分（%）", "精磁尾液位（%）"],
    [None, None, None, None, None, None, "A", "B", "401", "402", "473", "474"],
    # 开关 1/0 + 脱粉 开/停 + 多行工作面（取首行）
    ["6.16", 1, "09:31:00", "3309\n43下01", 31.82, 927, 1, 1, 0, 1, "开", "停", 13.86, 24.5, 55],
    # 换工作面 + 474 开
    ["8.23", 2, "10:36:00", "6303\n3309", 38.22, 785, 1, 1, 1, 1, "开", "开", 11.7, 28.3, 74],
    # 坏灰分行跳过
    ["8.23", 3, "11:00:00", "6303\n3309", 38.22, 700, 1, 0, 1, 0, "停", "停", "？", 26.0, 60],
]


def test_parse_coarse_factors_columns():
    r = parse_coarse_factors(COARSE_SHEET_ROWS)
    assert r["errors"] == []
    assert len(r["records"]) == 2

    a = r["records"][0]
    assert a["timestamp"] == "2026-06-16 09:31:00"
    assert a["ash_content"] == 13.86
    assert a["raw_ash"] == 31.82
    assert a["coal_amount"] == 927
    assert a["level"] == 55
    assert a["moisture"] == 24.5
    assert (a["sysA"], a["sysB"], a["sys401"], a["sys402"]) == (1, 1, 0, 1)
    assert (a["desliming473"], a["desliming474"]) == (1, 0)     # 开→1 / 停→0
    assert a["is_stoppage"] == 0
    assert a["mining_face"] == "3309"                            # 多行工作面取首行

    b = r["records"][1]
    assert b["timestamp"] == "2026-08-23 10:36:00"
    assert (b["sys401"], b["sys402"]) == (1, 1)
    assert (b["desliming473"], b["desliming474"]) == (1, 1)
    assert b["mining_face"] == "6303"
