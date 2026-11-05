"""Minimal, projected read-only DB evidence for the seven-day replay task."""
import json
from datetime import timedelta
from pathlib import Path

from sqlalchemy import text
from app.config import REMOTE_READONLY
from app.database import SessionLocal
from app.telemetry.domain import iso_time, parse_time


def main():
    if not REMOTE_READONLY:
        raise RuntimeError('This probe requires the configured remote readonly-test database')
    target = Path(__file__).resolve().parents[2]/'output/density-optimization-db7-20261104/backend'
    target.mkdir(parents=True, exist_ok=True)
    result = {}
    with SessionLocal() as db:
        result['host_now'] = db.execute(text("SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now')")).scalar_one()
        now = parse_time(result['host_now'])
        lower = iso_time(now-timedelta(days=7))
        result['window'] = dict(start=lower, end=iso_time(now))
        tables = ('decision_cycle_samples','gpt_decision_evidence_archives','telemetry_readings',
            'gpt_hourly_density_advice','gpt_hourly_density_anchors','gpt_density_actions')
        result['tables'] = {}
        for table in tables:
            sql = db.execute(text("SELECT sql FROM sqlite_master WHERE type='table' AND name=:name"),
                             {'name':table}).scalar()
            result['tables'][table] = sql
        for table, key in (('decision_cycle_samples','sampled_at'),('gpt_decision_evidence_archives','start'),
                ('gpt_hourly_density_advice','generated_at'),
                ('gpt_hourly_density_anchors','start'),('gpt_density_actions','detected_at')):
            query = f'SELECT count(*) AS n,min({key}) AS first,max({key}) AS last FROM {table} WHERE {key} >= :lower AND {key} <= :upper'
            result[table] = dict(db.execute(text(query),{'lower':lower,'upper':iso_time(now)}).mappings().one())
        result['telemetry_indexes'] = [dict(row) for row in db.execute(text(
            "SELECT name,sql FROM sqlite_master WHERE type='index' AND tbl_name='telemetry_readings'" )).mappings()]
        result['sample_projection'] = [dict(row) for row in db.execute(text('''SELECT slot,sampled_at,context_key,
            json_extract(snapshot,'$.storage_version') AS storage_version,
            json_extract(snapshot,'$.cadence_seconds') AS cadence_seconds,
            json_extract(snapshot,'$.density_channels.A304') AS density_a304,
            json_extract(snapshot,'$.inputs.is_stoppage') AS stoppage,
            json_extract(snapshot,'$.target') AS target,
            json_extract(snapshot,'$.total_ash') AS total_ash
            FROM decision_cycle_samples WHERE sampled_at>=:lower ORDER BY sampled_at DESC LIMIT 1'''),
            {'lower':lower}).mappings()]
        result['archive_hour_projection'] = [dict(row) for row in db.execute(text('''SELECT start,sample_count,
            length(payload) AS payload_bytes,checksum FROM gpt_decision_evidence_archives
            WHERE start>=:lower ORDER BY start DESC LIMIT 3'''), {'lower':lower}).mappings()]
    (target/'db-minimal-probe.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in result.items() if k not in ('tables','sample_projection')},ensure_ascii=False,indent=2))
    print('sample projection saved; keys:', list(result['sample_projection'][0]) if result['sample_projection'] else [])


if __name__ == '__main__':
    main()
