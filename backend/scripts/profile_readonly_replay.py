"""Bounded ten-hour replay experiment on read-only host evidence.

The second pass supplies hypothetical chemistry to the local calculation only.
It does not insert an assay, publish a result, or modify either database.
"""
from copy import deepcopy
from datetime import timedelta
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from profile_readonly_decisions import differences
from profile_readonly_hours import evaluate


def without_cache_flags(item):
    return {key: value for key, value in item.items()
            if key not in ('cached', 'frozen', 'calculation_origin')}


def main():
    from remote_mode import settings
    if not settings():
        raise SystemExit('This probe requires laptop read-only configuration')
    from app.config import REMOTE_READONLY
    if not REMOTE_READONLY:
        raise SystemExit('Read-only runtime is disabled')
    from sqlalchemy import text
    from app.database import SessionLocal
    from app.remote_readonly.runtime import host_now
    from app.remote_readonly.settlement_replay import SettlementReplay
    from app.remote_readonly.settled_hours import _sources, read_settled_hours
    from app.decision_hourly.ash_settlement import latest_end, manual_basis
    from app.telemetry.domain import iso_time

    cache = SettlementReplay()
    with SessionLocal() as db:
        db.execute(text('BEGIN'))
        now = host_now(db)
        end = latest_end(now)
        starts = [end-timedelta(hours=index) for index in range(10, 0, -1)]
        sources = _sources(db, starts)
        bases = {iso_time(start): manual_basis(db, start, start+timedelta(hours=1), now)
                 for start in starts}
        expected = {item['start']: item for item in read_settled_hours(db, now=now)['items']}

        def cold(current, *, now):
            return {iso_time(start): cache.replay(current, start, now,
                basis=bases[iso_time(start)], source=sources[iso_time(start)]) for start in starts}

        original, cold_metrics = evaluate(db, now, cold)
        mismatch, compared = [], 0
        for key, result in original.items():
            if not expected[key].get('pending'):
                compared += 1
                mismatch.extend(differences(without_cache_flags(expected[key]),
                                            without_cache_flags(result['item']), key))

        def revised(current, *, now):
            result = {}
            for start in starts:
                key = iso_time(start)
                basis = deepcopy(bases[key])
                basis['value'] = min(100., (basis['value'] or 8.)+.2)
                # The virtual record is never saved or returned by a live API.
                basis['records'] = [dict(id=-1, observed_at=iso_time(starts[0]),
                    created_at=iso_time(now), production_at=iso_time(starts[0]),
                    time_basis='production-time-v1', value=basis['value'])]
                basis['carried'] = start != starts[0]
                result[key] = cache.replay(current, start, now, basis=basis, source=sources[key])
            return result

        _, revision_metrics = evaluate(db, now, revised)
    print(json.dumps(dict(scope='ten real frozen evidence hours; hypothetical local chemistry only; no write or publication; not browser end-to-end',
        cold=cold_metrics, ten_hour_revision=revision_metrics, cache=cache.cache_info(),
        host_current_items_compared=compared,
        host_current_item_mismatch_paths=mismatch), ensure_ascii=False))
    return 1 if mismatch else 0


if __name__ == '__main__':
    raise SystemExit(main())
