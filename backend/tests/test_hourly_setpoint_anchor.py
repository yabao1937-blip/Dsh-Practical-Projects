"""Frozen MD_SET evidence survives target/lab changes, sessions and workers."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta

import pytest
from sqlalchemy import delete, select

from app.decision_hourly.models import GptHourlyDensityAnchor
from app.decision_hourly.service import context_definition, observation_tick, read_hour
from app.gpt_density.hourly_anchor import capture, read
from app.services.state import load_store
from app.telemetry.domain import iso_time
from test_hourly_quality_advice import prepared
from test_hourly_brief import HOUR, sessions, send, setup_clock
from test_decision_sources import seeded


def move(current, when, value):
    current['generated_at'] = iso_time(when)
    current['execution_basis']['value'] = value
    current['inputs']['density']['observed_at'] = iso_time(when)
    current['inputs']['is_stoppage']['observed_at'] = iso_time(when)
    for c in current['execution_basis']['components'].values():
        c['value'], c['observed_at'] = value, iso_time(when)
    return current


def test_same_hour_does_not_add_correction_again_after_setpoint_move(sessions, prepared):
    current, settlement, _, _ = prepared
    with sessions() as db:
        first = read_hour(db, HOUR, current)
        assert first['suggested_density']['value'] == 1.46
        original = deepcopy(read(db, HOUR))
    later = HOUR+timedelta(minutes=20)
    move(current, later, 1.46)
    with sessions.begin() as db:
        capture(db, later, current, context_definition(current, load_store(db)))
    with sessions() as db:
        result = read_hour(db, later, current)
        assert result['suggested_density']['value'] == 1.46  # Never 1.44.
        assert result['execution_guidance']['rhoNew'] == 1.46
        assert read(db, later) == original
        settlement['item'].update(value=9.6)
        settlement['revision'] = 'late-revised'
        assert read_hour(db, later, current)['suggested_density']['value'] == 1.44
        current['target'] = dict(min=9,max=9.5)
        assert read_hour(db, later, current)['suggested_density']['value'] == 1.473
        assert read(db, later) == original


def test_missing_anchor_cannot_use_actual_or_mean_and_can_capture_after_recovery(sessions, prepared):
    current = prepared[0]
    good = deepcopy(current)
    with sessions.begin() as db:
        db.execute(delete(GptHourlyDensityAnchor))
        current['execution_basis'] = dict(value=None,status='missing',components={},reason='MD_SET 缺数')
        assert capture(db,HOUR,current,context_definition(current,load_store(db))) is None
    with sessions() as db:
        r = read_hour(db,HOUR,current)
        assert r['suggested_density']['value'] is None and r['advice_blockers'] == ['density']
        assert '本小时尚未采到' in r['suggested_density']['reason']
    when = HOUR+timedelta(seconds=5)
    move(good,when,1.49)
    with sessions.begin() as db:
        capture(db,when,good,context_definition(good,load_store(db)))
    with sessions() as db:
        assert read_hour(db,when,good)['suggested_density']['value'] == 1.47
        # Already frozen: losing live MD_SET only blocks execution.
        r = read_hour(db,when,current)
        assert r['suggested_density']['value'] == 1.47
        assert r['execution_guidance']['rhoNew'] is None


@pytest.mark.parametrize('issue', ['stale','future','held','wrong_unit'])
def test_first_anchor_rejects_invalid_component(sessions, prepared, issue):
    current = prepared[0]
    c = next(iter(current['execution_basis']['components'].values()))
    if issue == 'stale': c['observed_at'] = iso_time(HOUR-timedelta(seconds=31))
    if issue == 'future': c['observed_at'] = iso_time(HOUR+timedelta(seconds=1))
    if issue == 'held': c['status'] = 'held'
    if issue == 'wrong_unit': c['unit'] = '%'
    with sessions.begin() as db:
        db.execute(delete(GptHourlyDensityAnchor))
        assert capture(db,HOUR,current,context_definition(current,load_store(db))) is None


def test_new_hour_uses_new_setpoint_and_worker_commit_is_concurrent_safe(sessions, prepared, seeded, monkeypatch):
    next_hour = HOUR+timedelta(hours=1)
    cfg, repo, values = seeded
    values[4] = values[-1] = 1.46  # A304 actual and MD_SET, same authoritative batch.
    setup_clock(monkeypatch,next_hour)
    monkeypatch.setattr('app.services.decision_sources.utc_now',lambda:next_hour)
    send(repo,cfg,next_hour-timedelta(seconds=90),values)
    send(repo,cfg,next_hour,values)
    with ThreadPoolExecutor(max_workers=2) as workers:
        list(workers.map(lambda _:observation_tick(next_hour,sessions),range(2)))
    with sessions() as db:
        rows = list(db.scalars(select(GptHourlyDensityAnchor)))
        assert len(rows) == 2
        assert read(db,HOUR)['value'] == 1.48
        assert read(db,next_hour)['value'] == 1.46
        assert read(db,next_hour)['captured_at'] == iso_time(next_hour)


def test_real_config_change_does_not_recapture_anchor(sessions, prepared):
    current = prepared[0]
    current['sources']['resolved_choices']['density-current'] = '["other","point"]'
    with sessions.begin() as db:
        frozen = deepcopy(read(db,HOUR))
        capture(db,HOUR,current,context_definition(current,load_store(db)))
        r = read_hour(db,HOUR,current)
        assert r['suggested_density']['value'] is None and 'configuration' in r['advice_blockers']
        assert read(db,HOUR) == frozen
