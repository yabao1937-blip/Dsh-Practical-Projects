"""Historical replay is never evidence of advice available before a real move."""
from copy import deepcopy
from datetime import timedelta

import pytest

from app.decision_hourly.advice_provenance import origin_class, select_recorded
from app.decision_hourly.density_trend import compare_actions
from app.decision_hourly.models import GptHourlyDensityAdvice
from app.telemetry.domain import iso_time
from test_density_trend import action, client, sessions, HOUR, result
from app.decision_hourly.advice_archive import save_result


def point(minute, value=1.49, **extra):
    return dict(at=iso_time(HOUR+timedelta(minutes=minute)), apply_start=iso_time(HOUR),
                value=value, status='calculated' if value is not None else 'missing', **extra)


def move(before=3, after=4):
    return dict(at=iso_time(HOUR+timedelta(minutes=after, seconds=5)),
        before_at=iso_time(HOUR+timedelta(minutes=before)),
        after_at=iso_time(HOUR+timedelta(minutes=after)), before=1.48, after=1.5,
        continuous=True)


@pytest.mark.parametrize('hour_offset', range(7))
def test_seven_replayed_hours_are_not_online_prior(hour_offset):
    # Different application hours cover every formerly matched replay scenario.
    at = HOUR-timedelta(hours=hour_offset)
    replay = dict(at=iso_time(at), apply_start=iso_time(at), value=1.51,
                  origin='advice-backfill-v1')
    a = dict(at=iso_time(at+timedelta(minutes=5)), before=1.48, after=1.50,
        before_at=iso_time(at+timedelta(minutes=4)), after_at=iso_time(at+timedelta(minutes=5)))
    compare_actions([a], [replay])
    assert a['hourly_advice'] is None and a['prior_hourly_advice'] is None
    assert a['difference'] is None
    assert a['comparison']['ignored_replay_count'] == 1


@pytest.mark.parametrize('latest', [point(2, None), point(2, origin='external-import'),
                                   point(2, origin=None), point(2, origin='')])
def test_latest_missing_or_unknown_cannot_resurrect_old_valid(latest):
    a = move()
    compare_actions([a], [point(1), latest])
    assert a['difference'] is None
    if 'origin' not in latest:
        assert a['hourly_advice']['value'] is None
    else:
        assert a['hourly_advice'] is None
        assert a['comparison']['origin_class'] == 'unknown'


def test_late_revision_and_observation_interval_are_distinguished():
    a = move()
    compare_actions([a], [point(2, 1.49), point(3.5, 1.51), point(5, 1.55)])
    assert a['hourly_advice']['value'] == 1.51
    assert a['prior_hourly_advice']['value'] == 1.49
    assert a['prior_difference'] == .01 and a['difference'] == -.01
    assert a['comparison']['prior_availability'] == 'available'
    assert a['comparison']['availability'] == 'within-observation-interval'
    assert a['hourly_advice']['origin_class'] == 'online-default'
    assert a['hourly_advice']['physical_availability'] == 'not-recorded'


def test_cross_hour_move_has_no_guaranteed_prior_even_when_after_advice_exists():
    a = move(before=-1, after=1)
    compare_actions([a], [point(0, 1.51)])
    assert a['hourly_advice']['value'] == 1.51
    assert a['prior_hourly_advice'] is None
    assert a['comparison']['availability'] == 'cross-hour-uncertain'


def test_endpoint_keeps_replay_origin_but_excludes_it_from_action_comparison(client, sessions, monkeypatch):
    monkeypatch.setattr('app.decision_hourly.router.utc_now', lambda:HOUR+timedelta(minutes=10))
    with sessions.begin() as db:
        row = save_result(db, HOUR, result(1.49))
        row.snapshot = {**row.snapshot, 'origin':'advice-backfill-v1'}
        action(db, 'replayed', HOUR+timedelta(minutes=5))
    with sessions() as db:
        before = list(db.connection().connection.driver_connection.iterdump())
        data = client.get('/api/v1/decision/evidence/density-trend').json()
        assert data['suggestions'][0]['origin'] == 'advice-backfill-v1'
        assert data['actions'][0]['hourly_advice'] is None
        assert data['actions'][0]['comparison']['ignored_replay_count'] == 1
        assert list(db.connection().connection.driver_connection.iterdump()) == before


def test_input_points_and_action_evidence_are_not_mutated_by_selector():
    points = [point(2, 1.5), point(1, 1.49)]
    saved = deepcopy(points)
    selected, _ = select_recorded(points, iso_time(HOUR), HOUR+timedelta(minutes=3))
    assert selected['value'] == 1.5
    assert points == saved and origin_class(selected) == 'online-default'
