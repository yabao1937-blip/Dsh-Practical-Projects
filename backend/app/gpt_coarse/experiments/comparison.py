"""Paired expert8/9 ablation on frozen expert9 rows, without production writes."""
import math

import numpy as np

from .. import predict as expert9
from ..training import ALPHAS, fit as fit9, metrics
from . import expert8


def split_rows(rows):
    days = sorted({r['day'] for r in rows})
    if len(rows) < 30 or len(days) < 12:
        raise ValueError('至少需要30条正常生产记录、覆盖12个日期')
    cut = max(8, math.floor(len(days)*.8))
    development = [r for r in rows if r['day'] in days[:cut]]
    forward = [r for r in rows if r['day'] in days[cut:]]
    inner = days[:cut]
    edges = sorted({max(4, math.floor(len(inner)*f)) for f in (.4, .6, .8)})+[len(inner)]
    folds = [([r for r in development if r['day'] in inner[:left]],
              [r for r in development if r['day'] in inner[left:right]])
             for left, right in zip(edges, edges[1:])]
    return development, forward, folds


def paired(rows, old_predictions, new_predictions):
    old, new = metrics(rows, old_predictions), metrics(rows, new_predictions)
    shifts = np.array(new_predictions)-np.array(old_predictions)
    return dict(old=old, new=new,
                delta_new_minus_old={k: new[k]-old[k] for k in ('mae', 'rmse', 'pass1')},
                prediction_change=dict(mean_absolute=float(np.mean(abs(shifts))),
                                       maximum_absolute=float(np.max(abs(shifts))),
                                       mean_signed=float(np.mean(shifts))),
                predictions=[dict(source_row=r['source_row'], day=r['day'], timestamp=r['timestamp'],
                                  measured=r['ash_content'], old=a, new=b, new_minus_old=b-a)
                             for r, a, b in zip(rows, old_predictions, new_predictions)])


def rolling(folds, alpha):
    all_rows, old_predictions, new_predictions, details = [], [], [], []
    for training, validation in folds:
        old_model, new_model = fit9(training, alpha), expert8.fit(training, alpha)
        old = [expert9.predict(r, old_model) for r in validation]
        new = [expert8.predict(r, new_model) for r in validation]
        fold = paired(validation, old, new)
        fold.update(train_end=max(r['day'] for r in training), train_n=len(training),
                    validation_start=min(r['day'] for r in validation),
                    validation_end=max(r['day'] for r in validation), validation_n=len(validation))
        details.append(fold)
        all_rows.extend(validation)
        old_predictions.extend(old)
        new_predictions.extend(new)
    result = paired(all_rows, old_predictions, new_predictions)
    result.update(alpha=alpha, folds=details)
    for name, predictions in [('old', old_predictions), ('new', new_predictions)]:
        errors = {}
        for row, prediction in zip(all_rows, predictions):
            errors.setdefault(row['day'], []).append(abs(row['ash_content']-prediction))
        result[name]['day_balanced_mae'] = float(np.mean([np.mean(v) for v in errors.values()]))
    return result


def compare(artifact):
    rows, old_model = artifact['rows'], artifact['model']
    if not expert9.valid(old_model):
        raise ValueError('原模型必须是有效的 GPT 专家九因子模型')
    alpha = old_model['alpha']
    if alpha not in ALPHAS:
        raise ValueError('原模型参数不在原训练网格中，无法作相同参数比较')
    # Refit the original pipeline as a reproduction check, not as a replacement.
    old_refit = fit9(rows, alpha)
    old_predictions = [expert9.predict(r, old_model) for r in rows]
    reproduced = [expert9.predict(r, old_refit) for r in rows]
    drift = float(np.max(abs(np.array(old_predictions)-np.array(reproduced))))
    if drift > 1e-10:
        raise ValueError(f'原模型复现偏差 {drift}，停止比较')
    development, forward, folds = split_rows(rows)
    trials = [rolling(folds, a) for a in ALPHAS]
    selected = {name: min(trials, key=lambda t: (t[name]['day_balanced_mae'], -t['alpha']))['alpha']
                for name in ('old', 'new')}
    # The ablation is locked to the current alpha even if sensitivity selection differs.
    model = expert8.fit(rows, alpha)
    frozen_old, frozen_new = fit9(development, alpha), expert8.fit(development, alpha)
    time_check = paired(forward, [expert9.predict(r, frozen_old) for r in forward],
                        [expert8.predict(r, frozen_new) for r in forward])
    time_check.update(train_n=len(development), train_days=len({r['day'] for r in development}),
                      train_end=max(r['day'] for r in development),
                      start=min(r['day'] for r in forward), end=max(r['day'] for r in forward),
                      forward_days=len({r['day'] for r in forward}),
                      frozen_models=dict(old=frozen_old, new=frozen_new),
                      expert_only=metrics(forward, [30.5-.3*min(75, max(25, r['level'])) for r in forward]),
                      training_mean=metrics(forward, [float(np.mean([r['ash_content'] for r in development]))]*len(forward)),
                      label='历史留后时序检验；数据已用于开发，不是新的独立现场验收')
    report = dict(
        status='candidate_only_not_activated', changed_factor='raw_ash removed', locked_alpha=alpha,
        original_reproduction_max_abs_error=drift,
        historical_fit=paired(rows, old_predictions, [expert8.predict(r, model) for r in rows]),
        tuning_validation=dict(label='同日期三折滚动验证，原网格敏感性复核；主比较锁定现用参数',
                               selected_alpha=selected, trials=trials,
                               main=next(t for t in trials if t['alpha'] == alpha)),
        forward_time_check=time_check,
        expert_checks=[dict(level=level, old=expert9.predict(dict(zip(expert9.FEATURES, old_model['reference']+[level])), old_model),
                            new=expert8.predict(dict(zip(expert8.FEATURES, model['reference']+[level])), model))
                       for level in (25, 50, 75)],
        limitations=[
            '原煤灰分仅从候选输入中删除；训练样本、目标值、日期权重、损失函数和专家先验保持不变。',
            '训练表中的液位已经提前90秒对齐，本轮不再移动历史行。',
            '历史记录可能不准确；这些误差衡量与旧记录的差异，不代表现场真实精度。',
            '全量最终拟合与142条训练后的35条留后预测分开报告，不用最终模型冒充向前检验。',
            '不使用同日或历史315化验标签作预测输入，不假定当时化验已经出报告。',
            'A系统关闭样本仍只有1条，沿用原规则冻结其修正系数为0。',
            '本轮没有新的独立现场化验数据，未评估移除原煤灰分对建议密度的现场效果。',
        ])
    return model, report
