"""One-hour primary-key witness only; no broad telemetry/time scan."""
import json
from hashlib import sha256
from pathlib import Path

from sqlalchemy import select
from app.config import REMOTE_READONLY
from app.database import SessionLocal
from app.telemetry.models import TelemetryReading


def main():
    if not REMOTE_READONLY:
        raise RuntimeError('Configured remote readonly-test is required')
    root=Path(__file__).resolve().parents[2]/'output/density-optimization-db7-20261104'
    samples=json.loads((root/'raw/samples-20261028T1200Z.json').read_text(encoding='utf-8'))
    ids=sorted({i for row in samples for i in row['provenance']['reading_ids']})
    found=[]
    for offset in range(0,len(ids),100):
        with SessionLocal() as db:
            for row in db.scalars(select(TelemetryReading).where(TelemetryReading.id.in_(ids[offset:offset+100])).order_by(TelemetryReading.id)):
                s=row.sample
                found.append(dict(id=row.id,source_id=row.source_id,point_key=row.point_key,batch_id=row.batch_id,
                    received_at=row.received_at,sample={key:s.get(key) for key in
                        ('value','raw_value','quality','status_code','issue','source_at','server_at','received_at',
                         'definition_id','cadence_seconds')}))
    missing=sorted(set(ids)-{r['id'] for r in found})
    result=dict(source='primary-key SELECT from telemetry_readings; metadata decoded by existing codec',
        hour='2026-10-28T12:00:00+00:00',sample_count=len(samples),requested_ids=ids,missing_ids=missing,rows=found)
    raw=json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False).encode()
    path=root/'raw/reading-ids-20261028T1200Z.json'
    path.write_bytes(raw)
    print(json.dumps(dict(sample_count=len(samples),unique_ids=len(ids),found=len(found),missing=len(missing),
        file=str(path),sha256=sha256(raw).hexdigest()),ensure_ascii=False))
    if missing:
        raise RuntimeError('Pinned witness readings missing')


if __name__=='__main__':
    main()
