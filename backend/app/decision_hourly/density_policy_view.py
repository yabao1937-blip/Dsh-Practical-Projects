"""Test-only new-policy presentation; raw production calculations stay internal."""
from copy import deepcopy
import os

from fastapi import HTTPException

from .. import config
from ..gpt_density.candidate import evaluate_candidate
from ..telemetry.domain import iso_time
from .density_candidates import current, load_views

POLICY='density-candidate-v1'


def enabled():
    return bool(config.REMOTE_READONLY) or os.environ.get('DMCS_DENSITY_EXPERIMENT')=='1'


def apply(raw, candidate):
    result=deepcopy(raw)
    step=candidate['candidate']
    advice=dict(value=step['value'],status=step['status'],unit='g/cm³',
        source='新版本密度建议',reason='；'.join(step['reasons']))
    guide=dict(policy=POLICY,rhoCur=candidate['setting']['value'],rhoNew=step['value'],
        deltaRho=step['delta'],direction='unavailable' if step['delta'] is None else
        'stable' if abs(step['delta'])<.0005 else 'up' if step['delta']>0 else 'down',
        hold=step['status']!='adjust',reason=advice['reason'],
        execution_state=step['status'],channels={},maxStep=candidate['parameters']['max_step'])
    result.update(density_policy=POLICY,advice_policy=POLICY,suggested_density=advice,
        density_suggestion=deepcopy(candidate),guidance=guide,execution_guidance=deepcopy(guide),
        advice_frozen=False,density_label='A304',density_version='database-trial-v1')
    if isinstance(result.get('cells'),dict):
        result['cells']['suggested_density']=deepcopy(advice)
    return result


def view(db, kind):
    try:
        raw_current,raw_hourly,now=load_views(db)
    except HTTPException as error:
        raise HTTPException(error.status_code,detail=dict(density_policy=POLICY,
            error_code='new_density_inputs_unavailable',message='新版本当前输入暂不可用',
            candidate=dict(value=None,status='unavailable'))) from None
    try:
        candidate=current(db,views=(raw_current,raw_hourly,now))
    except HTTPException:
        candidate=evaluate_candidate(dict(evaluated_at=iso_time(now)))
        candidate['candidate'].update(reason_codes=['candidate_inputs_unavailable'],
            reasons=['新版本调密历史或依据读取失败，不能假定没有调密，不显示旧规则回退值'])
    return apply(raw_current if kind=='current' else raw_hourly,candidate)
