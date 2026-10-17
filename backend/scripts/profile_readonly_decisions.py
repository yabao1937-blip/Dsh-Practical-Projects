"""Compare GPT input loaders in one read-only host snapshot, without row output.

This is a controlled query-count/timing probe, not a percentile benchmark.
It never downloads a database or writes production/business data.
"""
import argparse
from collections import Counter
from contextlib import contextmanager
import json
from pathlib import Path
import re
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def differences(left, right, path='', limit=20):
    """Return mismatch paths only; do not print production values or credentials."""
    if type(left) is not type(right):
        return [path or '/']
    if isinstance(left, dict):
        found = []
        for key in sorted(set(left) | set(right)):
            child = path + '/' + str(key)
            found.extend([child] if key not in left or key not in right else
                         differences(left[key], right[key], child, limit))
            if len(found) >= limit:
                break
        return found[:limit]
    if isinstance(left, list):
        if len(left) != len(right):
            return [(path or '/') + '/length']
        found = []
        for index, (first, second) in enumerate(zip(left, right)):
            found.extend(differences(first, second, path + '/' + str(index), limit))
            if len(found) >= limit:
                break
        return found[:limit]
    return [] if left == right else [path or '/']


@contextmanager
def sql_probe(engine):
    from sqlalchemy import event
    statements = []

    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    event.listen(engine, 'before_cursor_execute', capture)
    try:
        yield statements
    finally:
        event.remove(engine, 'before_cursor_execute', capture)


def evaluate(db, observed, loader):
    from app.services.decision_sources import shared_snapshot
    from app.decision_hourly.service import read_hour

    started = time.monotonic()
    with sql_probe(db.get_bind()) as statements:
        current = shared_snapshot(db, when=observed, live=True, store_loader=loader)
        hourly = read_hour(db, now=observed, live=current, defer_missing=True, store_loader=loader)
    value = {'current': current, 'hourly': hourly}
    tables = Counter()
    for statement in statements:
        for table in set(re.findall(r'\b(?:FROM|JOIN)\s+"?([a-zA-Z_][a-zA-Z_0-9]*)', statement, re.I)):
            tables[table] += 1
    return value, dict(seconds=round(time.monotonic() - started, 3), sql_count=len(statements),
                      table_reads=dict(sorted(tables.items())),
                      response_bytes=len(json.dumps(value, ensure_ascii=False).encode('utf-8')))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--compare', action='store_true', help='Compare legacy and light loaders on identical input')
    args = parser.parse_args()

    # Check the marker before importing any database/application entry point.
    from remote_mode import settings
    if not settings():
        parser.error('This probe requires the laptop read-only host configuration')
    from app.config import REMOTE_READONLY
    if not REMOTE_READONLY:
        parser.error('Read-only runtime is not enabled')
    from sqlalchemy import text
    from app.database import SessionLocal
    from app.gpt_runtime.decision_store import load_decision_store
    from app.remote_readonly.runtime import host_now
    from app.services.state import load_store

    with SessionLocal() as db:
        db.execute(text('BEGIN'))
        observed = host_now(db)
        result = {'scope': 'one read-only snapshot; setup BEGIN/clock queries excluded', 'light': None}
        legacy = None
        if args.compare:
            legacy, result['legacy'] = evaluate(db, observed, load_store)
        light, result['light'] = evaluate(db, observed, load_decision_store)
        if args.compare:
            mismatch = differences(legacy, light)
            result.update(equal=not mismatch, mismatch_paths=mismatch)
            print(json.dumps(result, ensure_ascii=False))
            return 1 if mismatch else 0
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
