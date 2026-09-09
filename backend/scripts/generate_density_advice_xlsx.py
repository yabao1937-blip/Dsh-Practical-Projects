# -*- coding: utf-8 -*-
"""生成 Excel 文档：建议密度-生成逻辑说明"""
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

OUT = r"D:\dense-medium-density-control-system\docs\建议密度-生成逻辑说明.xlsx"

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

# ================= Sheet1 当前生成逻辑（逐步） =================
ws = wb.active
ws.title = "当前生成逻辑"
ws.append(["步骤", "环节", "代码位置", "具体做法", "当前实际值"])
rows = [
    [1, "取最新密度值", "app.js getLatestDensity()",
     "从 store.calcLogs 中筛选 calc_type='ash_density' 的记录（表3 灰分、密度导入的数据），从数组末尾向前找第一条 input_json.density 为数字的记录并返回；找不到则回退默认 1.450",
     "1.490 g/cm³（表3 最后一条记录的密度值）"],
    [2, "卡片显示", "overview.js updateCards → #density-total",
     "把上一步得到的密度直接显示为“建议密度”（toFixed(3)），数值本身不做任何调整",
     "显示 1.490 g/cm³"],
    [3, "算灰分偏差", "overview.js updateCards",
     "偏差 dev = 实际总灰分 − 目标灰分（实际总灰分=三产品加权公式，目标默认 8.50% 可箭头微调）",
     "+0.82%（9.32% − 8.50%）"],
    [4, "给方向建议", "overview.js updateCards 方向指示",
     "|dev|≤0.05 → 保持稳定（密度合适）；dev>0 → 建议上调密度；dev<0 → 建议下调密度",
     "建议上调 ▲"],
    [5, "给效果说明", "overview.js updateCards effect",
     "dev>0：微调密度可降低灰分偏差至目标范围内；dev<0：适度降密可提升回收率，灰分回归目标值；无数据：暂无数据",
     "微调密度可降低灰分偏差至目标范围内"],
    [6, "详情弹窗", "overview.js showCardDetail 四、推荐密度",
     "推荐密度 = 同一最新密度值；依据文字按偏差方向给出（偏差≤0.05 当前密度合适 / 灰分偏高建议上调 / 灰分偏低建议下调）",
     "推荐密度 1.490，依据：灰分偏高，建议上调密度以降低灰分"],
]
for r in rows:
    ws.append(r)
style_sheet(ws, [6, 16, 26, 56, 26])
for row in ws.iter_rows(min_row=2):
    for cell in row:
        cell.font = BODY_FONT
        cell.border = BORDER
        cell.alignment = WRAP if cell.column != 1 else CENTER
ws.auto_filter.ref = f"A1:E{ws.max_row}"

# ================= Sheet2 方向判定表 =================
ws2 = wb.create_sheet("方向判定表")
ws2.append(["偏差情况", "方向指示", "效果说明", "判定条件"])
dir_rows = [
    ["实际灰分 ≈ 目标（|dev|≤0.05%）", "保持稳定（—）", "当前密度合适，无需调整", "|dev|<=0.05"],
    ["实际灰分偏高（dev>0）", "建议下调（▼）", "适度降密可降低灰分，使偏差回归目标范围", "dev>0.05"],
    ["实际灰分偏低（dev<0）", "建议上调（▲）", "适度提密可提升回收率，灰分回归目标值", "dev<-0.05"],
]
for r in dir_rows:
    ws2.append(r)
style_sheet(ws2, [30, 16, 44, 14])
for row in ws2.iter_rows(min_row=2):
    for cell in row:
        cell.font = BODY_FONT
        cell.border = BORDER
        cell.alignment = WRAP if cell.column != 1 else CENTER

# ================= Sheet3 特征与待确认问题 =================
ws3 = wb.create_sheet("特征与待确认问题")
ws3.append(["序号", "类别", "内容"])
notes = [
    [1, "特征1：数值不做调整",
     "建议密度 = 表3 最新一条记录里的密度值原样显示（1.490），没有任何模型或公式去“算”这个数；真正的“建议”只有方向（上调/下调/保持）三个字。"],
    [2, "特征2：不分系统、取最新一条",
     "合并后 getLatestDensity 不区分系统(A/B)、不区分皮带(501/502)，只从数组末尾往前找最新有效值；A/B 交替采样时显示值会在两条皮带间跳动。"],
    [3, "历史对比",
     "合并前：四张卡片密度是写死的常量（401=1.450/402=1.455/A=1.440/B=1.460），同样不参与计算；合并后改为取表3最新记录，反而更贴近真实数据。"],
    [4, "待确认1：调整幅度缺失",
     "当前只有方向没有幅度（比如“上调多少 g/cm³”没有给出）。若要给出具体建议值，需要 灰分偏差→密度调整量 的经验公式或模型（密度模型目前是预留状态，任务三可定）。"],
    [5, "已修正：方向约定按理论调整",
     "原代码约定：灰分偏高→上调密度、灰分偏低→下调密度（与重介理论相反）。已改为：灰分偏高→建议下调密度（分选密度↓→精煤灰分↓）；灰分偏低→建议上调密度（分选密度↑→精煤灰分↑、产率↑）。卡片方向指示与详情弹窗依据两处均已改（overview.js v14）。"],
    [6, "待确认3：建议密度应与“实际密度”区分",
     "卡片显示的是最新实测密度（当作建议值展示）。真正的“建议密度”应为：当前密度 + 方向×幅度。建议任务三给出明确口径后再改显示文案，避免“实测值”被当作“推荐值”执行。"],
]
for r in notes:
    ws3.append(r)
style_sheet(ws3, [6, 24, 100])
for row in ws3.iter_rows(min_row=2):
    for cell in row:
        cell.font = BODY_FONT
        cell.border = BORDER
        cell.alignment = WRAP if cell.column != 1 else CENTER
        if cell.column == 2 and "待确认" in str(cell.value or ""):
            cell.fill = WARN_FILL
ws3.auto_filter.ref = f"A1:C{ws3.max_row}"

wb.save(OUT)
print("[gen]", OUT)
