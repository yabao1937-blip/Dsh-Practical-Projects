"""Render saved offline results with matplotlib. No training or database access."""
from __future__ import annotations
import argparse
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
from matplotlib import font_manager
import matplotlib.pyplot as plt
import numpy as np

NAMES = {"mean": "开发集均值", "empirical_raw": "经验直线", "empirical_clipped": "经验截断曲线",
         "frozen_expert8": "冻结 GPT", "fixed_slope_offset": "固定斜率，仅调截距",
         "free_level": "自由液位直线", "free_level_coal": "自由液位＋入洗量", "robust_level": "稳健液位直线"}
COLORS = {"empirical_raw": "#c77a22", "frozen_expert8": "#8c6bb1", "free_level": "#1469bd", "robust_level": "#159581"}


def draw(result, folder):
    chinese = Path("C:/Windows/Fonts/msyh.ttc")
    if chinese.exists():
        font_manager.fontManager.addfont(str(chinese))
        plt.rcParams["font.family"] = font_manager.FontProperties(fname=str(chinese)).get_name()
    plt.rcParams.update({"axes.unicode_minus": False, "font.size": 11, "axes.spines.top": False,
                         "axes.spines.right": False, "axes.titlepad": 14, "figure.facecolor": "#f8fafc"})
    group = "systems3_strict"
    g = result["groups"][group]
    rr = [r for r in result["predictions"] if r["group"] == group]
    training = [r for r in rr if r["phase"] == "training_fit" and r["model"] == "free_level"]
    tail = [r for r in rr if r["phase"] == "tail_strict" and r["model"] == "free_level"]
    fig, axes = plt.subplots(2, 2, figsize=(16, 11), constrained_layout=True)
    ax = axes[0, 0]
    dates = sorted({r["date"] for r in training})
    ax.scatter([r["level"] for r in training], [r["actual"] for r in training],
               c=[dates.index(r["date"]) for r in training], cmap="viridis", s=48, edgecolors="white", label="开发化验（颜色随日期变化）")
    ax.scatter([r["level"] for r in tail], [r["actual"] for r in tail], marker="^", s=100,
               facecolors="none", edgecolors="#dc3948", linewidths=1.8, label="时间尾段化验（3条）")
    x = np.linspace(g["range"]["level"][0], g["range"]["level"][1], 150)
    ax.plot(x, 30.5 - .3 * x, color=COLORS["empirical_raw"], lw=2.1, label="经验：30.5 − 0.3 × 液位")
    for name in ("free_level", "robust_level"):
        c = g["models"][name]["coef"]
        ax.plot(x, c[0] + c[1] * (x - 50), color=COLORS[name], lw=2.1,
                linestyle="--" if name == "robust_level" else "-", label=f"{NAMES[name]}：斜率 {c[1]:+.3f}")
    for r in training:
        if r["id"] == 63:
            ax.annotate("31.42%：主分析保留", (r["level"], r["actual"]), xytext=(7, -18), textcoords="offset points", fontsize=10)
    ax.set(xlabel="精磁尾液位（%，按采样前90秒对齐）", ylabel="粗精煤泥化验灰分（%）", title="三系统：允许液位斜率和截距自由拟合")
    ax.legend(fontsize=9, loc="upper left")
    ax.grid(alpha=.15)
    ax = axes[0, 1]
    mm = [m for m in result["metrics"] if m["group"] == group and m["phase"] == "rolling" and m["model"] != "empirical_clipped"]
    ax.barh([NAMES[m["model"]] for m in mm], [m["date_mae"] for m in mm],
            color=[COLORS.get(m["model"], "#94a3b8") for m in mm], height=.65)
    for i, m in enumerate(mm):
        ax.text(m["date_mae"] + .025, i, f'{m["date_mae"]:.2f}', va="center", fontsize=11)
    ax.invert_yaxis()
    ax.set(xlabel="日期等权平均绝对误差（灰分百分点，越小越好）", title=f'开发期向前按日验证：{mm[0]["n"]}条／{mm[0]["dates"]}天')
    ax.set_xlim(0, max(m["date_mae"] for m in mm) * 1.18)
    ax.grid(axis="x", alpha=.15)
    ax = axes[1, 0]
    xx = np.arange(len(tail))
    ax.plot(xx, [r["actual"] for r in tail], "ko", markersize=9, label="化验实值", zorder=10)
    for j, name in enumerate(COLORS):
        rows = [r for r in rr if r["phase"] == "tail_strict" and r["model"] == name]
        ax.scatter(xx + (j - 1.5) * .07, [r["prediction"] for r in rows], color=COLORS[name], marker="x", s=65, label=NAMES[name])
    ax.set_xticks(xx, [r["date"][5:] + "\nL=" + f'{r["level"]:.1f}' for r in tail])
    ax.set(ylabel="粗精煤泥灰分（%）", title="预留时间尾段：仅3条，不能据此上线")
    ax.grid(axis="y", alpha=.15)
    ax.legend(fontsize=9, ncol=2, loc="best")
    ax = axes[1, 1]
    d = g["diagnostics"]
    labels = ["普通自由直线", "日期等权拟合", "稳健拟合", "仅诊断：去掉31.42", "控制入洗量", "同日期内关系"]
    values = [d["ols"]["coef"][1], d["equal_date_ols"]["coef"][1], g["models"]["robust_level"]["coef"][1],
              d["without_id63"]["coef"][1], g["models"]["free_level_coal"]["coef"][1], d["within_date_slope"]]
    ax.scatter(values, range(len(values)), s=70, color="#1469bd", zorder=3)
    ci = d["date_bootstrap"]["slope_percentiles_2_5_50_97_5"]
    ax.hlines(0, ci[0], ci[2], color="#1469bd", linewidth=2, label="自由直线：按日期重抽样95%范围")
    ax.axvline(-.3, color="#c77a22", linestyle="--", label="经验斜率 −0.3")
    ax.axvline(0, color="#64748b", linewidth=.7)
    ax.set_yticks(range(len(labels)), labels)
    ax.invert_yaxis()
    ax.set(xlabel="液位每增加1个百分点，灰分变化的百分点", title="斜率稳定性：不同处理是否改变结论")
    ax.legend(fontsize=9, loc="best")
    ax.grid(axis="x", alpha=.15)
    fig.suptitle("液位与粗精煤泥灰分：离线对比实验\n固定90秒滞后 · 三系统严格配对 · 生产模型保持原样", fontsize=19)
    fig.savefig(folder / "level-relationship.png", dpi=180)
    fig.savefig(folder / "level-relationship.svg")
    plt.close(fig)
    # Full same-assay comparison table, including small two-system and approximation groups.
    fig, axes = plt.subplots(1, 2, figsize=(14, 5), constrained_layout=True)
    for ax, count in zip(axes, (3, 2)):
        group = f"systems{count}_strict"
        rows = [r for r in result["predictions"] if r["group"] == group and r["phase"] == "training_fit" and r["model"] == "free_level"]
        ax.scatter([r["level"] for r in rows], [r["actual"] for r in rows], c="#1469bd", s=50, alpha=.8)
        bounds = result["groups"][group]["range"]["level"]
        x = np.linspace(*bounds, 150)
        ax.plot(x, 30.5 - .3 * x, color="#c77a22", label="原经验直线")
        c = result["groups"][group]["models"]["free_level"]["coef"]
        ax.plot(x, c[0] + c[1] * (x - 50), color="#1469bd", label=f"自由直线，斜率{c[1]:+.3f}")
        if count == 2:
            ax.plot(x, np.where(x >= 30, 14, 15), color="#159581", label="现用两系统查表")
        ax.set(title=f"{count}系统严格开发样本：{len(rows)}条", xlabel="液位（%）", ylabel="化验灰分（%）")
        ax.legend(fontsize=9)
        ax.grid(alpha=.15)
    fig.savefig(folder / "system-groups.png", dpi=180)
    plt.close(fig)
    (folder / "plot-runtime.json").write_text(json.dumps({"matplotlib": matplotlib.__version__, "numpy": np.__version__}, indent=2), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("folder", type=Path)
    args = parser.parse_args()
    draw(json.loads((args.folder / "result.json").read_text(encoding="utf-8")), args.folder)
    print(args.folder / "level-relationship.png")
