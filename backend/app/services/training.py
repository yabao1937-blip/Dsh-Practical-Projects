"""粗精煤泥灰分 MLR/PLS 训练引擎（逐值对齐前端 App.trainMlr/trainPls/trainCoarseModel）。

实现策略：
- 本模块用纯 Python 列表 + 显式循环逐操作镜像前端 JS（IEEE-754 double），保证
  与 Node oracle（scripts/dump_train_js.js 产出 data/train_js.json）逐位一致；
- sklearn（Ridge / PLSRegression）作为内层求解器的等价实现放在 training_sklearn.py，
  由 tests 对拍本模块（差异 <1e-9）。

关键镜像点（勿改，否则对拍失败）：
- z-score 用样本标准差 ddof=1，std < 1e-9 置 1（_col_means_std）；
- 岭回归 λ 网格 = [0,1e-6,1e-5,1e-4,1e-3,1e-2] × diagMean，λ 仅加到特征列对角、
  截距列不惩罚；λ 选优用 hat 矩阵 LOOCV SSE（严格 < 比较，平局取网格靠前者）；
- PLS1 NIPALS + 全局预处理 LOOCV 选 A（严格 > 比较，平局取小 A）；
- 高斯消元列主元，|pivot| < 1e-12 返回 None（该 λ 跳过）；
- _timeCvQ2 walk-forward：n<30→None；b1=max(12,round(0.7n))，b2=max(b1+8,round(0.85n))；
  测试段 sst 按每块测试均值分别中心化后跨块累加；cnt<10→None；
- 生产模型选择用未舍入的 q2Time（>= 取 pls），回退 LOOCV q2（>= 取 pls）。
"""
import datetime
import math
from fractions import Fraction

from .modeling import MLR_FEATURES, predict_coarse_ash

LAM_FACTORS = [0, 1e-6, 1e-5, 1e-4, 1e-3, 1e-2, 1e-1, 1]


def _is_num(v):
    """数值判定（排除 bool，因 isinstance(True,int) 为 True）。"""
    return isinstance(v, (int, float)) and not isinstance(v, bool)


# ---------------- 基础工具（镜像 App._mean/_std/_sst 等） ----------------
def _mean(a):
    return sum(a) / (len(a) or 1)


def _std(a):
    m = _mean(a)
    s = sum((v - m) ** 2 for v in a)
    return math.sqrt(s / (len(a) - 1)) if len(a) > 1 else 0.0


def _sst(a):
    m = _mean(a)
    return sum((v - m) ** 2 for v in a)


def _js_round(x, ndigits):
    """镜像 JS Number.prototype.toFixed：对 double 的精确十进制值 round-half-away-from-zero。

    用 Fraction(float) 取 double 的精确值，规避浮点乘法进位偏差（如 4.35→4.3）与
    负数平局方向（如 -0.25→-0.3）。历史/q2Time/predicted_ash 均走此函数。
    """
    if x is None:
        return None
    if isinstance(x, float) and (math.isnan(x) or math.isinf(x)):
        return x
    f = 10 ** ndigits
    exact = Fraction(x)
    sign = 1 if exact >= 0 else -1
    v = abs(exact) * f + Fraction(1, 2)
    return sign * math.floor(v) / f


def _js_round_int(x):
    """镜像 JS Math.round（正数 half-up；仅用于 _time_cv_q2 的 n*0.7/n*0.85，n>0）。"""
    return math.floor(x + 0.5)


def _solve(A, b):
    """列主元高斯消元（镜像 solveLinearSystem，不修改入参 A）。"""
    n = len(A)
    aug = [row[:] + [b[i]] for i, row in enumerate(A)]
    for col in range(n):
        max_row = col
        for row in range(col + 1, n):
            if abs(aug[row][col]) > abs(aug[max_row][col]):
                max_row = row
        aug[col], aug[max_row] = aug[max_row], aug[col]
        if abs(aug[col][col]) < 1e-12:
            return None
        for row in range(col + 1, n):
            factor = aug[row][col] / aug[col][col]
            for j in range(col, n + 1):
                aug[row][j] -= factor * aug[col][j]
    x = [0.0] * n
    for i in range(n - 1, -1, -1):
        x[i] = aug[i][n]
        for j in range(i + 1, n):
            x[i] -= aug[i][j] * x[j]
        x[i] /= aug[i][i]
    return x


def _mat_inv(A):
    n = len(A)
    cols = []
    for c in range(n):
        e = [0.0] * n
        e[c] = 1.0
        sol = _solve(A, e)
        if sol is None:
            return None
        cols.append(sol)
    return [[cols[j][i] for j in range(n)] for i in range(n)]


def _const_cols(X):
    """找出常数列/与前面已保留列完全重复的列（镜像 _constCols）。"""
    k = len(X[0])
    n = len(X)
    cols = []
    for j in range(k):
        first = X[0][j]
        constant = True
        for i in range(1, n):
            if X[i][j] != first:
                constant = False
                break
        if constant:
            cols.append(j)
            continue
        for p in range(j):
            if p in cols:
                continue
            dup = True
            for i in range(n):
                if X[i][j] != X[i][p]:
                    dup = False
                    break
            if dup:
                cols.append(j)
                break
    return cols


def _col_means_std(X):
    n = len(X)
    k = len(X[0])
    means = [0.0] * k
    stds = [0.0] * k
    for j in range(k):
        s = 0.0
        for i in range(n):
            s += X[i][j]
        means[j] = s / n
    for j in range(k):
        s = 0.0
        for i in range(n):
            dd = X[i][j] - means[j]
            s += dd * dd
        stds[j] = math.sqrt(s / (n - 1)) if n > 1 else 0.0
        if stds[j] < 1e-9:
            stds[j] = 1.0
    return means, stds


def _standardize(X, means, stds):
    return [[(v - means[j]) / stds[j] for j, v in enumerate(row)] for row in X]


def _un_drop(model, drop, full_k):
    if not drop:
        return model
    coefs = [0.0] * full_k
    means = [0.0] * full_k
    stds = [1.0] * full_k
    std_coef = [0.0] * full_k
    idx = 0
    for j in range(full_k):
        if j in drop:
            continue
        coefs[j] = model["coefs"][idx]
        means[j] = model["means"][idx]
        stds[j] = model["stds"][idx]
        std_coef[j] = model["stdCoef"][idx]
        idx += 1
    return {"coefs": coefs, "means": means, "stds": stds, "stdCoef": std_coef}


def _metrics(y, yhat, k, tol=0.8):
    n = len(y)
    ym = _mean(y)
    ss_res = 0.0
    ss_tot = 0.0
    mae = 0.0
    for i in range(n):
        e = y[i] - yhat[i]
        ss_res += e * e
        ss_tot += (y[i] - ym) ** 2
        mae += abs(e)
    r2 = (1 - ss_res / ss_tot) if ss_tot > 0 else 0.0
    adj_r2 = 1 - (1 - r2) * (n - 1) / max(1, n - k - 1)
    pass_rate = sum(1 for i in range(n) if abs(y[i] - yhat[i]) <= tol) / n * 100
    pass_rate1 = sum(1 for i in range(n) if abs(y[i] - yhat[i]) <= 1.0) / n * 100
    pass_rate15 = sum(1 for i in range(n) if abs(y[i] - yhat[i]) <= 1.5) / n * 100
    return {"r2": r2, "adjR2": adj_r2, "rmse": math.sqrt(ss_res / n), "mae": mae / n,
            "passRate": pass_rate, "passRate1": pass_rate1, "passRate15": pass_rate15}


# ---------------- 数据构造（镜像 filterTrainRows / _buildCoarseXY） ----------------
def _parse_epoch(ts):
    """镜像 JS new Date(ts).getTime()（本地时区，中国无夏令时）→ 秒。"""
    try:
        return datetime.datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").timestamp()
    except (ValueError, TypeError):
        return None


def filter_train_rows(records, range_="jun_jul"):
    rows = [d for d in records
            if _is_num(d.get("ash_content"))
            and not (isinstance(d.get("ash_content"), float) and math.isnan(d["ash_content"]))
            and d["ash_content"] > 0]
    r = range_ or "jun_jul"
    if r == "jun_jul":
        rows = [d for d in rows
                if str(d.get("timestamp") or "").startswith(("2026-06", "2026-07"))]
    elif r == "30d":
        times = [t for t in (_parse_epoch(str(d.get("timestamp") or "")) for d in rows) if t is not None]
        if times:
            max_t = max(times)
            rows = [d for d in rows
                    if (lambda t: t is not None and (max_t - t) <= 30 * 86400)(_parse_epoch(str(d.get("timestamp") or "")))]
    return rows


def build_coarse_xy(records, range_="jun_jul", features=MLR_FEATURES):
    rows = filter_train_rows(records, range_)
    rows.sort(key=lambda r: str(r.get("timestamp") or ""))
    if len(rows) < len(features) + 2:
        return None
    means = []
    for f in features:
        vals = [r[f] for r in rows
                if _is_num(r.get(f))
                and not (isinstance(r.get(f), float) and math.isnan(r[f]))]
        means.append(sum(vals) / len(vals) if vals else 0.0)
    X = []
    for r in rows:
        row = []
        for i, f in enumerate(features):
            v = r.get(f)
            if _is_num(v) and not (isinstance(v, float) and math.isnan(v)):
                row.append(v)
            else:
                row.append(means[i])
        X.append(row)
    y = [r["ash_content"] for r in rows]
    return {"X": X, "y": y, "means": means, "rows": rows}


# ---------------- MLR（镜像 trainMlr） ----------------
def train_mlr(X, y, tol=0.8):
    drop = _const_cols(X)
    Xd = [[row[j] for j in range(len(row)) if j not in drop] for row in X] if drop else X
    n = len(Xd)
    k = len(Xd[0])
    means, stds = _col_means_std(Xd)
    Z = _standardize(Xd, means, stds)
    K = k + 1
    A = [[0.0] * K for _ in range(K)]
    b = [0.0] * K
    for i in range(n):
        for p in range(K):
            mp = 1.0 if p == 0 else Z[i][p - 1]
            b[p] += mp * y[i]
            for q in range(p, K):
                mq = 1.0 if q == 0 else Z[i][q - 1]
                A[p][q] += mp * mq
    for p in range(K):
        for q in range(p):
            A[p][q] = A[q][p]

    diag_mean = 0.0
    for p in range(1, K):
        diag_mean += A[p][p]
    diag_mean /= (K - 1)
    lam_grid = [f * diag_mean for f in LAM_FACTORS]

    best = None
    for lam in lam_grid:
        if lam > 0:
            Ar = [row[:] for row in A]
            for i in range(1, K):
                Ar[i][i] += lam
        else:
            Ar = A
        beta = _solve(Ar, b)
        if beta is None:
            continue
        Minv = _mat_inv(Ar)
        if Minv is None:
            continue
        sse = 0.0
        for i in range(n):
            yh = beta[0]
            for p in range(1, K):
                yh += beta[p] * Z[i][p - 1]
            h = 0.0
            for p in range(K):
                mp = 1.0 if p == 0 else Z[i][p - 1]
                inner = 0.0
                for q in range(K):
                    mq = 1.0 if q == 0 else Z[i][q - 1]
                    inner += Minv[p][q] * mq
                h += mp * inner
            loo = (y[i] - yh) / (1 - h) if h < 1 - 1e-9 else (y[i] - yh)
            sse += loo * loo
        if best is None or sse < best["sse"]:
            best = {"sse": sse, "lam": lam, "beta": beta}
    if best is None:
        return None

    beta_std = best["beta"]
    coefs = [0.0] * k
    intercept = beta_std[0]
    for j in range(k):
        coefs[j] = beta_std[1 + j] / stds[j]
        intercept -= coefs[j] * means[j]

    yhat = []
    for r in Xd:
        s = intercept
        for j in range(k):
            s += coefs[j] * r[j]
        yhat.append(s)

    m = _metrics(y, yhat, k, tol)
    std_y = _std(y)
    std_coef = [(coefs[j] * stds[j] / std_y) if std_y > 0 else 0.0 for j in range(k)]
    sst = _sst(y)
    m["q2"] = (1 - best["sse"] / sst) if sst > 0 else 0.0
    m["lambda"] = best["lam"]

    full = _un_drop({"coefs": coefs, "means": means, "stds": stds, "stdCoef": std_coef}, drop, len(X[0]))
    return {"type": "mlr", "intercept": intercept, "coefs": full["coefs"], "means": full["means"],
            "stds": full["stds"], "stdCoef": full["stdCoef"], "yhat": yhat, "metrics": m,
            "n": n, "k": k, "drop": drop, "lambda": best["lam"]}


# ---------------- PLS（镜像 trainPls / _plsCore） ----------------
def _pls_core(Z, yc, A):
    n = len(Z)
    k = len(Z[0])
    X0 = [row[:] for row in Z]
    y0 = yc[:]
    W = []
    P = []
    C = []
    for _a in range(A):
        w = [0.0] * k
        for j in range(k):
            for i in range(n):
                w[j] += X0[i][j] * y0[i]
        wnorm = math.sqrt(sum(v * v for v in w)) or 1.0
        w = [v / wnorm for v in w]
        t = [0.0] * n
        for i in range(n):
            s = 0.0
            for j in range(k):
                s += X0[i][j] * w[j]
            t[i] = s
        tt = 0.0
        ty = 0.0
        for i in range(n):
            tt += t[i] * t[i]
            ty += t[i] * y0[i]
        c = (ty / tt) if tt else 0.0
        p = [0.0] * k
        for j in range(k):
            s = 0.0
            for i in range(n):
                s += X0[i][j] * t[i]
            p[j] = s / (tt or 1.0)
        for i in range(n):
            for j in range(k):
                X0[i][j] -= t[i] * p[j]
            y0[i] -= t[i] * c
        W.append(w)
        P.append(p)
        C.append(c)

    M = [[0.0] * A for _ in range(A)]
    for a in range(A):
        for bb in range(A):
            s = 0.0
            for j in range(k):
                s += P[a][j] * W[bb][j]
            M[a][bb] = s
    tmp = _solve(M, C[:])
    Bstd = [0.0] * k
    for j in range(k):
        for a in range(A):
            Bstd[j] += (W[a][j] * tmp[a]) if tmp else 0.0
    return {"W": W, "P": P, "C": C, "Bstd": Bstd}


def train_pls(X, y, tol=0.8, amax=None):
    drop = _const_cols(X)
    Xd = [[row[j] for j in range(len(row)) if j not in drop] for row in X] if drop else X
    n = len(Xd)
    k = len(Xd[0])
    means, stds = _col_means_std(Xd)
    Z = _standardize(Xd, means, stds)
    y_mean = _mean(y)
    std_y = _std(y)
    yc = [v - y_mean for v in y]
    amax = min(amax or k, k)

    best_a = 1
    best_q2 = float("-inf")
    sst = _sst(y)
    if n >= 10:
        sse_by_a = [0.0] * (amax + 1)
        for i in range(n):
            Zt = [Z[r] for r in range(n) if r != i]
            yct = [yc[r] for r in range(n) if r != i]
            for A in range(1, amax + 1):
                core = _pls_core(Zt, yct, A)
                pred = y_mean
                for j in range(k):
                    pred += core["Bstd"][j] * Z[i][j]
                e = y[i] - pred
                sse_by_a[A] += e * e
        for A in range(1, amax + 1):
            q = (1 - sse_by_a[A] / sst) if sst > 0 else 0.0
            if q > best_q2:
                best_q2 = q
                best_a = A

    core = _pls_core(Z, yc, best_a)
    coefs = [0.0] * k
    intercept = y_mean
    for j in range(k):
        coefs[j] = core["Bstd"][j] / stds[j]
        intercept -= coefs[j] * means[j]

    yhat = []
    for r in Xd:
        s = intercept
        for j in range(k):
            s += coefs[j] * r[j]
        yhat.append(s)

    m = _metrics(y, yhat, k, tol)
    m["q2"] = best_q2
    std_coef = [(coefs[j] * stds[j] / std_y) if std_y > 0 else 0.0 for j in range(k)]

    full = _un_drop({"coefs": coefs, "means": means, "stds": stds, "stdCoef": std_coef}, drop, len(X[0]))
    return {"type": "pls", "intercept": intercept, "coefs": full["coefs"], "means": full["means"],
            "stds": full["stds"], "stdCoef": full["stdCoef"], "A": best_a, "yhat": yhat,
            "metrics": m, "n": n, "k": k, "drop": drop}


# ---------------- walk-forward 时间序列 CV（镜像 _timeCvQ2） ----------------
def _predict_from_model_vec(xrow, mdl):
    s = mdl["intercept"]
    coefs = mdl["coefs"]
    nf = len(coefs)
    impute = mdl.get("imputeMeans")
    means = mdl.get("means") or []
    for j in range(nf):
        v = xrow[j]
        if not _is_num(v) or (isinstance(v, float) and math.isnan(v)):
            if impute is not None and j < len(impute) and _is_num(impute[j]):
                v = impute[j]
            else:
                v = means[j] if (j < len(means) and means[j]) else 0.0
        s += coefs[j] * v
    return s


def _time_cv_q2(X, y, kind, amax=None, tol=0.8):
    n = len(X)
    if n < 30:
        return None
    b1 = max(12, _js_round_int(n * 0.7))
    b2 = max(b1 + 8, _js_round_int(n * 0.85))
    blocks = [[b1, b2], [b2, n]]
    sse = 0.0
    sst = 0.0
    cnt = 0
    for s, e in blocks:
        if e - s < 5:
            continue
        Xtr = X[:s]
        ytr = y[:s]
        Xte = X[s:e]
        yte = y[s:e]
        mdl = None
        if kind == "mlr":
            mdl = train_mlr(Xtr, ytr, tol)
        else:
            mdl = train_pls(Xtr, ytr, tol, amax or min(8, len(Xtr[0])))
        if mdl is None:
            continue
        ym = _mean(yte)
        for i in range(len(yte)):
            err = yte[i] - _predict_from_model_vec(Xte[i], mdl)
            sse += err * err
            sst += (yte[i] - ym) ** 2
            cnt += 1
    if cnt < 10:
        return None
    return (1 - sse / sst) if sst > 0 else 0.0


# ---------------- 训练编排（镜像 trainCoarseModel） ----------------
def _orchestrate(records, range_, tol, mlr_trainer, pls_trainer):
    """训练编排：数据构造 → 拟合 mlr/pls → q2Time 选生产 → 历史 → 回填 predicted_ash。

    mlr_trainer(X, y, tol) / pls_trainer(X, y, tol, amax) 由调用方传入（精确移植版或
    sklearn 版）；q2Time 的 walk-forward 内部仍用精确移植版 train_mlr/train_pls，
    保证选型口径与前端 JS 逐位一致。
    """
    d = build_coarse_xy(records, range_)
    if d is None:
        return None
    mlr = mlr_trainer(d["X"], d["y"], tol)
    pls = pls_trainer(d["X"], d["y"], tol, len(MLR_FEATURES))
    if mlr is None or pls is None:
        return None
    mlr["imputeMeans"] = d["means"]
    pls["imputeMeans"] = d["means"]

    mlr_q2t = _time_cv_q2(d["X"], d["y"], "mlr", None, tol)
    pls_q2t = _time_cv_q2(d["X"], d["y"], "pls", pls["A"], tol)
    mlr["metrics"]["q2Time"] = None if mlr_q2t is None else _js_round(mlr_q2t, 4)
    pls["metrics"]["q2Time"] = None if pls_q2t is None else _js_round(pls_q2t, 4)

    if mlr_q2t is not None and pls_q2t is not None:
        production = "pls" if pls_q2t >= mlr_q2t else "mlr"
    else:
        production = "pls" if pls["metrics"]["q2"] >= mlr["metrics"]["q2"] else "mlr"

    history = {
        "n": len(d["rows"]), "tolerance": tol, "range": range_, "production": production,
        "mlr": {"r2": _js_round(mlr["metrics"]["r2"], 4),
                "passRate": _js_round(mlr["metrics"]["passRate"], 1),
                "q2": _js_round(mlr["metrics"]["q2"], 4),
                "q2Time": None if mlr_q2t is None else _js_round(mlr_q2t, 4),
                "rmse": _js_round(mlr["metrics"]["rmse"], 3),
                "mae": _js_round(mlr["metrics"]["mae"], 3)},
        "pls": {"r2": _js_round(pls["metrics"]["r2"], 4),
                "passRate": _js_round(pls["metrics"]["passRate"], 1),
                "q2": _js_round(pls["metrics"]["q2"], 4),
                "q2Time": None if pls_q2t is None else _js_round(pls_q2t, 4),
                "rmse": _js_round(pls["metrics"]["rmse"], 3),
                "mae": _js_round(pls["metrics"]["mae"], 3),
                "A": pls["A"]},
    }

    prod_model = mlr if production == "mlr" else pls
    for r in records:
        r["predicted_ash"] = _js_round(predict_coarse_ash(r, prod_model), 4)

    return {"mlr": mlr, "pls": pls, "production": production,
            "n": len(d["rows"]), "tolerance": tol, "range": range_, "history": history}


def train_coarse_model(records, range_="jun_jul", tol=0.8):
    """训练 MLR+PLS（精确移植版，逐值对齐前端 App.trainCoarseModel）。

    返回 {mlr, pls, production, n, tolerance, range, history}（不含 trainedAt 时间戳，
    由调用方按服务器时间生成）。mlr/pls 结构与前端 store.coarseModel 一致。
    """
    return _orchestrate(records, range_, tol, train_mlr, train_pls)
