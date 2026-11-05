"""Read-only experiment adapters. Original hourly advice and archives stay intact."""
from copy import deepcopy
import os

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from .. import config
from ..gpt_density.candidate import CandidateParameters, evaluate_candidate, timestamp
from ..telemetry.domain import iso_time, utc_now
from .advice_provenance import origin_class, select_recorded
from .models import DensityAction, GptHourlyDensityAdvice

MAX_ACTIONS = 500
MAX_REVISIONS = 8000


def require_enabled():
    if not config.REMOTE_READONLY and os.environ.get('DMCS_DENSITY_EXPERIMENT') != '1':
        raise HTTPException(404, '测试密度候选未启用')


def cell_view(cell):
    cell = deepcopy(cell or {})
    sample = cell.pop('sample', None) or {}
    return {**sample, **cell}


def identity(cell):
    return tuple(cell.get(key) for key in ('source_id', 'point_key', 'definition_id'))


def action_evidence(row, setpoint):
    if row is None:
        return None
    evidence = row.evidence or {}
    before, after = evidence.get('before') or {}, evidence.get('after') or {}
    b = cell_view((before.get('pair') or {}).get('setpoint'))
    a = cell_view((after.get('pair') or {}).get('setpoint'))
    return dict(id=row.id, before_at=before.get('sampled_at'), after_at=after.get('sampled_at'),
        before=b.get('value'), after=a.get('value'), continuous=bool(evidence.get('continuous')),
        gap=before.get('gap'), identity_known=all(identity(setpoint)) and
            identity(b) == identity(a) == identity(setpoint))


def latest_action(db, now, *, exclude=None):
    query = select(DensityAction).where(DensityAction.channel == 'A304',
        DensityAction.detected_at <= iso_time(now))
    if exclude:
        query = query.where(DensityAction.id != exclude)
    try:
        return db.scalar(query.order_by(DensityAction.detected_at.desc(), DensityAction.id.desc()).limit(1))
    except SQLAlchemyError:
        raise HTTPException(503, detail=dict(error_code='candidate_history_unavailable',
            message='调密历史读取失败，不能假定没有调密；候选暂不可用')) from None


def base_evidence(now, reference, setpoint, actual, operating, action, *, mode):
    setpoint, actual = cell_view(setpoint), cell_view(actual)
    return dict(evaluated_at=iso_time(now), mode=mode, reference=deepcopy(reference),
        current_setpoint=setpoint, actual_density=actual, operating=deepcopy(operating),
        identity_known=all(identity(setpoint)) and actual.get('source_id') == setpoint.get('source_id'),
        action_history_status='good', latest_action=action)


def load_views(db):
    """Raw underlying views; do not call public new-policy routes recursively."""
    if config.REMOTE_READONLY:
        from ..remote_readonly.runtime import decision_view
        current_result, hourly = decision_view('current'), decision_view('hourly')
        now = timestamp(current_result.get('generated_at')) or timestamp(hourly.get('checked_at'))
        if now is None:
            raise HTTPException(503, '测试端当前计算时刻不可用')
    else:
        from ..services.decision_sources import shared_snapshot
        from ..gpt_runtime.decision_store import load_decision_store
        from .service import read_hour
        now = utc_now()
        current_result = shared_snapshot(db, when=now, realtime=True, store_loader=load_decision_store)
        hourly = read_hour(db, now=now, live=current_result, defer_missing=True, store_loader=load_decision_store)
    return current_result,hourly,now


def current(db, *, views=None):
    require_enabled()
    current_result,hourly,now = load_views(db) if views is None else views
    basis = current_result.get('execution_basis') or {}
    parts = basis.get('components') or {}
    setpoint = cell_view(parts.get('A304')) if sorted(parts) == ['A304'] else {}
    inputs = current_result.get('inputs') or current_result.get('cells') or {}
    stop = inputs.get('is_stoppage') or {}
    ref = dict(value=(hourly.get('suggested_density') or {}).get('value'),
        status=(hourly.get('suggested_density') or {}).get('status'), policy=hourly.get('advice_policy'),
        generated_at=hourly.get('checked_at') or iso_time(now), origin=None,
        identity='live-calculation', availability='current-calculation',
        reason=(hourly.get('suggested_density') or {}).get('reason') or hourly.get('reason'),
        target=deepcopy(hourly.get('target')), ash=deepcopy((hourly.get('advice_inputs') or {}).get('ash')),
        basis=deepcopy((hourly.get('advice_inputs') or {}).get('density')))
    latest = latest_action(db, now)  # Query failure propagates; never becomes "no moves".
    e = base_evidence(now, ref, setpoint, inputs.get('density'),
        dict(status=stop.get('status'), stopped=stop.get('value'), observed_at=stop.get('observed_at')),
        action_evidence(latest, setpoint), mode='current')
    result = evaluate_candidate(e)
    result['formal_execution'] = deepcopy(hourly.get('execution_guidance'))
    return result


def replay_actions(db, now, *, hours=168, start=None, end=None):
    """Replay each action's BEFORE snapshot; never borrow its later observation."""
    require_enabled()
    from .density_trend import period
    start, end = period(now, hours, start, end)
    actions = list(db.scalars(select(DensityAction).where(DensityAction.channel == 'A304',
        DensityAction.detected_at >= iso_time(start), DensityAction.detected_at <= iso_time(end))
        .order_by(DensityAction.detected_at, DensityAction.id).limit(MAX_ACTIONS+1)))
    if len(actions) > MAX_ACTIONS:
        raise HTTPException(422, '动作过多，请缩小回放范围（最多500条）')
    # Include the first action's earlier BEFORE hour at minute/range boundaries.
    before_times = [timestamp(((row.evidence or {}).get('before') or {}).get('sampled_at')) for row in actions]
    lower = min([start] + [value for value in before_times if value]).replace(minute=0, second=0, microsecond=0)
    records = list(db.scalars(select(GptHourlyDensityAdvice).where(
        GptHourlyDensityAdvice.generated_at >= iso_time(lower),
        GptHourlyDensityAdvice.generated_at <= iso_time(end)).order_by(
            GptHourlyDensityAdvice.generated_at, GptHourlyDensityAdvice.id).limit(MAX_REVISIONS+1)))
    if len(records) > MAX_REVISIONS:
        raise HTTPException(422, '建议修订过多，请缩小回放范围')
    points = [dict(at=row.generated_at, apply_start=row.apply_start, **deepcopy(row.snapshot)) for row in records]
    rows = []
    for row, before_at in zip(actions, before_times):
        before = (row.evidence or {}).get('before') or {}
        when = timestamp(before.get('decision_at')) or before_at
        if when is None:
            when = timestamp(row.detected_at)
        # The observation instant is stricter than decision_at for prior advice.
        cutoff = before_at or when
        hour = cutoff.replace(minute=0, second=0, microsecond=0)
        point, ignored = select_recorded(points, iso_time(hour), cutoff)
        ref = deepcopy(point or {})
        source = origin_class(ref) if point else 'unknown'
        ref.update(identity=source, generated_at=ref.get('at'),
            availability='before-observation' if source == 'online-default' else 'unavailable')
        pair = before.get('pair') or {}
        setting = cell_view(pair.get('setpoint'))
        previous = latest_action(db, cutoff, exclude=row.id)
        state = before.get('operating_state') or {}
        stopped = state.get('stopped')
        e = base_evidence(when, ref, setting, pair.get('actual'),
            dict(status='good' if stopped in (0, 1) else 'missing', stopped=stopped,
                observed_at=before.get('sampled_at')),
            action_evidence(previous, setting), mode='historical-replay')
        result = evaluate_candidate(e)
        result.update(action_id=row.id, at=row.detected_at, evidence_at=before.get('sampled_at'),
            replay_provenance=dict(kind='before-action-snapshot', ignored_replay_count=ignored,
                physical_availability='not-recorded', outcome_evaluation=False))
        rows.append(result)
    summary = {band:dict(rows=0, candidate_values=0, adjust=0, hold=0, wait=0, unavailable=0)
               for band in ('low', 'in_band', 'high', 'unknown')}
    for row in rows:
        group = summary[row['basis']['band']]
        group['rows'] += 1
        group['candidate_values'] += row['candidate']['value'] is not None
        group[row['candidate']['status']] += 1
    return dict(experimental=True, policy='density-candidate-v1', mode='historical-replay',
        evaluated_at=iso_time(now), period=dict(start=iso_time(start), end=iso_time(end)),
        parameters=CandidateParameters().__dict__, rows=rows, summary=summary,
        limitations=['仅按当时动作调前快照回放；不是未执行动作结果或因果收益评价',
            '无真实写入/页面送达时刻；历史回算不作现场先验，缺少实测或来源则不可评价'])
