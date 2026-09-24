# -*- coding: utf-8 -*-
"""DS 方向判断模块的单元测试：既验"有信号时敢认"，也验"没信号时拒认"。

用的是合成序列（不依赖数据库/生产数据）：
  · 均值回复序列（AR(1)，φ=0.3）→ 方向应当可用（门控认）
  · 随机游走（φ=1，对称增量）→ 方向不可用（门控必须拒绝）
第二条是关键：没有它，门控可能只会一路说"可用"。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.coarse_direction import FEATS, direction_report  # noqa: E402

STATE = [12345]


def _rand():
    STATE[0] = (1103515245 * STATE[0] + 12345) % (1 << 31)
    return STATE[0] / (1 << 31)


def _series(phi, n=260, center=14.0, start="2026-06-01"):
    """生成 n 条记录：灰分按 y_t = c + φ(y_{t-1}-c) + ε 演化，10 因子用噪声填充。"""
    from datetime import datetime, timedelta

    t0 = datetime.strptime(start, "%Y-%m-%d")
    y = center
    out = []
    for i in range(n):
        eps = (_rand() - 0.5) * 4.0          # ±2% 噪声
        y = center + phi * (y - center) + eps
        ts = (t0 + timedelta(hours=4 * i)).strftime("%Y-%m-%d %H:%M:%S")
        rec = {"ts": ts, "ash_content": round(y, 2)}
        for f in FEATS:
            rec[f] = round(_rand(), 3) if f not in ("sysA", "sysB", "sys401", "sys402",
                                                    "desliming473", "desliming474", "is_stoppage") else int(_rand() > .5)
        out.append(rec)
    return out


def test_direction_usable_on_mean_reverting_series():
    rows = _series(phi=0.3)
    rep = direction_report(rows, ["2026-06"], ["2026-07"])
    assert rep["n"] >= 20
    assert rep["hit"] > rep["baseline"], rep
    assert rep["usable"] is True, rep


def test_direction_refused_on_random_walk():
    rows = _series(phi=1.0)
    rep = direction_report(rows, ["2026-06"], ["2026-07"])
    assert rep["usable"] is False, rep
    # 拒绝时也必须给出理由，不能只是 usable=False 了事
    assert "拒绝" in rep["note"] or "未" in rep["note"], rep


def test_report_declares_gating_and_sample_gate():
    rep = direction_report(_series(phi=0.3, n=40), ["2026-06"], ["2026-07"])
    assert "gating" in rep and "时间间隔" in rep["gating"]      # 声明不使用采样节奏特征
    assert rep["usable"] in (True, False)
    assert rep["recent"] == []                                  # 样本不足时不给"最近判断"清单


def test_recent_predictions_are_listed_for_observation():
    """用户 Q6 选 B（方向先只展示观察）→ 报告要给出"最近几次判断 vs 实际"，供页面显示。"""
    rep = direction_report(_series(phi=0.3, n=260), ["2026-06"], ["2026-07"])
    assert rep["usable"] is True, rep
    rec = rep["recent"]
    assert 5 <= len(rec) <= 20, rec
    assert all(set(x) == {"t", "pred", "actual", "ok"} for x in rec), rec
    assert all(x["pred"] in ("涨", "跌") and x["actual"] in ("涨", "跌") for x in rec), rec
    assert all(x["ok"] == (x["pred"] == x["actual"]) for x in rec), rec
    assert 0.0 <= rep["recentHit"] <= 1.0, rep["recentHit"]
    # 清单必须按时间升序（页面从右往左读"最近"，顺序反了会误导）
    assert rec == sorted(rec, key=lambda x: x["t"]), rec[:3]
