"""Read-only deployment checks; optionally write the resulting verification JSON."""
import argparse
import hashlib
import json
from pathlib import Path
import statistics
import sys
from datetime import datetime, timezone
from urllib.request import urlopen

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(BACKEND))

from sqlalchemy import select, text
from app.database import SessionLocal
from app.decision_hourly.models import DecisionCycleSample, DecisionTelemetryPin
from app.decision_hourly.references import VERSION, resolve_snapshots, walk


def digest_rows(rows):
    h=hashlib.sha256()
    count=0
    for row in rows:
        h.update(json.dumps(row,ensure_ascii=False,separators=(',',':')).encode())
        count+=1
    return dict(count=count,sha256=h.hexdigest())


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--baseline',type=Path,required=True)
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    baseline=json.loads(args.baseline.read_text(encoding='utf-8'))
    runtime=json.loads((BACKEND/'data/telemetry-runtime/kepserver-main.json').read_text(encoding='utf-8'))
    config=json.loads((BACKEND/'config/kepserver.local.json').read_text(encoding='utf-8-sig'))
    assert config['poll_seconds']==config['history_interval_seconds']==5
    result=dict(verified_at=datetime.now(timezone.utc).isoformat(),storage_version=VERSION,
        collector_started_at=runtime['started_at'],history_interval_seconds=5,read_only=True)
    with SessionLocal() as db:
        db.execute(text('PRAGMA query_only=ON'))
        connection=db.connection().connection.driver_connection
        preserved={}
        for table in ('decision_cycle_hours','hourly_brief_rows','coarse_models','gpt_coarse_model_revisions'):
            current=digest_rows(connection.execute(f'SELECT * FROM {table} ORDER BY 1'))
            assert current==baseline[table],f'{table} changed during deployment'
            preserved[table]=current
        current=digest_rows(connection.execute('SELECT slot,snapshot FROM decision_cycle_samples WHERE slot<=? ORDER BY slot',
                                              (baseline['legacy_max_slot'],)))
        assert current==baseline['legacy_decisions'],'Legacy decisions changed'
        preserved['legacy_decisions']=current
        result['preserved']=preserved
        batches=connection.execute('SELECT batch_id,min(received_at),count(*) FROM telemetry_readings '
            'WHERE source_id=? AND received_at>=? GROUP BY batch_id ORDER BY min(received_at)',
            (config['source_id'],runtime['started_at'])).fetchall()
        assert len(batches)>=20,'Wait for at least 20 actual collector batches (90s lag warm-up)'
        assert all(n==len(config['points']) for _,_,n in batches),'Incomplete persisted batch'
        times=[datetime.fromisoformat(row[1]) for row in batches]
        intervals=[(b-a).total_seconds() for a,b in zip(times,times[1:])]
        assert 4.5<=statistics.median(intervals)<=5.5,intervals
        result['cadence']=dict(batch_count=len(batches),points_per_batch=len(config['points']),
            first_at=batches[0][1],last_at=batches[-1][1],minimum_seconds=min(intervals),
            maximum_seconds=max(intervals),mean_seconds=statistics.mean(intervals),median_seconds=statistics.median(intervals))
        rows=list(db.scalars(select(DecisionCycleSample).where(DecisionCycleSample.slot>baseline['legacy_max_slot'])
                            .order_by(DecisionCycleSample.slot)))
        assert rows and all(r.snapshot.get('storage_version')==VERSION for r in rows)
        snapshots=resolve_snapshots(db,[r.snapshot for r in rows])
        ids={v['$reading'] for r in rows for v in walk(r.snapshot) if '$reading' in v}
        pins=set(db.scalars(select(DecisionTelemetryPin.reading_id).where(DecisionTelemetryPin.reading_id.in_(ids))))
        assert ids==pins,'Unprotected decision reference'
        latest=rows[-1]
        restored=snapshots[-1]
        level=restored['cells']['level']
        observed=datetime.fromisoformat(level['observed_at'])
        lag=(datetime.fromisoformat(latest.sampled_at)-observed).total_seconds()
        assert level['status']=='good' and 90<=lag<=120
        assert level['observed_at']>=runtime['started_at'],'Lag input has not reached new five-second authority yet'
        assert '$reading' in latest.snapshot['cells']['level']
        assert restored['coarse_ash']['value'] is not None
        result['decisions']=dict(count=len(rows),protected_reading_count=len(ids),latest_slot=latest.slot,
            sampled_at=latest.sampled_at,level_observed_at=level['observed_at'],level_lag_seconds=lag,
            coarse_ash=restored['coarse_ash'],total_ash=restored['total_ash'],suggested_density=restored['suggested_density'],
            latest_encoded_bytes=len(json.dumps(latest.snapshot,ensure_ascii=False).encode()),
            latest_resolved_bytes=len(json.dumps(restored,ensure_ascii=False).encode()))
    checks={}
    for path in ('/api/v1/health','/api/v1/telemetry/latest','/api/v1/decision/current',
                 '/api/v1/decision/hourly','/api/v1/decision/evidence/segments',
                 f'/api/v1/decision/evidence/samples/{latest.slot}'):
        with urlopen('http://127.0.0.1:8000'+path,timeout=20) as response:
            data=json.load(response)
            checks[path]=response.status
            if path.endswith('telemetry/latest'):
                assert data['sources'][0]['state']=='connected'
                assert data['summary']['available']==len(config['points'])
    result['http_status']=checks
    result['ds_baseline_sha256']=hashlib.sha256((BACKEND/'data/train_js.json').read_bytes()).hexdigest()
    result['deployment_note']='Existing site uses create_all/schema_upgrade without Alembic stamps; new reference table created with checkfirst. No existing table rebuilt.'
    if args.output:
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':
    main()
