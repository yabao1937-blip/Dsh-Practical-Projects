# -*- coding: utf-8 -*-
"""生成 Excel：DS 粗精煤泥灰分——「方向可用 / 水平不可用」的验证结论与落地清单。

产出 docs/DS粗灰-方向与水平验证结论.xlsx（openpyxl 自洽，无外部运行时依赖）。
数据来源：2026-09-23 三轮实验（开发集 6-7 月、检验集 8-9 月锁死）+ coarse_direction 模块 CLI 自检。
"""
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

DOCS = Path(__file__).resolve().parent.parent.parent / "docs"
OUT = DOCS / "DS粗灰-方向与水平验证结论.xlsx"

HEAD_FILL = PatternFill("solid", fgColor="2F5597")
HEAD_FONT = Font(name="微软雅黑", size=11, bold=True, color="FFFFFF")
TITLE_FONT = Font(name="微软雅黑", size=13, bold=True, color="2F5597")
BODY_FONT = Font(name="微软雅黑", size=10)
OK_FILL = PatternFill("solid", fgColor="E2EFDA")
BAD_FILL = PatternFill("solid", fgColor="F8CBAD")
WARN_FILL = PatternFill("solid", fgColor="FFE699")
THIN = Side(style="thin", color="B0B0B0")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
WRAP = Alignment(wrap_text=True, vertical="top")
CENTER = Alignment(horizontal="center", vertical="top", wrap_text=True)


def sheet(wb, name, title, headers, widths, rows, fill_col=None, fill_map=None):
    ws = wb.create_sheet(name)
    ws["A1"] = title
    ws["A1"].font = TITLE_FONT
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(headers))
    for c, (h, w) in enumerate(zip(headers, widths), start=1):
        cell = ws.cell(row=3, column=c, value=h)
        cell.fill, cell.font, cell.alignment, cell.border = HEAD_FILL, HEAD_FONT, CENTER, BORDER
        ws.column_dimensions[cell.column_letter].width = w
    for i, row in enumerate(rows, start=4):
        for c, v in enumerate(row, start=1):
            cell = ws.cell(row=i, column=c, value=v)
            cell.font = BODY_FONT
            cell.alignment = CENTER if c <= 2 else WRAP
            cell.border = BORDER
        if fill_col and fill_map:
            f = fill_map.get(str(row[fill_col - 1]))
            if f:
                for c in range(1, len(headers) + 1):
                    ws.cell(row=i, column=c).fill = f
    ws.freeze_panes = "A4"
    return ws


wb = Workbook()
wb.remove(wb.active)

sheet(
    wb, "结论", "DS 粗精煤泥灰分：水平值不可用、方向可用（2026-09-23 验证结论）",
    ["议题", "结论", "数据证据", "纪律 / 做法"], [16, 30, 60, 56],
    [
        ["水平值预测", "不可用（不承诺精度）",
         "锁死检验集（8-9 月 39 条）上：DS 基线 MAE 2.652、加滞后 2.628、持续性 3.244、增量模型 14.340；"
         "而「取开发集均值」基线 MAE 2.263 —— 所有候选都输给均值基线。",
         "原因不是算法：灰分中枢跨期漂移（6-7 月约 14.0、8-9 月约 14.9），水平外推必然被系统性偏移拖死。"
         "因此 DS 的水平输出只能标注为「历史对照」。"],
        ["方向判断", "可用（唯一稳健的前向信号）",
         "「第 t 条已知信息 → 第 t+1 条相对第 t 条涨跌」：三个切分点命中 0.771（6-7→8-9，区间 [0.629,0.914]，n=35）、"
         "0.727（6→7，[0.591,0.841]，n=44）、0.617（6→7+8+9，[0.506,0.716]，n=81）。多数基线 0.514~0.531。",
         "验收门槛：bootstrap 95% 区间**下界 > 多数基线** 才判可用。第三个切分因此被正确拒绝 —— 门控不是橡皮图章。"],
        ["机理", "均值回复（不是靠采样节奏）",
         "惯性基线（本次涨→下次继续涨）仅 0.343（明显反持续）；去掉「到下次读数的时间间隔」特征后，"
         "三个切分命中率不变（0.727/0.771/0.771）。",
         "模型从「Δprev + prev 水平」重建当前水平，当前偏高则下次多半回落。"
         "正式版**不使用**时间间隔特征 —— 排除「靠采样节奏猜方向」的偏利。"],
        ["纪律①（踩过坑）", "特征只能用 t 时刻**已知**量",
         "曾把「第 t 条自身的灰分同源测量」与「第 t 条自身的变化」配对，得到 0.833~0.917 的假命中率；"
         "把目标前移一格（真向前）后掉到 0.657~0.771。",
         "前者是「同时刻解释」——生产上第 t 条还不存在，等于用未来信息。"
         "这条假象已写进模块文档，作为反面教材保留。"],
        ["纪律②（对称的）", "也不能只追某个前向指标",
         "增量模型的向前 q2Time = +0.417（看着很好），但其检验集 R² = −11.47、MAE = 14.34 —— 彻底崩。",
         "原因：目标换成 Δ 后 Q² 是在「增量方差」上算的，量纲与水平值不同，跨口径比指标会得出相反结论。"
         "任何前向指标必须与「水平 MAE / 是否打赢均值基线」一起看。"],
    ],
    fill_col=1,
    fill_map={"水平值预测": BAD_FILL, "方向判断": OK_FILL, "机理": OK_FILL,
              "纪律①（踩过坑）": WARN_FILL, "纪律②（对称的）": WARN_FILL},
)

sheet(
    wb, "三切分验证明细", "方向信号稳健性：三切分 × 两特征组（Ridge 回归 Δ 取符号，|Δ|>0.5% 计入）",
    ["切分", "特征组", "方向命中", "多数基线", "95% bootstrap", "检验样本", "门控结论"],
    [20, 14, 12, 12, 20, 12, 22],
    [
        ["6-7月 → 8-9月", "full（含间隔）", 0.771, 0.514, "[0.629, 0.914]", 35, "可用"],
        ["6-7月 → 8-9月", "nogap（正式版）", 0.771, 0.514, "[0.629, 0.914]", 35, "可用"],
        ["6月 → 7月", "full", 0.727, 0.523, "[0.591, 0.841]", 44, "可用"],
        ["6月 → 7月", "nogap（正式版）", 0.727, 0.523, "[0.591, 0.841]", 44, "可用"],
        ["6月 → 7+8+9月", "nogap", 0.617, 0.531, "[0.506, 0.716]", 81, "拒绝（下界未超基线）"],
    ],
    fill_col=7,
    fill_map={"可用": OK_FILL, "拒绝（下界未超基线）": BAD_FILL},
)

sheet(
    wb, "落地与待办", "已完成 / 待做 / 现场待确认",
    ["项", "类型", "内容", "状态"], [22, 12, 76, 14],
    [
        ["coarse_direction.py", "已完成", "方向判断模块：direction_report() 输出 usable/hit/baseline/ci/n/note/gating，"
                                      "自带 CLI 自检；两条纪律写进模块文档。", "已提交 3b48f09"],
        ["单元测试", "已完成", "3 条：均值回复序列必须判可用；**随机游走必须判不可用**（锁住门控）；"
                               "报告必须声明 gating。", "3 passed"],
        ["接入训练编排", "已完成", "DS 训练结束时调用 direction_report()，作为新键 direction 返回；"
                                   "开发月=本次实际训练月份、检验月=不在开发月的最后两个月；异常隔离不影响主训练。",
         "已提交 2f6f9d1"],
        ["对拍基线", "不需要动", "direction 只进 HTTP 响应，不进 metrics/history/保存快照 → "
                                 "DS 基线 train_js.json 无需重生成（对拍测试保持通过即为证据）。", "已验证"],
        ["前端展示", "待做", "粗精煤泥页新增「下一读数方向：偏涨/偏跌（历史命中 0.77，区间 0.63~0.91）」；"
                             "水平值栏在 direction.usable=false 时标注「仅历史对照」。"
                             "需要把 direction 透出到保存快照与 /state 兼容模型 → 会牵动对拍基线，需一并重生成并跑对拍。",
         "未开始"],
        ["文档并表", "待做", "本结论后续并入 docs/待办-后续优化计划.xlsx（按编号关联，避免重复 Sheet）。", "未开始"],
        ["瞬时值前提", "待现场确认", "本结论建立在「粗精煤泥信息表全是瞬时值、无人为采样」之上（用户 2026-09-23 说明）；"
                                     "若其中含人为填报/班次加权值，需复核方向结论。", "待确认"],
        ["采样节奏", "已知事实", "37 天里每日 4~5 点，时刻集中在 00 时 / 07-10 时 / 21-23 时（白班 08-10 时一天 2~3 点）；"
                                 "相邻读数间隔中位 2.6 小时 —— 这是「水平值不可预测」的直接背景。", "已核实"],
    ],
    fill_col=4,
    fill_map={"已提交 3b48f09": OK_FILL, "已提交 2f6f9d1": OK_FILL, "3 passed": OK_FILL,
              "已验证": OK_FILL, "未开始": WARN_FILL, "待确认": WARN_FILL, "已核实": OK_FILL},
)

DOCS.mkdir(parents=True, exist_ok=True)
wb.save(OUT)
print("已生成:", OUT)
for ws in wb.worksheets:
    print("  - %-16s %d 行" % (ws.title, ws.max_row - 3))
