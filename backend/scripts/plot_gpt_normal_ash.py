"""Plot saved exploratory scores and export a one-row-per-assay comparison."""
import argparse
import csv
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import matplotlib
matplotlib.use("Agg")
from matplotlib import font_manager
import matplotlib.pyplot as plt
import numpy as np


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("folder", type=Path)
    args = parser.parse_args()
    result = json.loads((args.folder / "result.json").read_text(encoding="utf-8"))
    group, chosen = "systems3_strict_preceding_day", result["selected_algorithm"]
    rr = [r for r in result["predictions"] if r["group"] == group and r["quality"] == "strict"]
    selected = [r for r in rr if r["model"] == chosen]
    baseline = {r["id"]: r for r in rr if r["model"] == "frozen_expert8"}
    chinese = Path("C:/Windows/Fonts/msyh.ttc")
    if chinese.exists():
        font_manager.fontManager.addfont(str(chinese))
        plt.rcParams["font.family"] = font_manager.FontProperties(fname=str(chinese)).get_name()
    plt.rcParams.update({"axes.unicode_minus": False, "font.size": 11, "axes.spines.top": False,
                         "axes.spines.right": False, "figure.facecolor": "#f8fafc"})
    fig, axes = plt.subplots(2, 1, figsize=(14, 10), constrained_layout=True, gridspec_kw={"height_ratios": [1.25, 1]})
    x = np.arange(len(selected))
    actual = np.array([r["actual"] for r in selected])
    predicted = np.array([r["prediction"] for r in selected])
    ax = axes[0]
    ax.fill_between(x, actual - 1, actual + 1, color="#dceee5", alpha=.9, label="采样实值 ±1 的目标区间")
    ax.plot(x, actual, "o-", color="#26334c", lw=1.2, label="采样实值")
    ax.plot(x, predicted, "s--", color="#2472c8", lw=1.5, label="本轮候选：仅使用此前可用化验")
    boundary = sum(r["phase"] == "selection" for r in selected) - .5
    ax.axvline(boundary, color="#c47e2b", lw=1.5, linestyle=":")
    ax.set_xticks(x, [r["date"][5:] + "\n" + str(r["id"]) for r in selected], fontsize=9)
    ax.set(ylabel="粗精煤泥灰分（%）", xlabel="采样日期 / 样本编号（按采样顺序排列）",
           title="三系统严格配对：21条历史逐时点预测；虚线右侧为后段回放")
    ax.legend(fontsize=10, loc="upper right")
    ax.grid(axis="y", alpha=.18)
    ax = axes[1]
    phases = ["selection", "later_replay", "all_replay"]
    names = ["早段选型\n10/20—10/25", "后段回放\n10/26—10/31", "合计\n已查看的历史数据"]
    for j, (model, label, color) in enumerate([("frozen_expert8", "旧GPT", "#94a3b8"), (chosen, "本轮候选", "#2472c8")]):
        ms = [next(m for m in result["metrics"] if m["group"] == group and m["quality"] == "strict"
                   and m["subset"] == "normal" and m["phase"] == phase and m["model"] == model) for phase in phases]
        xx = np.arange(3) + (j - .5) * .32
        bars = ax.bar(xx, [m["hit1_rate"] * 100 for m in ms], width=.3, label=label, color=color)
        for bar, m in zip(bars, ms):
            ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 2,
                    f'{m["hit1"]}/{m["n"]}\n{m["hit1_rate"]:.1%}', ha="center", fontsize=11)
    ax.set_xticks(np.arange(3), names)
    ax.set(ylim=(0, 100), ylabel="绝对误差不超过1个百分点的比例（%）",
           title="已有改善，但后段仅2/6：尚不能称稳定达到大多数样本准确")
    ax.legend(fontsize=10)
    ax.grid(axis="y", alpha=.18)
    fig.suptitle("常规粗精煤泥样本：±1百分点目标\n共同异常规则；验证21条全部保留；生产模型未替换", fontsize=18)
    fig.savefig(args.folder / "normal-ash-comparison.png", dpi=180)
    fig.savefig(args.folder / "normal-ash-comparison.svg")
    plt.close(fig)
    with (args.folder / "sample-comparison.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["样本ID", "采样时间（北京时间）", "阶段", "液位", "入洗量", "采样灰分", "候选预测", "旧GPT预测", "候选误差", "误差不超过1", "共同规则下常规样本"])
        for r in selected:
            local_time = datetime.fromisoformat(r["observed_at"]).astimezone(timezone(timedelta(hours=8))).isoformat(timespec="seconds")
            writer.writerow([r["id"], local_time, r["phase"], r["level"], r["coal"], r["actual"],
                             r["prediction"], baseline[r["id"]]["prediction"], r["error"], abs(r["error"]) <= 1 + 1e-12, r["normal"]])
    print(args.folder / "normal-ash-comparison.png")


if __name__ == "__main__":
    main()
