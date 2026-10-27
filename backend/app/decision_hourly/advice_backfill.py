"""Backfill missing hourly advice archives by replaying stored hour evidence.

For every past application hour that has a stored first-setpoint anchor but no
archived advice row, this replays the exact card policy as of the anchor's
capture instant: shared inputs are reconstructed at that time (sources, models,
settings and assays known then), the hour keeps its frozen anchor, and the ash
settlement is rebuilt on demand instead of reporting a pending gap. The
configuration check keeps the hour's own anchor context as its reference — the
definition the running system stored at capture time — so replay compares
stored-then against stored-then exactly like the live check did, instead of
recomputing today's definition against hours captured by older code. The
result is the row the background archiver would have committed back then;
saved rows carry an ``origin`` marker so replayed advice stays distinguishable
from live archives. Hours that already have rows are never rewritten, and a
read-only test server never writes at all.
"""
from argparse import ArgumentParser
from collections import Counter
from copy import deepcopy
from datetime import timedelta
import sys

from sqlalchemy import select
from sqlalchemy import text

# Absolute imports keep this module loadable both inside the app package and
# as a standalone file next to a host-side bootstrap (backend on sys.path).
from app.database import REMOTE_READONLY, SessionLocal
from app.decision_hourly.advice_archive import compact
from app.decision_hourly.ash_settlement import get_settlement
from app.decision_hourly.models import (DecisionCycleHour, GptHourlyDensityAdvice,
                                        GptHourlyDensityAnchor)
from app.decision_hourly.quality_advice import apply_quality_advice
from app.decision_hourly.service import context_definition, window
from app.decision_hourly.statistics import digest
from app.gpt_density.hourly_anchor import hour_start
from app.gpt_runtime.decision_store import load_decision_store
from app.services.decision_sources import shared_snapshot
from app.telemetry.domain import iso_time, parse_time

ORIGIN = 'advice-backfill-v1'


def settlement_reader(db, start, now, defer_missing=True):
    """Offline replay computes a missing settlement instead of reporting pending."""
    return get_settlement(db, start, now, defer_missing=False)


def replay_hour(db, key):
    """Recompute the hourly card exactly at the anchor's capture instant."""
    anchor = db.get(GptHourlyDensityAnchor, key)
    if anchor is None:
        return None, 'no-anchor'
    now = parse_time(anchor.captured_at)

    def pinned(model_db, model_at=None):
        # Bind decision-store models to the replay instant, exactly like the
        # live snapshot does; read_hour's own store call stays in that instant.
        return load_decision_store(model_db, model_at=model_at or now)

    current = shared_snapshot(db, when=now, live=True, store_loader=pinned, realtime=True)
    start, _ = window(now)
    saved = db.get(DecisionCycleHour, iso_time(start))
    if saved is None:
        return None, 'hour-summary-pending'
    result = deepcopy(saved.snapshot)
    result['frozen'] = True
    result['checked_at'] = iso_time(now)
    store = pinned(db)
    definition = anchor.snapshot.get('context') or context_definition(current, store)
    # service.read_hour's exact sequence, with one substitution: the hour's own
    # stored anchor context is the compatibility reference the live check used.
    return apply_quality_advice(db, result, now, current, store, definition,
                                settlement_reader=settlement_reader), None


def backfill(sessions=SessionLocal, *, now=None, days=7, dry_run=False, log=None):
    """Fill missing advice rows for anchored hours in the trailing window.

    Returns a per-reason counter plus every saved hour for the caller's report.
    """
    from app.telemetry.domain import utc_now
    if REMOTE_READONLY and not dry_run:
        raise RuntimeError('只读测试端不得写入主机数据库；请仅使用 --dry-run')
    log = log or (lambda message: None)
    now = now or utc_now()
    end = hour_start(now)
    start = end - timedelta(days=days)
    lower, upper = iso_time(start), iso_time(end)
    with sessions() as db:
        anchors = {key: None for key in db.scalars(select(GptHourlyDensityAnchor.start)
            .where(GptHourlyDensityAnchor.start >= lower, GptHourlyDensityAnchor.start < upper))}
        archived = set(db.scalars(select(GptHourlyDensityAdvice.apply_start)
            .where(GptHourlyDensityAdvice.apply_start >= lower)))
        summarized = set(db.scalars(select(DecisionCycleHour.start)
            .where(DecisionCycleHour.start >= iso_time(start - timedelta(hours=1)),
                   DecisionCycleHour.start < upper)))
    counts = Counter(hours=len(anchors), already_archived=0, no_anchor_basis=0,
                     hour_summary_pending=0, replayed=0, saved=0, failed=0)
    saved = []
    for key in sorted(anchors):
        if key in archived:
            counts['already_archived'] += 1
            continue
        if iso_time(parse_time(key) - timedelta(hours=1)) not in summarized:
            counts['hour_summary_pending'] += 1
            continue
        try:
            with sessions() as db:
                if db.bind.dialect.name == 'sqlite':
                    db.execute(text('BEGIN'))
                result, reason = replay_hour(db, key)
        except Exception as error:  # one broken hour never stops the window
            counts['failed'] += 1
            log(f'{key} 回放失败：{type(error).__name__}: {error}')
            continue
        if reason == 'no-anchor':
            counts['no_anchor_basis'] += 1
            continue
        if reason == 'hour-summary-pending':
            counts['hour_summary_pending'] += 1
            continue
        counts['replayed'] += 1
        payload = compact(result)
        payload['origin'] = ORIGIN
        value, status = payload.get('value'), payload.get('status')
        if dry_run:
            log(f'{key} dry-run 建议密度={value} 状态={status}')
            saved.append((key, value, status))
            continue
        with sessions() as db:
            if db.bind.dialect.name == 'sqlite':
                db.execute(text('BEGIN IMMEDIATE'))
            if db.scalar(select(GptHourlyDensityAdvice.id)
                    .where(GptHourlyDensityAdvice.apply_start == key).limit(1)) is not None:
                db.rollback()
                counts['already_archived'] += 1
                continue
            db.add(GptHourlyDensityAdvice(apply_start=key, generated_at=result['checked_at'],
                                          fingerprint=digest(payload), snapshot=payload))
            db.commit()
        counts['saved'] += 1
        saved.append((key, value, status))
        log(f'{key} 建议密度={value} 状态={status} 已存档')
    return counts, saved


def main(argv=None):
    parser = ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--days', type=int, default=7, help='回补最近 N 天（默认 7）')
    parser.add_argument('--dry-run', action='store_true', help='只回放并打印，不写入数据库')
    args = parser.parse_args(argv)
    counts, saved = backfill(days=args.days, dry_run=args.dry_run, log=print)
    print('统计：' + ', '.join(f'{key}={counts[key]}' for key in sorted(counts)))
    values = [value for _, value, _ in saved if value is not None]
    if values:
        print(f'有效建议 {len(values)} 个小时，范围 {min(values):.3f}–{max(values):.3f} g/cm³')
    return 0


if __name__ == '__main__':
    sys.exit(main())
