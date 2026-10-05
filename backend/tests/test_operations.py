"""运行健康和备份保护；仅使用 conftest 的临时数据库。"""
import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health_and_request_correlation():
    response = client.get("/api/v1/health", headers={"X-Request-ID": "test-health-001"})
    assert response.status_code == 200
    assert response.json()["checks"]["database"]["status"] == "ok"
    assert response.headers["X-Request-ID"] == "test-health-001"
    assert response.headers["Cache-Control"] == "no-store"
    assert client.get("/api/v1/health", headers={"X-Request-ID": "unsafe/request"}).headers["X-Request-ID"] != "unsafe/request"


def test_request_log_has_correlation_without_query_or_credentials(caplog):
    import logging
    with caplog.at_level(logging.INFO, logger="dmcs.http"):
        client.get("/api/v1/health?secret=synthetic-private-value", headers={"X-Request-ID": "trace-001", "Authorization": "synthetic-private-header"})
    messages = " ".join(record.message for record in caplog.records if record.name == "dmcs.http")
    assert '"request_id": "trace-001"' in messages
    assert '"path": "/api/v1/health"' in messages
    assert "synthetic-private" not in messages and "secret=" not in messages


def test_database_outage_is_not_reported_healthy(monkeypatch):
    from app.routers import health

    class BrokenEngine:
        def connect(self):
            raise RuntimeError("sensitive fake connection string")

    monkeypatch.setattr(health, "engine", BrokenEngine())
    response = client.get("/api/v1/health/ready")
    assert response.status_code == 503
    assert response.json()["checks"]["database"]["error_code"] == "database_unavailable"
    assert "sensitive" not in response.text
    assert client.get("/api/v1/health/live").status_code == 200


def test_failed_backup_cancels_force_overwrite(monkeypatch):
    from app.services import migrate
    before = client.get("/api/v1/state").json()
    monkeypatch.setattr(migrate, "backup_database", lambda _: None)
    response = client.put("/api/v1/state?force=true", json={"coarseCoal": [], "floatCoal": [], "calcLogs": [], "ashTarget": 99})
    assert response.status_code == 503 and response.json()["backup_failed"]
    after = client.get("/api/v1/state").json()
    assert before == after
    assert client.put("/api/v1/state", json=before).json()["ok"]


def test_operational_frontend_regressions():
    subprocess.run(["node", str(Path(__file__).parents[1] / "scripts/verify_operations.js")], check=True, capture_output=True)
