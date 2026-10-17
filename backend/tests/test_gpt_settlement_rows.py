"""Projected chemistry transport matches the original loader, without writes."""
from copy import deepcopy
import hashlib
import json
import zlib

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.exc import DatabaseError

from app.decision_hourly import ash_settlement
from app.decision_hourly.models import DecisionCycleSample, DecisionEvidenceArchive
from app.gpt_runtime.settlement_rows import load_rows
from app.remote_readonly.server import _authorize
from app.telemetry.domain import iso_time
from app.telemetry.models import TelemetryReading
from test_ash_settlement import START, sample
from test_hourly_brief import sessions
from test_settlement_archive_projection import freeze_and_archive, referenced_sample


def public_rows(rows):
    return [vars(row) for row in rows]


@pytest.mark.parametrize('value', [None, True, False, 0, 1, 10**30, 1.25, 1.2345678901234567, '', '采样灰分', [],
    [None, True, False, 1, {'灰分': 8.5}], {}, {'nested': [True, None, 9.2]}])
def test_json_types_and_explicit_null_match_original_hot_loader(sessions, value):
    with sessions.begin() as db:
        original = sample(db)
        row = db.get(DecisionCycleSample, original.slot)
        # Every selected field can retain its JSON type; SQLite must not turn
        # booleans into integers, objects into strings, or null into absence.
        row.snapshot = {key: deepcopy(value) for key in ('storage_version', 'evidence_policy',
            'sources', 'coarse_ash', 'heavy_amount', 'total_ash')}
        row.snapshot['cells'] = {key: deepcopy(value) for key in (
            'belt501', 'belt502', 'float_ash', 'float_amount', 'coarse_amount')}
        row.snapshot['model'] = {'not_needed': 'x'*1000}
        db.flush()
        expected = public_rows(ash_settlement.load_rows(db, START))
        actual = public_rows(load_rows(db, START))
        assert json.dumps(actual, sort_keys=True) == json.dumps(expected, sort_keys=True)
        assert type(actual[0]['snapshot']['coarse_ash']) is type(value)
        assert type(actual[0]['snapshot']['cells']['belt501']) is type(value)


@pytest.mark.parametrize('snapshot', [{}, {'cells': {}},
    {'total_ash': None, 'cells': {'float_ash': None}},
    {'sources': {'heavy-ash': False}, 'cells': {'belt501': {'value': 0}}}])
def test_absent_keys_stay_absent_including_legacy_inline_rows(sessions, snapshot):
    with sessions.begin() as db:
        original = sample(db)
        row = db.get(DecisionCycleSample, original.slot)
        row.snapshot = deepcopy(snapshot)
        db.flush()
        expected = public_rows(ash_settlement.load_rows(db, START))
        actual = public_rows(load_rows(db, START))
        assert actual == expected
        assert set(actual[0]['snapshot']) == set(expected[0]['snapshot'])
        assert set(actual[0]['snapshot']['cells']) == set(expected[0]['snapshot']['cells'])


@pytest.mark.parametrize('cells', [None, True, [], 1, 'bad'])
def test_malformed_cells_are_not_silently_repaired(sessions, cells):
    with sessions.begin() as db:
        original = sample(db)
        db.get(DecisionCycleSample, original.slot).snapshot = {'cells': cells}
        db.flush()
        with pytest.raises(AttributeError):
            ash_settlement.load_rows(db, START)
        with pytest.raises(AttributeError):
            load_rows(db, START)


def test_packed_hot_and_archived_rows_match_original_and_hot_slot_wins(sessions):
    with sessions.begin() as db:
        slot, _ = referenced_sample(db)
        expected = public_rows(ash_settlement.load_rows(db, START))
        assert public_rows(load_rows(db, START)) == expected
        freeze_and_archive(db)
        expected_archive = public_rows(ash_settlement.load_rows(db, START))
        assert public_rows(load_rows(db, START)) == expected_archive
        # A hot row wins for the same slot, including its distinct metadata.
        replacement = sample(db)
        row = db.get(DecisionCycleSample, slot)
        row.snapshot = dict(row.snapshot, total_ash={'value': 9.87, 'status': 'predicted'})
        row.fingerprint = 'new-hot-fingerprint'
        db.flush()
        actual = public_rows(load_rows(db, START))
        assert actual == public_rows(ash_settlement.load_rows(db, START))
        assert len(actual) == 1 and actual[0]['fingerprint'] == 'new-hot-fingerprint'
        assert actual[0]['snapshot']['total_ash']['value'] == 9.87
        assert 'context_key' not in actual[0]  # Same hot metadata contract.


@pytest.mark.parametrize('archived', [False, True])
def test_missing_required_authoritative_reference_still_fails(sessions, archived):
    with sessions.begin() as db:
        _, ids = referenced_sample(db)
        if archived:
            freeze_and_archive(db)
        db.execute(delete(TelemetryReading).where(TelemetryReading.id == ids['belt501']))
    with sessions() as db, pytest.raises(ValueError, match='原始记录缺失'):
        load_rows(db, START)


def test_full_archive_checksum_cannot_be_bypassed_by_projected_reader(sessions):
    with sessions.begin() as db:
        referenced_sample(db)
        freeze_and_archive(db)
        saved = db.get(DecisionEvidenceArchive, iso_time(START))
        full = json.loads(zlib.decompress(saved.payload))
        full[0]['snapshot']['model'] = {'unused': 'tampered'}
        payload = json.dumps(full).encode()
        assert hashlib.sha256(payload).hexdigest() != saved.checksum
        saved.payload = zlib.compress(payload)
    with sessions() as db, pytest.raises(ValueError, match='归档校验失败'):
        load_rows(db, START)


def test_projection_works_under_existing_readonly_authorizer(sessions):
    with sessions.begin() as db:
        referenced_sample(db)
    with sessions() as db:
        expected = public_rows(ash_settlement.load_rows(db, START))
        connection = db.connection().connection.driver_connection
        connection.set_authorizer(_authorize)
        try:
            assert public_rows(load_rows(db, START)) == expected
            with pytest.raises(DatabaseError):
                db.execute(text('DELETE FROM decision_cycle_samples'))
        finally:
            connection.set_authorizer(None)


def test_projected_sql_result_excludes_unrelated_payload_before_transport(sessions):
    marker = 'UNRELATED-MODEL-EXPLANATION'
    with sessions.begin() as db:
        original = sample(db)
        row = db.get(DecisionCycleSample, original.slot)
        row.snapshot = dict(row.snapshot, model={'explanation': marker*10_000},
            density_channels={'raw': marker*10_000}, inputs={'unneeded': marker*10_000})
        db.flush()

    class BridgeResultProbe:
        """Capture the actual selected values as a simulated bridge response."""
        def __init__(self, db):
            self.db, self.results = db, []

        def __getattr__(self, name):
            return getattr(self.db, name)

        def execute(self, statement, *args, **kwargs):
            result = self.db.execute(statement, *args, **kwargs)
            rows = result.all()
            self.results.append(json.dumps([tuple(row) for row in rows], ensure_ascii=False))
            return rows

    with sessions() as db:
        full = db.scalar(select(DecisionCycleSample.snapshot))
        full_bytes = len(json.dumps(full, ensure_ascii=False).encode())
        probe = BridgeResultProbe(db)
        actual = public_rows(load_rows(probe, START))
        assert actual == public_rows(ash_settlement.load_rows(db, START))
        assert len(probe.results) == 1
        assert marker not in probe.results[0]
        projected_bytes = len(probe.results[0].encode())
        assert projected_bytes < 3000
        assert projected_bytes < full_bytes/100
