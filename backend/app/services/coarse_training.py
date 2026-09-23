"""粗灰时间分组训练；镜像 App._coarse*，保留十列系数以兼容历史模型。

采样保留专家候选；日级比较普通/稳健回归。均不强制系数方向或排名。
内层同时参考完整日期向前验证及留整日验证；嵌套外层只用过去训练、评估后续日期。
"""
import datetime
import math
import re

from .modeling import MLR_FEATURES, predict_coarse_ash
from .training import (_col_means_std, _impute, _is_num, _mean, _metrics,
                       _pls_core, _solve, _standardize, _std, filter_train_rows)

EXPERT = (0, 6, 7)
TRAINING_REVISION = "gpt-coverage-robust-20260922"


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


def candidates(kind, daily=False):
    if daily and kind == "mlr":
        # 日均只有少量日期：普通/Huber 岭回归共同竞争，不预设异常日期应被删除。
        for alpha in (.03, .1, .3, 1):
            for robust in (False, True):
                yield {"subset": "all", "alpha": alpha, "penalty": 1, "robust": robust}
        return
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
    offset = ym
    if active:
        z = [[r[j] for j in active] for r in Z]
        if kind == "mlr" and config.get("robust"):
            design = [[1.0, *r] for r in z]
            weights = [1.0] * len(rows)
            for _ in range(8):
                size = len(active) + 1
                A = [[sum(w * r[p] * r[q] for w, r in zip(weights, design))
                      for q in range(size)] for p in range(size)]
                b = [sum(w * r[p] * target for w, r, target in zip(weights, design, y))
                     for p in range(size)]
                for p in range(1, size):
                    A[p][p] += config["alpha"] * len(rows)
                fitted = _solve(A, b)
                residuals = [target - sum(v * coef for v, coef in zip(r, fitted))
                             for r, target in zip(design, y)]
                # 固定1.5灰分百分点的Huber拐点；不随页面合格容差调参。
                weights = [min(1.0, 1.5 / max(abs(v), 1e-9)) for v in residuals]
            offset, solution = fitted[0], fitted[1:]
        elif kind == "mlr":
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
    intercept = offset - sum(c * m for c, m in zip(coefs, means))
    yh = [intercept + sum(c * v for c, v in zip(coefs, r)) for r in X]
    sy = _std(y)
    return {"type": kind, "version": 2, "feature_names": MLR_FEATURES,
            "intercept": intercept, "coefs": coefs, "means": means, "stds": stds,
            "imputeMeans": impute, "stdCoef": [v / sy if sy else 0 for v in beta],
            "drop": [j for j in range(len(beta)) if j not in active],
            "n": len(rows), "k": len(active), "config": config,
            "lambda": config.get("alpha", 0) * len(rows), "A": config.get("A"),
            "yhat": yh, "metrics": _metrics(y, yh, len(active), tol),
            "inputPolicy": "clip-training-range",
            "inputBounds": [[min(r[j] for r in X), max(r[j] for r in X)]
                            for j in range(len(MLR_FEATURES))]}


def select(rows, kind, tol=.8, subset=None):
    blocks = folds(rows)
    if not blocks:
        return None
    # 整天一起留出，避免同日采样进入训练与验证两侧。
    # 所有这些日期均在当前训练窗口内；外层后续日期绝不参与选参。
    days = sorted({r["timestamp"][:10] for r in rows})
    day_splits = [([r for r in rows if r["timestamp"][:10] != day],
                   [r for r in rows if r["timestamp"][:10] == day]) for day in days]
    daily = all(r.get("day") for r in rows)
    best = None
    for config in candidates(kind, daily):
        if subset is not None and config["subset"] != subset:
            continue
        actual, pred = [], []
        for s, e in blocks:
            m = fit(rows[:s], kind, config, tol)
            actual.extend(r["ash_content"] for r in rows[s:e])
            pred.extend(predict_coarse_ash(r, m) for r in rows[s:e])
        score = _metrics(actual, pred, 0, tol)
        grouped_actual, grouped_pred = [], []
        for tr, te in day_splits:
            m = fit(tr, kind, config, tol)
            grouped_actual.extend(r["ash_content"] for r in te)
            grouped_pred.extend(predict_coarse_ash(r, m) for r in te)
        group_cv = {**_metrics(grouped_actual, grouped_pred, 0, tol),
                    "n": len(grouped_actual), "days": len(days), "method": "leave-one-day-out"}
        # 日级优先MAE，采样保留RMSE；不以训练集R²选参。
        # 两项来自同一训练窗口，综合值仅用于选参，不冒充独立预测误差。
        selection_rmse = math.sqrt((score["rmse"] ** 2 + group_cv["rmse"] ** 2) / 2)
        selection_mae = (score["mae"] + group_cv["mae"]) / 2
        objective = selection_mae if daily else selection_rmse
        if best is None or objective < best["objective"] - 1e-10:
            best = {"config": config, "score": score, "groupCv": group_cv,
                    "selectionRmse": selection_rmse, "selectionMae": selection_mae,
                    "objective": objective}
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
    daily = all(r.get("day") for r in rows)
    selection_metric = "mae" if daily else "rmse"
    selections = {k: select(rows, k, tol) for k in ("mlr", "pls")}
    # MLR / PLS 算法之间比较向前时间误差（日级MAE、采样RMSE）。
    production = min(selections, key=lambda k: selections[k]["score"][selection_metric])
    models = {k: fit(rows, k, selections[k]["config"], tol) for k in selections}
    actual, baseline, blocks = [], [], []
    predictions = {k: [] for k in ("mlr", "pls", "production")}
    for s, e in folds(rows, outer=True):
        tr, te = rows[:s], rows[s:e]
        selected = {k: select(tr, k, tol) for k in models}
        if any(v is None for v in selected.values()):
            continue
        chosen = min(selected, key=lambda k: selected[k]["score"][selection_metric])
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
                             "version": 2, "pipelineValidation": pipeline,
                             "trainingRevision": TRAINING_REVISION,
                             "groupCv": selections[k]["groupCv"],
                             "selectionRmse": selections[k]["selectionRmse"],
                             "selectionMae": selections[k]["selectionMae"],
                             "selectionPolicy": {"method": "balanced-time-and-day",
                                                 "timeWeight": .5, "groupWeight": .5,
                                                 "primary": selection_metric},
                             "inputPolicy": "clip-training-range", "inputBounds": m["inputBounds"]})
    history = {"n": len(rows), "range": range_, "tolerance": tol, "production": production,
               **{k: {**m["metrics"], "A": m.get("A")} for k, m in models.items()}}
    return {**models, "production": production, "n": len(rows), "range": range_,
            "tolerance": tol, "history": history}
