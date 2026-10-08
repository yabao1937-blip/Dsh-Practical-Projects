"""Single column contract used by API, page and workbook export."""
COLUMNS = [
    ("raw_ash", "原煤灰分", "%"),
    ("coal_amount", "入洗原煤量", "t/h"),
    ("systems", "系统运行状态", ""),
    ("desliming", "脱粉系统", ""),
    ("level", "精磁尾液位", "%"),
    ("coarse_amount", "粗精煤泥量", "t/h"),
    ("raw_slime", "原煤煤泥含量", "%"),
    ("coarse_ash", "粗精煤泥灰分", "%"),
    ("heavy_ash", "重介精煤灰分", "%"),
    ("heavy_amount", "重介精煤量", "t/h"),
    ("float_ash", "浮精灰分", "%"),
    ("float_amount", "浮精量", "t/h"),
    ("float_press", "浮精压滤机运行", ""),
    ("reagent", "药剂量", ""),
    ("density", "当前分选密度", "g/cm³"),
    ("total_ash", "总灰分", "%"),
    ("suggested_density", "建议密度", "g/cm³"),
]


def cell(value=None, source="", observed_at=None, status="missing", reason="", **metadata):
    return dict(value=value, source=source, observed_at=observed_at,
                status=status, reason=reason, **metadata)
