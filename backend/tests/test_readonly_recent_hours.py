"""Default hour-table readers coalesce; explicit historical queries stay exact."""
from datetime import datetime, timezone
from contextlib import contextmanager

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import pytest

from app.remote_readonly import runtime
from app.routers import decision


def test_recent_hours_producer_uses_one_session_host_clock_and_detaches_json(monkeypatch):
    import app.database
    import app.remote_readonly.settled_hours

    stamp = datetime(2026, 10, 17, 3, 20, tzinfo=timezone.utc)
    calls, results = [], {'generated_at': stamp.isoformat(), 'items': [{'value': 8.5}]}

    class Database:
        def __enter__(self):
            calls.append('open')
            return self

        def __exit__(self, *_):
            calls.append('close')

        def execute(self, statement):
            calls.append(str(statement))

    database = Database()
    monkeypatch.setattr(app.database, 'SessionLocal', lambda: database)

    @contextmanager
    def admission(kind):
        assert kind == 'display', 'Projected hours must not queue behind the model calculation'
        yield

    monkeypatch.setattr(runtime, 'read_window', admission)

    def host_clock(db):
        assert db is database
        calls.append('clock')
        return stamp

    def hours(db, **kwargs):
        assert db is database and kwargs == {'now': stamp}
        calls.append('hours')
        return results

    monkeypatch.setattr(runtime, 'host_now', host_clock)
    monkeypatch.setattr(app.remote_readonly.settled_hours, 'read_settled_hours', hours)
    result = runtime._build_recent_hours()
    assert calls == ['open', 'BEGIN', 'clock', 'hours', 'close']
    assert result == results
    result['items'][0]['value'] = 99
    assert results['items'][0]['value'] == 8.5


def test_default_route_waiter_never_opens_a_database_transaction(monkeypatch):
    class NoCheckout:
        @property
        def bind(self):
            pytest.fail('Cached hour-table request touched a database connection')

    monkeypatch.setattr(decision, 'REMOTE_READONLY', {'server_url': 'http://isolated-host'})
    monkeypatch.setattr(runtime, 'recent_hours_view', lambda: {'items': [], 'generated_at': 'host-time'})
    app = FastAPI()
    app.include_router(decision.router, prefix='/api/v1')
    app.dependency_overrides[decision.get_db] = lambda: NoCheckout()
    with TestClient(app) as client:
        response = client.get('/api/v1/decision/total-ash-hours')
    assert response.status_code == 200
    assert response.json() == {'items': [], 'generated_at': 'host-time'}


@pytest.mark.parametrize('readonly', [False, True])
def test_explicit_ranges_and_production_keep_uncached_path(monkeypatch, readonly):
    import app.decision_hourly.ash_settlement

    class Database:
        class bind:
            class dialect:
                name = 'sqlite'

        def __init__(self):
            self.statements = []

        def execute(self, statement):
            self.statements.append(str(statement))

    db, calls = Database(), []
    begin = datetime(2026, 10, 17, 0, tzinfo=timezone.utc) if readonly else None
    end = datetime(2026, 10, 17, 1, tzinfo=timezone.utc) if readonly else None
    now = datetime(2026, 10, 17, 3, tzinfo=timezone.utc)
    monkeypatch.setattr(decision, 'REMOTE_READONLY', {'server_url': 'http://isolated-host'} if readonly else None)
    monkeypatch.setattr(runtime, 'recent_hours_view', lambda: pytest.fail('Explicit/production read entered cache'))
    monkeypatch.setattr(runtime, 'host_now', lambda current: now)

    def hours(current, first, last, **kwargs):
        calls.append((current, first, last, kwargs))
        return {'items': []}

    monkeypatch.setattr(app.decision_hourly.ash_settlement, 'read_settled_hours', hours)
    assert decision.total_ash_hours(begin, end, db) == {'items': []}
    assert calls == [(db, begin, end, {'now': now} if readonly else {})]
    assert db.statements == ['BEGIN']


def test_recent_hours_failure_is_typed_503_and_has_no_private_details(monkeypatch):
    class Failed:
        def get(self):
            raise runtime.ReadOnlyRuntimeUnavailable('private SQL token')

    monkeypatch.setattr(runtime, '_recent_hours', Failed())
    with pytest.raises(HTTPException) as caught:
        runtime.recent_hours_view()
    assert caught.value.status_code == 503
    assert caught.value.detail['error_code'] == 'readonly_recent_hours_busy'
    assert 'private' not in str(caught.value.detail)
