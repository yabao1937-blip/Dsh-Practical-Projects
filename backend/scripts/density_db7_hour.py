"""Validate one full production hour through retained references before expansion."""
import json
from collections import Counter
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select
from app.config import REMOTE_READONLY
from app.database import SessionLocal
from app.decision_hourly.density_series import advice_points, projected_samples, sample_candidate
from app.decision_hourly.models import DensityAction
from app.telemetry.domain import parse_time,iso_time


def main():
    if not REMOTE_READONLY:
        raise RuntimeError('Remote readonly-test is required')
    start = parse_time('2026-11-03T00:00:00+00:00')
    end = start+timedelta(hours=1)
    target = Path(__file__).resolve().parents[2]/'output/density-optimization-db7-20261104/backend'
    with SessionLocal() as db:
        points = advice_points(db,start,end)
        samples = list(projected_samples(db,start,end))
        actions = list(db.scalars(select(DensityAction).where(DensityAction.channel=='A304',
            DensityAction.detected_at<=iso_time(end)).order_by(DensityAction.detected_at)))
        results = []
        for sample in samples:
            latest = next((a for a in reversed(actions) if a.detected_at<=sample['sampled_at']),None)
            results.append(sample_candidate(sample,points,latest))
    output = dict(period=dict(start=iso_time(start),end=iso_time(end)),samples=samples,results=results,
        summary=dict(samples=len(samples),statuses=dict(Counter(r['candidate']['status'] for r in results)),
            reasons=dict(Counter(r['candidate']['reason_codes'][0] for r in results)),
            cadences=dict(Counter(str(s['cadence_seconds']) for s in samples)),
            pair_values=sum(s['snapshot']['density_channels']['A304'].get('actual',{}).get('value') is not None
                and s['snapshot']['density_channels']['A304'].get('setpoint',{}).get('value') is not None for s in samples)))
    (target/'one-hour-reconstruction.json').write_text(json.dumps(output,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(output['summary'],ensure_ascii=False,indent=2))
    print('first evidence',json.dumps(results[1] if len(results)>1 else results,ensure_ascii=False)[:1800])


if __name__=='__main__':
    main()
