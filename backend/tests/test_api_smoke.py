"""P2 核心数据 API 冒烟测试"""
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_settings_roundtrip():
    r = client.put("/api/v1/settings/ash_target", json={"value": 8.50, "value_type": "number"})
    assert r.status_code == 200
    assert client.get("/api/v1/settings/ash_target").json()["value"] == 8.50






def test_heavy_sample():
    assert client.post("/api/v1/samples/heavy-ash",
                       json={"ts": "2026-06-16 09:31:00", "rho": 1.45, "ash_content": 8.5}).status_code == 200
