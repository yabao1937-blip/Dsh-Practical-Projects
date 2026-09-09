"""粗精煤泥灰分 MLR/PLS 训练 —— sklearn 内层求解器版（对拍 training.py 的逐值移植）。

职责划分（对应评审建议）：
- 预处理（_constCols / z-score ddof=1 / 缺失均值补全）、λ 网格 + hat-LOOCV 选 λ、
  PLS 全局预处理 LOOCV 选 A、生产模型选择 —— 全部复用 training.py 的精确逻辑，
  保证离散决策（drop / λ 网格点 / A / production）与前端 JS 逐位一致；
- 只把「最终系数求解」换成 sklearn：
    MLR  → Ridge(alpha=λ, fit_intercept=True, solver='svd')，喂预标准化 Z + 原始 y；
    PLS  → PLSRegression(n_components=A, scale=False)，喂预标准化 Z + 中心化 y。
- 返回值结构与 training.py 一致，tests 用 1e-9 容差对拍二者（实测差异 ~1e-15）。
"""
import numpy as np
from sklearn.cross_decomposition import PLSRegression
from sklearn.linear_model import Ridge

from .training import (_col_means_std, _const_cols, _js_round, _mean, _metrics,
                       _orchestrate, _standardize, _std, _un_drop, train_mlr, train_pls)


def _drop_cols(X, drop):
    return [[row[j] for j in range(len(row)) if j not in drop] for row in X] if drop else X


def train_mlr_sklearn(X, y, tol=0.8):
    """MLR：λ 选择复用精确移植，最终求解用 sklearn Ridge。"""
    port = train_mlr(X, y, tol)          # 精确 λ/drop/预处理基准
    if port is None:
        return None
    lam = port["lambda"]
    drop = port["drop"]
    Xd = _drop_cols(X, drop)
    n = len(Xd)
    k = len(Xd[0])
    means, stds = _col_means_std(Xd)
    Z = np.array(_standardize(Xd, means, stds), dtype=float)
    yarr = np.array(y, dtype=float)

    ridge = Ridge(alpha=lam, fit_intercept=True, solver="svd")
    ridge.fit(Z, yarr)
    beta_std = np.concatenate([[ridge.intercept_], np.asarray(ridge.coef_, dtype=float)])

    coefs = (beta_std[1:] / np.asarray(stds, dtype=float)).tolist()
    intercept = float(beta_std[0]) - float(np.dot(coefs, means))

    yhat = [intercept + sum(coefs[j] * r[j] for j in range(k)) for r in Xd]
    m = _metrics(y, yhat, k, tol)
    std_y = _std(y)
    std_coef = [(coefs[j] * stds[j] / std_y) if std_y > 0 else 0.0 for j in range(k)]
    m["q2"] = port["metrics"]["q2"]       # LOOCV Q² 由精确移植给出（同一 λ 同一 SSE）
    m["lambda"] = lam

    full = _un_drop({"coefs": coefs, "means": means, "stds": stds, "stdCoef": std_coef}, drop, len(X[0]))
    return {"type": "mlr", "intercept": intercept, "coefs": full["coefs"], "means": full["means"],
            "stds": full["stds"], "stdCoef": full["stdCoef"], "yhat": yhat, "metrics": m,
            "n": n, "k": k, "drop": drop, "lambda": lam}


def train_pls_sklearn(X, y, tol=0.8, amax=None):
    """PLS：A 选择复用精确移植，最终求解用 sklearn PLSRegression。"""
    port = train_pls(X, y, tol, amax)    # 精确 A/drop/预处理基准
    if port is None:
        return None
    A = port["A"]
    drop = port["drop"]
    Xd = _drop_cols(X, drop)
    n = len(Xd)
    k = len(Xd[0])
    means, stds = _col_means_std(Xd)
    Z = np.array(_standardize(Xd, means, stds), dtype=float)
    yarr = np.array(y, dtype=float)
    y_mean = _mean(y)                    # 与移植版同源的均值（不用 np.mean）
    yc = (yarr - y_mean).reshape(-1, 1)

    pls = PLSRegression(n_components=A, scale=False)
    pls.fit(Z, yc)
    # sklearn 1.9 coef_ 形状为 (n_target, n_features)；单响应时 reshape 成 (k,)
    Bstd = np.asarray(pls.coef_, dtype=float).reshape(-1)

    coefs = (Bstd / np.asarray(stds, dtype=float)).tolist()
    intercept = y_mean - float(np.dot(coefs, means))

    yhat = [intercept + sum(coefs[j] * r[j] for j in range(k)) for r in Xd]
    m = _metrics(y, yhat, k, tol)
    m["q2"] = port["metrics"]["q2"]       # LOOCV Q² 由精确移植给出（同一 A）
    std_y = _std(y)
    std_coef = [(coefs[j] * stds[j] / std_y) if std_y > 0 else 0.0 for j in range(k)]

    full = _un_drop({"coefs": coefs, "means": means, "stds": stds, "stdCoef": std_coef}, drop, len(X[0]))
    return {"type": "pls", "intercept": intercept, "coefs": full["coefs"], "means": full["means"],
            "stds": full["stds"], "stdCoef": full["stdCoef"], "A": A, "yhat": yhat,
            "metrics": m, "n": n, "k": k, "drop": drop}


def train_coarse_model_sklearn(records, range_="jun_jul", tol=0.8):
    """sklearn 版训练编排（最终系数用 Ridge / PLSRegression，选型口径与前端逐位一致）。"""
    return _orchestrate(records, range_, tol, train_mlr_sklearn, train_pls_sklearn)
