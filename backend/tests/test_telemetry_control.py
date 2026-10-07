"""后台启动与接口保护：临时配置/数据库，所有 Popen 都是替身，不连接设备。"""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json
import os
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import auth
from app.database import Base
from app.main import app
from app.routers import telemetry
from app.telemetry import control
from app.telemetry.config import load_config
from app.telemetry.domain import iso_time, utc_now
from app.telemetry.models import TelemetrySource
from app.telemetry.repository import TelemetryRepository


@pytest.fixture
def setup(tmp_path, monkeypatch):
    config = tmp_path / "collector.json"
    config.write_text(json.dumps({"enabled": True, "endpoint": "opc.tcp://127.0.0.1:49320",
        "security": "None", "allow_insecure": True,
        "points": [{"key": "density", "name": "密度", "node_id": "ns=2;s=Density"}]}), encoding="utf-8")
    engine = create_engine("sqlite:///" + str(tmp_path / "test.db"), connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    calls = []
    process = SimpleNamespace(pid=123456, poll=lambda: None)

    def launch(*args, **kwargs):
        calls.append((args, kwargs))
        return process

    monkeypatch.setattr(control.subprocess, "Popen", launch)
    monkeypatch.setattr(control, "_process_identity", lambda pid: "created-123" if pid == process.pid else None)
    def start():
        return control.ensure_collector_started(config, tmp_path / "runtime", session_factory=sessions, engine=engine)
    yield SimpleNamespace(config=config, engine=engine, sessions=sessions, calls=calls,
        process=process, start=start, runtime=tmp_path / "runtime")
    engine.dispose()


def test_concurrent_requests_start_only_one_detached_process(setup):
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: setup.start(), range(2)))
    assert len(setup.calls) == 1
    assert {r["action"] for r in results} == {"started", "already_starting"}
    args, kwargs = setup.calls[0]
    assert args[0][-2:] == ["--config", str(setup.config.resolve())]
    assert kwargs["stdin"] == control.subprocess.DEVNULL and kwargs["close_fds"]
    assert kwargs["env"]["DMCS_DATABASE_URL"] == setup.engine.url.render_as_string(hide_password=False)
    if os.name == "nt":
        assert kwargs["creationflags"] & control.subprocess.DETACHED_PROCESS
        assert kwargs["creationflags"] & control.subprocess.CREATE_NO_WINDOW
    else:
        assert kwargs["start_new_session"]
    with setup.sessions() as db:
        assert db.get(TelemetrySource, "kepserver-main") is None, "启动不等于采集已连接"


@pytest.mark.parametrize("state", ["connected", "connecting", "retrying"])
def test_existing_lease_is_reused_without_spawn_or_owner_change(setup, state):
    cfg = load_config(setup.config)
    repo = TelemetryRepository(setup.sessions)
    repo.acquire(cfg, "existing-owner")
    repo.state(cfg, "existing-owner", state)
    result = setup.start()
    assert result["action"] == "already_running" and result["state"] == state
    assert not setup.calls
    with setup.sessions() as db:
        assert db.get(TelemetrySource, cfg.source_id).owner == "existing-owner"


def test_expired_lease_can_start_without_claiming_connected(setup):
    cfg = load_config(setup.config)
    repo = TelemetryRepository(setup.sessions)
    repo.acquire(cfg, "old-owner")
    with setup.sessions() as db:
        db.get(TelemetrySource, cfg.source_id).lease_until = iso_time(utc_now() - timedelta(seconds=1))
        db.commit()
    result = setup.start()
    assert result["action"] == "started" and result["state"] == "starting"
    assert len(setup.calls) == 1


def test_pid_reuse_does_not_block_new_start(setup, monkeypatch):
    setup.start()
    monkeypatch.setattr(control, "_process_identity", lambda pid: "different-process")
    assert setup.start()["action"] == "started"
    assert len(setup.calls) == 2


@pytest.mark.parametrize("failure", ["missing", "invalid", "disabled", "popen", "early_exit"])
def test_failure_does_not_claim_connection_and_can_retry(setup, monkeypatch, failure):
    if failure == "missing":
        setup.config.unlink()
    elif failure == "invalid":
        setup.config.write_text("{}", encoding="utf-8")
    elif failure == "disabled":
        config = json.loads(setup.config.read_text(encoding="utf-8"))
        config["enabled"] = False
        setup.config.write_text(json.dumps(config), encoding="utf-8")
    elif failure == "popen":
        def fail(*args, **kwargs):
            raise OSError("synthetic-private-secret")
        monkeypatch.setattr(control.subprocess, "Popen", fail)
    else:
        setup.process.poll = lambda: 1
    with pytest.raises(control.CollectorStartError) as error:
        setup.start()
    assert "synthetic-private-secret" not in str(error.value)
    with setup.sessions() as db:
        assert db.get(TelemetrySource, "kepserver-main") is None


def test_api_requires_write_permission_before_launch(setup, monkeypatch):
    monkeypatch.setattr(auth, "WRITE_TOKEN", "test-control-token")
    monkeypatch.setattr(telemetry, "ensure_collector_started", setup.start)
    remote = TestClient(app, client=("10.0.0.5", 54321))
    assert remote.post("/api/v1/telemetry/reconnect").status_code == 401
    assert remote.post("/api/v1/telemetry/reconnect", headers={"X-DMCS-Token": "wrong"}).status_code == 401
    assert not setup.calls
    response = remote.post("/api/v1/telemetry/reconnect", headers={"X-DMCS-Token": "test-control-token"},
        json={"config": "synthetic-private-secret", "endpoint": "opc.tcp://unapproved:1"})
    assert response.status_code == 202 and response.json()["state"] == "starting"
    assert "synthetic-private-secret" not in response.text
    assert len(setup.calls) == 1
    assert setup.calls[0][0][0][-1] == str(setup.config.resolve())


def test_api_configuration_failure_is_visible_and_sanitized(setup, monkeypatch):
    setup.config.unlink()
    monkeypatch.setattr(telemetry, "ensure_collector_started", setup.start)
    response = TestClient(app).post("/api/v1/telemetry/reconnect")
    assert response.status_code == 503
    assert response.json()["detail"]["error_code"] == "configuration_invalid"
    assert str(setup.config) not in response.text and not setup.calls
