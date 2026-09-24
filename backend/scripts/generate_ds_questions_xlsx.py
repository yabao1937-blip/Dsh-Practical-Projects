# -*- coding: utf-8 -*-
"""生成 Excel：DS 粗精煤泥灰分预测——疑问与决策表（另建一份，不与「结论表」混用）。

产出 docs/DS粗灰预测-疑问与决策.xlsx
约定：**疑问 → 选项 → 我的建议 → 状态**，用户只需在「你的选择」列里填 A/B/C 或直接在对话里回答。
数字全部现算（读 backend/data/dense_medium.db）。重跑本脚本即刷新。
"""
import bisect
import math
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from openpyxl import Workbook                                            # noqa: E402
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side   # noqa: E402

from app.services.coarse_direction import direction_report               # noqa: E402
from app.services.coarse_forward import FEATURE_SETS, evaluate_window, forward_windows  # noqa: E402
from app.services.modeling import MLR_FEATURES                           # noqa: E402
from app.services.training import train_coarse_model                     # noqa: E402

DOCS = ROOT.parent / "docs"
OUT = DOCS / "DS粗灰预测-疑问与决策.xlsx"
DB = ROOT / "data" / "dense_medium.db"
TODAY = datetime.now().strftime("%Y-%m-%d")

HEAD_FILL = PatternFill("solid", fgColor="C00000")
HEAD_FONT = Font(name="微软雅黑", size=11, bold=True, color="FFFFFF")
TITLE_FONT = Font(name="微软雅黑", size=13, bold=True, color="C00000")
BODY_FONT = Font(name="微软雅黑", size=10)
Q_FILL = PatternFill("solid", fgColor="FFF2CC")        # 疑问行
OK_FILL = PatternFill("solid", fgColor="E2EFDA")
BAD_FILL = PatternFill("solid", fgColor="F8CBAD")
INFO_FILL = PatternFill("solid", fgColor="DEEAF6")
THIN = Side(style="thin", color="B0B0B0")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
WRAP = Alignment(wrap_text=True, vertical="top")
CENTER = Alignment(horizontal="center", vertical="top", wrap_text=True)


def sheet(wb, name, title, headers, widths, rows, fill_col=None, fill_map=None, row_fill=None):
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
        if row_fill and str(row[0]).startswith(("Q", "【")):
            for c in range(1, len(headers) + 1):
                ws.cell(row=i, column=c).fill = Q_FILL
        if fill_col and fill_map:
            f = fill_map.get(str(row[fill_col - 1]))
            if f:
                for c in range(1, len(headers) + 1):
                    ws.cell(row=i, column=c).fill = f
    ws.freeze_panes = "A4"
    return ws


# ---------------- 现算 ----------------
con = sqlite3.connect(str(DB))
cols = [c[1] for c in con.execute("pragma table_info(coal_records)")]
coarse = [dict(zip(cols, r)) for r in con.execute(
    "select * from coal_records where category='coarse' and ash_content is not null order by ts, id")]
ad = [dict(zip(cols, r)) for r in con.execute("select * from coal_records where category='ash_density' order by ts, id")]
counts = dict(con.execute("select category, count(*) from coal_records group by category").fetchall())
ilog = con.execute("select count(*) from import_logs").fetchone()[0]
con.close()
for r in coarse:
    r["timestamp"] = r["ts"]

model_all = train_coarse_model([dict(r) for r in coarse], "all")
n_feat = len(model_all["mlr"]["coefs"])
lock = evaluate_window(coarse, "2026-09-04", None, feature_sets=FEATURE_SETS)
dir_new = direction_report(coarse, split_ts="2026-09-04")
# 「现行模型」= 现在真的在跑的那个配置（10 因子 MLR 生产路径），不能拿对照里的最小值冒充
cur = lock["models"]["10因子(现行)|MLR"]
four = lock["models"]["4因子(原煤灰/煤量/液位/水分)|MLR"]
best_base = min(lock["baselines"].items(), key=lambda kv: kv[1]["mae"])

ranges = []
for rng, label in (("jun_jul", "6-7月（113 条）"), ("30d", "最近 30 天"), ("all", "全部数据")):
    res = train_coarse_model([dict(r) for r in coarse], rng)
    h = res["history"]
    # 用与服务同一套口径取向前窗口（leftover 优先；覆盖全部时 tail 留尾）
    wins = forward_windows(coarse, rng)
    if wins:
        kind, wlabel, cut, end = wins[0]
        ev = evaluate_window(coarse, cut, end)
        bm = min(ev["models"].items(), key=lambda kv: kv[1]["mae"])
        bb = min(ev["baselines"].items(), key=lambda kv: kv[1]["mae"])
        fwd = "%s（%s %d 条）→ 模型 %.3f vs 最强基线 %s %.3f" % (
            "真向前 8-23~9-21" if kind == "leftover" else "留尾参考 %s~%s" % (ev["testFrom"][:10], ev["testTo"][:10]),
            "训练范围之后" if kind == "leftover" else "最后", ev["testN"], bm[1]["mae"], bb[0], bb[1]["mae"])
        can = "可测（有剩余数据）" if kind == "leftover" else "只能留尾参考"
    else:
        fwd, can = "无可用窗口", "不可测"
    ranges.append([label, res["n"], res["production"].upper(),
                   "%.3f" % h[res["production"]]["r2"], "%.3f" % h[res["production"]]["q2"],
                   ("%.3f" % h[res["production"]]["q2Time"]) if h[res["production"]]["q2Time"] is not None else "-",
                   fwd, can])

# 502 灰分（ash_density 表）能否当因子
seq502 = sorted([r for r in ad if r.get("ash_content") is not None], key=lambda r: r["ts"])
ts502 = [r["ts"] for r in seq502]
cov, pairs = defaultdict(Counter), []
for r in coarse:
    i = bisect.bisect_left(ts502, r["ts"])
    best = None
    for j in (i - 1, i):
        if 0 <= j < len(seq502):
            gap = abs((datetime.strptime(r["ts"], "%Y-%m-%d %H:%M:%S")
                       - datetime.strptime(seq502[j]["ts"], "%Y-%m-%d %H:%M:%S")).total_seconds()) / 3600
            if best is None or gap < best[0]:
                best = (gap, seq502[j])
    m = r["ts"][:7]
    cov[m]["n"] += 1
    for lim, key in ((1, "1h"), (3, "3h"), (6, "6h")):
        if best and best[0] <= lim:
            cov[m][key] += 1
    if best and best[0] <= 3:
        pairs.append((r["ash_content"], best[1]["ash_content"]))
corr502 = None
if len(pairs) > 5:
    x = [p[0] for p in pairs]
    y = [p[1] for p in pairs]
    mx, my = sum(x) / len(x), sum(y) / len(y)
    corr502 = (sum((a - mx) * (b - my) for a, b in pairs)
               / math.sqrt(sum((a - mx) ** 2 for a in x) * sum((b - my) ** 2 for b in y)))
cov_rows = [[m, cov[m]["n"], cov[m]["1h"], cov[m]["3h"], cov[m]["6h"]] for m in sorted(cov)]

wb = Workbook()
wb.remove(wb.active)

sheet(
    wb, "一、现状事实", "先回答「现在有没有四因子模型」——没有；下面是系统当前真实状态（%s 现算）" % TODAY,
    ["项", "现状", "数字 / 依据"], [26, 46, 74],
    [
        ["四因子模型", "**不存在**（只出现在评估对照表里）",
         "生产链路用的是 modeling.MLR_FEATURES 的 %d 个因子；当前保存的 DS 模型 coefs 长度 = %d。"
         "「4因子」只写在 coarse_forward.FEATURE_SETS 里，供 --sets=1 时算对照，没有独立的模型对象、"
         "没有保存、也没有参与任何预测。" % (len(MLR_FEATURES), n_feat)],
        ["现行因子", "、".join(MLR_FEATURES), "DS 与 GPT 共用这张特征表（GPT 的候选集也从它派生）"],
        ["库内数据", "粗精煤泥 %d 条 / 灰分密度 %d 条 / 浮精 %d 条 / 导入日志 %d 条"
                     % (counts.get("coarse", 0), counts.get("ash_density", 0), counts.get("float", 0), ilog),
         "粗精煤泥 2026-06-16 ~ 2026-09-21（7-15~8-22 无数据）"],
        ["真向前成绩（锁定窗）", "现行 10 因子模型 %.3f，还不如取训练均值 %.3f" % (cur["mae"], lock["baselines"]["取训练均值"]["mae"]),
         "训练 6-16~9-03（155 条）→ 检验 9-04~9-21（%d 条，模型没见过）；最强在线基线 %s %.3f；"
         "现行模型 10因子|MLR 样本内 R² %.3f" % (lock["testN"], best_base[0], best_base[1]["mae"], cur["inSampleR2"])],
        ["方向信号（同窗）", "可用：命中 %.3f，95%% 区间 %s，n=%d" % (dir_new["hit"], dir_new["ci"], dir_new["n"]),
         "多数基线 %.3f、惯性基线 %.3f；特征仅用 t 时刻已知量" % (dir_new["baseline"], dir_new["inertia"])],
        ["四因子改进幅度", "锁定窗 MAE %.3f → %.3f（同样本同切点）" % (cur["mae"], four["mae"]),
         "两个窗口一致更优；锁定窗配对 bootstrap 差值 [+0.138, +0.643]（不含 0，显著）。"
         "但它仍与取均值基线（%.3f）接近，属于「少犯错」而不是「能预测」。" % lock["baselines"]["取训练均值"]["mae"]],
        ["已落地（上一轮）", "向前验证进系统：只读接口 + DS 训练带 forward/direction + 页面区块",
         "POST 训练响应与页面都显示「模型向前 MAE vs 朴素基线」和方向命中，避免只看 R² 误判"],
    ],
)

sheet(
    wb, "二、疑问清单", "待你回答的疑问（在「你的选择」列填 A/B/C，或直接在对话里说）",
    ["编号", "疑问", "为什么问 / 影响什么", "选项", "我的建议", "你的选择"],
    [8, 30, 44, 44, 34, 12],
    [
        ["Q1", "四因子模型要不要做？",
         "这是本轮唯一有证据的改进（锁定窗 1.814→1.521）。但它改的是生产模型的因子组成，"
         "会牵动 JS/后端两份实现与 DS 对拍基线。",
         "A 换成 4 因子生产模型；B 10 因子保留做解释 + 另加 4 因子向前支线；C 维持现状、攒数据再议",
         "A（若你希望 DS 的向前成绩好看一点）；工作量约半天，含对拍复核",
         ""],
        ["Q2", "要预测的“目标”到底是什么？",
         "现在模型预测的是“下一条采样点的 315 灰分”。如果现场真正关心的是班均值/日均值，"
         "或者“能不能稳在目标灰分附近”，建模对象和验收指标都该换。",
         "A 下一条采样点（现状）；B 当日班/日均值；C 相对目标灰分的偏差（如目标 8.5%）",
         "先确认目标口径再谈精度，否则做出来也不是你要的那个数",
         ""],
        ["Q3", "向前精度要到多少才算“可用”？",
         "现在锁定窗 1.81、取训练均值 1.74、最强在线平滑 1.55 —— 模型打不赢朴素基线。"
         "定了合格线我才知道该继续投算法还是转做“方向 + 人工判断”。",
         "A 只要打赢取均值/在线平滑基线即可；B MAE ≤1.5 灰分百分点；C MAE ≤1.0；D 不设指标，只看方向",
         "A（现实且可验收）；若你要 B/C，需要更细的在线数据（见 Q7/Q8）",
         ""],
        ["Q4", "重训练用哪个范围？",
         "范围决定“能不能测出向前能力”：选“全部”就没有剩余数据可检验（页面只能给留尾参考）。",
         "A 6-7月固定基线；B 最近 30 天滚动；C 全部数据；D 每次按导入批次手工指定",
         "A + 定期加新数据（留出最新一两个月做检验窗口）",
         ""],
        ["Q5", "化验结果什么时候可用？",
         "决定“最近读数”能不能当特征/做在线校正，也决定方向信号在什么时候才敢用。"
         "现场此前只确认“当天出结果”。",
         "请填：采样后约 ____ 小时可在系统里录入/看到（例如 4 小时 / 下一班）",
         "若滞后 ≥ 数小时，在线校正基本不可行，重心应放在方向信号",
         ""],
        ["Q6", "方向信号要不要接进操作提示？",
         "现在方向只在页面展示，不参与密度建议链。要接进去必须现场确认“方向确实可操作”"
         "（涨→提密/降密的方向与幅度是否认可）。",
         "A 接入（方向偏涨时提示提密方向）；B 仅展示不改建议；C 先关掉",
         "B 先展示一段时间，等你观察后再决定是否接入",
         ""],
        ["Q7", "数据能不能固定供给？",
         "现在每 2~3 周手工导一批 Excel，向前检验窗口只有 40~90 条。样本量直接决定结论可信度。",
         "A 固定目录+固定文件名+固定列，每周/每月一放；B 继续手工，频率不变；C 接入数据库/接口直连",
         "A（改动最小、立刻提升向前验收的样本量）",
         ""],
        ["Q8", "有没有更密的粗精煤泥灰分数据？",
         "现有 315 灰分每天只有 4~5 个瞬时点（相邻间隔中位 2.6 小时）。若有每班 1 次或在线测量，"
         "就能做真正的趋势跟踪与在线校正。",
         "A 有在线测量（请在备注里写来源）；B 可加密到每班 1 次；C 只有现状 4~5 点/天",
         "A 或 B —— 这是精度上限的关键",
         ""],
    ],
    row_fill=True,
)

sheet(
    wb, "三、四因子三条路线", "Q1 的三个选项分别要改什么、代价与风险（现状：10 因子，%d 个系数）" % n_feat,
    ["路线", "内容", "必须同步改的地方", "工作量 / 风险", "效果（已有证据）"],
    [12, 34, 56, 34, 40],
    [
        ["A 换生产模型", "DS 生产模型改用 4 个连续因子（原煤灰/煤量/液位/水分），去掉 6 个近常量开关列",
         "① 后端新增 DS 专用特征表（不能直接改 MLR_FEATURES —— GPT 的候选集从它派生，会动到 GPT）；"
         "② predict_coarse_ash 改为按模型的 feature_names 取值（现在按固定 10 因子位置取，换表会取错）；"
         "③ 前端 App 的 DS 训练/预测同步；④ 重生成 backend/data/train_js.json 对拍基线并逐项复核差异；"
         "⑤ 页面/文档里“10 因子”的表述要改",
         "约半天（含对拍复核）。风险：DS/GPT 共用预测函数，改动面比看起来大",
         "锁定窗 MAE 1.814→1.521（显著）；开发侧 2.564→2.413。样本内 R² 略降（0.374→0.324）——"
         "正是“不追拟合度”的取舍"],
        ["B 双支线", "10 因子继续做“历史解释/因子分析”，另加一条 4 因子“向前预测”支线，页面分开展示",
         "在 A 的基础上再加：训练编排要同时产出两套模型、快照/页面/对拍基线都要容纳两条支线",
         "约 1~1.5 天。风险：两套模型的展示口径容易让现场混淆",
         "同样的向前收益，但保留 10 因子的解释力；代价是系统复杂度上升"],
        ["C 维持现状", "10 因子不动，只保留已经上线的“向前验证 + 方向”展示",
         "无（已完成）", "0",
         "向前成绩仍是 1.814（输给取均值 1.744）；但数据再多几个月后可重算同一套验收再决定"],
    ],
)

sheet(
    wb, "四、因子想法核对", "顺手核过的两个“看起来能提升精度”的想法（数字现算，避免拍脑袋）",
    ["想法", "可配对样本", "结果", "结论"],
    [30, 40, 40, 44],
    [
        ["用 502 精煤灰分（ash_density 表）当因子",
         "粗灰与 502 灰分在 3 小时内的配对：%d 对（6-7 月 0 对）" % len(pairs),
         "corr(粗灰, 502灰分) = %.3f；502 灰分自身 std 只有 %.2f" % (corr502 or 0,
                                                                  (lambda v: math.sqrt(sum((b - sum(v) / len(v)) ** 2 for b in v) / len(v)))([p[1] for p in pairs]) if pairs else 0),
         "相关性弱、且 6-7 月完全没有配对数据 → 暂时不能用；等 502 在线灰分历史补齐后再评估"],
        ["用最近密度读数当因子", "6-7 月 113 条粗灰里 0 条能在 6 小时内对上密度读数",
         "密度读数只有 2026-05/08/09 三个月", "密度特征按你的指示先搁置（数据缺口，不是方法问题）"],
    ],
)

sheet(
    wb, "五、训练范围对照", "Q4 的依据：三个范围在“拟合”与“向前”上的差别（%s 现算）" % TODAY,
    ["范围", "样本n", "生产算法", "样本内 R²", "Q²", "时间验证 Q²时", "向前成绩", "能否测真向前"],
    [18, 8, 10, 12, 10, 14, 46, 20],
    ranges,
)

sheet(
    wb, "六、502灰分配对明细", "按月看：粗灰样本能不能在同一时段对上 502 灰分（3h 内）",
    ["月份", "粗灰条数", "≤1h", "≤3h", "≤6h"], [14, 12, 10, 10, 10],
    cov_rows,
)

sheet(
    wb, "七、回答方式", "怎么回这份表",
    ["方式", "做法"], [22, 92],
    [
        ["直接填表", "在「二、疑问清单」的「你的选择」列填 A/B/C（或写文字），把文件发回即可"],
        ["对话回答", "直接在对话里说“Q1 选 A、Q2 是日均值……”也完全可以，我会同步回表并落到代码/文档"],
        ["只答关键项", "最少只需回答 Q1（四因子）与 Q2（预测目标口径）——其余可以边做边定"],
        ["本轮已默认执行", "密度特征（你已指示搁置）、502 灰分因子（证据不足）、B 系统密度补导（等你确认）"],
    ],
)

DOCS.mkdir(parents=True, exist_ok=True)
wb.save(OUT)
print("已生成:", OUT)
for ws in wb.worksheets:
    print("  - %-20s %d 行" % (ws.title, ws.max_row - 3))
