"""Wash/coarse ratio integration without touching the production database."""
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace

import pytest
from app.gpt_coarse.amount import POLICY
from app.services.decision_live import calculate
from app.services.decision_sources import shared_snapshot, identity
from app.decision_hourly.service import record, compact_cell
from app.decision_hourly.history import historical_cells
from app.decision_hourly.models import DecisionCycleSample
from app.decision_hourly.references import resolve_snapshots
from app.models import DecisionSourceRevision
from app.telemetry.domain import iso_time
from test_decision_live import inputs, NOW
from test_hourly_brief import sessions, HOUR, send
from test_decision_sources import seeded, client


def test_ratio_replaces_manual_and_drives_balance_without_mutation(inputs):
    cells, store = inputs
    original = deepcopy(cells)
    for feed, coarse in [(600, 15), (800, 20), (692.31, 17.30775)]:
        cells['coal_amount']['value'] = feed
        cells['coarse_amount'].update(value=999, status='manual')
        result = calculate(cells, store, now=NOW)
        assert result['inputs']['coarse_amount']['value'] == pytest.approx(coarse)
        assert result['heavy_amount']['value'] == pytest.approx(500-40-coarse)
        assert result['total_ash']['value'] is not None
        assert result['suggested_density']['value'] is not None
    assert original['coarse_amount']['value'] == 20
    assert cells['coarse_amount']['value'] == 999


@pytest.mark.parametrize('change', [dict(value=None), dict(value=-1), dict(value=float('nan')),
    dict(value=True), dict(unit='kg/h'), dict(status='missing'), dict(observed_at=None),
    dict(observed_at=iso_time(NOW-timedelta(seconds=31)))])
def test_bad_feed_cannot_produce_valid_coarse_amount_or_advice(inputs, change):
    cells, store = inputs
    cells['coal_amount'].update(change)
    result = calculate(cells, store, now=NOW)
    assert result['inputs']['coarse_amount']['value'] is None
    assert result['total_ash']['value'] is None
    assert result['suggested_density']['value'] is None


def test_stopped_feed_keeps_ratio_for_observation_and_blocks_advice(inputs):
    cells, store = inputs
    cells['coal_amount'].update(value=800, raw_value=0, status='held')
    result = calculate(cells, store, now=NOW)
    assert result['inputs']['coarse_amount']['value'] == 20
    assert result['inputs']['coarse_amount']['status'] == 'stopped'
    assert result['total_ash']['status'] == 'held'
    assert result['suggested_density']['value'] is None


def test_obsolete_binding_cannot_override_ratio_and_new_binding_rejected(sessions, seeded, client):
    with sessions.begin() as db:
        db.add(DecisionSourceRevision(effective_at=iso_time(HOUR-timedelta(minutes=1)),
            config=dict(choices={'coarse-amount':identity('manual-measurements','coarse_slime_amount')},target_min=8.5,target_max=9)))
    with sessions() as db:
        result = shared_snapshot(db)
        assert result['sources']['resolved_choices']['coarse-amount'] == ''
        assert result['cells']['coarse_amount']['value'] == 15
    revision = client.get('/api/v1/decision/sources').json()['revision']
    response = client.patch('/api/v1/decision/sources',json={'revision':revision,
        'choices':{'coarse-amount':identity('manual-measurements','coarse_slime_amount')}})
    assert response.status_code == 422


def test_reference_roundtrip_and_hour_mean_keep_derived_values(sessions, seeded):
    cfg, repo, values = seeded
    cfg.history_interval_seconds = 5
    rows = []
    for index, feed in enumerate((600, 800)):
        at = HOUR + timedelta(seconds=5*index)
        values[1] = feed
        send(repo, cfg, at, values)
        with sessions.begin() as db:
            result = shared_snapshot(db, at)
            record(db, at, result)
            row = db.get(DecisionCycleSample, int(at.timestamp())//5)
            snapshot = resolve_snapshots(db,[row.snapshot])[0]
            assert snapshot['cells']['coarse_amount'] == compact_cell(result['cells']['coarse_amount'])
            assert snapshot['cells']['coarse_amount']['derivation'] == POLICY
            assert row.snapshot['cells']['coarse_amount']['$reading'] == row.snapshot['cells']['coal_amount']['$reading']
            rows.append(SimpleNamespace(snapshot=snapshot,sampled_at=row.sampled_at,fingerprint=row.fingerprint))
    with sessions() as db:
        output, _, _ = historical_cells(db,HOUR,HOUR+timedelta(hours=1),rows)
        assert output['coarse_amount']['value'] == 17.5
        assert output['coarse_amount']['sample_count'] == 2
        legacy = deepcopy(rows)
        legacy[0].snapshot['cells']['coarse_amount'].pop('derivation')
        assert historical_cells(db,HOUR,HOUR+timedelta(hours=1),legacy)[0]['coarse_amount']['value'] is None
