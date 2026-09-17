# -*- coding: utf-8 -*-
"""K 标定报告：密度决策日志 → 迷你阶跃点提取 → K 估计 → Excel 报告。

用法(在 backend 目录):
  ./.venv/Scripts/python.exe scripts/analyze_k_calibration.py
        默认读真实库(load_store),写 docs/K标定-决策响应分析.xlsx
  ./.venv/Scripts/python.exe scripts/analyze_k_calibration.py --from-json <store.json> --out <x.xlsx>
  ./.venv/Scripts/python.exe scripts/analyze_k_calibration.py --demo --out <x.xlsx>
        --demo 用合成数据跑通全管线(不读库),用于演示/联调报告格式。

口径与过滤见 app/services/k_calibration.py 模块 docstring;本脚本只读,不写库。
"""
import argparse
import json
import random
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent          # backend/
sys.path.insert(0, str(BASE))
sys.stdout.reconfigure(encoding="utf-8")

from openpyxl import Workbook                          # noqa: E402
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side  # noqa: E402
from openpyxl.utils import get_column_letter           # noqa: E402

from app.services.k_calibration import (               # noqa: E402
    DA_MIN, DRHO_MIN, K_MAX, K_MIN, LAG_MAX, LAG_MIN, extract_points, summarize)
from app.services.density import K_PREDICT, EXPERT_ADJUST  # noqa: E402

DEFAULT_OUT = BASE.parent / "docs" / "K标定-决策响应分析.xlsx"

# ---- 样式(与 docs/ 其它生成器一致) ----
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


def style_head(ws, widths):
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    for cell in ws[1]:
        cell.fill = HEAD_FILL
        cell.font = HEAD_FONT
        cell.alignment = CENTER
        cell.border = BORDER
    ws.freeze_panes = "A2"


def demo_store() -> dict:
    """合成 26 条决策:18 采信(K≈0.065±噪声)+ 各排除类型若干。确定性随机。"""
    rng = random.Random(20260917)
    log = []

    def entry(i, drho, da, lag, trigger, sysA, sysB, coal, raw, resp="ok"):
        e = {"ts": "2026-09-%02d %02d:%02d:00" % (1 + i % 15, 6 + i % 14, (i * 17) % 60),
             "trigger": trigger, "scheme": "total", "target": 8.5, "tol": 0.1,
             "rhoCur": round(rng.uniform(1.42, 1.55), 3), "rhoNew": None,
             "deltaRho": None, "deltaA": round(rng.uniform(-0.4, 0.4), 2),
             "kUsed": 0.075, "kSource": "default",
             "heavyAsh": round(rng.uniform(7.8, 9.2), 2), "totalAsh": round(rng.uniform(8.0, 9.0), 2),
             "ctx": {"coalAmount": coal, "rawAsh": raw, "sysA": sysA, "sysB": sysB,
                     "miningFace": rng.choice(["3301", "3309", "6303"]),
                     "levelTail": round(rng.uniform(8, 18), 1),
                     "densityActual": round(rng.uniform(1.42, 1.55), 3),
                     "heavyAshSource": "manual"},
             "response": None}
        e["rhoNew"] = round(e["rhoCur"] + drho, 3)
        e["deltaRho"] = drho
        if resp == "ok":
            e["response"] = {"ts": e["ts"], "lagMin": lag, "source": "heavySamples",
                             "rhoNow": e["rhoNew"], "heavyAshNow": round(rng.uniform(7.8, 9.2), 2),
                             "dRhoActual": drho, "dAActual": da}
        elif resp == "invalid":
            e["response"] = {"invalidated": True, "reason": "窗口内又发生新的密度决策"}
        # resp == "pending" → response 留空
        log.append(e)

    idx = 0
    for i in range(12):    # 采信:操作员设定,K≈0.065
        idx += 1
        dr = round(-rng.uniform(0.008, 0.02), 3) * (1 if rng.random() < 0.6 else -1)
        da = round(dr / (0.065 + rng.uniform(-0.006, 0.006)), 3)
        entry(idx, dr, da, rng.randint(45, 90), "density_set", 1, 1,
              round(rng.uniform(380, 700)), round(rng.uniform(18, 26), 1))
    for i in range(6):     # 采信:自动重定目标
        idx += 1
        dr = round(-rng.uniform(0.01, 0.022), 3)
        da = round(dr / (0.068 + rng.uniform(-0.005, 0.005)), 3)
        entry(idx, dr, da, rng.randint(45, 120), "retarget", 1, 0,
              round(rng.uniform(400, 650)), round(rng.uniform(19, 27), 1))
    idx += 1; entry(idx, -0.012, -0.02, 60, "density_set", 1, 1, 500, 22.0, resp="ok")   # weak_da
    idx += 1; entry(idx, -0.001, -0.2, 60, "density_set", 1, 1, 500, 22.0, resp="ok")    # small_drho
    idx += 1; entry(idx, -0.012, -0.2, 25, "density_set", 1, 1, 500, 22.0, resp="ok")    # lag 早
    idx += 1; entry(idx, -0.012, -0.2, 400, "retarget", 1, 1, 500, 22.0, resp="ok")      # lag 晚
    idx += 1; entry(idx, 0.012, -0.2, 60, "retarget", 0, 1, 520, 21.0, resp="ok")        # bounds(K<0)
    idx += 1; entry(idx, -0.012, -0.2, 60, "density_set", 1, 1, 480, 23.0, resp="pending")
    idx += 1; entry(idx, -0.010, -0.18, 60, "density_set", 1, 0, 480, 23.0, resp="pending")
    idx += 1; entry(idx, -0.015, -0.22, 60, "density_set", 1, 1, 510, 22.0, resp="invalid")
    idx += 1; entry(idx, -0.013, -0.19, 60, "retarget", 1, 1, 505, 22.0, resp="invalid")
    return {"densityDecisionLog": log}


def conclusion_text(counts: dict, est) -> list:
    lines = []
    n = counts["eligible"]
    if counts["total"] == 0:
        lines.append("尚无决策记录:现场每次真实调整密度(操作员设定或自动重定目标)会自动记录一条;"
                     "满 45 分钟且期间无新决策时,用决策后第一条人工化验补记响应。")
        return lines
    if n == 0:
        lines.append("尚无「采信点」(数据积累中):共 %d 条决策(待补记 %d / 作废 %d / 已闭环但被排除 %d)。"
                     % (counts["total"], counts["pending"], counts["invalidated"], counts["excluded"]))
        lines.append("每个采信点 = 一次真实调密 + 一条决策后人工化验;继续积累后再看本表。")
        return lines
    med = est["median"]
    lines.append("采信点 n=%d,median K=%.4f(g/cm³ 每 %% 灰分),IQR=[%.4f, %.4f]。"
                 % (n, med, est["q1"], est["q3"]))
    rel = abs(med - K_PREDICT) / K_PREDICT
    lines.append("对照:经验常数 K_PREDICT=%.3f;专家表等价 1/gain=%.4f。"
                 % (K_PREDICT, 1.0 / EXPERT_ADJUST["gain"]))
    if n < 5:
        lines.append("样本量偏小(n<5):仅作观测,不建议据此替换经验常数。")
    elif rel > 0.25:
        lines.append("与经验常数偏差 %.0f%%(>25%%):建议将 median 作为 K(工况) 初值引入,并随样本滚动更新。"
                     % (rel * 100))
    else:
        lines.append("与经验常数偏差 %.0f%%(≤25%%):维持 %.3f,继续积累。"
                     % (rel * 100, K_PREDICT))
    return lines


def main():
    ap = argparse.ArgumentParser(description="K 标定:密度决策日志 → K 估计报告")
    ap.add_argument("--from-json", help="读 store JSON(默认读真实库)")
    ap.add_argument("--demo", action="store_true", help="用合成数据演示全管线")
    ap.add_argument("--out", help="输出 xlsx 路径(默认 docs/K标定-决策响应分析.xlsx)")
    args = ap.parse_args()

    if args.demo:
        store, src_text = demo_store(), "示例(合成数据,由 --demo 生成)"
    elif args.from_json:
        store = json.loads(Path(args.from_json).read_text(encoding="utf-8"))
        src_text = "store JSON: %s" % args.from_json
    else:
        from app.database import SessionLocal
        from app.services.state import load_store
        store = load_store(SessionLocal())
        src_text = "真实库(backend/data/dense_medium.db → load_store)"

    pts = extract_points(store)
    s = summarize(pts)
    c, est, groups = s["counts"], s["estimates"], s["groups"]
    concl = conclusion_text(c, est)

    # ---------- 控制台摘要 ----------
    print("=" * 72)
    print("K 标定 —— 密度决策日志分析")
    print("数据源:", src_text)
    print("生成时间:", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    print("-" * 72)
    print("决策总数 %(total)d | 待补记 %(pending)d | 作废 %(invalidated)d | "
          "已闭环 %(closed)d | 采信 %(eligible)d | 排除 %(excluded)d" % c)
    if c["excluded"]:
        detail = {k: v for k, v in s["stages"].items()
                  if k in ("missing", "lag", "small_drho", "weak_da", "bounds")}
        print("排除明细:", detail)
    if est:
        print("采信点估计: n=%d mean=%.4f median=%.4f IQR=[%.4f, %.4f] std=%.4f"
              % (est["n"], est["mean"], est["median"], est["q1"], est["q3"], est["std"]))
    for line in concl:
        print("结论:", line)
    for axis, m in groups.items():
        print("[分组]", axis)
        for lab, st in m.items():
            print("   %-18s n=%d median=%.4f" % (lab, st["n"], st["median"]))

    # ---------- xlsx ----------
    wb = Workbook()

    ws = wb.active
    ws.title = "标定概览"
    ws.append(["项目", "值", "说明"])
    rows = [
        ["数据源", src_text, "--demo 为合成演示数据,仅供格式联调"],
        ["生成时间", datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
         "由 scripts/analyze_k_calibration.py 生成(只读分析,不写库)"],
        ["决策总数", c["total"], "密度决策日志全部条目(上限 200 条滚动)"],
        ["待补记", c["pending"], "决策未满 45 分钟或尚无决策后人工化验"],
        ["作废", c["invalidated"], "响应窗口内又发生新的密度决策,累积量无法归属"],
        ["已闭环", c["closed"], "已补记响应"],
        ["采信点", c["eligible"], "通过全部过滤(有效阶跃、非弱信号、滞后合理、K 物理有效)"],
        ["排除", c["excluded"], "排除明细: %s" % json.dumps(
            {k: v for k, v in s["stages"].items()
             if k in ("missing", "lag", "small_drho", "weak_da", "bounds")}, ensure_ascii=False)],
    ]
    if est:
        rows += [
            ["采信点 n", est["n"], ""],
            ["mean K", round(est["mean"], 4), "g/cm³ 每 % 灰分"],
            ["median K", round(est["median"], 4), "稳健中心(建议以它为准)"],
            ["IQR", "[%.4f, %.4f]" % (est["q1"], est["q3"]), "四分位距,反映分散度"],
            ["std", round(est["std"], 4), ""],
        ]
    rows += [
        ["经验常数对照", "K_PREDICT=%.3f / 专家表等价 1/gain=%.4f"
         % (K_PREDICT, 1.0 / EXPERT_ADJUST["gain"]), "K 暂未参与建议计算,仅展示与标定"],
        ["过滤阈值", "|Δρ|≥%.3f, |ΔA|≥%.2f, lag∈[%d,%d]min, K∈(%.3f,%.3f)"
         % (DRHO_MIN, DA_MIN, LAG_MIN, LAG_MAX, K_MIN, K_MAX), "见 app/services/k_calibration.py"],
    ]
    for ln in concl:
        rows.append(["结论", ln, ""])
    for r in rows:
        ws.append(r)
    style_head(ws, [18, 58, 52])
    ws.column_dimensions["B"].width = 58

    ws2 = wb.create_sheet("逐条明细")
    head2 = ["决策时间", "触发", "方案", "ρ旧", "ρ新", "Δρ建议", "出策K", "重介灰分", "总灰分",
             "带煤量", "原煤灰分", "系统A", "系统B", "工作面", "精磁尾液位", "实测密度",
             "响应状态", "滞后(分)", "响应来源", "Δρ实测", "ΔA实测", "K_i", "是否采信", "备注"]
    ws2.append(head2)
    for p in pts:
        ws2.append([
            p["ts"], p["trigger"], "重介版" if p["scheme"] == "heavy" else "总灰分版",
            p["rhoCur"], p["rhoNew"], p["deltaRhoSuggested"], p["kUsed"], p["heavyAsh"], p["totalAsh"],
            p["coalAmount"], p["rawAsh"], p["sysA"], p["sysB"], p["miningFace"], p["levelTail"],
            p["densityActual"], p["respStatus"], p["lagMin"], p["respSource"],
            p["dRhoActual"], p["dAActual"],
            round(p["k"], 4) if p["k"] is not None else None,
            "是" if p["eligible"] else "否", p["reason"],
        ])
    style_head(ws2, [19, 11, 10, 8, 8, 9, 8, 9, 8, 9, 9, 7, 7, 10, 11, 9, 10, 9, 12, 9, 9, 9, 9, 30])

    ws3 = wb.create_sheet("分组估计")
    ws3.append(["分组轴", "组", "n", "mean", "median", "q1", "q3", "std"])
    for axis, m in groups.items():
        for lab, st in m.items():
            ws3.append([axis, lab, st["n"], round(st["mean"], 4), round(st["median"], 4),
                        round(st["q1"], 4), round(st["q3"], 4), round(st["std"], 4)])
    style_head(ws3, [14, 22, 6, 10, 10, 10, 10, 10])

    ws4 = wb.create_sheet("口径说明")
    notes = [
        ["字段/概念", "说明"],
        ["采信点", "通过全部过滤的决策 = 一个迷你阶跃实验点:K_i = Δρ实测 / ΔA实测(g/cm³ 每 % 灰分)。"],
        ["响应量口径", "只取决策后第一条「人工化验」(采样记录 / 在线仪表手动录入);不用在线自动值——"
                     "502 在线是被控量,闭环下 ΔA≈0,K 不可辨识(densityGainK 恒 valid=false 的原因)。"],
        ["待补记", "决策未满 45 分钟(过程到位+化验周期),或尚无人化验;下次打开页面自动补记。"],
        ["作废", "响应窗口内又发生了新的密度决策,累积量无法归属本条——不可当阶跃点用。"],
        ["过滤阈值", "|Δρ|<%.3f 视为无有效阶跃;|ΔA|<%.2f 视为弱信号/化验噪声;滞后须在 [%d,%d] 分钟;"
                   "K 物理界 (%.3f, %.3f)。" % (DRHO_MIN, DA_MIN, LAG_MIN, LAG_MAX, K_MIN, K_MAX)],
        ["与经验常数", "K_PREDICT=%.3f 为历史经验展示常数;专家表等价 1/gain=%.4f。K 目前不参与建议密度计算。"
         % (K_PREDICT, 1.0 / EXPERT_ADJUST["gain"])],
        ["最小样本", "分组估计只在 n≥3 时输出;总体估计 n<5 时仅作观测。"],
        ["生成方式", "backend/scripts/analyze_k_calibration.py(只读;现场可随时重跑,数据随决策日志滚动更新)"],
    ]
    for r in notes:
        ws4.append(r)
    style_head(ws4, [16, 96])

    out = Path(args.out) if args.out else DEFAULT_OUT
    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(str(out))
    print("-" * 72)
    print("报告已生成:", out)


if __name__ == "__main__":
    main()
