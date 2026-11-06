"""Freeze seven days of real DB inputs and recompute, without production writes."""
import argparse
from collections import Counter
from copy import deepcopy
from datetime import timedelta
from hashlib import sha256
import json
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import select,text
from app.config import REMOTE_READONLY
from app.database import SessionLocal
from app.decision_hourly.density_series import (advice_points,projected_samples,sample_candidate,
    context_compatibility,compact_result,plot_indices,channel_points,with_gap_markers)
from app.decision_hourly.models import DensityAction,DecisionAshSettlement
from app.gpt_density.candidate import CandidateParameters
from app.services.manual_measurements import measurement_time
from app.telemetry.domain import iso_time,parse_time


def write(path,value):
    raw=json.dumps(value,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode('utf-8')
    path.write_bytes(raw)
    return dict(path=path.name,sha256=sha256(raw).hexdigest(),bytes=len(raw))


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--end')
    parser.add_argument('--out',help='snapshot directory name under output/ (default: density-optimization-db7-<END DATE>)')
    args=parser.parse_args()
    if not REMOTE_READONLY:
        raise RuntimeError('Remote readonly-test is required; no local database fallback')
    root=Path(__file__).resolve().parents[2]
    with SessionLocal() as db:
        host_now=parse_time(db.execute(text("SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now')")).scalar_one())
        end=parse_time(args.end) if args.end else host_now.replace(second=0,microsecond=0)
        if end>host_now or end.second or end.microsecond:
            raise ValueError('end must be a non-future aware minute boundary')
    out=root/'output'/(args.out or 'density-optimization-db7-'+end.strftime('%Y%m%d'))
    raw=out/'raw'
    raw.mkdir(parents=True,exist_ok=True)
    with SessionLocal() as db:
        start=end-timedelta(days=7)
        points=advice_points(db,start,end)
        actions=list(db.scalars(select(DensityAction).where(DensityAction.channel=='A304',
            DensityAction.detected_at>=iso_time(start-timedelta(days=1)),
            DensityAction.detected_at<iso_time(end)).order_by(DensityAction.detected_at,DensityAction.id)))
        prior=db.scalar(select(DensityAction).where(DensityAction.channel=='A304',
            DensityAction.detected_at<iso_time(start-timedelta(days=1))).order_by(DensityAction.detected_at.desc()).limit(1))
        if prior:
            actions.insert(0,prior)
        manual=[dict(row) for row in db.execute(text('''SELECT id,metric,value,unit,observed_at,created_at,time_basis
            FROM manual_measurements WHERE metric='heavy_clean_coal_ash' AND created_at<=:end
            ORDER BY observed_at,id LIMIT 2000'''),dict(end=iso_time(end))).mappings()]
        # Background curve is explicitly retrospective and never enters replay inputs.
        ash=[dict(at=r.start,value=r.snapshot.get('item',{}).get('value'),
            provisional=r.snapshot.get('item',{}).get('provisional'),generated_at=r.generated_at,
            role='retrospective-production-background') for r in db.scalars(select(DecisionAshSettlement).where(
                DecisionAshSettlement.start>=iso_time(start.replace(minute=0,second=0,microsecond=0)),
                DecisionAshSettlement.start<iso_time(end)).order_by(DecisionAshSettlement.start))]
    artifacts=[]
    artifacts.append(write(raw/'advice-revisions.json',points))
    action_data=[dict(id=a.id,detected_at=a.detected_at,channel=a.channel,evidence=a.evidence) for a in actions]
    artifacts.append(write(raw/'action-context.json',action_data))
    artifacts.append(write(raw/'manual-availability.json',manual))
    cache=dict(contexts={},anchors={})
    all_points=[]
    sample_count=0
    minute=start.replace(minute=0,second=0,microsecond=0)
    while minute<end:
        a,b=max(start,minute),min(end,minute+timedelta(hours=1))
        with SessionLocal() as db:
            samples=list(projected_samples(db,a,b))
            if not samples:
                # No hot rows is not proof of absence. Consult durable/cold evidence.
                from app.decision_hourly.archive import archived_rows
                def project(s):
                    return dict(storage_version=s.get('storage_version'),
                        density_channels=s.get('density_channels') or {},inputs=s.get('inputs') or {},target=s.get('target'))
                samples=[dict(r,cadence_seconds=r['snapshot'].get('cadence_seconds'),
                    storage_version=r['snapshot'].get('storage_version'),provenance=dict(archive=True))
                    for r in archived_rows(db,iso_time(minute),project_snapshot=project)
                    if iso_time(a)<=r['sampled_at']<iso_time(b)]
            results=[]
            for sample in samples:
                latest=next((v for v in reversed(actions) if v.detected_at<=sample['sampled_at']),None)
                ok,reason=context_compatibility(db,sample,cache)
                result=sample_candidate(sample,points,latest,context_ok=ok,context_reason=reason)
                # Resolve chemical assay availability at the archive's actual known-at cutoff.
                ref_at=result['source'].get('reference_known_at')
                ash_basis=result['basis']['ash']
                eligible=[r for r in manual if ref_at and r['created_at']<=ref_at and r['observed_at']<=ref_at
                    and ash_basis.get('end') and measurement_time(SimpleNamespace(**r))<ash_basis['end']]
                chosen=max(eligible,key=lambda r:(measurement_time(SimpleNamespace(**r)),r['id'])) if eligible else None
                result['source']['heavy_assay_availability']=deepcopy(chosen)
                result['source']['density_guide_context_key']=sample['context_key']
                results.append(compact_result(result))
        tag=minute.strftime('%Y%m%dT%H%MZ')
        artifacts.append(write(raw/f'samples-{tag}.json',samples))
        artifacts.append(write(raw/f'recalculated-{tag}.json',results))
        all_points.extend(results)
        sample_count+=len(samples)
        if minute.hour%6==0:
            print(json.dumps(dict(through=iso_time(b),samples=sample_count),ensure_ascii=False),flush=True)
        minute+=timedelta(hours=1)
    artifacts.append(write(raw/'context-definitions.json',cache))
    status=Counter(p['status'] for p in all_points)
    reasons=Counter(code for p in all_points for code in p['reason_codes'])
    all_points=with_gap_markers(all_points)
    chosen=plot_indices(all_points)
    plotted=[all_points[i] for i in chosen]
    candidate_path=root/'backend/app/gpt_density/candidate.py'
    output=dict(policy='density-candidate-v1',density_policy='density-candidate-v1',experimental=True,
        mode='database-seven-day-recalculation',period=dict(start=iso_time(start),end=iso_time(end),end_exclusive=True),
        generated_at=iso_time(host_now),channels=dict(A304=channel_points(plotted,'setpoint'),actual=channel_points(plotted,'actual')),
        ash=ash,suggestions=plotted,parameters=CandidateParameters().__dict__,
        summary=dict(total_samples=sample_count,plot_samples=len(plotted),statuses=dict(status),reasons=dict(reasons)),
        coverage=dict(real_samples=sample_count,non_null_candidates=sum(p['value'] is not None for p in all_points),
            missing_samples=sum(p['value'] is None for p in all_points)),
        source=dict(kind='readonly-db-projected-decisions-and-pinned-telemetry',
            cutoff=iso_time(end),candidate_sha256=sha256(candidate_path.read_bytes()).hexdigest(),
            downsampling='real-point extrema and status/quality boundaries; no interpolation',
            raw_directory='raw',physical_outcome_simulated=False),
        limitations=['按真实历史设定逐次给新规则建议，未假设执行候选后生成灰分',
            '灰分背景为查询时生产小时结算；重算仅使用当时已知建议存档内灰分及目标',
            '新结果为事后重算，不是当时已发布或已执行记录'])
    final=write(out/'series.json',output)
    write(out/'manifest.json',dict(period=output['period'],host_now=iso_time(host_now),
        summary=output['summary'],series=final,artifacts=artifacts,source=output['source']))
    print(json.dumps(dict(complete=True,**output['summary'],series=final),ensure_ascii=False),flush=True)


if __name__=='__main__':
    main()
