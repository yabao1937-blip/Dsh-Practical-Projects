# -*- coding: utf-8 -*-
"""生成 Excel 文档：密度阶跃实验-实施方案与记录表(辨识 K=Δρ/ΔA 的开环增益)。

产出 docs/密度阶跃实验-实施方案与记录表.xlsx,六个 Sheet:
概述 / 前置条件与安全 / 实验方案 / 采样记录表(可填写) / K计算表(带公式) / 系统录入指引
"""
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

OUT = r"D:\dense-medium-density-control-system\docs\密度阶跃实验-实施方案与记录表.xlsx"

HEAD_FILL = PatternFill("solid", fgColor="2F5597")
HEAD_FONT = Font(name="微软雅黑", size=11, bold=True, color="FFFFFF")
SEC_FILL = PatternFill("solid", fgColor="D6E4F0")
SEC_FONT = Font(name="微软雅黑", size=10, bold=True)
WARN_FILL = PatternFill("solid", fgColor="FDE9D9")
INPUT_FILL = PatternFill("solid", fgColor="FFF2CC")   # 待填写单元格
CALC_FILL = PatternFill("solid", fgColor="E2EFDA")    # 公式单元格
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


def style_body(ws, center_cols=(1,)):
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.font = BODY_FONT
            cell.border = BORDER
            cell.alignment = CENTER if cell.column in center_cols else WRAP


wb = Workbook()

# ================= Sheet1 实验概述 =================
ws = wb.active
ws.title = "实验概述"
ws.append(["序号", "项目", "内容"])
rows = [
    [1, "实验目的",
     "辨识「悬浮液密度 → 重介精煤灰分」的开环增益 K = Δρ/ΔA(密度每变化 0.01 g/cm³,重介精煤灰分变化多少)。"
     "K 是密度指导的核心物理参数:专家表的换算(0.15%→调0.01、0.25%→调0.02,反推 K≈0.067~0.08)与"
     "调密后灰分预测(当前用常数 0.075)都依赖它。"],
    [2, "为什么必须做实验",
     "常规操作数据是闭环的:操作工看到灰分偏高→下调密度,数据里高灰分与低密度总是同时出现,"
     "回归得到的是控制器的反方向而非过程增益。系统对现有 283 对(502灰分,密度)常规数据做过"
     "分系统辨识:斜率≈0或负(R²<0.07),物理约束正确拒绝——结论:常规数据无法辨识 K,必须主动做"
     "密度阶跃,让密度独立于灰分变动,打破闭环。"],
    [3, "实验原理",
     "在稳定工况下,将密度设定值主动阶跃 ±0.01 g/cm³ 并保持足够时间,记录新稳态下的 502 皮带灰分"
     "(502 只承载重介精煤,即重介灰分的在线测量)变化量 ΔA。每一步得到一对 (Δρ, ΔA),"
     "K_i = Δρ_i / ΔA_i;多步平均即开环增益。上-下交替的阶跃序列可抵消原煤性质慢漂移的影响。"],
    [4, "预期产出",
     "① 3~6 组 (Δρ, ΔA) 稳态对;② 数据驱动的 K(均值±离散度),与专家经验值 0.067~0.08 对比;"
     "③ 录入系统后:K 估计链路(≥5 对生效,物理约束校验 0.005<K<0.2)自动从「不可辨识」切换为"
     "「数据驱动」,密度指导与调密后灰分预测全部用实测 K。"],
    [5, "量级预估",
     "按专家值 K≈0.075:步长 0.01 → ΔA≈0.13%;步长 0.02 → ΔA≈0.27%。502 灰分仪分辨率若为 0.05%~0.1%,"
     "0.01 步的信号可能接近噪声下限——因此方案以 0.01 为主步长,若 ΔA 信号不足(见判定标准)加做 0.02 步。"],
    [6, "时长与人力",
     "标准方案 6 阶段 × 2h = 12h(一个班次);最少可行方案 4 阶段(见实验方案 Sheet)。"
     "每阶段需要:密度调节 1 次、密度计/502灰分仪每 15min 抄表(各 8 点)、315 灰分化验 1~2 次。"],
]
for r in rows:
    ws.append(r)
style_sheet(ws, [6, 18, 100])
style_body(ws)

# ================= Sheet2 前置条件与安全 =================
ws2 = wb.create_sheet("前置条件与安全")
ws2.append(["类别", "检查项", "标准/说明", "确认(√)"])
rows2 = [
    ["工况稳定", "原煤性质", "同一煤层/同一配煤方案,近 2h 原煤灰分波动 < ±1%(表1 原煤灰分列)", ""],
    ["工况稳定", "带煤量", "稳定在正常范围,近 2h 波动 < ±10%(实验期间不改带煤量)", ""],
    ["工况稳定", "系统组合", "A/B/401/402/473/474 开停组合不变;确需倒系统则本次实验作废重做", ""],
    ["工况稳定", "浮选环节", "加药制度不变(浮精灰分不受本实验影响,用于事后校核)", ""],
    ["仪表可用", "密度计", "读数稳定、已校验;阶跃后实际密度应在 10min 内到位(±0.003)", ""],
    ["仪表可用", "502 皮带灰分仪", "正常运行,记录间隔 ≤15min;这是 ΔA 的主观测点", ""],
    ["仪表可用", "315 灰分化验", "化验资源就位:每阶段稳态末可出 1 次化验(与 502 在线值互相印证)", ""],
    ["安全边界", "密度范围", "全程钳制在 ρ0±0.03 且 [1.44, 1.52] g/cm³ 内;越界立即回基线", ""],
    ["安全边界", "产品质量", "任一时刻总精煤灰分偏离目标 ±0.5% 以上、或产品判定异常 → 中止实验,恢复常规控制", ""],
    ["安全边界", "单步幅度", "每步 ≤0.01(信号不足经判定后最多放宽到 0.02),严禁一步到位式大步长", ""],
    ["组织", "人员分工", "密度调节/抄表/化验/记录 各有专人;实验期间暂停其他工艺试验", ""],
    ["组织", "时间窗", "选在检修后稳定生产时段,避开交接班前后 1h", ""],
]
for r in rows2:
    ws2.append(r)
style_sheet(ws2, [10, 16, 60, 10])
style_body(ws2, center_cols=(1, 4))

# ================= Sheet3 实验方案 =================
ws3 = wb.create_sheet("实验方案")
ws3.append(["阶段", "时长", "密度设定", "动作要点", "采样要求", "判稳条件"])
rho0 = "ρ0(当前运行密度,示例 1.49)"
rows3 = [
    ["P1 基准期", "2h", rho0,
     "不调节,保持当前密度;记录基线密度与基线灰分",
     "密度计/502仪每15min;315化验×2(阶段初+末)",
     "连续 1h 内 502 灰分 15min 极差 < ±0.1%"],
    ["P2 阶跃↑", "2h", "ρ0 + 0.01",
     "一步阶跃上调密度 0.01,到位后保持;期间不改其他任何操作",
     "密度计/502仪每15min;315化验×1(稳态末)",
     "调后 ≥60min 且 502 灰分趋稳(15min 极差 < ±0.1%)"],
    ["P3 回基线", "2h", rho0,
     "一步调回 ρ0,验证响应可复现(上下行的 ΔA 应同量级)",
     "同上;315化验×1",
     "同上"],
    ["P4 阶跃↓", "2h", "ρ0 − 0.01",
     "一步阶跃下调 0.01;与 P2 构成上-下交替,抵消慢漂移",
     "同上;315化验×1",
     "同上"],
    ["P5 回基线", "2h", rho0,
     "回到 ρ0 结束(最少可行方案到此为止,共 8h/4 阶段)",
     "同上;315化验×1",
     "同上"],
    ["P6 加测(可选)", "2h", "ρ0 + 0.02 或 ρ0 − 0.02",
     "仅当 0.01 步的 ΔA 信号不足(判定见 K计算表)时执行:加大步长提高信噪比",
     "同上;315化验×1",
     "同上"],
]
for r in rows3:
    ws3.append(r)
style_sheet(ws3, [13, 7, 18, 40, 30, 30])
style_body(ws3, center_cols=(1, 2))

# ================= Sheet4 采样记录表 =================
ws4 = wb.create_sheet("采样记录表")
ws4.append(["阶段", "日期时间", "密度设定值(g/cm³)", "密度计实测(g/cm³)",
            "502灰分仪(%)", "315化验灰分(%)", "带煤量(t/h)", "浮精灰分(%)",
            "原煤灰分(%)", "操作员", "备注"])
# 预填阶段模板:每阶段 8 个抄表行(15min间隔)+ 化验行由抄表行兼任(填315列)
phases = [("P1 基准期", "ρ0"), ("P2 阶跃↑", "ρ0+0.01"), ("P3 回基线", "ρ0"),
          ("P4 阶跃↓", "ρ0−0.01"), ("P5 回基线", "ρ0"), ("P6 加测(可选)", "ρ0±0.02")]
r = 2
for name, setpt in phases:
    for k in range(8):
        ws4.cell(row=r, column=1, value=name)
        ws4.cell(row=r, column=3, value=setpt)
        r += 1
style_sheet(ws4, [13, 17, 15, 15, 12, 13, 11, 11, 11, 9, 18])
for row in ws4.iter_rows(min_row=2):
    for cell in row:
        cell.font = BODY_FONT
        cell.border = BORDER
        cell.alignment = CENTER
        if cell.column in (2, 4, 5, 6, 7, 8, 9, 10, 11):
            cell.fill = INPUT_FILL
ws4.cell(row=r + 1, column=1, value="填写说明:①每行=一次抄表(建议15min间隔);②315化验有结果时填第6列,"
         "该行其余列照常抄;③密度计实测填调节到位后的稳定读数;④备注记录异常(停机/倒系统/加药变动)。").font = \
    Font(name="微软雅黑", size=9, italic=True, color="808080")

# ================= Sheet5 K计算表 =================
ws5 = wb.create_sheet("K计算表")
ws5.append(["项目", "说明", "数值/公式"])
calc_rows = [
    ["ρ0 基线密度", "P1 稳态密度计均值(g/cm³)", ""],
    ["A0 基线灰分", "P1 稳态 502 仪均值(%)", ""],
    ["", "", ""],
    ["#表头", "Δρ(稳态密度−ρ0)", "ΔA(稳态502均值−A0)"],
    ["P2 (ρ0+0.01) 稳态密度", "填 P2 判稳后密度均值", ""],
    ["P2 稳态502灰分", "填 P2 判稳后 502 均值(%)", ""],
    ["P3 回基线 稳态密度", "填 P3 判稳后密度均值", ""],
    ["P3 稳态502灰分", "填 P3 判稳后 502 均值(%)", ""],
    ["P4 (ρ0−0.01) 稳态密度", "填 P4 判稳后密度均值", ""],
    ["P4 稳态502灰分", "填 P4 判稳后 502 均值(%)", ""],
    ["P6 (可选) 稳态密度", "填 P6 判稳后密度均值(未做留空)", ""],
    ["P6 稳态502灰分", "填 P6 判稳后 502 均值(未做留空)", ""],
]
for r in calc_rows:
    ws5.append(r)
# 行号布局(显式常量,避免错位):
#   输入区: B2=ρ0, B3=A0, B6..B13 = P2/P3/P4/P6 的 稳态密度/稳态灰分
#   计算区: 第16行表头, 17..20 = 四步, 21=K均值, 22=标准差, 23=CV, 24=对比, 25=结论
ROW_HEADER, ROW_STEPS, ROW_MEAN, ROW_STD, ROW_CV, ROW_CMP, ROW_CONC = 16, 17, 21, 22, 23, 24, 25
ws5.cell(row=14, column=1, value="自动计算区")
ws5.cell(row=14, column=2, value="公式自动计算,无需填写")
steps = [
    ("P2 ↑", 6, 7), ("P3 ↓(回基线)", 8, 9), ("P4 ↓", 10, 11), ("P6(可选)", 12, 13),
]
for i, (name, r_rho, r_ash) in enumerate(steps):
    rr = ROW_STEPS + i
    ws5.cell(row=rr, column=1, value=name)
    ws5.cell(row=rr, column=2, value=f'=IF(B{r_rho}="","",B{r_rho}-$B$2)')
    ws5.cell(row=rr, column=3, value=f'=IF(B{r_ash}="","",B{r_ash}-$B$3)')
    ws5.cell(row=rr, column=4, value=f'=IF(OR(B{r_rho}="",B{r_ash}=""),"",(B{r_rho}-$B$2)/(B{r_ash}-$B$3))')
    ws5.cell(row=rr, column=5, value=f'=IF(D{rr}="","",IF((B{r_rho}-$B$2)*(B{r_ash}-$B$3)>0,"同向(密度↑灰分↑)","反向!检查数据"))')
    ws5.cell(row=rr, column=6, value=f'=IF(D{rr}="","",IF(AND(D{rr}>0.005,D{rr}<0.2),"有效","越界(0.005~0.2),弃用"))')
k_first, k_last = ROW_STEPS, ROW_STEPS + len(steps) - 1
ws5.cell(row=ROW_MEAN, column=1, value="K 平均值")
ws5.cell(row=ROW_MEAN, column=2, value=f'=IFERROR(AVERAGE(D{k_first}:D{k_last}),"")')
ws5.cell(row=ROW_STD, column=1, value="K 标准差")
ws5.cell(row=ROW_STD, column=2, value=f'=IFERROR(STDEV(D{k_first}:D{k_last}),"")')
ws5.cell(row=ROW_CV, column=1, value="变异系数CV")
ws5.cell(row=ROW_CV, column=2, value=f'=IFERROR(B{ROW_STD}/B{ROW_MEAN},"")')
ws5.cell(row=ROW_CV, column=3, value="CV<30% 视为可辨识;≥30% 说明信号不足或工况漂移")
ws5.cell(row=ROW_CMP, column=1, value="与专家值对比")
ws5.cell(row=ROW_CMP, column=2, value=f'=IF(B{ROW_MEAN}="","",B{ROW_MEAN}-0.075)')
ws5.cell(row=ROW_CMP, column=3, value="与专家表反推值 0.075 的偏差;±0.02 内认为经验值得到验证")
ws5.cell(row=ROW_CONC, column=1, value="结论建议")
ws5.cell(row=ROW_CONC, column=2, value=(
    f'=IF(B{ROW_MEAN}="","待填",IF(ABS(B{ROW_MEAN}-0.075)<0.02,"专家值得到验证,维持现行专家表",'
    f'IF(B{ROW_CV}>0.3,"信号不足:加做0.02步长(P6)或延长保持时间","建议按实测K修订专家表与K_PREDICT")))'))
style_sheet(ws5, [17, 42, 15, 13, 20, 22])
style_body(ws5, center_cols=(1,))
# 填写/公式单元格着色
for row in ws5.iter_rows(min_row=2):
    for cell in row:
        if cell.column in (2, 3) and cell.row in (2, 3, 6, 7, 8, 9, 10, 11, 12, 13):
            cell.fill = INPUT_FILL
        if cell.row >= ROW_HEADER and cell.column >= 2:
            cell.fill = CALC_FILL

# ================= Sheet6 系统录入指引 =================
ws6 = wb.create_sheet("系统录入指引")
ws6.append(["序号", "步骤", "操作", "说明"])
rows6 = [
    [1, "整理采样对", "从采样记录表提取「判稳时段」的 (采样时间, 密度计实测, 502灰分仪读数) 三列,剔除判稳前的过渡段",
     "只录稳态对——阶跃实验的价值在稳态增益,过渡段数据会污染 K"],
    [2, "录入方式A(推荐):表3补录", "系统「数据采集与补录」页 → 灰分密度补录:系统=A/B、皮带=502、灰分=502读数、密度=密度计读数;"
     "或按表3 Excel 模板(采样时间|系统|皮带|灰分%|密度值)整理后走「批量数据导入」",
     "表3(ash_density)记录是 K 估计链路的直接数据源(densityGainK 分系统 OLS);502 皮带 = 重介在线灰分,与工艺分工一致"],
    [3, "录入方式B:重介灰分采样", "「数据采集」页重介灰分采样表单 (ts, rho, ash) → heavy_samples 表",
     "作为化验级记录留存;注意当前 K 计算链消费的是表3,方式A才是生效路径"],
    [4, "生效条件", "有效配对 ≥5 条(分系统)后,K 估计自动从「不可辨识(回退专家值0.075)」切换为「数据驱动」;"
     "物理约束(0.005<K<0.2,分系统斜率为正)会自动校验",
     "总览页「预测增益K」的来源会显示为「表3配对数据驱动,n=xx,分系统加权」"],
    [5, "结果核对", "对比页面显示的 K 与本工作簿 K计算表 的手工结果,两者应一致(±舍入)",
     "不一致通常意味着录入的时段包含过渡段数据——回查第1步"],
    [6, "后续校准(可选)", "若实测 K 与 0.075 偏差 >0.02:把专家表锚点按 K 重算(0.01/K=灰分死区上沿等),"
     "并同步 K_PREDICT 常数——需要开发侧修改,先记录在案",
     "专家表当前锚点:|ΔA|0.15%→Δρ0.01、0.25%→0.02(反推 K=0.067~0.08)"],
]
for r in rows6:
    ws6.append(r)
style_sheet(ws6, [6, 20, 52, 44])
style_body(ws6)

wb.save(OUT)
print("已生成:", OUT)
print("Sheet:", wb.sheetnames)
