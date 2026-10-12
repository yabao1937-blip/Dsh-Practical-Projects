"""Promote a reviewed candidate without fitting or changing its coefficients."""
from copy import deepcopy

from .expert8 import valid


def prepare_expert8(candidate, comparison, candidate_sha256):
    if (not valid(candidate.get('model')) or candidate.get('status') != 'candidate_only_not_activated'
            or comparison.get('candidate_artifact_sha256') != candidate_sha256):
        raise ValueError('八因子候选与已审阅对比报告不匹配')
    forward = comparison['forward_time_check']
    report = dict(
        historical_fit=deepcopy(comparison['historical_fit']['new']),
        tuning_validation=dict(selected_alpha=comparison['locked_alpha'],
            trials=[dict(alpha=t['alpha'], metrics=deepcopy(t['new']),
                         day_balanced_mae=t['new']['day_balanced_mae'],
                         folds=[{k:v for k,v in f.items() if k in
                                 ('train_end','train_n','validation_start','validation_end','validation_n')}
                                for f in t['folds']]) for t in comparison['tuning_validation']['trials']],
            label='原日期划分、原参数；旧开发数据重用，不是新的独立验收'),
        forward_time_check=dict(train_end=forward['train_end'], start=forward['start'], end=forward['end'],
            metrics=deepcopy(forward['new']), expert_only=deepcopy(forward['expert_only']),
            historical_mean=deepcopy(forward['training_mean']), label=forward['label']),
        limitations=deepcopy(comparison['limitations']),
        production_validation='尚无新化验独立验证；去掉原煤灰分后历史表现基本持平')
    return dict(trained_at=candidate['trained_at'], status='production_ready',
                model=deepcopy(candidate['model']), rows=deepcopy(candidate['rows']), report=report,
                source=deepcopy(candidate['source']['original_audit']),
                lineage=dict(candidate_sha256=candidate_sha256,
                             comparison='docs/verification/gpt-expert8-20261012/comparison.json'),
                level_alignment=candidate['level_alignment'],
                input_policy='raw_ash is display-only; all eight model factors and other density protections remain required')
