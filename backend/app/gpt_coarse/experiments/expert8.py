"""Controlled ablation of expert9: remove raw ash, keep the fitting policy.

This candidate is intentionally not accepted by the production expert9 loader.
No raw-ash value is read by fitting, validation or prediction.
"""
from collections import Counter
import math

import numpy as np
from scipy.optimize import minimize

from ..predict import number

KIND = 'gpt-expert8-no-raw-v1'
FEATURES = ['coal_amount', 'sysA', 'sysB', 'sys401', 'sys402',
            'desliming473', 'desliming474', 'level']
LABELS = ['小时带煤量', 'A系统', 'B系统', '401系统', '402系统',
          '473脱粉', '474脱粉', '精磁尾液位']


def valid(model):
    if not isinstance(model, dict) or model.get('kind') != KIND or model.get('feature_names') != FEATURES:
        return False
    for key in ('reference', 'scale', 'coefs'):
        values = model.get(key)
        if not isinstance(values, list) or len(values) != 7 or not all(number(v) for v in values):
            return False
    bounds = model.get('bounds')
    return (all(v > 0 for v in model['scale']) and isinstance(bounds, list) and len(bounds) == 7
            and all(isinstance(b, list) and len(b) == 2 and all(number(v) for v in b)
                    and b[0] <= b[1] for b in bounds)
            and model.get('expert') == {'intercept': 30.5, 'slope': -0.3, 'level_range': [25, 75]}
            and model.get('correction_limit') == 2.0)


def explain(record, model):
    if not valid(model):
        return None
    values = [record.get(k) for k in FEATURES]
    if not all(number(v) for v in values):
        return None
    if not (values[0] > 10 and 0 <= values[-1] <= 100 and all(v in (0, 1) for v in values[1:7])):
        return None
    used = [min(b[1], max(b[0], v)) for v, b in zip(values[:7], model['bounds'])]
    terms = [c*(v-r)/s for c, v, r, s in zip(model['coefs'], used, model['reference'], model['scale'])]
    correction = 2*math.tanh(sum(terms)/2)
    level = min(75, max(25, values[-1]))
    baseline = 30.5-.3*level
    return dict(value=baseline+correction, baseline=baseline, correction=correction,
                outside_expert_range=level != values[-1],
                features=[dict(key=k, raw=raw, used=v, clipped=raw != v)
                          for k, raw, v in zip(FEATURES, values, used+[level])])


def predict(record, model):
    detail = explain(record, model)
    return detail['value'] if detail else None


def fit(rows, alpha):
    if not rows or not number(alpha) or alpha <= 0:
        raise ValueError('需要有效训练记录和正的岭参数')
    for row in rows:
        v = [row.get(k) for k in FEATURES]
        if not (all(number(x) for x in v) and v[0] > 10 and 0 <= v[-1] <= 100
                and all(x in (0, 1) for x in v[1:7]) and number(row.get('ash_content'))
                and 0 < row['ash_content'] <= 100 and row.get('day')):
            raise ValueError('八因子或315灰分无效')
    x = np.array([[r[k] for k in FEATURES[:7]] for r in rows], dtype=float)
    y = np.array([r['ash_content'] for r in rows])
    state = Counter(tuple(row[1:]) for row in x).most_common(1)[0][0]
    reference = np.array([float(np.median(x[:, 0])), *state])
    scale = np.array([max(float(np.std(x[:, 0])), 50), *([1]*6)])
    bounds = [[float(x[:, j].min()), float(x[:, j].max())] if j == 0 else [0., 1.] for j in range(7)]
    z = (x-reference)/scale
    active = [j for j in range(7) if np.ptp(x[:, j]) > 1e-12 and
              (j == 0 or min(np.sum(x[:, j] == 0), np.sum(x[:, j] == 1)) >= 3)]
    counts = Counter(r['day'] for r in rows)
    weights = np.array([1/counts[r['day']] for r in rows])
    weights /= weights.sum()
    baseline = np.array([30.5-.3*min(75, max(25, r['level'])) for r in rows])
    za = z[:, active]

    def objective(beta):
        t = np.tanh(za @ beta/2)
        residual = baseline+2*t-y
        huber = np.where(abs(residual) <= 1.5, .5*residual**2, 1.5*(abs(residual)-.75))
        loss = float(weights @ huber + .5*alpha*(beta @ beta))
        grad = za.T @ (weights*np.clip(residual, -1.5, 1.5)*(1-t*t)) + alpha*beta
        return loss, grad

    beta = np.zeros(7)
    if active:
        solution = minimize(objective, np.zeros(len(active)), jac=True, method='L-BFGS-B',
                            options={'maxiter': 2000, 'ftol': 1e-12, 'gtol': 1e-8})
        if not solution.success:
            raise ValueError('GPT residual fit did not converge: '+solution.message)
        beta[active] = solution.x
    return dict(kind=KIND, feature_names=FEATURES, expert=dict(intercept=30.5, slope=-.3, level_range=[25, 75]),
                reference=reference.tolist(), scale=scale.tolist(), bounds=bounds, coefs=beta.tolist(),
                correction_limit=2.0, alpha=alpha, n=len(rows), days=len(counts),
                inactive_factors=[FEATURES[j] for j in range(7) if j not in active],
                fit_policy='Huber(1.5); equal total weight per date; no intercept; ridge; bounded tanh residual',
                level_policy='clamp-to-expert-range; show-boundary-estimate; pause-density-advice-outside')
