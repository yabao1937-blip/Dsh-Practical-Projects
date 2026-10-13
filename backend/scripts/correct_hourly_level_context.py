"""Dry run by default; --apply backs up and corrects one explicitly selected hour."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select, text
from app.database import SessionLocal
from app.decision_hourly.models import DecisionCycleHour, DecisionCycleSample, DecisionCycleContext
from app.decision_hourly.references import hydrate_rows
from app.decision_hourly.statistics import digest
from app.decision_hourly.corrections import correct_missing_level_context
from app.telemetry.domain import parse_time, iso_time, utc_now


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--start', required=True, help='Statistics hour start, ISO timestamp with timezone')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    start = parse_time(args.start)
    if start.minute or start.second or start.microsecond:
        parser.error('start must be an exact hour')
    with SessionLocal() as db:
        hour = db.get(DecisionCycleHour, iso_time(start))
        if hour is None:
            raise ValueError('指定小时没有冻结结果')
        before = hour.snapshot
        original_digest = digest(before)
        rows = hydrate_rows(db, list(db.scalars(select(DecisionCycleSample).where(
            DecisionCycleSample.sampled_at >= hour.start,
            DecisionCycleSample.sampled_at < hour.end).order_by(DecisionCycleSample.slot))))
        contexts = {key:db.get(DecisionCycleContext, key).definition for key in {r.context_key for r in rows}}
        corrected_at = iso_time(utc_now())
        after = correct_missing_level_context(before, rows, contexts, corrected_at)
    report = dict(start=iso_time(start), changed=after != before, applied=False,
        before=before['suggested_density'], after=after['suggested_density'], coverage=after['coverage'])
    if args.apply and after != before:
        with SessionLocal() as db:
            db.execute(text('BEGIN IMMEDIATE'))
            hour = db.get(DecisionCycleHour, iso_time(start))
            if digest(hour.snapshot) != original_digest:
                raise ValueError('冻结结果已被修改，请重新核对')
            folder = Path(__file__).resolve().parents[1]/'data/backups'
            folder.mkdir(parents=True, exist_ok=True)
            backup = folder/('hourly-level-context-'+utc_now().strftime('%Y%m%dT%H%M%S%fZ')+'.json')
            with backup.open('x', encoding='utf-8') as handle:
                json.dump(dict(start=hour.start, original_digest=original_digest,
                    original=before, corrected=after), handle, ensure_ascii=False, indent=2)
            hour.snapshot = after
            db.commit()
            report.update(applied=True, backup=str(backup))
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
