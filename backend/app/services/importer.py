"""三表 Excel 导入解析（逐值对齐前端 import.js 的 normalizeTs / parseCoarseFactors）。

关键：M.DD 手写日期语义补偿（个位天数省略尾零：6.3=6月30日、7.4=7月4日、6.1=6月1日）。
"""
import math
import re
from datetime import datetime, timedelta

# 手写表无年份，统一按 2026（与前端 import.js 一致；接入跨年数据时需调整）
DEFAULT_YEAR = 2026


def _pad(n) -> str:
    return str(n).rjust(2, "0")


def fix_day(m: int, dd100: int) -> dict:
    """与前端 fixDay 一致。"""
    if dd100 == 10:
        return {"m": m, "d": 1}
    if 1 <= dd100 <= 31:
        return {"m": m, "d": dd100}
    return {"m": m, "d": math.floor(dd100 / 10)}


def _excel_serial_to_date(serial: float) -> dict | None:
    # JS: new Date((serial - 25569) * 86400 * 1000)，取本地年月日（+08:00）
    dt = datetime(1970, 1, 1) + timedelta(days=serial - 25569) + timedelta(hours=8)
    return {"m": dt.month, "d": dt.day}


def extract_date(dc) -> dict | None:
    """与前端 parseCoarseFactors 的 extractDate 一致。"""
    if dc is None:
        return None
    if isinstance(dc, bool):
        return None
    if isinstance(dc, (int, float)):
        if dc >= 1000:  # Excel 日期序列
            return _excel_serial_to_date(float(dc))
        m = math.floor(dc)
        dd100 = round((dc - m) * 100)
        if dd100 <= 0:
            return None
        return fix_day(m, dd100)
    s = str(dc).strip()
    iso = re.match(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", s)
    if iso:
        return {"m": int(iso.group(2)), "d": int(iso.group(3))}
    dot = re.match(r"^(\d{1,2})\.(\d{1,2})$", s)
    if dot:
        m = int(dot.group(1))
        frac = int(dot.group(2))
        return fix_day(m, frac * 10 if frac < 10 else frac)
    parts = re.split(r"[/-]", s)
    if len(parts) >= 2 and parts[0].isdigit() and 1 <= int(parts[0]) <= 12:
        return {"m": int(parts[0]), "d": int(parts[1])}
    return None


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
        tot = round(frac * 86400)
        return f"{_pad(tot // 3600)}:{_pad((tot % 3600) // 60)}:{_pad(tot % 60)}"
    s = str(tc)
    mt = re.search(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", s)
    if mt:
        return f"{_pad(int(mt.group(1)))}:{_pad(int(mt.group(2)))}:{_pad(int(mt.group(3) or 0))}"
    return "00:00:00"


def parse_ts(dc, tc) -> str:
    md = extract_date(dc)
    t = extract_time(tc)
    return f"{DEFAULT_YEAR}-{_pad(md['m'])}-{_pad(md['d'])} {t}" if md else f"{str(dc or '').strip()} {t}"


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
        errors.append("未识别到多因素表头")
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
    data_rows = raw[h_idx + 2:]
    for row in data_rows:
        if not row or not any(c not in (None, "") for c in row):
            continue
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
        face = str("" if face_raw is None else face_raw).split("\n")[0].strip()
        rec = {
            "timestamp": parse_ts(cell(row, date_col), cell(row, time_col)),
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
