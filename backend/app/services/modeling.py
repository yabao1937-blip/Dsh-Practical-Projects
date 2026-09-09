"""粗精煤泥灰分多因素模型（MLR/PLS）—— 预测函数（逐值对齐前端 predictCoarseAsh）。

训练引擎见 training.py（精确移植）与 training_sklearn.py（sklearn 内层求解器）。
"""
import math

MLR_FEATURES = ['raw_ash', 'coal_amount', 'sysA', 'sysB', 'sys401', 'sys402',
                'desliming473', 'desliming474', 'is_stoppage', 'level']


def predict_coarse_ash(rec, model):
    """与前端 predictCoarseAsh 一致：缺失因子用训练均值补全。"""
    if not rec:
        return None
    feats = MLR_FEATURES
    means = model.get("imputeMeans") or model.get("means") or [0] * len(feats)
    coefs = model.get("coefs") or []
    y = model.get("intercept", 0.0)
    for j, f in enumerate(feats):
        v = rec.get(f)
        if not isinstance(v, (int, float)) or isinstance(v, bool) or (isinstance(v, float) and math.isnan(v)):
            v = means[j] if j < len(means) else 0.0
        if j < len(coefs):
            y += coefs[j] * v
    return y
