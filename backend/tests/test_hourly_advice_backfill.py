"""Replayed backfill archives stored-input advice without touching live rows."""
from datetime import timedelta

from sqlalchemy import func, select

from test_hourly_quality_advice import prepared, sessions, HOUR, ASH_START
from test_decision_sources import seeded  # noqa: F401  (fixture used by prepared)
from app.decision_hourly import advice_backfill
from app.decision_hourly.advice_archive import save_result
from app.decision_hourly.models import DecisionCycleHour, GptHourlyDensityAdvice
from app.telemetry.domain import iso_time


def seeded_settlement(settlement):
    def reader(db, start, now, defer_missing=True):
        assert start == ASH_START  # delayed ash hour, exactly as the card policy reads it
        return dict(revision=settlement['revision'], item=dict(settlement['item']))
    return reader


def test_backfill_replays_stored_inputs_and_marks_origin(sessions, prepared, monkeypatch):
    current, settlement, _, _ = prepared
    monkeypatch.setattr(advice_backfill, 'get_settlement', seeded_settlement(settlement))
    counts, saved = advice_backfill.backfill(sessions, now=HOUR+timedelta(hours=1))
    assert counts['saved'] == 1 and saved == [(iso_time(HOUR), 1.46, 'calculated')]
    with sessions() as db:
        rows = list(db.scalars(select(GptHourlyDensityAdvice)))
        assert len(rows) == 1
        assert rows[0].apply_start == iso_time(HOUR)
        assert rows[0].generated_at == iso_time(HOUR)  # the anchor's capture instant
        assert rows[0].snapshot['value'] == 1.46
        assert rows[0].snapshot['status'] == 'calculated'
        assert rows[0].snapshot['origin'] == advice_backfill.ORIGIN
        assert rows[0].snapshot['basis']['channels'] == ['A304']
        assert rows[0].snapshot['ash']['revision'] == 'first'


def test_backfill_never_rewrites_hours_that_already_have_rows(sessions, prepared, monkeypatch):
    _, settlement, _, _ = prepared
    monkeypatch.setattr(advice_backfill, 'get_settlement', seeded_settlement(settlement))
    live = dict(advice_policy='gpt-hourly-reference-v4',
        suggested_density=dict(value=1.5, status='calculated', reason='live'),
        advice_inputs=dict(ash=dict(value=9), density=dict(value=1.48,
            execution_basis=dict(components={'A304': {}}))))
    with sessions.begin() as db:
        save_result(db, HOUR, live)
    counts, saved = advice_backfill.backfill(sessions, now=HOUR+timedelta(hours=1))
    assert counts['saved'] == 0 and counts['already_archived'] == 1
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(GptHourlyDensityAdvice)) == 1


def test_backfill_is_idempotent_across_runs(sessions, prepared, monkeypatch):
    _, settlement, _, _ = prepared
    monkeypatch.setattr(advice_backfill, 'get_settlement', seeded_settlement(settlement))
    advice_backfill.backfill(sessions, now=HOUR+timedelta(hours=1))
    counts, _ = advice_backfill.backfill(sessions, now=HOUR+timedelta(hours=2))
    assert counts['saved'] == 0 and counts['already_archived'] == 1


def test_backfill_skips_hours_whose_previous_summary_is_missing(sessions, prepared, monkeypatch):
    _, settlement, _, _ = prepared
    monkeypatch.setattr(advice_backfill, 'get_settlement', seeded_settlement(settlement))
    with sessions.begin() as db:
        db.delete(db.get(DecisionCycleHour, iso_time(HOUR-timedelta(hours=1))))
    counts, _ = advice_backfill.backfill(sessions, now=HOUR+timedelta(hours=1))
    assert counts['saved'] == 0 and counts['hour_summary_pending'] == 1
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(GptHourlyDensityAdvice)) == 0


def test_backfill_dry_run_computes_but_writes_nothing(sessions, prepared, monkeypatch):
    _, settlement, _, _ = prepared
    monkeypatch.setattr(advice_backfill, 'get_settlement', seeded_settlement(settlement))
    counts, saved = advice_backfill.backfill(sessions, now=HOUR+timedelta(hours=1), dry_run=True)
    assert saved == [(iso_time(HOUR), 1.46, 'calculated')]
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(GptHourlyDensityAdvice)) == 0


def test_readonly_server_never_writes_but_may_dry_run(sessions, prepared, monkeypatch):
    _, settlement, _, _ = prepared
    monkeypatch.setattr(advice_backfill, 'get_settlement', seeded_settlement(settlement))
    monkeypatch.setattr(advice_backfill, 'REMOTE_READONLY', True)
    try:
        advice_backfill.backfill(sessions, now=HOUR+timedelta(hours=1))
    except RuntimeError as error:
        assert '只读' in str(error)
    else:
        raise AssertionError('readonly apply must be refused')
    advice_backfill.backfill(sessions, now=HOUR+timedelta(hours=1), dry_run=True)
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(GptHourlyDensityAdvice)) == 0
