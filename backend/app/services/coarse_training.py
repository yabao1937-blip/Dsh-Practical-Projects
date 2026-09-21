"""粗灰时间分组训练；镜像 App._coarse*，保留十列系数以兼容历史模型。

专家假设作为候选（仅三因子 / 其他因子惩罚 ×4），不强制系数方向或排名。
按完整日期划分 expanding-window；嵌套外层只评估，内层选参数和算法。
"""
import datetime
import math
import re

from .modeling import MLR_FEATURES, predict_coarse_ash
from .training import (_col_means_std, _impute, _is_num, _mean, _metrics,
                       _pls_core, _solve, _standardize, _std, filter_train_rows)

EXPERT = (0, 6, 7)


def prepare_rows(records, range_="all"):
    # 同时间跨系统重复导入仅算一条；冲突保留最后一条，与重新导入覆盖一致。
    unique = {}
    for r in filter_train_rows(records, range_):
        ts = str(r.get("timestamp") or "")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", ts):
            continue
        try:
            datetime.datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        if r["ash_content"] <= 40:
            unique[ts] = r
    return [unique[t] for t in sorted(unique)]


def aggregate_daily(records):
    groups = {}
    for r in prepare_rows(records):
        groups.setdefault(r["timestamp"][:10], []).append(r)
    out = []
    for day, rows in groups.items():
        row = {"day": day, "timestamp": day + " 12:00:00", "n": len(rows), "records": rows}
        for f in MLR_FEATURES + ["ash_content", "moisture"]:
            values = [r[f] for r in rows if _is_num(r.get(f))]
            row[f] = _mean(values) if values else None
        out.append(row)
    return out


def folds(rows, outer=False):
    days = sorted({r["timestamp"][:10] for r in rows})
    cuts = [math.floor(len(days) * f) for f in ([.7, .85, 1] if outer else [.5, .7, .85, 1])]
    result = []
    for a, b in zip(cuts, cuts[1:]):
        if a < 4 or a >= b:
            continue
        s = next(i for i, r in enumerate(rows) if r["timestamp"][:10] == days[a])
        e = len(rows) if b == len(days) else next(i for i, r in enumerate(rows) if r["timestamp"][:10] == days[b])
        if s >= 6:
            result.append((s, e))
    return result


def candidates(kind):
    for subset in ("all", "expert"):
        if kind == "mlr":
            for alpha in (.001, .01, .1, 1, 10):
                for penalty in ((1, 4) if subset == "all" else (1,)):
                    yield {"subset": subset, "alpha": alpha, "penalty": penalty}
        else:
            for a in range(1, (4 if subset == "all" else 3) + 1):
                yield {"subset": subset, "A": a, "penalty": 1}


def fit(rows, kind, config, tol=.8):
    X = [[r.get(f) for f in MLR_FEATURES] for r in rows]
    X, impute = _impute(X)
    means, stds = _col_means_std(X)
    Z = _standardize(X, means, stds)
    y = [r["ash_content"] for r in rows]
    ym = _mean(y)
    # 常量不能外推为已学习因素；日级开关值保留开启比例，不做多数表决。
    active = [j for j in range(len(MLR_FEATURES))
              if (config["subset"] == "all" or j in EXPERT)
              and any(abs(r[j] - X[0][j]) > 1e-9 for r in X)]
    beta = [0.0] * len(MLR_FEATURES)
    if active:
        z = [[r[j] for j in active] for r in Z]
        if kind == "mlr":
            A = [[sum(r[p] * r[q] for r in z) for q in range(len(active))] for p in range(len(active))]
            b = [sum(r[p] * (y[i] - ym) for i, r in enumerate(z)) for p in range(len(active))]
            for p, j in enumerate(active):
                A[p][p] += config["alpha"] * len(rows) * (1 if j in EXPERT else config["penalty"])
            solution = _solve(A, b)
        else:
            solution = _pls_core(z, [v - ym for v in y], min(config["A"], len(active)))["Bstd"]
        for j, v in zip(active, solution):
            beta[j] = v
    coefs = [b / s for b, s in zip(beta, stds)]
    intercept = ym - sum(c * m for c, m in zip(coefs, means))
    yh = [intercept + sum(c * v for c, v in zip(coefs, r)) for r in X]
    sy = _std(y)
    return {"type": kind, "version": 2, "feature_names": MLR_FEATURES,
            "intercept": intercept, "coefs": coefs, "means": means, "stds": stds,
            "imputeMeans": impute, "stdCoef": [v / sy if sy else 0 for v in beta],
            "drop": [j for j in range(len(beta)) if j not in active],
            "n": len(rows), "k": len(active), "config": config,
            "lambda": config.get("alpha", 0) * len(rows), "A": config.get("A"),
            "yhat": yh, "metrics": _metrics(y, yh, len(active), tol)}


def select(rows, kind, tol=.8, subset=None):
    blocks = folds(rows)
    if not blocks:
        return None
    best = None
    for config in candidates(kind):
        if subset is not None and config["subset"] != subset:
            continue
        actual, pred = [], []
        for s, e in blocks:
            m = fit(rows[:s], kind, config, tol)
            actual.extend(r["ash_content"] for r in rows[s:e])
            pred.extend(predict_coarse_ash(r, m) for r in rows[s:e])
        score = _metrics(actual, pred, 0, tol)
        if best is None or score["rmse"] < best["score"]["rmse"] - 1e-10:
            best = {"config": config, "score": score}
    return best


def _validation(actual, pred, baseline, blocks, tol):
    if not actual:
        return None
    m = _metrics(actual, pred, 0, tol)
    bm = _metrics(actual, baseline, 0, tol)
    return {**m, "n": len(actual), "folds": blocks, "baselineMae": bm["mae"],
            "baselineRmse": bm["rmse"], "beatsBaseline": m["rmse"] < bm["rmse"],
            "method": "nested-day-walk-forward"}


def train_models(records, range_="all", tol=.8):
    rows = prepare_rows(records, range_)
    if len(rows) < 12 or len({r["timestamp"][:10] for r in rows}) < 8:
        return None
    selections = {k: select(rows, k, tol) for k in ("mlr", "pls")}
    production = min(selections, key=lambda k: selections[k]["score"]["rmse"])
    models = {k: fit(rows, k, selections[k]["config"], tol) for k in selections}
    actual, baseline, blocks = [], [], []
    predictions = {k: [] for k in ("mlr", "pls", "production")}
    for s, e in folds(rows, outer=True):
        tr, te = rows[:s], rows[s:e]
        selected = {k: select(tr, k, tol) for k in models}
        if any(v is None for v in selected.values()):
            continue
        chosen = min(selected, key=lambda k: selected[k]["score"]["rmse"])
        fold_pred = {k: [predict_coarse_ash(r, fit_model) for r in te]
                     for k in models for fit_model in [fit(tr, k, selected[k]["config"], tol)]}
        for k in models:
            predictions[k].extend(fold_pred[k])
        predictions["production"].extend(fold_pred[chosen])
        actual.extend(r["ash_content"] for r in te)
        baseline.extend([_mean([r["ash_content"] for r in tr])] * len(te))
        blocks.append({"trainEnd": tr[-1]["timestamp"], "testStart": te[0]["timestamp"],
                       "testEnd": te[-1]["timestamp"], "n": len(te), "production": chosen})
    pipeline = _validation(actual, predictions["production"], baseline, blocks, tol)
    for k, m in models.items():
        v = _validation(actual, predictions[k], baseline, blocks, tol)
        m["metrics"].update({"q2": selections[k]["score"]["r2"],
                             "q2Time": v["r2"] if v else None, "validation": v,
                             "selectionCv": selections[k]["score"], "config": m["config"],
                             "version": 2, "pipelineValidation": pipeline})
    history = {"n": len(rows), "range": range_, "tolerance": tol, "production": production,
               **{k: {**m["metrics"], "A": m.get("A")} for k, m in models.items()}}
    return {**models, "production": production, "n": len(rows), "range": range_,
            "tolerance": tol, "history": history}
