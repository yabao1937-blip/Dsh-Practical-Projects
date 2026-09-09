# -*- coding: utf-8 -*-
"""生成 Excel 文档：粗精煤泥趋势图-同日多点说明"""
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

OUT = r"D:\dense-medium-density-control-system\docs\粗精煤泥趋势图-同日多点说明.xlsx"

HEAD_FILL = PatternFill("solid", fgColor="2F5597")
HEAD_FONT = Font(name="微软雅黑", size=11, bold=True, color="FFFFFF")
BODY_FONT = Font(name="微软雅黑", size=10)
THIN = Side(style="thin", color="B0B0B0")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
WRAP = Alignment(wrap_text=True, vertical="top")
CENTER = Alignment(horizontal="center", vertical="top", wrap_text=True)

wb = Workbook()

ws = wb.active
ws.title = "原因说明"
ws.append(["序号", "问题", "结论与说明"])
rows = [
    [1, "为什么同一日出现多个实测灰分值",
     "因为数据源表1《粗精煤泥灰分影响因素6.16-7.14》一天内有多个采样时间点：113 条记录分布在 25 天，每天 2~8 条（多数 4~5 条），每个采样时间点化验一次 315 灰分，各点灰分不同（如 6月16日 09:31=13.86%、09:36=14.80%、09:43=14.27%、10:39=15.37%、10:42=11.70%）。趋势图按“每条记录一个点、横轴按真实时间排列”，所以同一天会出现多个点。"],
    [2, "是否数据重复/错误",
     "不是。同一时间戳的重复记录已被去重（uniqueByTime 只保留一条）；同日不同时间点的不同值，是正常的多点采样数据。"],
    [3, "图表实现口径",
     "coarse.js updateCharts：数据按时间排序+同时间戳去重后，逐条记录画点，X 轴按真实时间间隔分布，标签为 MM-DD HH:MM；没有按天聚合，所以同一天多个点全部显示。"],
    [4, "如希望一天一个点",
     "可选改进：按天聚合（日均值/中位数/最大最小值带）显示。当前逐点显示保留全部采样细节，更贴近原始化验记录；如需日级视图可另加聚合开关。"],
]
for r in rows:
    ws.append(r)
for i, w in enumerate([6, 30, 110], 1):
    ws.column_dimensions[get_column_letter(i)].width = w
for cell in ws[1]:
    cell.fill = HEAD_FILL
    cell.font = HEAD_FONT
    cell.alignment = CENTER
    cell.border = BORDER
for row in ws.iter_rows(min_row=2):
    for cell in row:
        cell.font = BODY_FONT
        cell.border = BORDER
        cell.alignment = WRAP if cell.column != 1 else CENTER
ws.freeze_panes = "A2"

ws2 = wb.create_sheet("每天记录数明细")
ws2.append(["日期", "记录数", "当日采样时间"])
days = {
    "2026-06-16": "5 条 09:31/09:36/09:43/10:39/10:42",
    "2026-06-17": "5 条 08:18/09:37/09:45/09:53/10:38",
    "2026-06-19": "5 条 08:23/09:35/09:41/09:47/10:42",
    "2026-06-22": "5 条 08:26/09:06/09:12/10:36/10:41",
    "2026-06-23": "5 条 08:19/09:44/09:51/09:59/10:42",
    "2026-06-24": "5 条 08:27/09:27/09:34/09:40/10:44",
    "2026-06-25": "5 条 08:25/09:35/09:44/09:54/10:42",
    "2026-06-26": "5 条 08:23:30/08:28:30/09:28:30/09:33:30/10:42:30",
    "2026-06-27": "5 条 08:28/09:28/09:34/09:39/10:38",
    "2026-06-28": "5 条 08:18/09:38/09:45/09:51/10:38",
    "2026-06-29": "5 条 08:16/09:56/10:02/10:07/10:40",
    "2026-06-30": "4 条 08:25/10:43/21:37/00:06",
    "2026-07-01": "8 条 08:13/10:35/21:49/00:07/08:12/10:41…",
    "2026-07-04": "4 条 08:23/10:39/22:35/00:09",
    "2026-07-05": "4 条 08:21/10:36/21:42/00:06",
    "2026-07-06": "4 条 08:26/10:39/21:33/00:03",
    "2026-07-07": "4 条 08:25/10:40/23:27/01:27",
    "2026-07-08": "4 条 08:11/10:43/22:45/00:20",
    "2026-07-09": "4 条 11:33/13:23/21:58/00:14",
    "2026-07-11": "4 条 08:20/11:22/22:26/00:36",
    "2026-07-12": "4 条 08:18/10:41/22:55/00:52",
    "2026-07-13": "4 条 08:23/10:38/22:40/00:28",
    "2026-07-14": "2 条 07:43/10:04",
    "2026-07-20": "4 条 08:18/10:41/21:39/00:10",
    "2026-07-30": "4 条 08:21/10:41/21:25/00:09",
}
for d, t in days.items():
    ws2.append([d, t.split(" ")[0], t.split(" ", 1)[1]])
for i, w in enumerate([14, 10, 60], 1):
    ws2.column_dimensions[get_column_letter(i)].width = w
for cell in ws2[1]:
    cell.fill = HEAD_FILL
    cell.font = HEAD_FONT
    cell.alignment = CENTER
    cell.border = BORDER
for row in ws2.iter_rows(min_row=2):
    for cell in row:
        cell.font = BODY_FONT
        cell.border = BORDER
        cell.alignment = CENTER if cell.column != 3 else WRAP
ws2.freeze_panes = "A2"

wb.save(OUT)
print("[gen]", OUT)
