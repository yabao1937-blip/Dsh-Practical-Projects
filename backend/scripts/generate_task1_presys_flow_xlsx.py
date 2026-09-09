# -*- coding: utf-8 -*-
"""生成 Excel 文档：四系统合并前-各系统运算流程"""
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

OUT = r"D:\dense-medium-density-control-system\docs\四系统合并前-各系统运算流程.xlsx"

HEAD_FILL = PatternFill("solid", fgColor="2F5597")
HEAD_FONT = Font(name="微软雅黑", size=11, bold=True, color="FFFFFF")
SEC_FILL = PatternFill("solid", fgColor="D6E4F0")
SEC_FONT = Font(name="微软雅黑", size=10, bold=True)
BODY_FONT = Font(name="微软雅黑", size=10)
SHARE_FILL = PatternFill("solid", fgColor="FDE9D9")
SYS_FILL = PatternFill("solid", fgColor="E2EFDA")
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


def add_rows(ws, rows, start, fill_col=None):
    r = start
    for row in rows:
        if isinstance(row, str):
            ws.cell(row=r, column=1, value=row)
            for c in range(1, ws.max_column + 1):
                cc = ws.cell(row=r, column=c)
                cc.fill = SEC_FILL
                cc.font = SEC_FONT
                cc.border = BORDER
            ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=ws.max_column)
        else:
            for c, v in enumerate(row, 1):
                cc = ws.cell(row=r, column=c, value=v)
                cc.font = BODY_FONT
                cc.alignment = WRAP if c != 1 else CENTER
                cc.border = BORDER
                if fill_col and isinstance(v, str) and v:
                    if v == "共享":
                        cc.fill = SHARE_FILL
                    elif v == "分系统":
                        cc.fill = SYS_FILL
        r += 1
    return r


wb = Workbook()

# ================= Sheet1 运算流程（步骤级，标注共享/分系统） =================
ws = wb.active
ws.title = "运算流程"
ws.append(["步骤", "环节", "计算内容", "共享 or 分系统", "说明"])
rows = [
    "一、数据进入（全部带系统标签）",
    [1, "表1 导入", "粗精煤泥影响因素 → store.coarseCoal；每条记录 system=开启系统组合(如'401+402')，并带 sysA/sysB/sys401/sys402 四开关", "分系统",
     "系统信息只做标记，不拆分存储"],
    [2, "表2 导入", "浮精灰分/煤量/压滤机 → store.floatCoal，按系统列记 system", "分系统", "同左"],
    [3, "表3 导入", "灰分、密度 → store.calcLogs(calc_type=ash_density)，带 system(A/B)、灰分、密度", "分系统", "趋势图与 getAshByTime 用到"],
    [4, "手工补录", "灰分仪/密度计/皮带秤/精磁尾/浮精 → 对应数组，system=表单所选系统", "分系统", "四类表单均有系统下拉"],
    "二、卡片运算（关键：四卡共用一套结果）",
    [5, "粗精煤泥灰分", "取全局最新一条粗精煤泥记录做多因素前馈预测（10特征，含四开关），失败则用实测灰分", "共享",
     "不分系统，取最新一条"],
    [6, "浮精灰分/煤量", "取全局最新一条浮精记录", "共享", "不分系统"],
    [7, "重介参数", "重介精煤灰分=8.50%、重介精煤量=250 t/h 固定值", "共享", "写死，不参与系统区分"],
    [8, "实际灰分", "calcTotalAsh = (8.5×250 + 浮精灰分×浮精量 + 粗精灰分×粗精量) / (250+浮精量+粗精量)", "共享",
     "四个卡片显示同一个值"],
    [9, "目标灰分", "默认 8.50%，各卡可用箭头 ±0.1 手动微调", "共享+可调", "_adjustedAsh['{sys}_target']"],
    [10, "偏差", "实际灰分 − 目标灰分（各卡显示同一偏差，可微调）", "共享+可调", "偏差>0 偏高，<0 偏低"],
    [11, "建议密度", "写死的常量：401=1.450、402=1.455、A=1.440、B=1.460", "分系统(写死)",
     "不是计算出来的，是各系统固定标称值"],
    [12, "置信度角标", "写死：401=高、402=高、A=中、B=低", "分系统(写死)", "静态标定"],
    [13, "方向指示", "|偏差|≤0.05 保持；偏差>0 建议上调密度；偏差<0 建议下调密度", "共享", "同一判定结果用于四卡"],
    "三、趋势图与告警",
    [14, "灰分趋势图", "calcLogs 灰分按 system 拆 4 条线（401/402/A/B 各自灰分曲线）+ 目标线 8.50", "分系统",
     "唯一真正按系统分开展示的数据"],
    [15, "偏差告警", "用同一套 actual 算一个 deviation，|deviation|>0.15% 时对 4 个系统各推一条相同内容的告警并高亮 4 卡", "共享计算、分系统发条",
     "4 条告警内容完全一样，只是 system 字段不同"],
    "四、详情弹窗与模型",
    [16, "卡片详情弹窗", "每系统弹窗公式相同（重介8.5×250、最新粗精/浮精、总灰分、偏差），仅推荐密度显示该系统写死值", "共享公式",
     "弹窗内置信度为实时计算（regressionModels R²）"],
    [17, "粗精煤泥预测模型", "predictCoarseAsh 按每条记录特征（含四开关）计算，与记录归属系统无分支", "共享",
     "训练集=全部 coarseCoal 记录，不分系统拆分训练"],
    [18, "浮精页 getAshByTime", "按时间戳+可选 system 从 calcLogs 取最近灰分（浮精页传 system）", "分系统(可选)",
     "system 参数可选，不传则全局"],
]
add_rows(ws, rows, 2, fill_col=4)
style_sheet(ws, [6, 14, 52, 14, 26])
ws.auto_filter.ref = f"A1:E{ws.max_row}"

# ================= Sheet2 四系统差异点 =================
ws2 = wb.create_sheet("四系统差异点")
ws2.append(["维度", "401系统", "402系统", "A系统", "B系统", "说明"])
rows2 = [
    ["皮带归属", "501", "501", "502", "502", "卡片标签"],
    ["建议密度(写死)", "1.450", "1.455", "1.440", "1.460", "不参与实际灰分运算，仅展示"],
    ["置信度(写死)", "高", "高", "中", "低", "静态标定，非实时计算"],
    ["实际灰分", "共享同一值", "共享同一值", "共享同一值", "共享同一值", "全局最新粗精+浮精加权"],
    ["目标灰分", "8.50(可微调)", "8.50(可微调)", "8.50(可微调)", "8.50(可微调)", "各卡独立微调值"],
    ["偏差", "共享同一值", "共享同一值", "共享同一值", "共享同一值", "实际−目标"],
    ["趋势曲线", "calcLogs中401记录", "calcLogs中402记录", "calcLogs中A记录", "calcLogs中B记录", "按记录system拆分"],
    ["告警", "各发1条(内容相同)", "各发1条(内容相同)", "各发1条(内容相同)", "各发1条(内容相同)", "超±0.15%阈值时"],
    ["模型预测", "无差异", "无差异", "无差异", "无差异", "按记录10特征(含四开关)预测"],
]
for r in rows2:
    ws2.append(r)
style_sheet(ws2, [14, 18, 18, 18, 18, 30])
for row in ws2.iter_rows(min_row=2):
    for cell in row:
        cell.font = BODY_FONT
        cell.border = BORDER
        cell.alignment = WRAP if cell.column != 1 else CENTER
ws2.auto_filter.ref = f"A1:F{ws2.max_row}"

# ================= Sheet3 关键结论 =================
ws3 = wb.create_sheet("关键结论")
ws3.append(["序号", "结论", "详细说明"])
rows3 = [
    [1, "合并前“四套系统”并没有四套独立运算",
     "实际灰分、偏差、告警阈值判定、方向建议全部是一套共享计算：取全局最新粗精煤泥（多因素前馈预测）+ 全局最新浮精 + 固定重介参数(8.5%×250t/h) 做 calcTotalAsh 加权，四张卡片显示同一个结果。"],
    [2, "系统间真正的差异只有四处",
     "①建议密度为写死常量(1.450/1.455/1.440/1.460)；②置信度为写死标签(高/高/中/低)；③趋势图按 system 拆 4 条灰分曲线；④超阈值告警逐系统各发一条(内容相同)。"],
    [3, "建议密度在合并前就不参与运算",
     "卡片上的密度是标称值，从未进入灰分计算；合并后才改为 getLatestDensity 取最新灰分密度日志的密度值。"],
    [4, "预测模型始终按记录特征计算",
     "predictCoarseAsh 用每条记录的 10 特征（含四系统开关）计算，与记录归属哪个系统无分支；训练集是全部记录，不分系统拆分。"],
    [5, "合并的影响因此很小",
     "任务一合并只把共享结果改成单卡展示；算法（特征/模型/公式）不变；失去的是趋势图分系统曲线、分系统告警条数、四个写死密度值——单系统差异本就不在卡片运算里。"],
]
add_rows(ws3, rows3, 2)
style_sheet(ws3, [6, 34, 90])
ws3.auto_filter.ref = f"A1:C{ws3.max_row}"

wb.save(OUT)
print("[gen]", OUT)
