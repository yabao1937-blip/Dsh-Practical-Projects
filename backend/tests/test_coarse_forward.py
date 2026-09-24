# -*- coding: utf-8 -*-
"""DS「真向前」验证模块的单元测试（合成数据，不依赖生产库）。

覆盖三件容易悄悄坏掉的事：
  1. store 口径记录（时间戳键是 `timestamp` 而不是 `ts`）也必须能算 —— 2026-09-24 实测：
     训练编排传的就是 store 行，只认 `ts` 会让方向报告永远返回"样本不足"，而且不报错。
  2. 训练范围之后有剩余数据时 → leftover 口径（真向前）；训练范围覆盖全部时 → tail 留尾参考，
     且必须用 note 说明"这不是部署模型的向前成绩"。
  3. 训练编排要把 forward/direction 一起带出来，并在没有向前窗口时给出可读的理由。
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.coarse_direction import FEATS, direction_report        # noqa: E402
from app.services.coarse_forward import evaluate_window, forward_report, report_cached  # noqa: E402
from app.services.training import train_coarse_model                     # noqa: E402

STATE = [20260924]


def _rand():
    STATE[0] = (1103515245 * STATE[0] + 12345) % (1 << 31)
    return STATE[0] / (1 << 31)


def _series(n=140, start="2026-06-01", hours=6, key="ts", phi=0.3, center=14.0):
    """灰分均值回复 + 因子带真实信号（level 与灰分负相关），时间戳键可选。"""
    t0 = datetime.strptime(start, "%Y-%m-%d")
    y, out = center, []
    for i in range(n):
        y = center + phi * (y - center) + (_rand() - 0.5) * 3.0
        lvl = 55.0 - (y - center) * 6.0 + (_rand() - 0.5) * 4.0
        rec = {key: (t0 + timedelta(hours=hours * i)).strftime("%Y-%m-%d %H:%M:%S"),
               "ash_content": round(y, 2), "raw_ash": round(40 + _rand() * 6, 2),
               "coal_amount": round(600 + _rand() * 300, 1), "level": round(lvl, 1),
               "moisture": round(27 + _rand() * 2, 2)}
        for f in FEATS:
            rec.setdefault(f, int(_rand() > .5) if f in ("sysA", "sysB", "sys401", "sys402",
                                                         "desliming473", "desliming474", "is_stoppage")
                           else round(_rand(), 3))
        out.append(rec)
    return out


def test_direction_accepts_store_style_timestamp_key():
    """store 行用 `timestamp` 键：必须正常给出结论（回归：曾因只读 `ts` 恒返回"样本不足"）。"""
    rows = _series(n=140, start="2026-06-01", key="timestamp")
    rep = direction_report(rows, split_ts="2026-07-01")
    assert rep["n"] > 5, rep
    assert rep["hit"] is not None and "样本不足" not in rep["note"], rep


def test_direction_split_ts_partitions_by_time():
    rows = _series(n=140, start="2026-06-01", key="ts")
    cut = rows[90]["ts"]
    rep = direction_report(rows, split_ts=cut)
    assert rep["usable"] is True, rep
    # 切点之前的样本不参与检验：检验对数不可能超过切点之后的样本数
    assert rep["n"] <= len(rows) - 90, rep
    assert rep["inertia"] is not None and rep["inertia"] < rep["baseline"] + 0.2, rep


def test_forward_windows_leftover_and_tail():
    # 序列跨到 8 月：range=jun_jul 只覆盖 6-7 月 → 8 月那一段就是"训练范围之后"的真向前窗口
    rows = _series(n=300, start="2026-06-01", key="timestamp")
    rep = forward_report(rows, "jun_jul")
    assert rep["windows"], rep
    w = rep["windows"][0]
    assert w["kind"] == "leftover" and w["usable"] is True, w
    assert w["testFrom"] > w["trainEnd"], w
    assert "取训练均值" in w["baselines"], w
    # 全部数据都用于训练 → 没有剩余数据，退化为留尾参考，并明确说明
    rep_all = forward_report(rows, "all")
    assert rep_all["windows"] and rep_all["windows"][0]["kind"] == "tail", rep_all
    assert "留尾" in rep_all["note"], rep_all


def test_forward_report_cached_does_not_recompute():
    rows = _series(n=140, start="2026-06-01", key="timestamp")
    first = report_cached(rows, "jun_jul", None, ("mlr",))
    second = report_cached(rows, "jun_jul", None, ("mlr",))
    assert first["cached"] is False
    assert second["cached"] is True
    assert second["windows"][0]["models"] == first["windows"][0]["models"]
    assert evaluate_window(rows, "2026-07-01", None, kinds=("mlr",))["models"]


def test_training_result_carries_forward_and_direction():
    rows = _series(n=300, start="2026-06-01", key="timestamp")
    res = train_coarse_model(rows, "jun_jul")
    assert res is not None
    fwd = res["forward"]
    assert fwd["usable"] is True and fwd["testN"] > 5, fwd
    assert set(fwd["models"]) == {"mlr", "pls"}, fwd
    assert "取训练均值" in fwd["baselines"] and fwd["verdict"]["aheadOfBaseline"] in (True, False)
    assert res["direction"]["n"] > 5 and res["direction"]["testFrom"] == fwd["testFrom"], res["direction"]
    # 方向"最近判断"清单要随训练结果一起带出来（页面用于观察，用户 Q6=B）
    assert res["direction"]["recent"] and all("ok" in x for x in res["direction"]["recent"])


def test_training_result_states_reason_when_no_forward_window():
    rows = _series(n=60, start="2026-06-01", key="timestamp")
    res = train_coarse_model(rows, "all")
    assert res is not None
    assert res["forward"]["usable"] is False
    assert "真向前检验" in res["forward"]["note"], res["forward"]
    assert res["direction"]["usable"] is False and res["direction"]["hit"] is None, res["direction"]
