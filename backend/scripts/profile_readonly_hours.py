"""Compare hour-table readers in one host read transaction without row output.

Only bounded SELECTs are issued. The script reports query-result transport bytes,
not database size or network framing, and never prints production row values.
"""
import json
from pathlib import Path
import sys
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from profile_readonly_decisions import differences, sql_probe


def evaluate(db, now, reader):
    from remote_sqlite import RemoteConnection

    original, wire = RemoteConnection._request, []

    def measured(connection, method, suffix, body=None):
        result = original(connection, method, suffix, body)
        if isinstance(result, dict) and 'rows' in result:
            wire.append(len(json.dumps(result, ensure_ascii=False).encode('utf-8')))
        return result

    started = time.monotonic()
    with sql_probe(db.get_bind()) as statements, patch.object(RemoteConnection, '_request', measured):
        value = reader(db, now=now)
    return value, dict(seconds=round(time.monotonic()-started, 3), sql_count=len(statements),
        query_result_json_bytes=sum(wire), response_bytes=len(json.dumps(value, ensure_ascii=False).encode('utf-8')))


def main():
    from remote_mode import settings
    if not settings():
        raise SystemExit('This probe requires the laptop read-only host configuration')
    from app.config import REMOTE_READONLY
    if not REMOTE_READONLY:
        raise SystemExit('Read-only runtime is not enabled')
    from sqlalchemy import text
    from app.database import SessionLocal
    from app.remote_readonly.runtime import host_now
    from app.decision_hourly.ash_settlement import read_settled_hours as old_reader
    from app.remote_readonly.settled_hours import read_settled_hours as new_reader

    with SessionLocal() as db:
        db.execute(text('BEGIN'))
        observed = host_now(db)
        old, old_metrics = evaluate(db, observed, old_reader)
        # The old ORM identity map must not turn the comparison into cache hits.
        db.expunge_all()
        new, new_metrics = evaluate(db, observed, new_reader)
    mismatch = differences(old, new)
    print(json.dumps(dict(scope='one read-only transaction; fixed host clock; single sample, not P95',
        legacy=old_metrics, batched=new_metrics, equal=not mismatch, mismatch_paths=mismatch)))
    return 1 if mismatch else 0


if __name__ == '__main__':
    raise SystemExit(main())
