"""GPT production transport delay; training rows are already aligned by the plant."""
from copy import deepcopy
from datetime import timedelta

from sqlalchemy import select

from ..hourly_brief.columns import cell
from ..hourly_brief.inputs import business_time, numeric
from ..telemetry.domain import availability, definition_id, iso_time, parse_time
from ..telemetry.retained import raw_sample, usable_at_receipt

LEVEL_LAG_SECONDS = 90
LEVEL_TOLERANCE_SECONDS = 30
POLICY = dict(version='gpt-level-lag-v1', level_lag_seconds=LEVEL_LAG_SECONDS,
              max_lookback_seconds=LEVEL_TOLERANCE_SECONDS, clock='received_at',
              selection='latest-at-or-before-target', training_rows='already-aligned')


def aligned_level(inputs, current):
    """Latest raw event at/before t-90, within 30s; never skip a bad latest event.

    The sparse telemetry archive is augmented by the existing 5s decision archive.
    That archive keeps the unshifted observation separately, so delay never compounds.
    All reads remain within the prediction's knowledge boundary.
    """
    from ..decision_hourly.models import DecisionCycleSample
    from ..telemetry.models import TelemetryPoint, TelemetryReading

    target = inputs.hour-timedelta(seconds=LEVEL_LAG_SECONDS)
    lower = target-timedelta(seconds=LEVEL_TOLERANCE_SECONDS)
    alignment = dict(**POLICY, prediction_at=iso_time(inputs.hour), target_at=iso_time(target))
    meta = {k:deepcopy(current[k]) for k in ('source_id','point_key','item_id','unit') if k in current}
    meta.update(source='KEPServer · 提前 90 秒', alignment=alignment)
    source, key = current.get('source_id'), current.get('point_key')
    point = inputs.db.get(TelemetryPoint, (source,key)) if source and key else None
    if not point or not point.enabled:
        return cell(reason='提前 90 秒液位缺失：未指定有效液位采集点', **meta)
    identity = definition_id(point.config)
    candidates = []

    def add(sample):
        if not sample or sample.get('definition_id') != identity:
            return
        try:
            at = parse_time(sample['received_at'])
            if lower <= at <= target:
                candidates.append(sample)
        except (KeyError, TypeError, ValueError):
            pass

    add(raw_sample(point.latest))
    rows = inputs.db.scalars(select(TelemetryReading).where(
        TelemetryReading.source_id == source, TelemetryReading.point_key == key,
        TelemetryReading.received_at >= iso_time(lower), TelemetryReading.received_at <= iso_time(target)
    ).order_by(TelemetryReading.received_at.desc(), TelemetryReading.id.desc()))
    for row in rows:
        add(dict(raw_sample(row.sample) or {}, batch_id=row.batch_id))

    # Project only the needed JSON fragments, not the full decision/evidence payload.
    rows = inputs.db.execute(select(DecisionCycleSample.snapshot['level_observation'],
        DecisionCycleSample.snapshot['cells']['level'], DecisionCycleSample.snapshot['input_timing']).where(
        DecisionCycleSample.sampled_at >= iso_time(lower),
        DecisionCycleSample.sampled_at <= inputs.cutoff))
    for observation, old_level, timing in rows:
        # Pre-delay archives stored the raw level directly. New archives never reuse
        # their shifted model input as a raw observation, even if the field is absent.
        c = observation if isinstance(observation,dict) else old_level if not timing else None
        if not c or c.get('alignment') or c.get('source_id') != source or c.get('point_key') != key:
            continue
        sample = c.get('sample') or {k:c.get(k) for k in
            ('value','raw_value','definition_id','quality','issue','status_code','source_at','server_at','received_at','batch_id')}
        add(sample)
    sample = max(candidates, key=lambda s:parse_time(s['received_at']), default=None)
    if sample is None:
        return cell(reason='提前 90 秒液位缺失：目标时刻之前 30 秒内无采集记录', **meta)
    meta.update(sample=deepcopy(sample), observed_at=sample['received_at'])
    if not usable_at_receipt(sample,point.config) or not availability(sample,point.config,True,target)[0]:
        return cell(reason='提前 90 秒液位缺失：对应采样质量异常或已过期', **meta)
    if not numeric(sample.get('value')) or not 0 <= sample['value'] <= 100:
        return cell(reason='提前 90 秒液位缺失：对应采样超出 0–100% 范围', **meta)
    alignment['offset_seconds'] = (target-parse_time(sample['received_at'])).total_seconds()
    return cell(sample['value'],status='good',reason='采用预测时刻前 90 秒的液位', **meta)


def level_mean(rows, start, end, mean_cell):
    """Aggregate the level actually used by each decision, grouped by prediction time."""
    candidates, policies, identities, definitions = [], set(), set(), set()
    for row in rows:
        c = row.snapshot.get('cells',{}).get('level',{})
        policies.add((c.get('alignment') or {}).get('version','unshifted'))
        if c.get('source_id') or c.get('point_key'):
            identities.add((c.get('source_id'),c.get('point_key')))
        definition = c.get('definition_id') or (c.get('sample') or {}).get('definition_id')
        if definition:
            definitions.add(definition)
        at = business_time(row.sampled_at)
        if start <= at < end:
            # Keep the true observation timestamp; a separate bucket time assigns
            # pre-hour readings to the hour whose predictions used them.
            candidates.append(dict(c, _period_at=row.sampled_at))
    if len(policies)>1:
        return cell(reason='小时内液位时间口径变化，暂不混合均值', unit='%', alignment=deepcopy(POLICY))
    if len(identities)>1 or len(definitions)>1:
        return cell(reason='小时内液位来源或点位定义变化，暂不混合均值', unit='%', alignment=deepcopy(POLICY))
    result = mean_cell('level',candidates,{},start,end)
    result.update(unit='%',source='逐次预测采用的液位',alignment=deepcopy(POLICY))
    result['reason'] += '；每次预测采用提前 90 秒液位'
    return result
