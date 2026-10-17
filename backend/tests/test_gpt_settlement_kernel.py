"""The smaller evidence representation must preserve shared settlement output."""
from copy import deepcopy
from datetime import timedelta
import json

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.exc import DatabaseError

from app.decision_hourly import ash_settlement
from app.decision_hourly.models import DecisionCycleSample, DecisionEvidenceArchive
from app.gpt_runtime.settlement_kernel import load_rows
from app.remote_readonly.server import _authorize
from app.telemetry.models import TelemetryReading
from test_ash_settlement import START, END, NOW, PUBLISH, sample, assay
from test_hourly_brief import sessions
from test_settlement_archive_projection import freeze_and_archive, referenced_sample


def same(db, now=NOW):
    expected = ash_settlement.build_settlement(db, START, now)
    actual = ash_settlement.build_settlement(db, START, now, rows=load_rows(db, START))
    assert actual == expected
    assert json.dumps(actual, sort_keys=True) == json.dumps(expected, sort_keys=True)
    return actual


@pytest.mark.parametrize('packed', [False, True])
@pytest.mark.parametrize('archived', [False, True])
def test_inline_referenced_and_archived_late_assays_are_exact(sessions, packed, archived):
    with sessions.begin() as db:
        maker = referenced_sample if packed else sample
        maker(db, START+timedelta(seconds=59))
        maker(db, START+timedelta(minutes=30), rate=100)
        early = assay(db, 7, observed=START-timedelta(minutes=1), created=START)
        early.time_basis = 'production-time-v1'
        db.flush()
        before = same(db, PUBLISH)
        if archived:
            freeze_and_archive(db)
        late = assay(db, 8, observed=START+timedelta(minutes=47), created=NOW)
        late.time_basis = 'production-time-v1'
        db.flush()
        result = same(db)
        assert before['item']['value'] != result['item']['value']
        assert result['item']['recalculated_count'] == 2
        contributions = result['contributions']
        whole = ash_settlement.contribution_item(contributions, START, END)
        left = ash_settlement.contribution_item(contributions, START, START+timedelta(minutes=1))
        right = ash_settlement.contribution_item(contributions, START+timedelta(minutes=1), END)
        assert left['mass_t']+right['mass_t'] == pytest.approx(whole['mass_t'])
        assert left['ash_mass_t']+right['ash_mass_t'] == pytest.approx(whole['ash_mass_t'])


@pytest.mark.parametrize('change', ['invalid_coarse', 'held_coarse', 'bad_float_unit', 'stale_float',
    'bad_belt', 'zero_belt', 'mismatched_batch', 'missing_amount', 'unbalanced_amount', 'online', 'legacy'])
def test_invalid_inputs_frozen_source_choices_and_legacy_exclusions_stay_exact(sessions, change):
    with sessions.begin() as db:
        original = sample(db)
        row = db.get(DecisionCycleSample, original.slot)
        value = deepcopy(row.snapshot)
        if change == 'invalid_coarse':
            value['coarse_ash']['value'] = 200
        elif change == 'held_coarse':
            value['coarse_ash']['status'] = 'held'
        elif change == 'bad_float_unit':
            value['cells']['float_ash']['unit'] = 'bad'
        elif change == 'stale_float':
            value['cells']['float_ash'].update(status='good', observed_at='2026-10-14T00:00:00Z')
        elif change == 'bad_belt':
            value['cells']['belt501']['quality'] = 'bad'
        elif change == 'zero_belt':
            for key in ('belt501', 'belt502'):
                value['cells'][key]['value'] = 0
        elif change == 'mismatched_batch':
            value['cells']['belt501']['batch_id'] = 'other'
        elif change == 'missing_amount':
            value['cells'].pop('float_amount')
        elif change == 'unbalanced_amount':
            value['heavy_amount']['value'] += 100
        elif change == 'online':
            value['sources']['resolved_choices']['heavy-ash'] = json.dumps(['kep', 'online'])
        elif change == 'legacy':
            value.pop('evidence_policy')
        # Preserve every resolved source choice, not just heavy-ash, because
        # equality of choices controls historical zero-production lookups.
        value['sources']['resolved_choices']['unused-source-identity'] = 'frozen'
        row.snapshot = value
        assay(db)
        db.flush()
        same(db)


def test_kernel_reference_query_excludes_unconsumed_sample_and_snapshot_payload(sessions):
    marker = 'UNCONSUMED-REFERENCE-PAYLOAD'
    with sessions.begin() as db:
        slot, ids = referenced_sample(db)
        row = db.get(DecisionCycleSample, slot)
        value = deepcopy(row.snapshot)
        value['sources']['choices'] = marker*10000
        value['model'] = marker*10000
        value['cells']['belt501']['overlay']['unused_payload'] = marker*10000
        row.snapshot = value
        reading = db.get(TelemetryReading, ids['belt501'])
        reading.sample = dict(reading.sample, unused_payload=marker*10000)
        assay(db)
        db.flush()
        expected = ash_settlement.build_settlement(db, START, NOW)

    class Probe:
        def __init__(self, db):
            self.db, self.responses = db, []
        def __getattr__(self, name):
            return getattr(self.db, name)
        def execute(self, statement, *args, **kwargs):
            rows = self.db.execute(statement, *args, **kwargs).all()
            self.responses.append(json.dumps([tuple(row) for row in rows]))
            return rows

    with sessions() as db:
        probe = Probe(db)
        rows = load_rows(probe, START)
        assert marker not in '\n'.join(probe.responses)
        assert ash_settlement.build_settlement(db, START, NOW, rows=rows) == expected
        assert len(json.dumps([row.snapshot for row in rows])) < 2000


def test_missing_consumed_reference_and_corrupt_archive_fail_explicitly(sessions):
    with sessions.begin() as db:
        _, ids = referenced_sample(db)
        db.execute(delete(TelemetryReading).where(TelemetryReading.id == ids['belt501']))
    with sessions() as db, pytest.raises(ValueError, match='原始记录缺失'):
        load_rows(db, START)
    with sessions.begin() as db:
        freeze_and_archive(db)
        db.scalar(select(DecisionEvidenceArchive)).payload = b'corrupt'
    with sessions() as db, pytest.raises(Exception):
        load_rows(db, START)


def test_kernel_queries_use_existing_readonly_authorizer(sessions):
    with sessions.begin() as db:
        referenced_sample(db)
        assay(db)
    with sessions() as db:
        connection = db.connection().connection.driver_connection
        connection.set_authorizer(_authorize)
        try:
            same(db)
            with pytest.raises(DatabaseError):
                db.execute(text('DELETE FROM decision_cycle_samples'))
        finally:
            connection.set_authorizer(None)


def test_legacy_fingerprint_deduplication_and_hot_archive_precedence_are_exact(sessions):
    with sessions.begin() as db:
        first = sample(db)
        first_row = db.get(DecisionCycleSample, first.slot)
        first_row.snapshot = {key: value for key, value in first_row.snapshot.items() if key != 'evidence_policy'}
        second = sample(db, START+timedelta(seconds=5))
        second_row = db.get(DecisionCycleSample, second.slot)
        second_row.snapshot = deepcopy(first_row.snapshot)
        second_row.fingerprint = first.fingerprint
        db.flush()
        result = same(db)
        assert result['item']['sample_count'] == 1
        freeze_and_archive(db)
        same(db)
        replacement = sample(db)
        hot = db.get(DecisionCycleSample, replacement.slot)
        hot.snapshot = dict(hot.snapshot, total_ash={'value': 9.87, 'status': 'predicted'})
        db.flush()
        same(db)
