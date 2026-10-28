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
    ("coarse_slime_ash", "%", 23.25), ("float_clean_coal_ash", "%", 10.5),
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
    response = client.post(URL, json={"metric": "coarse_slime_ash", "value": 40, "observed_at": time})
    assert response.status_code == 201, response.text
    assert response.json()["observed_at"] == "2026-10-07T01:12:35.000+00:00"


@pytest.mark.parametrize("time", [None, "", "  "])
def test_blank_time_uses_server_time(setup, time):
    client, _ = setup
    record = client.post(URL, json={"metric": "raw_coal_slime_content", "value": 0, "observed_at": time}).json()
    assert record["observed_at"] == record["created_at"] and record["value"] == 0


@pytest.mark.parametrize("body", [
    {"metric": "unknown", "value": 8}, {"metric": "heavy_clean_coal_ash", "value": 100.01},
    {"metric": "raw_coal_slime_content", "value": -1}, {"metric": "coarse_slime_ash", "value": -0.1},
    {"metric": "coarse_slime_ash", "value": True}, {"metric": "coarse_slime_ash", "value": ""},
    {"metric": "coarse_slime_ash", "value": 100.01}, {"metric": "coarse_slime_amount", "value": 20},
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
    for metric in ["heavy_clean_coal_ash", "coarse_slime_ash", "heavy_clean_coal_ash"]:
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
        "heavy_clean_coal_ash", "raw_coal_slime_content", "coarse_slime_amount", "float_clean_coal_ash"}
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
                               "observed_at": current["observed_at"], "created_at": current["created_at"],
                               "entered_observed_at": current["observed_at"], "time_basis": "production-time-v1"}
    with sessions() as db:
        assert db.get(ManualMeasurement, point["sample"]["record_id"]).value == 8.5


def test_latest_tie_uses_last_entry_and_future_record_waits(setup):
    client, _ = setup
    for value in (8.5, 8.7):
        record = client.post(URL, json={"metric": "heavy_clean_coal_ash", "value": value,
                                       "observed_at": "2026-01-02T11:04:13+08:00"}).json()
    client.post(URL, json={"metric": "heavy_clean_coal_ash", "value": 9,
                          "observed_at": "2099-01-01T12:00:00+08:00"})
    client.post(URL, json={"metric": "raw_coal_slime_content", "value": 0,
                          "observed_at": "2026-01-01T12:00:00+08:00"})
    points = {point["key"]: point for point in client.get(URL + "/latest").json()["items"]}
    assert points["heavy_clean_coal_ash"]["sample"]["record_id"] == record["id"]
    assert points["heavy_clean_coal_ash"]["sample"]["value"] == 8.7
    assert points["raw_coal_slime_content"]["available"] and points["raw_coal_slime_content"]["sample"]["value"] == 0
    assert not points["coarse_slime_amount"]["available"]


def test_archive_only_catalog_persistence_retry_and_legacy_history(setup):
    client, sessions = setup
    metrics = {item['key']: item for item in client.get(URL+'/metrics').json()['items']}
    assert set(metrics) == {'heavy_clean_coal_ash', 'raw_coal_slime_content', 'coarse_slime_ash', 'float_clean_coal_ash'}
    assert metrics['coarse_slime_ash']['archive_only'] and metrics['coarse_slime_ash']['unit'] == '%'
    assert not metrics['float_clean_coal_ash']['archive_only']
    assert metrics['float_clean_coal_ash']['unit'] == '%' and metrics['float_clean_coal_ash']['name'] == '浮精灰分'
    body = dict(metric='coarse_slime_ash', value=8.25, observed_at='2026-01-01T12:00+08:00', client_id='archive-retry')
    first = client.post(URL, json=body)
    assert first.status_code == 201
    assert first.json()['name'] == '粗精煤泥灰分'
    assert client.post(URL, json=body).json() == first.json()
    assert client.get(URL, params={'metric':'coarse_slime_ash'}).json()['total'] == 1
    assert 'coarse_slime_ash' not in {p['key'] for p in client.get(URL+'/latest').json()['items']}
    # Retired measurements remain readable, even above the new assay's 100% limit.
    with sessions.begin() as db:
        db.add(ManualMeasurement(metric='coarse_slime_amount', name='粗精煤泥量', value=250.25,
            unit='t/h', observed_at='2026-01-01T04:00:00.000+00:00', created_at='2026-01-01T04:00:00.000+00:00', request_hash='legacy'))
    assert client.get(URL).json()['total'] == 2
    old = client.get(URL, params={'metric':'coarse_slime_amount'})
    assert old.status_code == 200 and old.json()['items'][0]['value'] == 250.25
    assert client.post(URL, json={'metric':'coarse_slime_amount','value':20}).status_code == 422


def test_manual_latest_does_not_require_kepserver(setup, monkeypatch):
    from app.routers import telemetry
    client, _ = setup
    def unavailable(*_):
        raise RuntimeError("KEP query unavailable")
    monkeypatch.setattr(telemetry, "latest_view", unavailable)
    client.post(URL, json={"metric": "heavy_clean_coal_ash", "value": 8.5})
    assert client.get(URL + "/latest").json()["items"][0]["sample"]["value"] == 8.5


def test_edit_changes_metric_and_value_in_database(setup):
    client, sessions = setup
    record = client.post(URL, json={"metric": "heavy_clean_coal_ash", "value": 8.5,
                                    "observed_at": "2026-10-01T12:00+08:00"}).json()
    edited = client.patch(f"{URL}/{record['id']}", json={"metric": "float_clean_coal_ash", "value": 10.5})
    assert edited.status_code == 200, edited.text
    body = edited.json()
    assert (body["metric"], body["value"], body["name"], body["unit"]) == ("float_clean_coal_ash", 10.5, "浮精灰分", "%")
    assert body["observed_at"] == record["observed_at"] and body["created_at"] == record["created_at"]
    with sessions() as db:
        stored = db.get(ManualMeasurement, record["id"])
        assert (stored.metric, stored.value, stored.name, stored.unit) == ("float_clean_coal_ash", 10.5, "浮精灰分", "%")
        assert stored.observed_at == record["observed_at"] and stored.created_at == record["created_at"]
        assert stored.request_hash  # the original POST idempotency token survives edits
    assert client.get(URL).json()["items"][0]["value"] == 10.5


def test_edit_value_only_keeps_metric_and_metric_only_renames(setup):
    client, _ = setup
    record = client.post(URL, json={"metric": "raw_coal_slime_content", "value": 16.5}).json()
    value_only = client.patch(f"{URL}/{record['id']}", json={"value": 20}).json()
    assert (value_only["metric"], value_only["value"], value_only["name"]) == ("raw_coal_slime_content", 20, "原煤煤泥含量")
    metric_only = client.patch(f"{URL}/{record['id']}", json={"metric": "coarse_slime_ash"}).json()
    assert (metric_only["metric"], metric_only["value"], metric_only["name"], metric_only["unit"]) == \
        ("coarse_slime_ash", 20, "粗精煤泥灰分", "%")


def test_edit_validation_and_missing_record(setup):
    client, sessions = setup
    record = client.post(URL, json={"metric": "heavy_clean_coal_ash", "value": 8.5}).json()
    for body in [{}, {"value": 100.01}, {"value": -1}, {"value": True}, {"metric": "unknown"},
                 {"metric": "coarse_slime_amount", "value": 5}, {"metric": "heavy_clean_coal_ash", "value": 8, "unit": "%"}]:
        assert client.patch(f"{URL}/{record['id']}", json=body).status_code == 422, body
    with sessions() as db:
        stored = db.get(ManualMeasurement, record["id"])
        assert (stored.metric, stored.value) == ("heavy_clean_coal_ash", 8.5)
    assert client.patch(f"{URL}/{record['id'] + 999}", json={"value": 9}).status_code == 404
    with sessions.begin() as db:  # a retired amount row cannot jump into a 0-100% metric
        db.add(ManualMeasurement(metric='coarse_slime_amount', name='粗精煤泥量', value=250.25, unit='t/h',
            observed_at='2026-01-01T04:00:00.000+00:00', created_at='2026-01-01T04:00:00.000+00:00', request_hash='legacy'))
    legacy = client.get(URL, params={"metric": "coarse_slime_amount"}).json()["items"][0]
    assert client.patch(f"{URL}/{legacy['id']}", json={"value": 300}).json()["value"] == 300
    assert client.patch(f"{URL}/{legacy['id']}", json={"metric": "heavy_clean_coal_ash"}).status_code == 422


def test_edit_then_stale_client_retry_conflicts(setup):
    client, _ = setup
    body = {"metric": "heavy_clean_coal_ash", "value": 8.5, "client_id": "edit-retry"}
    record = client.post(URL, json=body).json()
    client.patch(f"{URL}/{record['id']}", json={"value": 9})
    # The original POST keeps its idempotent reuse: the row exists (now edited).
    retry = client.post(URL, json=body)
    assert retry.status_code == 200 and retry.json()["value"] == 9
    assert client.post(URL, json={**body, "value": 9.5}).status_code == 409


def test_delete_removes_the_stored_row(setup):
    client, sessions = setup
    record = client.post(URL, json={"metric": "heavy_clean_coal_ash", "value": 8.5}).json()
    removed = client.delete(f"{URL}/{record['id']}")
    assert removed.status_code == 200 and removed.json() == {"ok": True, "id": record["id"]}
    with sessions() as db:
        assert db.get(ManualMeasurement, record["id"]) is None
    assert client.get(URL).json()["total"] == 0
    assert client.delete(f"{URL}/{record['id']}").status_code == 404


def test_edit_and_delete_require_the_write_token(setup):
    client, _ = setup
    body = {"metric": "heavy_clean_coal_ash", "value": 8}
    with TestClient(app, client=("10.92.25.107", 12345)) as remote:
        record = remote.post(URL, json=body, headers={"X-DMCS-Token": "manual-test-token"}).json()
        assert remote.patch(f"{URL}/{record['id']}", json={"value": 9}).status_code == 401
        assert remote.patch(f"{URL}/{record['id']}", json={"value": 9},
                            headers={"X-DMCS-Token": "wrong"}).status_code == 401
        edited = remote.patch(f"{URL}/{record['id']}", json={"value": 9},
                              headers={"X-DMCS-Token": "manual-test-token"})
        assert edited.status_code == 200 and edited.json()["value"] == 9
        assert remote.delete(f"{URL}/{record['id']}").status_code == 401
        assert remote.delete(f"{URL}/{record['id']}", headers={"X-DMCS-Token": "manual-test-token"}).status_code == 200
    assert client.get(URL).json()["total"] == 0


def test_observed_time_window_filters_records(setup):
    client, _ = setup
    stamps = {"old": "2026-01-01T12:00+08:00", "edge": "2026-09-01T00:00+08:00",
              "recent": "2026-10-15T12:00+08:00", "future": "2099-01-01T12:00+08:00"}
    for label, observed in stamps.items():
        client.post(URL, json={"metric": "heavy_clean_coal_ash", "value": 8, "observed_at": observed})
    # Inclusive window in UTC: 2026-09-01 00:00 Beijing == 2026-08-31T16:00Z.
    window = client.get(URL, params={"start": "2026-08-31T16:00:00Z", "end": "2026-10-20T12:00+08:00"}).json()
    assert [item["id"] for item in window["items"]] == [3, 2]  # entry order: recent, then edge
    assert window["total"] == 2
    future_only = client.get(URL, params={"start": "2026-12-01T00:00"}).json()
    assert future_only["total"] == 1 and future_only["items"][0]["id"] == 4
    assert client.get(URL, params={"start": "2099-06-01T00:00"}).json()["total"] == 0
    assert client.get(URL, params={"start": "2026-01-01T00:00", "end": "2026-02-01T00:00"}).json()["total"] == 1
    assert client.get(URL).json()["total"] == 4  # without bounds the behaviour is unchanged
    for bad in ("2026-02-30T00:00", "not-a-time", "2026-12-01"):  # date-only is not a valid bound
        assert client.get(URL, params={"start": bad}).status_code == 422
        assert client.get(URL, params={"end": bad}).status_code == 422
