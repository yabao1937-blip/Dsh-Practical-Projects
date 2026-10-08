"""Manual form persistence, time semantics, retries, and mirror isolation."""
from datetime import datetime, timezone
import math

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import auth
from app.database import Base, get_db
from app.main import app
from app.models import ManualMeasurement
from app.schemas import ManualMeasurementIn
from app.services import migrate

URL = "/api/v1/manual-measurements"


@pytest.fixture
def setup(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{(tmp_path / 'manual.db').as_posix()}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, autoflush=False)

    def database():
        with sessions() as db:
            yield db

    app.dependency_overrides[get_db] = database
    monkeypatch.setattr(migrate, "SessionLocal", sessions)
    monkeypatch.setattr(migrate, "backup_database", lambda *_: "isolated-test-backup")
    monkeypatch.setattr(auth, "WRITE_TOKEN", "manual-test-token")
    with TestClient(app) as client:
        yield client, sessions
    app.dependency_overrides.pop(get_db, None)
    engine.dispose()


@pytest.mark.parametrize("metric,unit,value", [
    ("heavy_clean_coal_ash", "%", 8.52), ("raw_coal_slime_content", "%", 16.5),
    ("coarse_slime_amount", "t/h", 250.25),
])
def test_default_time_and_persistent_history(setup, metric, unit, value):
    client, sessions = setup
    before = datetime.now(timezone.utc)
    response = client.post(URL, json={"metric": metric, "value": value})
    assert response.status_code == 201, response.text
    record = response.json()
    assert record["unit"] == unit and record["value"] == value
    assert record["created_at"] == record["observed_at"]
    observed = datetime.fromisoformat(record["observed_at"])
    assert abs((observed - before).total_seconds()) < 5
    with sessions() as db:
        stored = db.get(ManualMeasurement, record["id"])
        assert stored.value == value and stored.name == record["name"]
    assert client.get(URL).json()["items"][0]["id"] == record["id"]


@pytest.mark.parametrize("time", ["2026-10-07T09:12:35", "2026-10-07 09:12:35", "2026-10-07T09:12:35+08:00"])
def test_explicit_beijing_time(setup, time):
    client, _ = setup
    response = client.post(URL, json={"metric": "coarse_slime_amount", "value": 40, "observed_at": time})
    assert response.status_code == 201, response.text
    assert response.json()["observed_at"] == "2026-10-07T01:12:35.000+00:00"


@pytest.mark.parametrize("time", [None, "", "  "])
def test_blank_time_uses_server_time(setup, time):
    client, _ = setup
    record = client.post(URL, json={"metric": "raw_coal_slime_content", "value": 0, "observed_at": time}).json()
    assert record["observed_at"] == record["created_at"] and record["value"] == 0


@pytest.mark.parametrize("body", [
    {"metric": "unknown", "value": 8}, {"metric": "heavy_clean_coal_ash", "value": 100.01},
    {"metric": "raw_coal_slime_content", "value": -1}, {"metric": "coarse_slime_amount", "value": -0.1},
    {"metric": "coarse_slime_amount", "value": True}, {"metric": "coarse_slime_amount", "value": ""},
    {"metric": "heavy_clean_coal_ash", "value": "NaN"}, {"metric": "heavy_clean_coal_ash", "value": "Infinity"},
    {"metric": "heavy_clean_coal_ash"}, {"metric": "heavy_clean_coal_ash", "value": 8, "observed_at": "2026-02-30T09:00"},
    {"metric": "heavy_clean_coal_ash", "value": 8, "observed_at": "2026-10-07"},
    {"metric": "heavy_clean_coal_ash", "value": 8, "unit": "t/h"},
])
def test_invalid_inputs_do_not_write(setup, body):
    client, sessions = setup
    assert client.post(URL, json=body).status_code == 422
    with sessions() as db:
        assert db.query(ManualMeasurement).count() == 0


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf, True])
def test_schema_rejects_nonfinite_or_boolean(value):
    with pytest.raises(ValidationError):
        ManualMeasurementIn(metric="heavy_clean_coal_ash", value=value)


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
def test_nonstandard_json_numbers_return_validation_errors_without_writing(setup, value):
    client, _ = setup
    response = client.post(URL, content='{"metric":"heavy_clean_coal_ash","value":' + value + '}',
                           headers={"Content-Type": "application/json"})
    assert response.status_code == 422, response.text
    assert client.get(URL).json()["total"] == 0


def test_retry_reuses_original_default_time_and_conflicts_on_changes(setup):
    client, _ = setup
    body = {"metric": "heavy_clean_coal_ash", "value": 8.5, "client_id": "retry-test"}
    first = client.post(URL, json=body)
    second = client.post(URL, json=body)
    assert first.status_code == 201 and second.status_code == 200
    assert first.json() == second.json()
    assert client.post(URL, json={**body, "value": 9}).status_code == 409
    assert client.post(URL, json={**body, "observed_at": "2026-10-01T12:00+08:00"}).status_code == 409
    assert client.get(URL).json()["total"] == 1


def test_filter_pagination_and_backfill_entry_order(setup):
    client, _ = setup
    for metric in ["heavy_clean_coal_ash", "coarse_slime_amount", "heavy_clean_coal_ash"]:
        client.post(URL, json={"metric": metric, "value": 8, "observed_at": "2026-01-01T12:00"})
    data = client.get(URL, params={"metric": "heavy_clean_coal_ash", "limit": 1, "offset": 1}).json()
    assert data["total"] == 2 and len(data["items"]) == 1
    assert data["items"][0]["id"] == 1
    assert client.get(URL, params={"metric": "unknown"}).status_code == 422


def test_remote_write_auth_and_shared_reads(setup):
    client, _ = setup
    body = {"metric": "heavy_clean_coal_ash", "value": 8}
    with TestClient(app, client=("10.92.25.107", 12345)) as remote:
        assert remote.post(URL, json=body).status_code == 401
        assert remote.post(URL, json=body, headers={"X-DMCS-Token": "wrong"}).status_code == 401
        assert remote.post(URL, json=body, headers={"X-DMCS-Token": "manual-test-token"}).status_code == 201
        assert remote.get(URL).json()["total"] == client.get(URL).json()["total"] == 1


def test_state_mirrors_do_not_remove_manual_measurements(setup):
    client, _ = setup
    client.post(URL, json={"metric": "heavy_clean_coal_ash", "value": 8.5})
    snapshot = client.get("/api/v1/state").json()
    snapshot["manualEntries"] = []
    assert client.put("/api/v1/state", json=snapshot).json()["ok"]
    assert client.put("/api/v1/state?force=true", json=snapshot).json()["ok"]
    assert client.get(URL).json()["total"] == 1


def test_latest_points_exist_without_records(setup):
    client, _ = setup
    result = client.get(URL + "/latest")
    assert result.status_code == 200
    points = result.json()["items"]
    assert {point["key"] for point in points} == {
        "heavy_clean_coal_ash", "raw_coal_slime_content", "coarse_slime_amount"}
    assert all(point["source_id"] == "manual-measurements" and not point["available"]
               and point["sample"]["value"] is None for point in points)


def test_latest_uses_measurement_time_instead_of_entry_order(setup):
    client, sessions = setup
    current = client.post(URL, json={"metric": "heavy_clean_coal_ash", "value": 8.5,
                                    "observed_at": "2026-01-02T11:04:13+08:00"}).json()
    older = client.post(URL, json={"metric": "heavy_clean_coal_ash", "value": 9.5,
                                  "observed_at": "2026-01-01T12:00:00+08:00"}).json()
    # History stays in entry order, while the independent DB query chooses 8.5.
    assert client.get(URL).json()["items"][0]["id"] == older["id"]
    point = client.get(URL + "/latest").json()["items"][0]
    assert point["available"] and point["unit"] == "%"
    assert point["sample"] == {"record_id": current["id"], "value": 8.5,
                               "observed_at": current["observed_at"], "created_at": current["created_at"]}
    with sessions() as db:
        assert db.get(ManualMeasurement, point["sample"]["record_id"]).value == 8.5


def test_latest_tie_uses_last_entry_and_future_record_waits(setup):
    client, _ = setup
    for value in (8.5, 8.7):
        record = client.post(URL, json={"metric": "heavy_clean_coal_ash", "value": value,
                                       "observed_at": "2026-01-02T11:04:13+08:00"}).json()
    client.post(URL, json={"metric": "heavy_clean_coal_ash", "value": 9,
                          "observed_at": "2099-01-01T12:00:00+08:00"})
    client.post(URL, json={"metric": "coarse_slime_amount", "value": 0,
                          "observed_at": "2026-01-01T12:00:00+08:00"})
    points = {point["key"]: point for point in client.get(URL + "/latest").json()["items"]}
    assert points["heavy_clean_coal_ash"]["sample"]["record_id"] == record["id"]
    assert points["heavy_clean_coal_ash"]["sample"]["value"] == 8.7
    assert points["coarse_slime_amount"]["available"] and points["coarse_slime_amount"]["sample"]["value"] == 0
    assert not points["raw_coal_slime_content"]["available"]


def test_manual_latest_does_not_require_kepserver(setup, monkeypatch):
    from app.routers import telemetry
    client, _ = setup
    def unavailable(*_):
        raise RuntimeError("KEP query unavailable")
    monkeypatch.setattr(telemetry, "latest_view", unavailable)
    client.post(URL, json={"metric": "heavy_clean_coal_ash", "value": 8.5})
    assert client.get(URL + "/latest").json()["items"][0]["sample"]["value"] == 8.5
