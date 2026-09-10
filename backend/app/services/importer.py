"""三表 Excel 导入解析（逐值对齐前端 import.js 的 normalizeTs / parseCoarseFactors）。

关键：M.DD 手写日期语义补偿（个位天数省略尾零：6.3=6月30日、7.4=7月4日、6.1=6月1日）。

年份：手写表无年份按 DEFAULT_YEAR；单元格自带年份（ISO 写法 / Excel 真实日期序列）优先；
解析出的月日早于上一行时年份 +1（跨年）。与前端同名函数逐值一致。
"""
import math
import re
from datetime import datetime, timedelta

# 手写表无年份，统一按 2026（与前端 import.js 的 DEFAULT_YEAR 一致；跨年由 resolve_ymd 递推）
DEFAULT_YEAR = 2026

# 表头识别失败的错误文案：必须与前端 import.js 的同名字符串逐字一致，
# 否则导入跨语言 golden 无法逐条对拍 errors（此前后端是裸字符串、前端是 {row,col,msg} 对象）
UNRECOGNIZED_HEADER_MSG = "未识别到多因素表头（需含“原煤灰分/315灰分/精磁尾液位”）"


def _pad(n) -> str:
    return str(n).rjust(2, "0")


def fix_day(m: int, dd100: int) -> dict:
    """与前端 fixDay 一致。"""
    if dd100 == 10:
        return {"m": m, "d": 1}
    if 1 <= dd100 <= 31:
        return {"m": m, "d": dd100}
    return {"m": m, "d": math.floor(dd100 / 10)}


def _js_round(x: float) -> int:
    """JS Math.round 语义（四舍五入，.5 向上）。

    Python 内建 round 是银行家舍入（round(12.5) == 12），与前端不一致：
    数值日期 6.125 会得到 6月12日(JS 6月13日)。所有与前端对拍的取整都必须走这里。
    """
    return math.floor(x + 0.5)


def _excel_serial_to_date(serial: float) -> dict | None:
    # JS: new Date(Math.round((serial - 25569) * 86400 * 1000))，取本地年月日（+08:00）
    ms = _js_round((serial - 25569) * 86400 * 1000)
    if not math.isfinite(ms):
        return None
    try:
        dt = datetime(1970, 1, 1) + timedelta(milliseconds=ms) + timedelta(hours=8)
    except (OverflowError, ValueError):
        # 已知边界差异：JS Date 可表示到 ±273790 年，datetime 只能到公元 9999 年。
        # Excel 真实日期序列在 1~60000 之间，出现这种数字说明该单元格不是日期；
        # 旧实现会抛 OverflowError 冒泡成 500，这里返回 None（该行回退原始文本）。
        return None
    # 带上年份：Excel 单元格里若写了真实日期（如 2027-01-05），年份必须保留，
    # 否则会被 DEFAULT_YEAR 顶掉成 2026-01-05
    return {"y": dt.year, "m": dt.month, "d": dt.day}


def extract_date(dc) -> dict | None:
    """与前端 extractDate 一致（无上下文的旧规则,保留作回退）。"""
    if dc is None:
        return None
    if isinstance(dc, bool):
        return None
    if isinstance(dc, (int, float)):
        if dc >= 1000:  # Excel 日期序列
            return _excel_serial_to_date(float(dc))
        m = math.floor(dc)
        dd100 = _js_round((dc - m) * 100)
        if dd100 <= 0:
            return None
        return fix_day(m, dd100)
    s = str(dc).strip()
    iso = re.match(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", s)
    if iso:
        # 保留单元格里的年份（旧实现只取月日，ISO 写法里的年份被无声丢弃）
        return {"y": int(iso.group(1)), "m": int(iso.group(2)), "d": int(iso.group(3))}
    dot = re.match(r"^(\d{1,2})\.(\d{1,2})$", s)
    if dot:
        m = int(dot.group(1))
        frac_s = dot.group(2)
        # 单个小数位 = 手写「省略尾零」写法（6.3 可能是 30 日），需 fix_day 补偿
        if len(frac_s) == 1:
            return fix_day(m, int(frac_s) * 10)
        # 两位小数是显式日：6.02→2 日、6.10→10 日、6.16→16 日。
        # 旧实现一律「frac<10 就 ×10」，把文本 "6.02" 读成 20 日；"6.10" 又撞上
        # fix_day 的 dd100==10 特例被读成 1 日 —— 与同内容的数值单元格(6.02→2 日)自相矛盾。
        d = int(frac_s)
        return {"m": m, "d": d} if 1 <= d <= 31 else fix_day(m, d)
    parts = re.split(r"[/-]", s)
    # parts[1] 必须也是纯数字：日期列里出现 "6-16(早班)" / "7-" / "6/abc" 时，
    # int(parts[1]) 会抛 ValueError 冒泡成 500（前端同位置则产生 NaN 时间戳毒行）
    if (len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit()
            and 1 <= int(parts[0]) <= 12 and 1 <= int(parts[1]) <= 31):
        return {"m": int(parts[0]), "d": int(parts[1])}
    return None


def _ambiguous_frac(dc):
    """识别「M.f 单个小数位」写法(6.3 可能是 6月30 也可能是 6月3),返回 (m, f) 或 None。

    数值型 6.3 的 (dc-m)*100 舍入为 30(尾零已被 Excel 归一化),单个小数位
    数值上必然 %10==0;字符串型直接匹配一位小数。
    """
    if dc is None or isinstance(dc, bool):
        return None
    if isinstance(dc, (int, float)):
        if dc >= 1000:
            return None
        m = math.floor(dc)
        frac_dd = _js_round((dc - m) * 100)
        if frac_dd % 10 == 0 and 10 <= frac_dd <= 90:
            return (m, frac_dd // 10)
        return None
    mt = re.match(r"^(\d{1,2})\.(\d)$", str(dc).strip())
    if mt:
        return (int(mt.group(1)), int(mt.group(2)))
    return None


def _prev_md(prev):
    """上一行日期归一成 (月, 日)：兼容 (y,m,d) 元组与 {"y","m","d"} 字典两种形式。"""
    if prev is None:
        return None
    if isinstance(prev, (tuple, list)):
        return (prev[-2], prev[-1])
    return (prev["m"], prev["d"])


def extract_date_seq(dc, prev=None) -> dict | None:
    """带序列上下文的日期解析（与前端 import.js extractDateSeq 一致）。

    prev 可为 (y,m,d) 元组或 {"m","d"} 字典；只用到月日。

    背景：同一手写表中「6.3」跟在 6.29 后=6月30日,而「7.1~7.9」「9.1~9.3」
    就是 1~9 日本身——旧 fixDay 一律按"省略尾零"把 7.2/7.3/9.2/9.3 解析成
    20/30/20/30 日,产生错误时间戳。规则：
    - 候选日 = {f, f*10}(单个小数位才有歧义;两位小数无歧义);
    - 取「不早于前一行日期的最小候选」(同一行内多条同日记录靠 >= 保持同日;
      6.3 在 6.29 之后 → 3<29 被拒 → 30 ✓;9.2 在 9.1 之后 → 2 ✓);
    - 本月候选全不合格时再试下个月(m+1)：手写表跨月常忘记改月份,
      如 6.29 → 6.30 → 「6.4」应为 7月4日;不加这一步会退回旧 fixDay 得到 6月4日,
      比前一行倒退 26 天(时间戳非单调,后续切桶与排序都会错);
    - 仍不合格 → 回退旧 fixDay 规则。
    """
    legacy = extract_date(dc)
    if legacy is None:
        return None
    pmd = _prev_md(prev)
    amb = _ambiguous_frac(dc)
    if amb is None or pmd is None:
        return legacy
    m, f = amb
    cands = sorted({f, f * 10})
    for d in cands:
        if 1 <= d <= 31 and (m, d) >= pmd:
            return {"m": m, "d": d}
    if m + 1 <= 12:
        for d in cands:
            if 1 <= d <= 31 and (m + 1, d) >= pmd:
                return {"m": m + 1, "d": d}
    return legacy


def extract_time(tc) -> str:
    """与前端 extractTime 一致。"""
    if tc is None:
        return "00:00:00"
    if isinstance(tc, bool):
        return "00:00:00"
    if isinstance(tc, (int, float)):
        if tc < 0:
            return "00:00:00"
        frac = tc - math.floor(tc)
        tot = _js_round(frac * 86400)     # 半秒分数：JS Math.round 向上，Python round 是银行家舍入
        return f"{_pad(tot // 3600)}:{_pad((tot % 3600) // 60)}:{_pad(tot % 60)}"
    s = str(tc)
    mt = re.search(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", s)
    if mt:
        return f"{_pad(int(mt.group(1)))}:{_pad(int(mt.group(2)))}:{_pad(int(mt.group(3) or 0))}"
    return "00:00:00"


def resolve_ymd(dc, prev=None) -> tuple | None:
    """把日期单元格解析成 (年, 月, 日)，与前端 import.js resolveYmd 一致。

    年份规则（旧实现一律写死 DEFAULT_YEAR，跨年数据会整批错一年）：
    - 单元格自带年份（ISO 写法 / Excel 真实日期序列）→ 用它；
    - 否则沿用上一行的年份，再退回 DEFAULT_YEAR；
    - **仅当上一行是 12 月、本行月份小于 12** 时年份 +1（12.30 之后接 1.1 = 次年）。
      刻意不采用「月份变小就跨年」的宽规则：手写表里月份偶尔写乱（7月里混一行 6.02）
      很常见，宽规则会把这类乱序行整批抬到下一年，比时间戳略早更严重。
    """
    md = extract_date_seq(dc, prev)
    if md is None:
        return None
    y = md.get("y") or (prev[0] if prev else None) or DEFAULT_YEAR
    if prev is not None and md["m"] < prev[1] and prev[1] == 12:
        y += 1
    return (y, md["m"], md["d"])


def fmt_ts(ymd, tc) -> str:
    """(年,月,日) + 时间单元格 → "YYYY-MM-DD HH:MM:SS"。"""
    return f"{ymd[0]}-{_pad(ymd[1])}-{_pad(ymd[2])} {extract_time(tc)}"


def parse_ts(dc, tc, prev=None) -> str:
    ymd = resolve_ymd(dc, prev)
    return fmt_ts(ymd, tc) if ymd else f"{str(dc or '').strip()} {extract_time(tc)}"


def normalize_ts(raw) -> str | None:
    """与前端 normalizeTs 一致（表2/表3 模板导入的时间解析）。"""
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        d = datetime(1970, 1, 1) + timedelta(days=raw - 25569) + timedelta(hours=8)
        return d.strftime("%Y-%m-%d %H:%M:%S")
    s = str(raw).strip()
    if not s:
        return None
    m = re.match(r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})[ T](\d{1,2}):(\d{2})(?::(\d{2}))?", s)
    if m:
        return f"{m.group(1)}-{_pad(int(m.group(2)))}-{_pad(int(m.group(3)))} {_pad(int(m.group(4)))}:{_pad(int(m.group(5)))}:{_pad(int(m.group(6) or 0))}"
    m = re.match(r"^(\d{1,2})\.(\d{1,2})[ T](\d{1,2}):(\d{2})(?::(\d{2}))?", s)
    if m:
        mm = int(m.group(1))
        frac = int(m.group(2))
        dd100 = frac * 10 if frac < 10 else frac
        d = fix_day(mm, dd100)["d"]
        return f"{DEFAULT_YEAR}-{_pad(mm)}-{_pad(d)} {_pad(int(m.group(3)))}:{_pad(int(m.group(4)))}:{_pad(int(m.group(5) or 0))}"
    m = re.match(r"^(\d{1,2})/(\d{1,2})[ T](\d{1,2}):(\d{2})(?::(\d{2}))?", s)
    if m:
        return f"{DEFAULT_YEAR}-{_pad(int(m.group(1)))}-{_pad(int(m.group(2)))} {_pad(int(m.group(3)))}:{_pad(int(m.group(4)))}:{_pad(int(m.group(5) or 0))}"
    return None


def _to_num(v):
    if v is None or v == "":
        return float("nan")
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return float("nan")


def _parse_des(v) -> float:
    """脱粉列解析（与前端 import.js parseDes 一致：开/1→1，停/关/0→0，数值原样，坏值 0）。"""
    s = str("" if v is None else v).strip()
    if s in ("开", "1"):
        return 1.0
    if s in ("停", "关", "0"):
        return 0.0
    n = _to_num(v)
    return 0.0 if math.isnan(n) else n


def _switch_val(row, col_idx) -> float:
    """系统开关列（0/1）：NaN/空 → 0，与前端 toNum(x) || 0 一致。"""
    if col_idx is None or col_idx < 0:
        return 0.0
    v = _to_num(row[col_idx] if col_idx < len(row) else None)
    return 0.0 if math.isnan(v) else v


def parse_coarse_factors(raw: list) -> dict:
    """与前端 parseCoarseFactors 一致：解析表1 多因素（多Sheet合并后传入行数组）。"""
    errors, records = [], []
    if not raw or len(raw) < 3:
        return {"records": records, "errors": errors}

    # 定位主表头行
    h_idx = -1
    for i in range(min(len(raw), 12)):
        if any("原煤灰分" in str(c or "") for c in raw[i] or []):
            h_idx = i
            break
    if h_idx < 0:
        for i in range(min(len(raw), 12)):
            if any("液位" in str(c or "") for c in raw[i] or []):
                h_idx = i
                break
    if h_idx < 0:
        errors.append({"row": 0, "col": 0, "msg": UNRECOGNIZED_HEADER_MSG})
        return {"records": records, "errors": errors}

    h_row = raw[h_idx]
    sub_row = raw[h_idx + 1] if h_idx + 1 < len(raw) else []

    def find_col(kw, not_kw=None):
        for j, c in enumerate(h_row or []):
            cs = str(c or "")
            if kw in cs and not (not_kw and not_kw in cs):
                return j
        return -1

    col = {
        "raw_ash": find_col("原煤灰分"), "coal_amount": find_col("煤量"),
        "ash": find_col("灰分", "原"), "moisture": find_col("水分"),
        "level": find_col("液位"), "time": find_col("时间"), "face": find_col("入洗工作面"),
    }
    if col["ash"] < 0:
        for j, c in enumerate(h_row or []):
            if "315" in str(c or ""):
                col["ash"] = j

    def find_sub(kw):
        for j, c in enumerate(sub_row or []):
            if kw in str(c or ""):
                return j
        return -1

    col["sysA"] = find_sub("A")
    col["sysB"] = find_sub("B")
    col["sys401"] = find_sub("401")
    col["sys402"] = find_sub("402")
    col["des473"] = find_sub("473")
    col["des474"] = find_sub("474")

    date_col = 0
    time_col = col["time"] if col["time"] >= 0 else 2

    def cell(row, c):
        return row[c] if c is not None and c >= 0 and c < len(row) else None

    last_raw_ash = None
    last_ymd = None   # 上一行解析出的 (y, m, d)，供序列消歧与跨年判断
    data_rows = raw[h_idx + 2:]
    for row in data_rows:
        if not row or not any(c not in (None, "") for c in row):
            continue
        dc_raw = cell(row, date_col)
        ymd = resolve_ymd(dc_raw, last_ymd)
        if ymd is not None:
            last_ymd = ymd
        time_cell = cell(row, time_col)
        # 日期解析失败时回退原始文本（不再造出 NaN 时间戳毒行）
        ts = fmt_ts(ymd, time_cell) if ymd else f"{str(dc_raw or '').strip()} {extract_time(time_cell)}"
        ash = _to_num(cell(row, col["ash"]))
        if math.isnan(ash) or ash < 1 or ash > 40:
            continue
        raw_ash = _to_num(cell(row, col["raw_ash"]))
        if math.isnan(raw_ash):
            raw_ash = last_raw_ash
        else:
            last_raw_ash = raw_ash
        coal = _to_num(cell(row, col["coal_amount"]))
        coal_safe = 0.0 if math.isnan(coal) else coal
        level = _to_num(cell(row, col["level"]))
        moist = _to_num(cell(row, col["moisture"]))
        on_sys = []
        for key, code in (("sys401", "401"), ("sys402", "402"), ("sysA", "A"), ("sysB", "B")):
            v = _to_num(cell(row, col[key]))
            if not math.isnan(v) and v:
                on_sys.append(code)
        face_raw = cell(row, col["face"]) if col["face"] >= 0 else None
        face = str("" if face_raw is None else face_raw).replace("&#10;", "\n").split("\n")[0].strip()
        rec = {
            "timestamp": ts,
            "system": "合并",
            "ash_content": round(ash, 4),
            "coal_amount": 0.0 if math.isnan(coal) else coal,
            "level": None if math.isnan(level) else level,
            "raw_ash": None if raw_ash is None or math.isnan(raw_ash) else raw_ash,
            "moisture": None if math.isnan(moist) else moist,
            "sysA": _switch_val(row, col["sysA"]),
            "sysB": _switch_val(row, col["sysB"]),
            "sys401": _switch_val(row, col["sys401"]),
            "sys402": _switch_val(row, col["sys402"]),
            "desliming473": _parse_des(cell(row, col["des473"])) if col["des473"] >= 0 else 0.0,
            "desliming474": _parse_des(cell(row, col["des474"])) if col["des474"] >= 0 else 0.0,
            "is_stoppage": 1 if coal_safe <= 10 else 0,
            "mining_face": face,
        }
        records.append(rec)

    return {"records": records, "errors": errors}
