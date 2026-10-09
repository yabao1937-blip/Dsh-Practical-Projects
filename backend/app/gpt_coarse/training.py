"""Robust, day-balanced residual fitting around user-provided expert anchors.

Training and selection are independent of the DS shared solvers. No label lags,
same-day assay availability assumptions, or synthetic 'measured' anchor rows.
"""
from collections import Counter
import math

import numpy as np
from scipy.optimize import minimize

from .predict import KIND, FEATURES, predict

ALPHAS = (0.1, 1.0, 10.0)


def metrics(rows, predictions):
    y, p = np.array([r['ash_content'] for r in rows]), np.array(predictions)
    error = y-p
    sst = float(np.sum((y-y.mean())**2)) if len(y) else 0
    return dict(n=len(y), mae=float(np.mean(abs(error))), rmse=float(np.sqrt(np.mean(error**2))),
                r2=1-float(np.sum(error**2))/sst if sst > 0 else None,
                pass1=float(np.mean(abs(error) <= 1)*100)) if len(y) else None


def fit(rows, alpha):
    x = np.array([[r[k] for k in FEATURES[:8]] for r in rows], dtype=float)
    y = np.array([r['ash_content'] for r in rows])
    state = Counter(tuple(row[2:]) for row in x).most_common(1)[0][0]
    reference = np.array([*np.median(x[:, :2], axis=0), *state])
    scale = np.array([max(float(np.std(x[:, 0])), 1), max(float(np.std(x[:, 1])), 50), *([1]*6)])
    bounds = [[float(x[:, j].min()), float(x[:, j].max())] if j < 2 else [0., 1.] for j in range(8)]
    z = (x-reference)/scale
    # A switch observed in fewer than 3 samples in either state is not identifiable.
    active = [j for j in range(8) if np.ptp(x[:, j]) > 1e-12 and
              (j < 2 or min(np.sum(x[:, j] == 0), np.sum(x[:, j] == 1)) >= 3)]
    counts = Counter(r['day'] for r in rows)
    weights = np.array([1/counts[r['day']] for r in rows])
    weights /= weights.sum()
    baseline = np.array([30.5 - .3*min(75, max(25, r['level'])) for r in rows])
    za = z[:, active]

    def objective(beta):
        t = np.tanh(za @ beta/2)
        residual = baseline + 2*t-y
        huber = np.where(abs(residual) <= 1.5, .5*residual**2, 1.5*(abs(residual)-.75))
        loss = float(weights @ huber + .5*alpha*(beta @ beta))
        grad = za.T @ (weights*np.clip(residual, -1.5, 1.5)*(1-t*t)) + alpha*beta
        return loss, grad

    beta = np.zeros(8)
    if active:
        solution = minimize(objective, np.zeros(len(active)), jac=True, method='L-BFGS-B',
                            options={'maxiter': 2000, 'ftol': 1e-12, 'gtol': 1e-8})
        if not solution.success:
            raise ValueError('GPT residual fit did not converge: '+solution.message)
        beta[active] = solution.x
    return dict(kind=KIND, feature_names=FEATURES, expert=dict(intercept=30.5, slope=-.3, level_range=[25,75]),
                reference=reference.tolist(), scale=scale.tolist(), bounds=bounds, coefs=beta.tolist(),
                correction_limit=2.0, alpha=alpha, n=len(rows), days=len(counts),
                inactive_factors=[FEATURES[j] for j in range(8) if j not in active],
                fit_policy='Huber(1.5); equal total weight per date; no intercept; ridge; bounded tanh residual',
                level_policy='clamp-to-expert-range; show-boundary-estimate; pause-density-advice-outside')


def train(rows):
    days = sorted({r['day'] for r in rows})
    if len(rows) < 30 or len(days) < 12:
        raise ValueError('至少需要30条正常生产记录、覆盖12个日期')
    split = max(8, math.floor(len(days)*.8))
    development = [r for r in rows if r['day'] in days[:split]]
    forward = [r for r in rows if r['day'] in days[split:]]
    inner_days = days[:split]
    edges = sorted({max(4, math.floor(len(inner_days)*f)) for f in (.4,.6,.8)})+[len(inner_days)]
    trials = []
    for alpha in ALPHAS:
        validation, predicted, folds = [], [], []
        for left, right in zip(edges, edges[1:]):
            tr = [r for r in development if r['day'] in inner_days[:left]]
            va = [r for r in development if r['day'] in inner_days[left:right]]
            if not tr or not va:
                continue
            model = fit(tr, alpha)
            validation.extend(va)
            predicted.extend(predict(r, model) for r in va)
            folds.append(dict(train_end=tr[-1]['day'], validation_start=va[0]['day'], validation_end=va[-1]['day'],
                              train_n=len(tr), validation_n=len(va)))
        score = metrics(validation, predicted)
        # Equal influence per validation date, just as in fitting; not training R².
        errors = {}
        for r, p in zip(validation, predicted):
            errors.setdefault(r['day'], []).append(abs(r['ash_content']-p))
        day_mae = float(np.mean([np.mean(v) for v in errors.values()]))
        trials.append(dict(alpha=alpha, metrics=score, day_balanced_mae=day_mae, folds=folds))
    selected = min(trials, key=lambda trial:(trial['day_balanced_mae'], -trial['alpha']))
    frozen = fit(development, selected['alpha'])
    forward_predictions = [predict(r, frozen) for r in forward]
    model = fit(rows, selected['alpha'])
    full_predictions = [predict(r, model) for r in rows]
    baseline = [30.5-.3*min(75,max(25,r['level'])) for r in rows]
    report = dict(
        historical_fit=metrics(rows, full_predictions), expert_only_historical=metrics(rows, baseline),
        tuning_validation=dict(selected_alpha=selected['alpha'], trials=trials,
                               label='训练窗口内按完整日期滚动选参，不是独立验收'),
        forward_time_check=dict(train_end=development[-1]['day'], start=forward[0]['day'], end=forward[-1]['day'],
                                metrics=metrics(forward, forward_predictions),
                                expert_only=metrics(forward,[30.5-.3*min(75,max(25,r['level'])) for r in forward]),
                                historical_mean=metrics(forward,[float(np.mean([r['ash_content'] for r in development]))]*len(forward)),
                                label='历史留后时序检验；旧开发数据重用，不是新的独立现场验收'),
        final_refit='选定参数后用全部有效记录重新拟合；不把最终模型在留后段的拟合称为向前成绩',
        label_availability='历史仅有采样时间，没有化验报告时间；本模型不使用历史/同日的315灰分化验值作输入',
        production_validation='尚无本次上线之后的独立化验验证',
    )
    return model, report
