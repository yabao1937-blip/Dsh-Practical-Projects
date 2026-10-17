"""Local retrospective replay preserves chemistry and never changes host tables."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
import json

import pytest

from app.decision_hourly.ash_settlement import build_settlement, latest_end
from app.decision_hourly.models import DecisionAshSettlement, DecisionCycleHour
from app.remote_readonly import settlement_replay as replay_module
from app.remote_readonly.settlement_replay import SettlementReplay
from app.telemetry.domain import iso_time, normalize
from app.telemetry.models import TelemetryPoint, TelemetryReading
from test_ash_settlement import sample
from test_hourly_brief import sessions
from test_readonly_settled_hours import START, NOW, cache, lab
from test_decision_sources import seeded


ORIGIN = 'readonly-local-replay'


def expected(db, start, now=NOW):
    result = build_settlement(db, start, now)
    result['item'].update(frozen=False, cached=False)
    return result


def assert_same(actual, original):
    copy = deepcopy(actual)
    assert copy.pop('calculation_origin') == ORIGIN
    assert copy['item'].pop('calculation_origin') == ORIGIN
    assert copy == original


@pytest.fixture
def loads(monkeypatch):
    calls = []
    original = replay_module.load_rows

    def count(db, start):
        calls.append(start)
        return original(db, start)

    monkeypatch.setattr(replay_module, 'load_rows', count)
    return calls


def test_ten_visible_hours_revise_in_one_pass_without_rehydrating_evidence(sessions, loads):
    end = latest_end(NOW)
    starts = [end-timedelta(hours=10-index) for index in range(10)]
    replay = SettlementReplay()
    with sessions.begin() as db:
        for start in starts:
            sample(db, start)
    with sessions() as db:
        before = [replay.get(db, start, NOW) for start in starts]
        assert len(loads) == 10
    with sessions.begin() as db:
        lab(db, starts[0]+timedelta(minutes=20), 8)
    with sessions() as db:
        original_tables = list(db.connection().connection.driver_connection.iterdump())
        after = [replay.get(db, start, NOW) for start in starts]
        assert len(loads) == 10
        for start, old, new in zip(starts, before, after):
            assert_same(new, expected(db, start))
            assert new['item']['value'] != old['item']['value']
        assert list(db.connection().connection.driver_connection.iterdump()) == original_tables
        assert replay.cache_info()['hours'] == 10
        assert replay.cache_info()['estimated_bytes'] <= 32*1024*1024


def test_first_manual_assay_does_not_rescue_invalid_coarse_prediction(sessions, loads):
    replay = SettlementReplay()
    with sessions.begin() as db:
        sample(db, START, coarse_status='missing')
    with sessions() as db:
        first = replay.get(db, START, NOW)
        assert first['item']['value'] is not None  # Original recorded total remains reference.
    with sessions.begin() as db:
        lab(db, START+timedelta(minutes=47), 8)
    with sessions() as db:
        revised = replay.get(db, START, NOW)
        assert_same(revised, expected(db, START))
        assert revised['item']['value'] is None and revised['item']['coverage'] == 0
        assert revised['item']['recalculated_count'] == 0
        assert len(loads) == 1


def test_late_actual_sample_uses_latest_observation_and_stops_at_next_sample(sessions, loads):
    starts = [START+timedelta(hours=index) for index in range(4)]
    replay = SettlementReplay()
    with sessions.begin() as db:
        for start in starts:
            sample(db, start)
        lab(db, START+timedelta(hours=2, minutes=15), 7, basis='legacy-lag-2h')
        lab(db, START+timedelta(minutes=47), 8)
        lab(db, START+timedelta(hours=3, minutes=10), 10)
    with sessions() as db:
        old = [replay.get(db, start, NOW) for start in starts]
    with sessions.begin() as db:
        latest_id = lab(db, START+timedelta(hours=1, minutes=20), 9).id
        lab(db, START+timedelta(hours=1, minutes=5), 1)
        lab(db, START+timedelta(hours=1, minutes=50), 99, created=NOW+timedelta(seconds=1))
    with sessions() as db:
        new = [replay.get(db, start, NOW) for start in starts]
        assert new[0] == old[0] and new[3] == old[3]
        for index in (1, 2):
            assert new[index]['heavy_ash']['records'][0]['id'] == latest_id
            assert new[index]['item']['value'] != old[index]['item']['value']
        for start, result in zip(starts, new):
            assert_same(result, expected(db, start))
        assert len(loads) == 4


def test_explicit_online_heavy_source_is_not_replaced_by_late_manual(sessions, loads):
    replay = SettlementReplay()
    with sessions.begin() as db:
        sample(db, START, choice=json.dumps(['kepserver', 'heavy-online']))
    with sessions() as db:
        old = replay.get(db, START, NOW)
    with sessions.begin() as db:
        lab(db, START+timedelta(minutes=47), 9)
    with sessions() as db:
        new = replay.get(db, START, NOW)
        assert_same(new, expected(db, START))
        assert new['item']['value'] == old['item']['value']
        assert new['item']['recalculated_count'] == 0 and len(loads) == 1


@pytest.mark.parametrize('completed', [False, True])
def test_changed_source_revision_reloads_evidence(sessions, loads, completed):
    replay = SettlementReplay()
    with sessions.begin() as db:
        sample(db, START)
        if completed:
            db.add(DecisionCycleHour(start=iso_time(START), end=iso_time(START+timedelta(hours=1)),
                generated_at=iso_time(START+timedelta(hours=1)), snapshot={}))
    with sessions() as db:
        replay.replay(db, START, NOW)
    with sessions.begin() as db:
        sample(db, START+timedelta(seconds=5))
        if completed:
            db.get(DecisionCycleHour, iso_time(START)).generated_at = iso_time(NOW)
    with sessions() as db:
        new = replay.replay(db, START, NOW)
        assert_same(new, expected(db, START))
        assert new['item']['sample_count'] == 2 and len(loads) == 2


def test_valid_host_settlement_is_returned_without_local_origin_or_hydration(sessions, loads):
    replay = SettlementReplay()
    with sessions.begin() as db:
        sample(db, START)
        cache(db, START)
    with sessions() as db:
        result = replay.get(db, START, NOW, defer_missing=True)
        assert result['item']['cached']
        assert 'calculation_origin' not in result and 'calculation_origin' not in result['item']
        assert loads == [] and replay.cache_info()['hours'] == 0


def test_hour_and_byte_bounds_and_elapsed_window_prune(sessions, loads):
    replay = SettlementReplay(max_hours=2)
    tiny = SettlementReplay(max_bytes=1)
    starts = [START+timedelta(hours=index) for index in range(3)]
    with sessions.begin() as db:
        for start in starts:
            sample(db, start)
    with sessions() as db:
        for start in starts:
            assert_same(replay.replay(db, start, NOW), expected(db, start))
        assert replay.cache_info()['hours'] == 2
        replay.replay(db, starts[0], NOW)
        assert len(loads) == 4  # Oldest entry was evicted, never an unbounded result cache.
        first = tiny.replay(db, START, NOW)
        assert tiny.cache_info() == dict(hours=0, estimated_bytes=0)
        assert tiny.replay(db, START, NOW) == first
        assert len(loads) == 6
        outside = replay.replay(db, START, NOW+timedelta(hours=12))
        assert outside['pending'] and 'calculation_origin' not in outside
        assert replay.cache_info() == dict(hours=0, estimated_bytes=0)
        assert len(loads) == 6


def test_sqlite_readonly_authorizer_and_detached_result(sessions):
    from app.remote_readonly.server import _authorize

    replay = SettlementReplay()
    with sessions.begin() as db:
        sample(db, START)
        lab(db, START+timedelta(minutes=47), 8)
    with sessions() as db:
        reference = expected(db, START)
        raw = db.connection().connection.driver_connection
        before = list(raw.iterdump())
        raw.set_authorizer(_authorize)
        try:
            first = replay.get(db, START, NOW)
            assert_same(first, reference)
            first['item']['value'] = 99
            first['contributions'].clear()
            assert_same(replay.get(db, START, NOW), reference)
        finally:
            raw.set_authorizer(None)
        assert list(raw.iterdump()) == before


def test_failed_new_revision_drops_old_result_and_propagates_error(sessions, monkeypatch, loads):
    replay = SettlementReplay()
    with sessions.begin() as db:
        sample(db, START)
    with sessions() as db:
        replay.replay(db, START, NOW)
    with sessions.begin() as db:
        lab(db, START+timedelta(minutes=47), 8)
    original = replay_module.build_settlement

    def fail(*_args, **_kwargs):
        raise ValueError('isolated replay failure')

    monkeypatch.setattr(replay_module, 'build_settlement', fail)
    with sessions() as db:
        with pytest.raises(ValueError, match='isolated replay failure'):
            replay.get(db, START, NOW)
        assert replay.cache_info() == dict(hours=0, estimated_bytes=0)
    monkeypatch.setattr(replay_module, 'build_settlement', original)
    with sessions() as db:
        assert_same(replay.get(db, START, NOW), expected(db, START))
        assert len(loads) == 2


def test_unsealed_stopped_production_evidence_is_rechecked_across_assay_revisions(sessions, monkeypatch):
    replay = SettlementReplay()
    stopped = [[START.timestamp(), START.timestamp()+5]]
    checks = []

    def stop_evidence(*_args):
        checks.append(True)
        return deepcopy(stopped)

    monkeypatch.setattr(replay_module, 'stopped_intervals', stop_evidence)
    with sessions.begin() as db:
        sample(db, START)
        sample(db, START+timedelta(seconds=5))
    with sessions() as db:
        replay.replay(db, START, NOW)
    with sessions.begin() as db:
        lab(db, START+timedelta(minutes=47), 8)
    with sessions() as db:
        result = replay.replay(db, START, NOW)
        reference = build_settlement(db, START, NOW, stopped=stopped)
        reference['item'].update(frozen=False, cached=False)
        assert_same(result, reference)
        assert result['item']['stopped_seconds'] == 5
        assert checks == [True, True]


def zero_production(db, config, when):
    for index in (1, 5, 6):
        point = config.points[index]
        db.add(TelemetryReading(source_id=config.source_id, point_key=point.key,
            batch_id='isolated-stop-batch', received_at=iso_time(when),
            sample=normalize(point, 0, 0, when, when, when)))
    db.flush()


@pytest.mark.parametrize('change', ['enabled', 'definition'])
def test_unsealed_coverage_point_changes_invalidate_result_without_reloading_rows(sessions, seeded, loads, change):
    config, _, _ = seeded
    replay = SettlementReplay()
    with sessions.begin() as db:
        sample(db, START)
        zero_production(db, config, START+timedelta(seconds=10))
    with sessions() as db:
        first = replay.replay(db, START, NOW)
        assert first['item']['stopped_seconds'] == 5
    with sessions.begin() as db:
        point = db.get(TelemetryPoint, (config.source_id, config.points[1].key))
        if change == 'enabled':
            point.enabled = False
        else:
            point.config = {**point.config, 'scale': 2}
    with sessions() as db:
        new = replay.replay(db, START, NOW)
        assert_same(new, expected(db, START))
        assert new['revision'] == first['revision']
        assert new['item']['stopped_seconds'] == 0
        assert len(loads) == 1


def test_later_host_seal_replaces_unsealed_coverage_then_ignores_point_toggle(sessions, seeded, loads):
    config, _, _ = seeded
    replay = SettlementReplay()
    stopped = [[START.timestamp()+10, START.timestamp()+15]]
    with sessions.begin() as db:
        sample(db, START)
    with sessions() as db:
        first = replay.replay(db, START, NOW)
        assert first['item']['stopped_seconds'] == 0
    with sessions.begin() as db:
        sealed = build_settlement(db, START, NOW, stopped=stopped)
        # No completed-hour insertion: the original source revision stays equal.
        db.add(DecisionAshSettlement(start=iso_time(START), end=iso_time(START+timedelta(hours=1)),
            deadline=iso_time(START+timedelta(hours=2)), generated_at=iso_time(NOW), snapshot=sealed))
    with sessions() as db:
        result = replay.replay(db, START, NOW)
        assert_same(result, expected(db, START))
        assert result['revision'] == first['revision']
        assert result['item']['stopped_seconds'] == 5
    with sessions.begin() as db:
        db.get(TelemetryPoint, (config.source_id, config.points[1].key)).enabled = False
    with sessions() as db:
        result = replay.replay(db, START, NOW)
        assert_same(result, expected(db, START))
        assert result['item']['stopped_seconds'] == 5 and len(loads) == 1


def test_clock_rollback_cannot_return_a_future_entered_assay(sessions, loads):
    replay = SettlementReplay()
    with sessions.begin() as db:
        sample(db, START)
        lab(db, START+timedelta(minutes=47), 8, created=NOW)
    with sessions() as db:
        present = replay.replay(db, START, NOW)
        earlier = NOW-timedelta(hours=2)
        past = replay.replay(db, START, earlier)
        assert_same(past, expected(db, START, earlier))
        assert past['heavy_ash']['records'] == []
        assert past['item']['value'] != present['item']['value']
        assert replay.replay(db, START, NOW) == present
        assert len(loads) == 1


def test_read_error_and_malformed_result_never_serve_old_revision(sessions, monkeypatch):
    replay = SettlementReplay()
    with sessions.begin() as db:
        sample(db, START)
    with sessions() as db:
        replay.replay(db, START, NOW)
        original_source = replay_module.source_revision

        def disconnected(*_args):
            raise RuntimeError('isolated read unavailable')

        monkeypatch.setattr(replay_module, 'source_revision', disconnected)
        with pytest.raises(RuntimeError, match='isolated read unavailable'):
            replay.replay(db, START, NOW)
        assert replay.cache_info() == dict(hours=0, estimated_bytes=0)
        monkeypatch.setattr(replay_module, 'source_revision', original_source)
        original_builder = replay_module.build_settlement

        def malformed(*args, **kwargs):
            result = original_builder(*args, **kwargs)
            result['item']['value'] = float('nan')
            return result

        monkeypatch.setattr(replay_module, 'build_settlement', malformed)
        with pytest.raises(ValueError):
            replay.replay(db, START, NOW)
        assert replay.cache_info() == dict(hours=0, estimated_bytes=0)


def test_concurrent_readers_share_one_evidence_load_and_detach_results(sessions, loads):
    replay = SettlementReplay()
    with sessions.begin() as db:
        sample(db, START)
        lab(db, START+timedelta(minutes=47), 8)

    def read(_index):
        with sessions() as db:
            return replay.get(db, START, NOW)

    with ThreadPoolExecutor(max_workers=6) as executor:
        results = list(executor.map(read, range(12)))
    assert len(loads) == 1 and all(result == results[0] for result in results)
    results[0]['item']['value'] = 99
    assert results[1]['item']['value'] != 99


@pytest.mark.parametrize('options', [dict(max_hours=0), dict(max_hours=11),
    dict(max_bytes=0), dict(max_bytes=32*1024*1024+1), dict(max_hours=True)])
def test_invalid_limits_are_rejected(options):
    with pytest.raises(ValueError):
        SettlementReplay(**options)
