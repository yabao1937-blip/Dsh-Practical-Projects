"""Recompute the experimental rule at real retained decision instants, read-only."""
from copy import deepcopy
from datetime import timedelta
from hashlib import sha256
import json
from collections import Counter
from functools import lru_cache
from pathlib import Path

from fastapi import HTTPException

from sqlalchemy import select, text

from ..gpt_density.candidate import evaluate_candidate, timestamp
from ..gpt_density.candidate import number
from ..hourly_brief.inputs import LOCAL
from ..telemetry.domain import iso_time
from ..telemetry.metadata import unpack
from .advice_provenance import select_recorded, origin_class
from .density_candidates import action_evidence, base_evidence, cell_view
from .models import DensityAction, GptHourlyDensityAdvice, DecisionCycleContext, GptHourlyDensityAnchor
from .references import resolve_snapshots, VERSION, walk


def projected_samples(db, start, end, *, page_size=60):
    """Only required subtrees; resolve pinned telemetry by its immutable row id."""
    after = -1
    while True:
        rows = list(db.execute(text('''SELECT slot,sampled_at,context_key,
            json_extract(snapshot,'$.storage_version') AS storage_version,
            json_extract(snapshot,'$.cadence_seconds') AS cadence_seconds,
            json_extract(snapshot,'$.density_channels.A304') AS density_a304,
            json_extract(snapshot,'$.inputs.is_stoppage') AS stoppage,
            json_extract(snapshot,'$.target') AS target
            FROM decision_cycle_samples
            WHERE sampled_at>=:start AND sampled_at<:end AND slot>:after
            ORDER BY slot LIMIT :page_size'''), dict(start=iso_time(start),end=iso_time(end),
                after=after,page_size=page_size)).mappings())
        if not rows:
            break
        snapshots = []
        provenance = []
        for row in rows:
            def decode(key):
                raw = row[key]
                return unpack(db,json.loads(raw),projected=True) if isinstance(raw,str) else raw
            snapshots.append(dict(storage_version=row['storage_version'],
                density_channels=dict(A304=decode('density_a304') or {}),
                inputs=dict(is_stoppage=decode('stoppage') or {}), target=decode('target') or {}))
            provenance.append(dict(reading_ids=sorted({c['$reading'] for c in walk(snapshots[-1]) if '$reading' in c}),
                manual_ids=sorted({c['$manual'] for c in walk(snapshots[-1]) if '$manual' in c})))
        hydrated = resolve_snapshots(db,snapshots)
        for row,snapshot,provenance_row in zip(rows,hydrated,provenance):
            at=timestamp(row['sampled_at'])
            future=[]
            for cell in walk(snapshot):
                for key in ('received_at','created_at','entered_at'):
                    stamp=timestamp(cell.get(key))
                    if stamp and stamp>at:
                        future.append(dict(field=key,at=cell[key]))
            provenance_row['future_input_times']=future
            yield dict(slot=row['slot'],sampled_at=row['sampled_at'],context_key=row['context_key'],
                cadence_seconds=row['cadence_seconds'],storage_version=row['storage_version'],
                provenance=provenance_row,snapshot=snapshot)
        after = rows[-1]['slot']


def advice_points(db, start, end):
    rows = db.scalars(select(GptHourlyDensityAdvice).where(
        GptHourlyDensityAdvice.generated_at >= iso_time(start.replace(minute=0,second=0,microsecond=0)),
        GptHourlyDensityAdvice.generated_at <= iso_time(end)).order_by(
            GptHourlyDensityAdvice.generated_at,GptHourlyDensityAdvice.id))
    return [dict(at=row.generated_at,apply_start=row.apply_start,**deepcopy(row.snapshot)) for row in rows]


def sample_candidate(sample, points, latest=None, *, context_ok=True, context_reason=None):
    now = timestamp(sample['sampled_at'])
    hour = now.replace(minute=0,second=0,microsecond=0)
    point,ignored = select_recorded(points,iso_time(hour),now)
    ref = deepcopy(point or {})
    source = origin_class(ref) if point else 'unknown'
    ref.update(identity=source,generated_at=ref.get('at'),
        availability='before-observation' if source=='online-default' else 'unavailable')
    if not context_ok:
        ref.update(status='blocked',value=None,reason=context_reason or '历史来源、模型或参数口径不兼容')
    if (sample.get('provenance') or {}).get('future_input_times'):
        ref.update(status='blocked',value=None,reason='引用输入存在评估时刻之后的接收/创建时间')
    captured=timestamp((ref.get('basis') or {}).get('captured_at'))
    if point and (captured is None or captured>now):
        ref.update(status='blocked',value=None,reason='小时设定基准尚未捕获或捕获时刻晚于当前采样')
    snapshot = sample['snapshot']
    pair = (snapshot.get('density_channels') or {}).get('A304') or {}
    setting = cell_view(pair.get('setpoint'))
    actual = cell_view(pair.get('actual'))
    stop = (snapshot.get('inputs') or {}).get('is_stoppage') or {}
    # A target edit is an objective change, not permission to use future ash.
    target = snapshot.get('target') or {}
    if point and target != ref.get('target') and ref.get('value') is not None:
        from ..gpt_density.execution import empirical_target
        ash,basis = ref.get('ash') or {},ref.get('basis') or {}
        if (ash.get('value') is not None and basis.get('value') is not None
                and target.get('min') is not None and target.get('max') is not None):
            ref.update(value=round(empirical_target(ash['value'],basis['value'],target)['value'],3),
                target=deepcopy(target),target_recomputed=True)
    e = base_evidence(now,ref,setting,actual,
        dict(status=stop.get('status'),stopped=stop.get('value'),observed_at=stop.get('observed_at')),
        action_evidence(latest,setting),mode='historical-series-recalculation')
    result = evaluate_candidate(e)
    result.update(at=sample['sampled_at'],slot=sample['slot'],
        source=dict(kind='decision-cycle-sample',context_key=sample['context_key'],
            cadence_seconds=sample['cadence_seconds'],storage_version=sample['storage_version'],
            ignored_replay_count=ignored,reference_known_at=ref.get('generated_at'),
            retrospective_recalculation=True,physical_outcome_simulated=False,
            context_compatible=context_ok,context_reason=context_reason,
            **deepcopy(sample.get('provenance') or {})),
        input_evidence=dict(setpoint=setting,actual=actual,operating=deepcopy(stop)))
    return result


def context_compatibility(db, sample, cache):
    """Use immutable historical definitions, not today's model or source settings."""
    from .quality_advice import compatible_contexts
    key=sample['context_key']
    if key not in cache['contexts']:
        row=db.get(DecisionCycleContext,key)
        cache['contexts'][key]=deepcopy(row.definition) if row else None
    hour=iso_time(timestamp(sample['sampled_at']).replace(minute=0,second=0,microsecond=0))
    if hour not in cache['anchors']:
        row=db.get(GptHourlyDensityAnchor,hour)
        cache['anchors'][hour]=deepcopy(row.snapshot) if row else None
    definition=cache['contexts'][key]
    anchor=cache['anchors'][hour]
    if not definition or not anchor or not anchor.get('context'):
        return False,'缺少历史采样或小时基准定义，不能跳过来源/参数保护'
    return compatible_contexts(db,[],definition,[anchor['context']])


def compact_result(row):
    """Plot payload keeps real instants and status boundaries, no invented points."""
    c=row['candidate']
    return dict(at=row['at'],slot=row['slot'],value=c['value'],status=c['status'],
        reason='；'.join(c['reasons']),reason_codes=c['reason_codes'],delta=c['delta'],
        setting=row['setting'],ash=row['basis']['ash'],target=row['basis']['target'],
        band=row['basis']['band'],quality=row['quality'],observation=row['observation'],
        source=row['source'],input_evidence=row['input_evidence'])


def plot_indices(points, maximum=3000, *, preserve_trim_edges=False):
    if len(points)<=maximum:
        return list(range(len(points)))
    selected={0,len(points)-1}
    for i in range(1,len(points)):
        a,b=points[i-1],points[i]
        if (b.get('break_before') or (preserve_trim_edges and trend_exclusion(a) != trend_exclusion(b))
                or (a['status'],a['reason_codes'],a['quality']['level']) != (b['status'],b['reason_codes'],b['quality']['level'])):
            selected.update((i-1,i))
    width=max(1,(len(points)+(maximum//6)-1)//(maximum//6))
    for start in range(0,len(points),width):
        ids=range(start,min(len(points),start+width))
        for getter in (lambda p:p['value'],lambda p:(p.get('input_evidence',{}).get('actual') or {}).get('value'),
                       lambda p:(p.get('input_evidence',{}).get('setpoint') or {}).get('value')):
            finite=[i for i in ids if isinstance(getter(points[i]),(float,int))]
            if finite:
                selected.update((min(finite,key=lambda i:getter(points[i])),max(finite,key=lambda i:getter(points[i]))))
    return sorted(selected)


def with_gap_markers(points):
    """Detect gaps BEFORE downsampling using recorded cadence, not plot spacing."""
    result=deepcopy(points)
    previous=None
    for point in result:
        gap=(timestamp(point['at'])-timestamp(previous['at'])).total_seconds() if previous else None
        cadences=[p.get('source',{}).get('cadence_seconds') for p in (previous,point) if p]
        cadence=max((c for c in cadences if number(c) and c>0),default=60)
        point.update(break_before=bool(gap is not None and gap>max(15,1.5*cadence)),
            sample_gap_seconds=round(gap,3) if gap is not None else None)
        previous=point
    return result


def sample_cadence(point):
    cadence = point.get('source', {}).get('cadence_seconds')
    return cadence if number(cadence) and cadence > 0 else 60


def trend_exclusion(point):
    operating = (point.get('input_evidence') or {}).get('operating') or {}
    raw = operating.get('raw_value')
    if (number(raw) and raw == 0 and operating.get('quality') == 'good'
            and operating.get('status') in ('held', 'good')):
        return 'stopped'
    actual = (point.get('observation') or {}).get('actual_reading') or {}
    if actual.get('usable') is False:
        return 'invalid_actual'
    return None


def trend_exclusions(points, start, end):
    """Clip full-sample evidence to the query without filling real acquisition gaps."""
    intervals = []
    for i, point in enumerate(points):
        left = timestamp(point['at'])
        if left >= end:
            break
        reason = trend_exclusion(point)
        if not reason:
            continue
        following = points[i + 1] if i + 1 < len(points) else None
        right = timestamp(following['at']) if following else end
        if not following or following.get('break_before'):
            right = min(right, left + timedelta(seconds=sample_cadence(point)))
        left, right = max(left, start), min(right, end)
        if left >= right:
            continue
        if intervals and intervals[-1][2] == reason and left == intervals[-1][1]:
            intervals[-1] = (intervals[-1][0], right, reason)
        else:
            intervals.append((left, right, reason))
    return [dict(start=iso_time(left), end=iso_time(right), reason=reason)
            for left, right, reason in intervals]


def channel_points(points, key):
    result=[]
    for p in points:
        cell=p['input_evidence'].get(key) or {}
        reading=(p.get('observation') or {}).get('actual_reading') or {}
        actual=key=='actual'
        value=reading.get('value') if actual and reading.get('usable') else None
        if not actual:
            value=cell.get('value') if cell.get('status')=='good' else None
        result.append(dict(at=p['at'],value=value,raw_value=cell.get('value'),
            status=reading.get('status','invalid') if actual else cell.get('status'),
            source_status=cell.get('status'),observed_at=cell.get('observed_at'),
            received_at=cell.get('received_at'),slot=p['slot'],reading_ids=p['source'].get('reading_ids'),
            break_before=p.get('break_before',False),sample_gap_seconds=p.get('sample_gap_seconds')))
    return result


def hourly_finals(points, start, cutoff):
    """Select global last records first, then filter completed business hours.

    Input is the full frozen series, never its plot/downsample or custom slice.
    Unavailable last records remain unavailable; an earlier good is not final.
    """
    aggregation='last-record-of-completed-hour'
    by_hour={}
    for point in points:
        at=timestamp(point.get('at'))
        if at is None:
            continue
        hour=at.astimezone(LOCAL).replace(minute=0,second=0,microsecond=0)
        previous=by_hour.get(hour)
        if previous is None or at>=timestamp(previous['at']):
            by_hour[hour]=point
    result=[]
    for hour,point in sorted(by_hour.items()):
        end=hour+timedelta(hours=1)
        final_at=timestamp(point['at'])
        if end>cutoff or not start<=final_at<cutoff:
            continue
        row=deepcopy(point)
        row.update(hour_start=iso_time(hour),hour_end=iso_time(end),final_sample_at=point['at'],
            complete=True,aggregation=aggregation,
            final_valid=number(point.get('value')) and point.get('status') in ('adjust','hold','wait'),
            hourly_break_before=bool(result and hour-timestamp(result[-1]['hour_start'])>timedelta(hours=1)))
        result.append(row)
    valid=sum(row['final_valid'] for row in result)
    summary=dict(aggregation=aggregation,total_hours=len(result),valid_hours=valid,
        missing_hours=len(result)-valid,statuses=dict(Counter(row.get('status') for row in result)),
        cutoff=iso_time(cutoff),selection='global-final-record-then-final-sample-time-filter')
    return result,summary


@lru_cache(maxsize=1)
def _frozen(path, modified):
    root=Path(path).parent
    manifest=json.loads((root/'manifest.json').read_text(encoding='utf-8'))
    raw=Path(path).read_bytes()
    if sha256(raw).hexdigest()!=manifest['series']['sha256']:
        raise ValueError('Frozen series checksum mismatch')
    result=json.loads(raw)
    points=[]
    for artifact in manifest['artifacts']:
        name=artifact['path']
        if not name.startswith('recalculated-'):
            continue
        if Path(name).name!=name:
            raise ValueError('Invalid frozen artifact identity')
        raw=(root/'raw'/name).read_bytes()
        if sha256(raw).hexdigest()!=artifact['sha256']:
            raise ValueError('Frozen point checksum mismatch')
        points.extend(json.loads(raw))
    return result,with_gap_markers(points)


def _snapshot_dirs():
    """All frozen DB7 snapshot dirs with a series file, newest first (name sort).
    Manifest/artifact verification happens inside _frozen; corrupt newest dirs fall back."""
    from ..config import BASE_DIR
    base = BASE_DIR.parent/'output'
    return sorted((p for p in base.glob('density-optimization-db7-*')
        if p.is_dir() and (p/'series.json').is_file()), reverse=True)


def _load_latest_frozen():
    """Newest snapshot that passes manifest/artifact verification; falls back to older ones."""
    problems=[]
    for directory in _snapshot_dirs():
        path=directory/'series.json'
        try:
            return _frozen(str(path),path.stat().st_mtime_ns),directory
        except (OSError,ValueError,KeyError,TypeError) as error:
            problems.append(f'{directory.name}: {error}')
    raise FileNotFoundError('; '.join(problems) or 'no density-optimization-db7-* snapshot directory')


def read_frozen(hours=168, start=None, end=None):
    """No DB queries or writes. Serve the latest verified frozen seven-day recalculation."""
    from .density_candidates import require_enabled
    require_enabled()
    try:
        (source,all_points),snapshot=_load_latest_frozen()
    except (OSError,ValueError,KeyError,TypeError):
        raise HTTPException(503,detail=dict(density_policy='density-candidate-v1',
            message='新版本七天数据库重算文件尚未就绪或校验失败')) from None
    lower,upper=timestamp(source['period']['start']),timestamp(source['period']['end'])
    if start is None and end is None:
        end=upper
        start=end-timedelta(hours=hours)
    elif start is None or end is None:
        raise HTTPException(422,'自定义范围须同时提供开始和结束时间')
    if (start.tzinfo is None or end.tzinfo is None or start.second or end.second or start.microsecond or end.microsecond
            or start<lower or end>upper or end<=start):
        raise HTTPException(422,'新版本已冻结近7天；请选择该窗口内含时区的分钟级范围')
    selected=[p for p in all_points if start<=timestamp(p['at'])<end]
    plotted=[selected[i] for i in plot_indices(selected,preserve_trim_edges=True)]
    result=deepcopy(source)
    result.update(period=dict(start=iso_time(start),end=iso_time(end),end_exclusive=True),suggestions=plotted,
        summary=dict(total_samples=len(selected),plot_samples=len(plotted),
            statuses=dict(Counter(p['status'] for p in selected)),
            reasons=dict(Counter(code for p in selected for code in p['reason_codes']))),
        coverage=dict(real_samples=len(selected),non_null_candidates=sum(p['value'] is not None for p in selected),
            missing_samples=sum(p['value'] is None for p in selected)))
    result['channels']=dict(A304=channel_points(plotted,'setpoint'),actual=channel_points(plotted,'actual'))
    result['ash']=[p for p in result['ash'] if start<=timestamp(p['at'])<end]
    result['source']['fixed_window']=deepcopy(source['period'])
    result['source']['snapshot']=snapshot.name
    result['trend_exclusions']=trend_exclusions(all_points,start,end)
    hours_final,hours_summary=hourly_finals(all_points,start,end)
    result.update(hourly_suggestions=hours_final,hourly_summary=hours_summary,
        latest_completed_hour=deepcopy(hours_final[-1]) if hours_final else None)
    return result
