"""History pagination across online and verified cold data; no production I/O."""
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base, get_db
from app.routers import telemetry
from app.telemetry.models import TelemetryReading


@pytest.fixture
def history_client(tmp_path, monkeypatch):
    engine = create_engine("sqlite:///" + str(tmp_path / "history.db"),
        connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    cold_rows = []
    cold_calls = []

    def isolated_db():
        with sessions() as db:
            yield db

    def selected_cold(source_id, point_key, before_id):
        return [row for row in cold_rows if row["source_id"] == source_id
            and row["point_key"] == point_key and (before_id is None or row["id"] < before_id)]

    def cold_history_ceiling(db, source_id, point_key, before_id=None):
        return max((row["id"] for row in selected_cold(source_id, point_key, before_id)), default=0)

    def cold_history(db, source_id, point_key, before_id=None, limit=100, floor_id=None):
        cold_calls.append((source_id, point_key, before_id, limit, floor_id))
        matching = [row for row in selected_cold(source_id, point_key, before_id)
            if floor_id is None or row["id"] > floor_id]
        return sorted(matching, key=lambda row: row["id"], reverse=True)[:limit]

    monkeypatch.setattr(telemetry, "cold_history", cold_history)
    monkeypatch.setattr(telemetry, "cold_history_ceiling", cold_history_ceiling)
    app = FastAPI()
    app.include_router(telemetry.router, prefix="/api/v1")
    app.dependency_overrides[get_db] = isolated_db

    def add_hot(reading_id, *, source_id="source", point_key="density", value=None):
        with sessions() as db:
            db.add(TelemetryReading(id=reading_id, source_id=source_id, point_key=point_key,
                batch_id=f"batch-{reading_id}", received_at=datetime.now(timezone.utc).isoformat(),
                sample={"value": reading_id if value is None else value}))
            db.commit()

    def add_cold(reading_id, *, source_id="source", point_key="density", value=None):
        cold_rows.append({"id": reading_id, "source_id": source_id, "point_key": point_key,
            "sample": {"value": reading_id if value is None else value}})

    with TestClient(app) as client:
        yield client, add_hot, add_cold, cold_calls
    engine.dispose()


def get_page(client, **params):
    return client.get("/api/v1/telemetry/history", params={
        "source_id": "source", "point_key": "density", **params})


def test_pagination_crosses_online_cold_boundary_without_skipping(history_client):
    client, hot, cold, calls = history_client
    for reading_id in (10, 9):
        hot(reading_id)
    for reading_id in (8, 7, 6):
        cold(reading_id)

    first = get_page(client, limit=3).json()
    assert [row["id"] for row in first["items"]] == [10, 9, 8]
    assert first["next_before_id"] == 8
    second = get_page(client, limit=3, before_id=first["next_before_id"]).json()
    assert [row["id"] for row in second["items"]] == [7, 6]
    assert second["next_before_id"] is None
    assert calls == [("source", "density", None, 4, None), ("source", "density", 8, 4, None)]


def test_overlap_is_deduplicated_and_online_copy_wins(history_client):
    client, hot, cold, _ = history_client
    hot(12)
    hot(10, value="online")
    for reading_id in (11, 10, 9, 8):
        cold(reading_id, value="archived")
    first = get_page(client, limit=3).json()
    assert first == {"items": [{"id": 12, "value": 12},
        {"id": 11, "value": "archived"}, {"id": 10, "value": "online"}],
        "next_before_id": 10}
    second = get_page(client, limit=3, before_id=10).json()
    assert [row["id"] for row in second["items"]] == [9, 8]
    assert second["next_before_id"] is None


def test_history_filters_source_point_and_before_id_in_both_tiers(history_client):
    client, hot, cold, _ = history_client
    hot(10)
    cold(9)
    hot(8, source_id="other")
    cold(7, source_id="other")
    hot(6, point_key="level")
    cold(5, point_key="level")
    hot(4)
    cold(3)
    page = get_page(client, limit=2, before_id=9).json()
    assert page == {"items": [{"id": 4, "value": 4}, {"id": 3, "value": 3}],
        "next_before_id": None}


@pytest.mark.parametrize("tier", ["online", "cold"])
def test_exact_full_page_has_no_next_cursor(history_client, tier):
    client, hot, cold, _ = history_client
    add = hot if tier == "online" else cold
    add(2)
    add(1)
    page = get_page(client, limit=2).json()
    assert [row["id"] for row in page["items"]] == [2, 1]
    assert page["next_before_id"] is None


def test_empty_history_and_parameter_limits(history_client):
    client, _, _, calls = history_client
    assert get_page(client).json() == {"items": [], "next_before_id": None}
    assert get_page(client, limit=500).status_code == 200
    assert calls[-1] == ("source", "density", None, 501, None)
    for params in ({"limit": 0}, {"limit": 501}, {"before_id": 0}):
        assert get_page(client, **params).status_code == 422


@pytest.mark.parametrize("exception,status,code", [
    ("ColdDetailExpired", 410, "cold_detail_expired"),
    ("ColdReadError", 503, "cold_history_unavailable"),
    ("ColdReadLimit", 503, "cold_history_unavailable"),
])
def test_cold_failure_is_explicit_and_does_not_expose_archive_paths(
        history_client, monkeypatch, exception, status, code):
    client, _, _, _ = history_client

    def fail(*args, **kwargs):
        raise getattr(telemetry, exception)("secret archive path or transport token")

    monkeypatch.setattr(telemetry, "cold_history", fail)
    response = get_page(client)
    assert response.status_code == status
    assert response.json()["detail"]["error_code"] == code
    assert "secret" not in response.text


@pytest.mark.parametrize("cold_id", [0, 7, 8])
def test_full_online_page_skips_cold_when_lookahead_covers_its_ceiling(history_client, cold_id):
    client, hot, cold, calls = history_client
    for reading_id in (10, 9, 8):
        hot(reading_id)
    if cold_id:
        cold(cold_id)
    cold(100, point_key="unrelated")
    page = get_page(client, limit=2).json()
    assert [row["id"] for row in page["items"]] == [10, 9]
    assert page["next_before_id"] == 9
    assert calls == []


def test_cold_rows_above_full_online_lookahead_are_merged(history_client):
    client, hot, cold, calls = history_client
    for reading_id in (12, 10, 8):
        hot(reading_id)
    for reading_id in (11, 9, 7):
        cold(reading_id)
    first = get_page(client, limit=2).json()
    assert [row["id"] for row in first["items"]] == [12, 11]
    assert first["next_before_id"] == 11
    assert calls == [("source", "density", None, 3, 8)]
    second = get_page(client, limit=2, before_id=11).json()
    assert [row["id"] for row in second["items"]] == [10, 9]
    assert second["next_before_id"] == 9


def test_unknown_legacy_ceiling_does_not_skip_cold(history_client, monkeypatch):
    client, hot, cold, calls = history_client
    for reading_id in (10, 9, 8):
        hot(reading_id)
    cold(11)
    monkeypatch.setattr(telemetry, "cold_history_ceiling", lambda *args, **kwargs: None)
    page = get_page(client, limit=2).json()
    assert [row["id"] for row in page["items"]] == [11, 10]
    assert calls == [("source", "density", None, 3, 8)]


@pytest.mark.parametrize("exception,reason", [
    ("ColdReadLimit", "cold_read_limit"),
    ("ColdDetailExpired", "cold_detail_expired"),
])
def test_online_data_survives_expiry_or_read_budget_with_explicit_partial_marker(
        history_client, monkeypatch, exception, reason):
    client, hot, _, _ = history_client
    hot(10)

    def fail(*args, **kwargs):
        raise getattr(telemetry, exception)("private path")

    monkeypatch.setattr(telemetry, "cold_history", fail)
    response = get_page(client, limit=2)
    assert response.status_code == 200
    assert response.json() == {"items": [{"id": 10, "value": 10}], "next_before_id": None,
        "cold_partial": True, "cold_partial_reason": reason}
    assert "private" not in response.text


def test_cold_integrity_error_is_not_hidden_by_online_data(history_client, monkeypatch):
    client, hot, _, _ = history_client
    hot(10)

    def fail(*args, **kwargs):
        raise telemetry.ColdReadError("checksum mismatch at private path")

    monkeypatch.setattr(telemetry, "cold_history", fail)
    response = get_page(client)
    assert response.status_code == 503
    assert response.json()["detail"]["error_code"] == "cold_history_unavailable"
    assert "private" not in response.text
