"""P3 导入解析单测：M.DD 日期补偿 + 时间解析 + normalizeTs + 多因素列提取

含 2026-09 审查修复的回归用例：文本两位小数是显式日、畸形日期不抛异常、
跨月回退（月份不推进导致时间倒退）、JS Math.round 取整语义、Excel 序列越界。
"""
from app.services.importer import (
    _js_round, extract_date, extract_date_seq, extract_time, fix_day, normalize_ts,
    parse_coarse_factors,
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
    assert normalize_ts("6.3 08:23") == "2026-06-30 08:23:00"   # 无上下文:沿用旧省尾零规则
    assert normalize_ts("6/16 08:23") == "2026-06-16 08:23:00"


def test_extract_date_seq_disambiguation():
    """序列上下文消歧:同一写法在不同前导日期下含义不同(真实 9-4 表场景)。"""
    # 6.29 之后 → 30 日(省尾零);同日多条靠 >= 保持
    assert extract_date_seq("6.3", {"m": 6, "d": 29}) == {"m": 6, "d": 30}
    assert extract_date_seq("6.3", {"m": 6, "d": 30}) == {"m": 6, "d": 30}
    # 7.1 之后 → 7月2日本身(旧规则会错解析成 7月20)
    assert extract_date_seq("7.2", {"m": 7, "d": 1}) == {"m": 7, "d": 2}
    assert extract_date_seq("7.3", {"m": 7, "d": 2}) == {"m": 7, "d": 3}
    # 8.31 之后跨月 → 9月1日
    assert extract_date_seq("9.1", {"m": 8, "d": 31}) == {"m": 9, "d": 1}
    assert extract_date_seq("9.2", {"m": 9, "d": 1}) == {"m": 9, "d": 2}
    assert extract_date_seq("9.3", {"m": 9, "d": 2}) == {"m": 9, "d": 3}
    # 无上下文 → 旧规则(6.3=30)
    assert extract_date_seq("6.3", None) == {"m": 6, "d": 30}
    # 数值型同规则
    assert extract_date_seq(7.2, {"m": 7, "d": 1}) == {"m": 7, "d": 2}
    assert extract_date_seq(6.3, {"m": 6, "d": 29}) == {"m": 6, "d": 30}
    # 两位小数无歧义
    assert extract_date_seq("6.16", {"m": 6, "d": 15}) == {"m": 6, "d": 16}


def test_parse_coarse_factors_date_sequence():
    """整表解析:7.1/7.2/7.3 连续日期不被误解析成 1/20/30 日。"""
    rows = [
        ["粗精煤泥的灰分影响因素"],
        ["日期", "序号", "时间", "入洗工作面", "原煤灰分（%）", "小时带煤量（t/h）",
         "开启的系统", None, None, None, "脱粉", None, "315灰分（%）", "315全水分（%）", "精磁尾液位（%）"],
        [None, None, None, None, None, None, "A", "B", "401", "402", "473", "474"],
        ["6.29", 1, "08:00:00", "3309", 31.8, 900, 1, 1, 0, 1, "开", "开", 12.0, 25.0, 55],
        ["6.3", 2, "09:00:00", "3309", 31.8, 900, 1, 1, 0, 1, "开", "开", 12.1, 25.0, 55],   # → 6月30
        ["7.1", 3, "10:00:00", "3309", 32.0, 900, 1, 1, 0, 1, "开", "开", 12.2, 25.0, 55],   # → 7月1
        ["7.2", 4, "11:00:00", "3309", 32.0, 900, 1, 1, 0, 1, "开", "开", 12.3, 25.0, 55],   # → 7月2(旧规则=7.20 ✗)
        ["7.2", 5, "12:00:00", "3309", 32.0, 900, 1, 1, 0, 1, "开", "开", 12.4, 25.0, 55],   # 同日第二条
        ["7.3", 6, "13:00:00", "3309", 32.0, 900, 1, 1, 0, 1, "开", "开", 12.5, 25.0, 55],   # → 7月3(旧规则=7.30 ✗)
    ]
    r = parse_coarse_factors(rows)
    ts = [x["timestamp"] for x in r["records"]]
    assert ts == [
        "2026-06-29 08:00:00", "2026-06-30 09:00:00", "2026-07-01 10:00:00",
        "2026-07-02 11:00:00", "2026-07-02 12:00:00", "2026-07-03 13:00:00",
    ]


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


# ---------------- 2026-09 审查修复回归 ----------------

def test_extract_date_text_two_digits_is_explicit_day():
    """文本两位小数是显式日：旧实现一律「frac<10 就 ×10」，与同内容数值单元格自相矛盾。"""
    assert extract_date("6.02") == {"m": 6, "d": 2}      # 旧 → 6月20日 ✗
    assert extract_date("6.03") == {"m": 6, "d": 3}      # 旧 → 6月30日 ✗
    assert extract_date("6.10") == {"m": 6, "d": 10}     # 旧命中 fix_day 的 dd100==10 特例 → 6月1日 ✗
    assert extract_date("6.30") == {"m": 6, "d": 30}
    assert extract_date("6.16") == {"m": 6, "d": 16}
    # 同一内容的数值单元格本来就是 2 日 —— 修复后文本/数值一致
    assert extract_date(6.02) == {"m": 6, "d": 2}
    # 单个小数位的「省尾零」补偿保持不变
    assert extract_date("6.3") == {"m": 6, "d": 30}
    assert extract_date("6.1") == {"m": 6, "d": 1}
    assert extract_date("7.4") == {"m": 7, "d": 4}


def test_extract_date_malformed_returns_none_without_raising():
    """畸形日期单元格不再抛异常（旧实现 int(parts[1]) → ValueError → /import/parse 500）。"""
    for bad in ["6-16(早班)", "6-7月", "7-", "6/abc", "-16", "abc-def"]:
        assert extract_date(bad) is None, bad
    # 越界同样返回 None，而不是造出 6月32日
    assert extract_date("6-32") is None
    assert extract_date("13-1") is None
    # 合法写法不受影响
    assert extract_date("6-16") == {"m": 6, "d": 16}
    assert extract_date("6/16") == {"m": 6, "d": 16}


def test_extract_date_seq_rolls_month_forward():
    """跨月忘记改月份：6.30 之后的「6.4」是 7月4日，不能倒退成 6月4日（时间戳非单调）。"""
    prev = {"m": 6, "d": 29}
    prev = extract_date_seq("6.3", prev)
    assert prev == {"m": 6, "d": 30}
    prev = extract_date_seq("6.4", prev)
    assert prev == {"m": 7, "d": 4}                      # 旧 → 6月4日（比前一行早 26 天）✗
    # 不跨月的原有行为不变
    assert extract_date_seq("6.4", {"m": 6, "d": 1}) == {"m": 6, "d": 4}
    assert extract_date_seq("7.2", {"m": 7, "d": 1}) == {"m": 7, "d": 2}
    # 12 月不再往后推到 13 月
    assert extract_date_seq("12.4", {"m": 12, "d": 30}) == {"m": 12, "d": 4}


def test_js_round_semantics_matches_math_round():
    """Python round 是银行家舍入（round(12.5)=12），与 JS Math.round（.5 向上）不一致。"""
    assert _js_round(12.5) == 13                         # Python round(12.5) == 12 ✗
    assert _js_round(13.5) == 14
    assert _js_round(-0.5) == 0                          # JS Math.round(-0.5) === -0（数值等于 0）
    # 0.125 可被二进制精确表示 → (6.125-6)*100 == 12.5 恰好落在半值上
    assert extract_date(6.125) == {"m": 6, "d": 13}      # 旧 → 6月12日（与前端差 1 天）✗
    # 半秒时间分数同理会差 1 秒
    assert extract_time(0.5 / 86400) == "00:00:01"


def test_excel_serial_out_of_range_returns_none():
    """日期列里的游离大数字：旧实现 OverflowError → 500。

    注意这是「已知边界差异」而非严格对拍：JS 的 Date 能表示到 ±273790 年，
    9999999 这类序列号在浏览器里会得到一个无意义日期；Python datetime 只能到公元 9999 年，
    故返回 None（该行回退原始文本）。Excel 真实日期序列在 1~60000 之间，不受影响。
    """
    assert extract_date(9999999) is None
    assert extract_date(45292) == {"m": 1, "d": 1}       # 2024-01-01（正常序列，与 JS 一致）
    assert extract_date(25569 + 19723) == {"m": 1, "d": 1}


def test_parse_coarse_factors_malformed_date_no_crash():
    """整表解析遇到畸形日期单元格不得抛异常：该行日期回退原始文本，其余行照常解析。"""
    rows = [
        ["粗精煤泥的灰分影响因素"],
        ["日期", "序号", "时间", "入洗工作面", "原煤灰分（%）", "小时带煤量（t/h）",
         "开启的系统", None, None, None, "脱粉", None, "315灰分（%）", "315全水分（%）", "精磁尾液位（%）"],
        [None, None, None, None, None, None, "A", "B", "401", "402", "473", "474"],
        ["6-16(早班)", 1, "09:31:00", "3309", 31.8, 927, 1, 1, 0, 1, "开", "开", 13.86, 24.5, 55],
        ["6.17", 2, "10:31:00", "3309", 31.8, 927, 1, 1, 0, 1, "开", "开", 13.90, 24.5, 55],
    ]
    r = parse_coarse_factors(rows)                       # 旧实现此处直接 ValueError
    ts = [x["timestamp"] for x in r["records"]]
    assert ts[0] == "6-16(早班) 09:31:00"                # 解析失败 → 回退原始文本（不再是 NaN 毒行）
    assert ts[1] == "2026-06-17 10:31:00"
