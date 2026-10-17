"""Read-only local replay reaches both views without persisting derived results."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from datetime import timedelta
import io
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import QueuePool

from app.decision_hourly.ash_settlement import get_settlement, latest_end
from app.decision_hourly.models import DecisionAshSettlement
from app.decision_hourly.service import read_hour
from app.gpt_density.hourly_anchor import read as read_anchor
from app.remote_readonly import runtime
from app.remote_readonly.server import ReadOnlyManager, create_router
from app.remote_readonly.settlement_replay import SettlementReplay
from app.telemetry.domain import iso_time
from remote_sqlite import connect
from test_ash_settlement import sample
from test_hourly_brief import HOUR, sessions
from test_hourly_quality_advice import prepared
from test_decision_sources import seeded
from test_readonly_settled_hours import NOW, cache, lab


def test_pending_replay_releases_display_transaction_before_calculation(monkeypatch):
    import app.database
    from app.remote_readonly import settled_hours

    events, counter = [], []

    class Database:
        def __enter__(self):
            counter.append(len(counter)+1)
            self.number = counter[-1]
            events.append(('open', self.number))
            return self

        def __exit__(self, *_):
            events.append(('close', self.number))

        def execute(self, statement):
            assert str(statement) == 'BEGIN'

    @contextmanager
    def admission(lane):
        events.append(('enter', lane))
        yield
        events.append(('leave', lane))

    provider = SettlementReplay()

    def read(db, *, now, settlement_reader=None):
        if db.number == 1:
            assert settlement_reader is None
            return {'items': [{'pending': True}]}
        assert ('close', 1) in events and ('leave', 'display') in events
        assert settlement_reader == provider.replay
        return {'items': [{'value': 8.8, 'calculation_origin': 'readonly-local-replay'}]}

    monkeypatch.setattr(app.database, 'SessionLocal', Database)
    monkeypatch.setattr(runtime, 'read_window', admission)
    monkeypatch.setattr(runtime, 'host_now', lambda db: NOW)
    monkeypatch.setattr(runtime, 'settlement_replay', lambda: provider)
    monkeypatch.setattr(settled_hours, 'read_settled_hours', read)
    assert runtime._build_recent_hours()['items'][0]['value'] == 8.8
    assert events == [('enter', 'display'), ('open', 1), ('close', 1), ('leave', 'display'),
                      ('enter', 'calculation'), ('open', 2), ('close', 2), ('leave', 'calculation')]


def test_ten_late_hours_reach_three_readers_through_real_readonly_bridge(sessions, monkeypatch):
    import app.database

    end = latest_end(NOW)
    starts = [end-timedelta(hours=i) for i in range(10, 0, -1)]
    with sessions.begin() as db:
        db.execute(text('PRAGMA journal_mode=WAL'))
        lab(db, starts[0]-timedelta(hours=1), 7, created=NOW-timedelta(seconds=1))
        for stamp in starts:
            sample(db, stamp)
            cache(db, stamp)
    token = 'isolated-replay-test-'+'x'*40
    path = sessions.kw['bind'].url.database
    manager = ReadOnlyManager(path, {'token': token, 'clients': ['127.0.0.1']})
    host = FastAPI()
    host.include_router(create_router(manager), prefix='/api/v1')
    clock, observed = [0.], [NOW]
    provider = SettlementReplay()

    class Response(io.BytesIO):
        def __init__(self, response):
            super().__init__(response.content)
            self.status = response.status_code

    with TestClient(host, client=('127.0.0.1', 12345)) as client:
        def opener(request, timeout):
            response = client.request(request.method, urlsplit(request.full_url).path,
                content=request.data, headers=dict(request.header_items()))
            if response.status_code >= 400:
                raise HTTPError(request.full_url, response.status_code, 'isolated bridge',
                                response.headers, io.BytesIO(response.content))
            return Response(response)

        engine = create_engine('sqlite://', creator=lambda: connect(
            {'server_url': 'http://isolated-host', 'token': token}, opener=opener),
            poolclass=QueuePool, pool_size=3, max_overflow=0, pool_timeout=3)
        shared = runtime.SingleFlightBundle(runtime._build_recent_hours,
            ttl_seconds=runtime._recent_hours.ttl_seconds, clock=lambda: clock[0])
        monkeypatch.setattr(app.database, 'SessionLocal', sessionmaker(engine, autoflush=False))
        monkeypatch.setattr(runtime, 'host_now', lambda db: observed[0])
        monkeypatch.setattr(runtime, 'settlement_replay', lambda: provider)
        try:
            before = shared.get()
            assert all(item['value'] == pytest.approx(7.95) for item in before['items'][-10:])
            with sessions.begin() as db:
                lab(db, starts[0]+timedelta(minutes=47), 8, created=NOW+timedelta(milliseconds=1))
                frozen = {row.start: deepcopy(row.snapshot) for row in db.scalars(select(DecisionAshSettlement))}
            # An assay arrives just after a cached response; the browser's next
            # ten-second check crosses the short result-cache expiry.
            clock[0], observed[0] = 10., NOW+timedelta(seconds=10)
            started = time.perf_counter()
            with ThreadPoolExecutor(max_workers=3) as browsers:
                results = list(browsers.map(lambda _: shared.get(), range(3)))
            assert 10+time.perf_counter()-started < 30
            assert results[0] == results[1] == results[2]
            assert all(item['value'] == pytest.approx(8.8) and
                       item['calculation_origin'] == 'readonly-local-replay'
                       for item in results[0]['items'][-10:])
            assert all(item['pending'] for item in results[0]['items'][:-10])
            assert provider.cache_info()['hours'] == 10
            assert not manager._transactions and engine.pool.checkedout() == 0
            with sessions() as db:
                assert {row.start: row.snapshot for row in db.scalars(select(DecisionAshSettlement))} == frozen
                for stamp, item in zip(starts, results[0]['items'][-10:]):
                    expected = get_settlement(db, stamp, observed[0])['item']
                    assert {k: v for k, v in item.items() if k != 'calculation_origin'} == expected
        finally:
            shared.close()
            engine.dispose()
            manager.close()


def test_hourly_advice_injection_uses_same_revised_ash_and_frozen_anchor(sessions, prepared):
    current, old_settlement, _, _ = prepared
    revised = deepcopy(old_settlement)
    revised['revision'] = 'readonly-new-assay'
    revised['item'].update(value=8.7, calculation_origin='readonly-local-replay')
    calls = []

    def reader(db, start, now, defer_missing=False):
        calls.append((start, now, defer_missing))
        return deepcopy(revised)

    with sessions() as db:
        before = list(db.connection().connection.driver_connection.iterdump())
        anchor = deepcopy(read_anchor(db, HOUR))
        assert read_hour(db, HOUR, current)['suggested_density']['value'] == 1.46
        result = read_hour(db, HOUR, current, settlement_reader=reader)
        assert result['suggested_density']['value'] == 1.48
        assert result['advice_inputs']['ash']['revision'] == 'readonly-new-assay'
        assert result['advice_inputs']['ash']['calculation_origin'] == 'readonly-local-replay'
        assert calls == [(HOUR-timedelta(hours=2), HOUR, True)]
        assert read_anchor(db, HOUR) == anchor
        assert list(db.connection().connection.driver_connection.iterdump()) == before
