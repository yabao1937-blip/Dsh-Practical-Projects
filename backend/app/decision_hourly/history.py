"""Read retained observations without replaying today's model over yesterday's data."""
import json
from collections import defaultdict
from copy import deepcopy

from sqlalchemy import select

from ..hourly_brief.inputs import Inputs, LOCAL, business_time
from ..models import ManualMeasurement, CoalRecord
from ..services.decision_sources import NODE_CELLS, GROUPS, config_view, collect_shared
from ..telemetry.derived import DENSITY_AVERAGE_MEMBERS
from ..telemetry.domain import iso_time
from ..telemetry.models import TelemetryReading
from ..telemetry.retained import raw_sample, usable_at_receipt
from .statistics import COMPUTED, digest, event_key, mean_cell, valid


def historical_cells(db, start, end, rows):
    from datetime import timedelta
    inputs = Inputs(db, end-timedelta(microseconds=1))
    sources = config_view(db, inputs.hour, inputs)
    fallback = collect_shared(inputs, sources)
    # Use the frozen end-of-hour configuration if it exists (including old point labels).
    if rows and rows[-1].snapshot['sources']['revision']==sources['revision']:
        sources = deepcopy(rows[-1].snapshot['sources'])
    series = defaultdict(list)
    for key,c in fallback.items():
        if c.get('status') in ('manual','historical','default'):
            extra = dict(c)
            if c.get('metric') and c.get('record_id'):
                extra['_event'] = f'manual:{c["record_id"]}'
            elif c.get('source') in ('历史浮精记录','手工记录') and c.get('record_id'):
                extra['_event'] = f'record:{c["record_id"]}'
            series[key].append(extra)
    by_point = defaultdict(dict)
    for r in db.scalars(select(TelemetryReading).where(
            TelemetryReading.received_at >= iso_time(start), TelemetryReading.received_at < iso_time(end))):
        by_point[(r.source_id, r.point_key)][r.received_at] = {**raw_sample(r.sample), 'batch_id':r.batch_id}
    # A last raw sample can be newer than the throttled history row.
    for group in inputs.points.values():
        for p in group:
            s = raw_sample(p.latest)
            if s and iso_time(start) <= s['received_at'] < iso_time(end):
                by_point[(p.source_id, p.key)][s['received_at']] = s

    def point_series(p):
        if p is None:
            return []
        result = []
        for s in by_point[(p.source_id, p.key)].values():
            result.append(dict(value=s.get('value'), status='good' if usable_at_receipt(s,p.config) else 'missing',
                observed_at=s['received_at'], source_id=p.source_id, point_key=p.key, item_id=p.config.get('item_id'),
                source='KEPServer', unit=p.config.get('unit') or '', batch_id=s.get('batch_id'),definition_id=s.get('definition_id')))
        return result

    for node,key in NODE_CELLS.items():
        choice = sources['resolved_choices'].get(node)
        if not choice:
            continue
        source, point_key = json.loads(choice)
        if source == 'manual-measurements':
            for r in db.scalars(select(ManualMeasurement).where(ManualMeasurement.metric==point_key,
                    ManualMeasurement.observed_at>=iso_time(start), ManualMeasurement.observed_at<iso_time(end),
                    ManualMeasurement.created_at<iso_time(end))):
                series[key].append(dict(value=r.value,status='manual',observed_at=r.observed_at,
                    created_at=r.created_at,record_id=r.id,metric=r.metric,source_id=source,point_key=point_key,
                    source='手工补录',_event=f'manual:{r.id}'))
        elif point_key in GROUPS:
            for label,item in GROUPS[point_key]['members']:
                series[f'{key}:{label}'].extend(point_series(inputs.point(item,source)))
        elif point_key == 'a303_a304_density_average':
            parts = [point_series(inputs.point(item,source)) for _,item in DENSITY_AVERAGE_MEMBERS]
            right = {(c['observed_at'],c['batch_id']):c for c in parts[1]}
            for a in parts[0]:
                b = right.get((a['observed_at'],a['batch_id']))
                if b and a['batch_id']:
                    series[key].append(dict(a,value=(a['value']+b['value'])/2 if valid(a,'density') and valid(b,'density') else None,
                        point_key=point_key,item_id=point_key,_event='density-average:'+a['observed_at']))
        else:
            p = next((p for group in inputs.points.values() for p in group if p.source_id==source and p.key==point_key),None)
            series[key].extend(point_series(p))
    for belt in ('501','502'):
        series['belt'+belt].extend(point_series(inputs.point(f'TIA_3_4.TIA_3_4.{belt}/COAL_CAP')))

    # Unbound manual metrics still have durable event histories.
    for key,c in fallback.items():
        node = next((n for n,k in NODE_CELLS.items() if k==key),None)
        if node and sources['resolved_choices'].get(node):
            continue
        if c.get('metric'):
            for r in db.scalars(select(ManualMeasurement).where(ManualMeasurement.metric==c['metric'],
                    ManualMeasurement.observed_at>=iso_time(start), ManualMeasurement.observed_at<iso_time(end),
                    ManualMeasurement.created_at<iso_time(end))):
                series[key].append(dict(c,value=r.value,observed_at=r.observed_at,created_at=r.created_at,
                    record_id=r.id,_event=f'manual:{r.id}'))
        elif c.get('source') in ('历史浮精记录','手工记录') and key in ('float_ash','float_amount'):
            for r in db.scalars(select(CoalRecord).where(CoalRecord.category=='float',
                    CoalRecord.ts>=start.astimezone(LOCAL).strftime('%Y-%m-%d %H:%M:%S'),
                    CoalRecord.ts<end.astimezone(LOCAL).strftime('%Y-%m-%d %H:%M:%S'),
                    CoalRecord.created_at<end.astimezone(LOCAL).replace(tzinfo=None))):
                series[key].append(dict(c,value=r.ash_content if key=='float_ash' else r.coal_amount,
                    observed_at=iso_time(business_time(r.ts)),record_id=r.id,_event=f'record:{r.id}'))

    seen_decisions = set()
    for r in rows:
        snapshot = r.snapshot
        duplicate = r.fingerprint in seen_decisions
        seen_decisions.add(r.fingerprint)
        for key,c in snapshot['cells'].items():
            if key == 'suggested_density':
                continue
            node = next((n for n,k in NODE_CELLS.items() if k==key),None)
            if node and snapshot['sources']['resolved_choices'].get(node) != sources['resolved_choices'].get(node):
                continue
            if key in COMPUTED:
                if not duplicate:
                    series[key].append(dict(c,observed_at=r.sampled_at,_event=r.fingerprint))
            elif isinstance(c.get('components'),dict):
                for label,part in c['components'].items():
                    series[f'{key}:{label}'].append(part)
            else:
                # The same telemetry event in raw history and the decision archive counts once.
                if c.get('status') in ('held','stopped'):
                    continue
                copy = dict(c)
                if c.get('metric') and c.get('record_id'):
                    copy['_event'] = f'manual:{c["record_id"]}'
                elif c.get('source') in ('历史浮精记录','手工记录') and c.get('record_id'):
                    copy['_event'] = f'record:{c["record_id"]}'
                series[key].append(copy)
            if c.get('status') in ('manual','historical','default') and valid(c,key):
                old = fallback.get(key,{})
                if (c.get('observed_at') or '') >= (old.get('observed_at') or ''):
                    fallback[key] = deepcopy(c)

    # Normalize telemetry identity across both stores; reject held/zero/1 rather than reusing old values.
    for key,candidates in series.items():
        for c in candidates:
            if c.get('source_id') and c.get('source_id')!='manual-measurements':
                c['_event'] = digest([c.get('source_id'),c.get('point_key') or c.get('item_id'),c.get('observed_at')])
    result = {}
    for key,c in fallback.items():
        if key in ('systems','desliming','float_press'):
            labels = [label for g in GROUPS.values() for label,_ in g['members']
                      if any(k==f'{key}:{label}' for k in series)]
            if not labels:
                labels = list((c.get('components') or {}).keys())
            parts = {label:mean_cell(f'{key}:{label}',series[f'{key}:{label}'],{},start,end) for label in labels}
            result[key] = dict(c,value={label:p['value'] for label,p in parts.items()},components=parts,
                status='average',reason='有效状态样本中的运行占比（1 为运行，0 为停止）')
        elif key != 'suggested_density':
            result[key] = mean_cell(key,series[key],c,start,end)
            definitions = {p['definition_id'] for p in series[key] if p.get('definition_id')}
            if len(definitions)>1:
                result[key].update(value=None,status='missing',reason='小时内采集点定义变化，暂不混合不同口径的均值')
    for key in COMPUTED:
        result[key] = mean_cell(key,series[key],dict(unit='t/h' if key=='heavy_amount' else '%',source='逐次决策存档'),start,end)
    densities = [c for c in series['density'] if valid(c,'density') and c.get('observed_at') and
                 start <= business_time(c['observed_at']) < end]
    basis = max(densities,key=lambda c:c['observed_at'],default={})
    return result, sources, basis
