"""Three browser-equivalent readers share producers, not pool checkouts."""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import threading

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import TimeoutError as PoolTimeout
from sqlalchemy.pool import QueuePool

from app.remote_readonly import runtime


def test_three_browsers_and_compatibility_views_fit_one_three_connection_pool(monkeypatch):
    from app.remote_readonly import operations, startup

    engine = create_engine('sqlite://', poolclass=QueuePool, pool_size=3,
        max_overflow=0, pool_timeout=.05, connect_args={'check_same_thread': False})
    release, all_started, lock = threading.Event(), threading.Event(), threading.Lock()
    calls = Counter()
    active_lanes = set()
    stamp = '2026-10-17T03:00:00.000+00:00'
    bundles = {
        'decisions': {'current': {'generated_at': stamp}, 'hourly': {'checked_at': stamp}},
        'operations': {'schema_version': 1, 'generated_at': stamp,
            'health': {'ok': True, 'status': 'ok', 'checked_at': stamp},
            'telemetry': {'checked_at': stamp, 'items': []},
            'manual_measurements': {'checked_at': stamp, 'items': []}, 'errors': {}},
        'hours': {'generated_at': stamp, 'items': [{'value': 8.5}]},
        'startup': {'state': {'_revision': 'host-revision'},
            'model': {'model': {'method': 'expert8'}}, 'sources': {'revision': 7}},
    }

    def producer(name):
        # Deliberately hold each actual pool connection while every other caller
        # arrives. A waiter doing its own SELECT would exhaust this real pool.
        def build():
            lane = 'display' if name in ('operations', 'startup', 'hours') else 'calculation'
            with runtime.read_window(lane), engine.connect() as db:
                assert db.execute(text('SELECT 1')).scalar_one() == 1
                with lock:
                    calls[name] += 1
                    active_lanes.add(lane)
                    if len(active_lanes) == 2:
                        all_started.set()
                assert release.wait(8)
                return deepcopy(bundles[name])
        return build

    caches = {name: runtime.SingleFlightBundle(producer(name), clock=lambda: 0)
              for name in bundles}
    monkeypatch.setattr(runtime, '_decisions', caches['decisions'])
    monkeypatch.setattr(runtime, '_recent_hours', caches['hours'])
    monkeypatch.setattr(operations, '_operations', caches['operations'])
    monkeypatch.setattr(startup, '_startup', caches['startup'])
    reads = [lambda: runtime.decision_view('current'), lambda: runtime.decision_view('hourly'),
        lambda: operations.operations_view(), lambda: operations.operations_view('health'),
        lambda: operations.operations_view('telemetry'),
        lambda: operations.operations_view('manual_measurements'), runtime.recent_hours_view,
        lambda: startup.startup_view('state'), lambda: startup.startup_view('model'),
        lambda: startup.startup_view('sources')]
    try:
        with ThreadPoolExecutor(max_workers=30) as browsers:
            futures = [browsers.submit(read) for _browser in range(3) for read in reads]
            try:
                assert all_started.wait(5)
                assert len(calls) == 2 and all(count == 1 for count in calls.values())
                assert engine.pool.checkedout() == 2
                # An explicit query can still use the spare third connection.
                with engine.connect() as explicit:
                    assert explicit.execute(text('SELECT 1')).scalar_one() == 1
                    with pytest.raises(PoolTimeout):
                        with engine.connect():
                            pytest.fail('Pool admitted a fourth connection')
            finally:
                release.set()
            results = [future.result(timeout=5) for future in futures]
        assert results[:10] == results[10:20] == results[20:30]
        assert calls == {'decisions': 1, 'operations': 1, 'hours': 1, 'startup': 1}
        assert engine.pool.checkedout() == 0
        results[0]['generated_at'] = 'caller mutation'
        assert runtime.decision_view('current')['generated_at'] == stamp
        for read in reads:
            read()
        assert calls == {'decisions': 1, 'operations': 1, 'hours': 1, 'startup': 1}
        assert engine.pool.checkedout() == 0
    finally:
        release.set()
        for cache in caches.values():
            cache.close()
        engine.dispose()


def test_failed_producer_releases_its_admission_window():
    with pytest.raises(RuntimeError):
        with runtime.read_window('display'):
            raise RuntimeError('isolated producer failure')
    with ThreadPoolExecutor(max_workers=1) as reader:
        def next_read():
            with runtime.read_window('display'):
                return 'released'
        assert reader.submit(next_read).result(timeout=1) == 'released'
