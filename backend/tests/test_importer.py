"""P3 导入解析单测：M.DD 日期补偿 + 时间解析 + normalizeTs"""
from app.services.importer import extract_date, extract_time, fix_day, normalize_ts


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
