# -*- coding: utf-8 -*-
"""生成 Excel 文档：反推重介精煤灰分-如何指导密度选择"""
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

OUT = r"D:\dense-medium-density-control-system\docs\反推重介灰分-如何指导密度选择.xlsx"

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

# ================= Sheet1 原理关系 =================
ws = wb.active
ws.title = "原理关系"
ws.append(["序号", "环节", "说明"])
rows = [
    [1, "重介分选的基本关系",
     "悬浮液密度 ρ 决定分选密度：ρ↑ → 更多中间密度物进入精煤 → 精煤灰分↑、产率↑。即 重介精煤灰分 与 悬浮液密度 正相关。"],
    [2, "反推值的含义",
     "把实测总精煤灰分代入反推公式得到的“重介精煤灰分”，代表当前工况下重介系统的实际灰分水平（隐含值），而非设定值。"],
    [3, "指导密度的桥梁",
     "反推重介灰分(A_act) → 与目标重介灰分(A_tgt)比差 → 通过“灰分↔密度”经验关系换算成密度调整量(Δρ) → 给出建议密度。灰分-密度关系可由表3历史数据（灰分 vs 密度，124组）直接拟合。"],
    [4, "目标重介灰分的算法",
     "把目标总精煤灰分(8.50%)代入反推公式：A_tgt = (目标总灰分×总煤量 − 浮精灰分×浮精量 − 粗精煤泥灰分×粗精煤泥量) ÷ 重介精煤量。"],
    [5, "两种指导方式",
     "①增量式：ρ_new = ρ_cur + K×(A_tgt − A_act)；②反查式：用灰分-密度拟合曲线由 A_tgt 直接反查 ρ_tgt。二者都需要历史“灰分-密度”数据支撑。"],
]
for r in rows:
    ws.append(r)
style_sheet(ws, [6, 20, 96])
for row in ws.iter_rows(min_row=2):
    for cell in row:
        cell.font = BODY_FONT
        cell.border = BORDER
        cell.alignment = WRAP if cell.column != 1 else CENTER

# ================= Sheet2 闭环流程 =================
ws2 = wb.create_sheet("闭环流程")
ws2.append(["步骤", "动作", "输入", "输出", "备注"])
rows2 = [
    [1, "取总精煤灰分实测值", "化验值 或 501/502皮带灰分仪加权", "总精煤灰分 A_total", "任务三待确认来源"],
    [2, "反推当前重介灰分", "反推公式", "A_act（当前实际重介精煤灰分）", "重介精煤量>0 才计算"],
    [3, "算目标重介灰分", "目标总灰分(8.50)代入同一公式", "A_tgt", "三量取当前值"],
    [4, "灰分偏差", "ΔA = A_act − A_tgt", "±ΔA%", "正=偏高，负=偏低"],
    [5, "死区判断", "|ΔA| ≤ 死区(≈±0.05%)？", "保持密度", "避免频繁调整"],
    [6, "灰分→密度换算", "历史拟合：Δρ = f(ΔA) 或 Δρ = K×ΔA", "Δρ 调整量", "方向：灰分偏高→降密度（常规理论）"],
    [7, "限幅与限位", "单次调整 ≤ ±0.005~0.01 g/cm³；总范围 1.35~1.55", "Δρ_clamped", "防止过调"],
    [8, "给出建议密度", "ρ_new = ρ_cur + Δρ_clamped", "建议密度", "显示在卡片并说明幅度"],
    [9, "闭环等待", "执行调整→等新灰分数据→回到步骤1", "—", "时滞内不再重复调整"],
]
for r in rows2:
    ws2.append(r)
style_sheet(ws2, [6, 26, 34, 26, 30])
for row in ws2.iter_rows(min_row=2):
    for cell in row:
        cell.font = BODY_FONT
        cell.border = BORDER
        cell.alignment = WRAP if cell.column != 1 else CENTER

# ================= Sheet3 两种算法对比 =================
ws3 = wb.create_sheet("两种算法对比")
ws3.append(["维度", "增量式（偏差调节）", "反查式（目标反查）"])
rows3 = [
    ["公式", "ρ_new = ρ_cur + K×(A_tgt − A_act)", "ρ_tgt = f⁻¹(A_tgt)，灰分-密度曲线反查"],
    ["数据需求", "K（灰分-密度斜率，历史回归）", "完整灰分-密度拟合曲线（线性/二次）"],
    ["优点", "结构简单、鲁棒，小偏差下稳", "直接给目标值，物理意义直观"],
    ["缺点", "K 固定不适应用工况漂移；大偏差时可能不够", "依赖曲线拟合质量；需外推保护"],
    ["适用", "数据少、关系近似线性的初期", "数据积累后（表3已有124组可用）"],
    ["建议", "先用增量式上线，K 用表3回归初值；数据多了切反查式", "作为二期升级"],
]
for r in rows3:
    ws3.append(r)
style_sheet(ws3, [14, 40, 40])
for row in ws3.iter_rows(min_row=2):
    for cell in row:
        cell.font = BODY_FONT
        cell.border = BORDER
        cell.alignment = WRAP if cell.column != 1 else CENTER

# ================= Sheet4 工程注意事项 =================
ws4 = wb.create_sheet("工程注意事项")
ws4.append(["序号", "事项", "说明"])
rows4 = [
    [1, "方向约定（已修正）",
     "原代码写的是“灰分偏高→建议上调”（与理论相反）。已按重介理论修正：灰分偏高(A_act>A_tgt) → 建议下调密度；灰分偏低 → 建议上调密度。卡片方向指示与详情弹窗两处已改（overview.js v14），后续任务三的调整量计算沿用此方向。"],
    [2, "时滞与调节周期",
     "密度调整→产品变化→皮带灰分仪/化验反馈存在时滞；建议 15~30 分钟一个调节周期，周期内不重复调整。"],
    [3, "死区与限幅",
     "设死区（±0.05% 灰分对应不调）与单次限幅（±0.005~0.01 g/cm³），避免频繁来回调。"],
    [4, "灰分-密度关系要现场回归",
     "理论曲线与现场有偏差（旋流器浓缩效应、介质性质、煤质变化），K 或曲线必须用表3历史数据回归并定期更新。"],
    [5, "反推值的可信度",
     "反推精度取决于总精煤灰分来源准确性（化验滞后但准、皮带灰分仪及时但有偏差）与三量的准确性；建议化验值与仪表值相互校验。"],
    [6, "多系统口径",
     "合并系统下密度只有一个建议值；若 A/B 皮带（501/502）分选密度本就不同，建议按皮带分别建模后再给出各自建议（表3系统列已保留）。"],
    [7, "与现有卡片的关系",
     "任务三完成后，“建议密度”将从“最新实测值+方向”升级为“当前密度+调整量=建议值”，并把反推重介灰分一并显示，形成完整闭环。"],
]
for r in rows4:
    ws4.append(r)
style_sheet(ws4, [6, 20, 100])
for row in ws4.iter_rows(min_row=2):
    for cell in row:
        cell.font = BODY_FONT
        cell.border = BORDER
        cell.alignment = WRAP if cell.column != 1 else CENTER
        if cell.column == 2 and cell.value == "方向约定（重要）":
            cell.fill = WARN_FILL
ws4.auto_filter.ref = f"A1:C{ws4.max_row}"

wb.save(OUT)
print("[gen]", OUT)
