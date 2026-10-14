"""Separate an empirical process target from an operator's next setpoint.

Read-only and time bounded, including when called by historical hourly briefs.
Raw setpoint history also closes the race before the action observer has run.
"""
from copy import deepcopy
from datetime import timedelta
from math import fsum, ceil, floor

from sqlalchemy import select, func

from ..hourly_brief.inputs import Inputs, LOCAL, business_time, numeric
from ..telemetry.derived import DENSITY_AVERAGE_MEMBERS
from ..telemetry.domain import iso_time
from ..telemetry.models import TelemetryReading
from ..telemetry.retained import usable_at_receipt
from ..decision_hourly.models import DensityAction

POLICY = 'gpt-setpoint-execution-v1'
EPSILON = .0005


def setpoints(inputs, actual):
    source, item = actual.get('source_id'), actual.get('item_id')
    chosen = [(label, tag) for label, tag in DENSITY_AVERAGE_MEMBERS
              if item == tag or item == 'a303_a304_density_average']
    parts, problems = {}, []
    for label, tag in chosen:
        point = inputs.point(tag+'_SET', source) if source else None
        if point and point.config.get('unit') not in ('', None, 'g/cm³', 'g/cm3'):
            problems.append(label+' 设定密度单位不正确')
        c = inputs.telemetry(tag+'_SET', source_id=source, unit='g/cm³') if source else {}
        parts[label] = c
        s = c.get('sample') or {}
        if c.get('status') != 'good' or not numeric(c.get('value')) or not 1 < c['value'] < 3:
            problems.append(label+' 设定密度缺失、过期或无效')
            continue
        a = inputs.telemetry(tag, source_id=source, unit='g/cm³')
        raw = a.get('sample') or {}
        if (a.get('status') != 'good' or not s.get('batch_id') or s.get('batch_id') != raw.get('batch_id')
                or s.get('received_at') != raw.get('received_at')):
            problems.append(label+' 实测与设定密度不属于同一有效批次')
    if not chosen:
        problems.append('无法确定实际密度对应的设定点位')
    return dict(value=fsum(c['value'] for c in parts.values())/len(parts) if parts and not problems else None,
                status='missing' if problems else 'good', unit='g/cm³', components=parts,
                reason='；'.join(problems), source='同源同路 MD_SET')


def assay_end(cell):
    """Manual sample in hour 10 describes production 08–09, not its entry time."""
    try:
        stamp = business_time(cell['observed_at'])
        if cell.get('status') in ('manual', 'historical', 'default', 'carried'):
            return stamp.astimezone(LOCAL).replace(minute=0, second=0, microsecond=0)-timedelta(hours=1)
        return stamp if cell.get('status') == 'good' else None
    except (KeyError, TypeError, ValueError):
        return None


def feedback_start(cell):
    """An hourly mixed sample must wholly follow a move to unlock a new move."""
    end = assay_end(cell)
    return end-timedelta(hours=1) if end and cell.get('status') in ('manual', 'historical', 'default', 'carried') else end


def changes_since(db, basis, since, now):
    if since is None:
        return [], '灰分缺少可归属的生产时间，暂缓新的调密'
    changes = []
    for channel, c in basis['components'].items():
        sample = c.get('sample') or {}
        if business_time(sample['received_at']) <= since:
            continue  # This observed setpoint already predates the feedback window.
        definition = sample.get('definition_id')
        # Durable actions also detect a change followed by a return to the old value.
        actions = db.scalars(select(DensityAction).where(DensityAction.channel == channel,
            DensityAction.detected_at > iso_time(since), DensityAction.detected_at <= iso_time(now))
            .order_by(DensityAction.detected_at))
        found = None
        for action in actions:
            after = action.evidence.get('after', {})
            point = (after.get('pair') or {}).get('setpoint') or {}
            if (point.get('source_id') == c.get('source_id') and point.get('point_key') == c.get('point_key')
                    and point.get('definition_id') == definition):
                found = dict(channel=channel, action_id=action.id, detected_at=action.detected_at,
                    before=action.evidence.get('before', {}).get('pair', {}).get('setpoint', {}).get('value'),
                    after=point.get('value'), evidence='recorded-action')
                break
        if found:
            changes.append(found)
            continue
        # No writes on a GET. Inspect retained raw values so a setpoint change is
        # protected even before observe(), after a restart, or with another browser.
        point = inputs_point(db, c)
        query = select(TelemetryReading).where(TelemetryReading.source_id == c.get('source_id'),
            TelemetryReading.point_key == c.get('point_key'),
            TelemetryReading.received_at >= iso_time(since-timedelta(seconds=5)),
            TelemetryReading.received_at < sample.get('received_at', iso_time(now)),
            TelemetryReading.sample['definition_id'].as_string() == definition,
            TelemetryReading.sample['quality'].as_string() == 'good',
            func.abs(TelemetryReading.sample['value'].as_float()-c['value']) >= EPSILON)
        for row in db.scalars(query.order_by(TelemetryReading.received_at)):
            if point and usable_at_receipt(row.sample, point.config) and 1 < row.sample['value'] < 3:
                changes.append(dict(channel=channel, before=row.sample['value'], after=c['value'],
                    before_at=row.received_at, detected_at=sample['received_at'], evidence='raw-setpoint-history'))
                break
    return changes, ''


def inputs_point(db, cell):
    from ..telemetry.models import TelemetryPoint
    return db.get(TelemetryPoint, (cell.get('source_id'), cell.get('point_key')))


def empirical_target(ash, base, target):
    boundary = min(target['max'], max(target['min'], ash))
    deviation = round(ash-boundary, 3)
    correction = -deviation/15 if abs(deviation) > .05 else 0.
    return dict(value=base+correction, correction=correction, deviation=deviation, boundary=boundary,
                validation='awaiting_experiment', reason='专家经验参考目标，非实验验证的达标密度')


def advise(db, now, basis, ash, target, base, evidence_end, settings=None, anchor_to_setpoint=False):
    settings = settings or {}
    step = settings.get('maxStep', .03)
    step = min(.03, step) if numeric(step) and step > 0 else .03
    low, high = settings.get('rhoMin', 1.35), settings.get('rhoMax', 1.60)
    if not numeric(low) or not numeric(high) or low >= high:
        low, high = 1.35, 1.60
    g = dict(policy=POLICY, rhoCur=basis['value'], rhoNew=None, deltaRho=None, maxStep=step,
        direction='unavailable', execution_basis=deepcopy(basis), evidence_end=iso_time(evidence_end) if evidence_end else None,
        validation='awaiting_experiment', hold=False, execution_state='unavailable', actions=[])
    if not numeric(basis['value']):
        return dict(g, reason=basis['reason'])
    for label, c in basis.get('components', {}).items():
        try:
            age = (now-business_time(c['observed_at'])).total_seconds()
        except (KeyError, ValueError, TypeError):
            age = float('inf')
        if c.get('status') != 'good' or not 0 <= age <= 30:
            return dict(g, reason=label+' 当前设定密度过期或不可用')
    if not numeric(ash) or not numeric(base):
        return dict(g, reason='灰分或工况参考密度不可用')
    changes, problem = changes_since(db, basis, evidence_end, now)
    if anchor_to_setpoint and changes:
        anchors = {label:c['value'] for label,c in basis['components'].items()}
        anchors.update({a['channel']:a['before'] for a in changes if numeric(a.get('before'))})
        base = fsum(anchors.values())/len(anchors)
    estimate = empirical_target(ash, base, target)
    # In range/deadband means KEEP the operator's actual setpoint, not a past mean.
    full = estimate['value'] if estimate['correction'] else basis['value']
    g.update(reference_target={**estimate, 'value':full}, rhoTargetFull=full,
             deltaA=estimate['deviation'], target=estimate['boundary'])
    g['actions'] = changes
    if problem:
        return dict(g, reason=problem)
    awaiting = bool(changes and estimate['correction'])
    adjustment = 0. if awaiting else max(-step, min(step, full-basis['value']))
    channels, precision_held = {}, []
    for label, cell in basis['components'].items():
        current = cell['value']
        if not low <= current <= high:
            return dict(g, reason=label+' 设定密度超出既有运行范围，暂缓新的调密')
        lower = ceil((max(low, current-step, current if adjustment > 0 else low)-1e-7)*1000)/1000
        upper = floor((min(high, current+step, current if adjustment < 0 else high)+1e-7)*1000)/1000
        if adjustment and lower > upper:
            precision_held.append(label)
        value = current if adjustment == 0 or lower > upper else max(lower, min(upper, round(current+adjustment, 3)))
        channels[label] = dict(current=current, suggested=value, delta=round(value-current, 6))
    result = fsum(v['suggested'] for v in channels.values())/len(channels)
    delta = round(result-basis['value'], 6)
    reason = ('设定密度已调整，现有灰分仍反映调整前时段；保持当前设定，等待能反映调后工况的新化验'
        if awaiting else '灰分在目标范围或死区内，保持当前设定密度' if not estimate['correction']
        else '按工况参考目标调整设定密度；每路单次执行相对当前设定不超过 ±'+f'{step:.3f}')
    if precision_held:
        reason += '；'+ '/'.join(precision_held)+' 在三位小数精度下无同方向且不超限的调整，保持当前设定'
    return dict(g, rhoNew=result, deltaRho=delta, channels=channels,
        direction='stable' if abs(delta) < EPSILON else 'down' if delta < 0 else 'up',
        execution_state='awaiting_feedback' if awaiting else 'keep' if abs(delta) < EPSILON else 'adjust', reason=reason)


def apply_live(db, inputs, result, cells, store):
    basis = setpoints(inputs, cells['density'])
    result['execution_basis'] = basis
    if result['suggested_density'].get('value') is None:
        return  # Existing missing-input, stoppage and explicit action guards remain.
    guide = advise(db, inputs.hour, basis, result['total_ash']['value'], result['target'],
        basis['value'], feedback_start(cells['heavy_ash']), store.get('densityGuide'), anchor_to_setpoint=True)
    result['guidance'] = guide
    result['suggested_density'] = dict(value=guide['rhoNew'],
        status='calculated' if guide['rhoNew'] is not None else 'missing', unit='g/cm³', reason=guide['reason'])
