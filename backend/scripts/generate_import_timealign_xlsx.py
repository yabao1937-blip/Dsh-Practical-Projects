# -*- coding: utf-8 -*-
"""生成 Excel 文档：导入功能-时间对齐现状（任务四输入）"""
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

OUT = r"D:\dense-medium-density-control-system\docs\导入功能-时间对齐现状.xlsx"

HEAD_FILL = PatternFill("solid", fgColor="2F5597")
HEAD_FONT = Font(name="微软雅黑", size=11, bold=True, color="FFFFFF")
SEC_FILL = PatternFill("solid", fgColor="D6E4F0")
SEC_FONT = Font(name="微软雅黑", size=10, bold=True)
WARN_FILL = PatternFill("solid", fgColor="FDE9D9")
BODY_FONT = Font(name="微软雅黑", size=10)
THIN = Side(style="thin", color="B0B0B0")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
WRAP = Alignment(wrap_text=True, vertical="top")
CENTER = Alignment(horizontal="center", vertical="top", wrap_text=True)


def style_sheet(ws, widths):
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    for cell in ws[1]:
        cell.fill = HEAD_FILL
        cell.font = HEAD_FONT
        cell.alignment = CENTER
        cell.border = BORDER
    ws.freeze_panes = "A2"


wb = Workbook()

ws = wb.active
ws.title = "时间处理现状"
ws.append(["层面", "环节", "现状", "结论"])
rows = [
    ["导入时", "表1 时间解析（parseTs）",
     "日期列（6.16 这类 M.DD）+ 时间列（Excel时间序列或 HH:MM:SS）合成 '2026-MM-DD HH:MM:SS'；注意：浮点日期无补偿规则，'6.3' 会被读成 6月3日（原表语义可能是 6月30日）",
     "各表按自己时间戳入库"],
    ["导入时", "表2/表3 模板导入",
     "采样时间列原样取字符串（String(row[0])），不做解析、不做规范化；缺时间用导入当天日期兜底",
     "同上"],
    ["导入时", "表1 原煤灰分前向填充",
     "同一表内：原煤灰分为空时沿用上一行非空值（lastRawAsh 前向填充）",
     "仅表内填充，非跨表对齐"],
    ["导入时", "去重",
     "按 '时间戳|系统' 或整行内容判重，重复行跳过；只去完全相同的行",
     "不做时间合并"],
    ["使用时", "浮精页 getAshByTime",
     "从灰分密度日志里找时间最接近的灰分值（就近匹配），找不到回退 8.50",
     "软对齐：用最近时间替代"],
    ["使用时", "粗精页 _getLevel",
     "粗精煤泥记录液位：优先自身 level；否则在精磁尾里找同系统同时间戳，再找时间最近且不晚于该记录的记录",
     "软对齐：就近+不晚于"],
    ["使用时", "读取排序/取最新",
     "数据不保证按时间序存储：_sortByTime 排序、_latestByTime 取时间最新、uniqueByTime 同一时间戳去重展示",
     "展示层容错"],
    ["结论", "跨表时间对齐",
     "导入时不生成统一时间轴，也没有插值/重采样/对齐合并；跨表对齐只在使用时按“就近匹配”临时完成",
     "不会自动对齐"],
]
for r in rows:
    ws.append(r)
style_sheet(ws, [10, 22, 70, 20])
for row in ws.iter_rows(min_row=2):
    for cell in row:
        cell.font = BODY_FONT
        cell.border = BORDER
        cell.alignment = WRAP if cell.column != 1 else CENTER
ws.auto_filter.ref = f"A1:D{ws.max_row}"

ws2 = wb.create_sheet("时间相关隐患")
ws2.append(["序号", "隐患", "说明", "建议（任务四处理）", "状态"])
rows2 = [
    [1, "表1 日期浮点语义",
     "手写表里 6.3 可能表示 6月30日（个位天数省尾零），import.js 当前会读成 6月3日；种子生成脚本 build_web2_seed.py 里已实现了补偿规则（dd100==10→1；1~31→dd；否则 dd//10），在线导入没有该规则",
     "把补偿规则移植到 import.js 的 extractDate", "已处理"],
    [2, "三表时间粒度不一致",
     "表1 原煤灰分为班均值（班级），315灰分为 5 分钟瞬时点样，两者时间粒度不匹配，是模型 R²/合格率偏低（噪声地板）的原因之一",
     "任务四明确粒度口径：班均值按班次对齐到点样时间，或只用同时刻数据", "待处理"],
    [3, "表2/表3 时间未解析",
     "采样时间列原样存字符串，无法与表1 时间做比较/排序（'2026-06-16 08:00' 与 '6.16 08:00:00' 不一致）；缺时间还会用导入当天日期兜底造成错误时间",
     "统一时间解析器 + 非法时间报错而不是兜底", "已处理"],
    [4, "跨表无统一时间轴",
     "反推/影响值计算依赖三表数据在同一点上可比，当前全靠运行时“就近匹配”，匹配距离没有上限（可能跨天匹配）",
     "任务四可加：就近匹配时间窗（±5分钟）+ 超出窗口记缺失", "已处理"],
    [5, "演示数据时间错位",
     "表3（5月）与表1/表2（6-7月）不重叠，反推重介灰分演示时按最新值计算，属演示数据问题",
     "真实数据接入后自然解决；演示种子可考虑时间对齐", "待处理"],
]
for r in rows2:
    ws2.append(r)
style_sheet(ws2, [6, 22, 70, 34, 10])
for row in ws2.iter_rows(min_row=2):
    for cell in row:
        cell.font = BODY_FONT
        cell.border = BORDER
        cell.alignment = WRAP if cell.column not in (1, 5) else CENTER
        if cell.column == 2:
            cell.fill = WARN_FILL
ws2.auto_filter.ref = f"A1:E{ws2.max_row}"

wb.save(OUT)
print("[gen]", OUT)
