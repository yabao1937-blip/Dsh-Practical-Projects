"""Observe setpoint changes; never assume who acted, causality, or write a device."""
from copy import deepcopy
from sqlalchemy import select
from ..hourly_brief.inputs import Inputs, business_time, numeric
from ..telemetry.domain import iso_time
from .models import DensityAction, DensityObservation, DensityActionEntry
from .statistics import digest
from .evidence import state


def density_channels(db, now, compact):
    inputs = Inputs(db, now, live=True, authoritative=True)
    result = {}
    for channel in ('A303','A304'):
        result[channel] = {key:compact(inputs.telemetry(f'TIA_4_1.TIA_4_1.{channel}/{item}',unit='g/cm³'))
            for key,item in (('actual','MD'),('setpoint','MD_SET'))}
    return result


def observe(db, row):
    for channel,pair in row.snapshot.get('density_channels',{}).items():
        c = pair['setpoint']
        key = channel
        cached = db.get(DensityObservation,key)
        good = c.get('status') == 'good' and numeric(c.get('value')) and c['value'] > 0
        if not good:
            # Missing rows break continuity but preserve the last known setpoint.
            if cached:
                cached.snapshot = dict(cached.snapshot, gap=True)
            continue
        current = dict(pair=pair, slot=row.slot, sampled_at=c.get('observed_at') or row.sampled_at,
                       decision_at=row.sampled_at, context_key=row.context_key, operating_state=state(row),
                       suggestion=deepcopy(row.snapshot.get('suggested_density')), gap=False)
        if cached:
            old = cached.snapshot
            previous = old['pair']['setpoint']
            same = all(c.get(k) and c.get(k)==previous.get(k) for k in ('source_id','point_key','definition_id'))
            if same and abs(c['value']-previous['value']) >= .0005:
                identifier = digest([channel,old['slot'],row.slot,c.get('batch_id')])
                db.add(DensityAction(id=identifier,detected_at=row.sampled_at,channel=channel,evidence=dict(
                    before=old, after=current,
                    continuous=not old.get('gap') and row.slot-old['slot'] <= 3 and
                        0 < (business_time(current['sampled_at'])-business_time(old['sampled_at'])).total_seconds() <= 15,
                    note='检测到设定值变化；实际操作时刻位于两次观测之间，来源及是否采纳建议待人工确认。')))
            cached.snapshot = current
        else:
            db.add(DensityObservation(key=key,snapshot=current))


def action_view(db, action):
    entries = list(db.scalars(select(DensityActionEntry).where(DensityActionEntry.action_id==action.id)
                            .order_by(DensityActionEntry.id)))
    assays = [e for e in entries if e.content.get('kind') == 'assay']
    phases = {e.content.get('phase') for e in assays}
    before,after = action.evidence['before'],action.evidence['after']
    warnings = ['工况无法保证单一变量，记录仅用于后续评估；响应暂按 0 延迟，报告时间单独记录。']
    for label,observed in (('调前',before),('调后',after)):
        pair=observed['pair'];actual=pair['actual'];setting=pair['setpoint']
        if actual.get('status')!='good' or not numeric(actual.get('value')) or actual['value']<=0 or abs(actual['value']-1)<1e-9:
            warnings.append(label+'实测密度异常或缺失，不能作为有效响应')
        elif actual.get('batch_id')!=setting.get('batch_id') or actual.get('observed_at')!=setting.get('observed_at'):
            warnings.append(label+'实测与设定读数不属于同一批次，仅作对照')
    if not action.evidence['continuous']:
        warnings.append('两次观测间存在缺口，不能确定操作次数或准确时刻')
    if before['context_key'] != after['context_key']:
        warnings.append('调密前后计算口径变化')
    if before.get('operating_state') != after.get('operating_state'):
        warnings.append('调密前后系统、开停机或质量状态变化')
    if phases != {'before','after'}:
        warnings.append('尚缺调前或调后化验，不能估计灰分变化')
    # Find intervening actions against the sample window, not against report arrival time.
    other = list(db.scalars(select(DensityAction).where(DensityAction.channel==action.channel,
                                                       DensityAction.id!=action.id)))
    for entry in assays:
        body = entry.content
        a,b = business_time(body['sample_start']),business_time(body['sample_end'])
        relevant_start = min(a,business_time(before['sampled_at']))
        relevant_end = max(b,business_time(after['sampled_at']))
        if any(business_time(x.evidence['before']['sampled_at']) <= relevant_end and
               business_time(x.evidence['after']['sampled_at']) >= relevant_start for x in other):
            warnings.append('采样关联期间还存在其他调密，不能归因于单次操作')
            break
    return dict(id=action.id,detected_at=action.detected_at,channel=action.channel,evidence=action.evidence,
        revision=len(entries),entries=[dict(id=e.id,client_id=e.client_id,created_at=e.created_at,content=e.content) for e in entries],
        status='已有前后样品 · 待实验分析' if phases=={'before','after'} else '等待化验/确认',warnings=warnings)


def assess_window(db,action,start,end):
    """Freeze a descriptive stability check, never an estimate of causal response."""
    from datetime import timedelta
    from types import SimpleNamespace
    from .archive import archived_rows
    from .models import DecisionCycleSample
    from .references import hydrate_rows
    from .evidence import SEGMENT_POLICY, statistics, weights
    first=min(start,business_time(action.evidence['before']['sampled_at']))
    last=max(end,business_time(action.evidence['after']['sampled_at']))
    if (last-first).total_seconds()>3600:
        return dict(status='insufficient',reason='采样与调密间隔超过 1 小时，需人工核对完整工况，不能推断单次响应')
    rows=list(db.scalars(select(DecisionCycleSample).where(DecisionCycleSample.sampled_at>=iso_time(first),
        DecisionCycleSample.sampled_at<=iso_time(last)).order_by(DecisionCycleSample.slot)))
    cursor=first.replace(minute=0,second=0,microsecond=0)
    by_slot={r.slot:r for r in hydrate_rows(db,rows)}
    while cursor<=last:
        for r in archived_rows(db,iso_time(cursor)):
            if iso_time(first)<=r['sampled_at']<=iso_time(last):
                by_slot.setdefault(r['slot'],SimpleNamespace(**r))
        cursor+=timedelta(hours=1)
    rows=sorted(by_slot.values(),key=lambda r:r.slot)
    if len(rows)<2:
        return dict(status='insufficient',reason='缺少采样关联区间的逐次工况证据')
    w=weights(rows,last+timedelta(seconds=5))
    factors={key:statistics(rows,key,w) for key in SEGMENT_POLICY['thresholds']}
    warnings=[]
    if len({digest(state(r)) for r in rows})>1:
        warnings.append('系统、质量或计算口径在采样关联期间变化')
    if any(rows[i+1].slot-rows[i].slot>3 for i in range(len(rows)-1)) or (business_time(rows[0].sampled_at)-first).total_seconds()>15 or (last-business_time(rows[-1].sampled_at)).total_seconds()>15:
        warnings.append('逐次证据存在超过 15 秒的缺口')
    for key,limit in SEGMENT_POLICY['thresholds'].items():
        # Density is deliberately adjusted; other inputs confound the response.
        if key=='density':
            continue
        c=factors[key]
        if c['count']!=len(rows):
            warnings.append(key+' 有缺失或沿用值')
        elif c['maximum']-c['minimum']>=limit:
            warnings.append(key+' 存在明显变化')
    return dict(status='changed' if warnings else 'descriptive_only',
        reason='；'.join(warnings) if warnings else '未检出超过初始阈值的变化；仍不能认定单变量因果关系',
        start=iso_time(first),end=iso_time(last),sample_count=len(rows),factors=factors)
