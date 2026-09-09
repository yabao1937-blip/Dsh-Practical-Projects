"""P2 核心数据 API 冒烟测试"""
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_settings_roundtrip():
    r = client.put("/api/v1/settings/ash_target", json={"value": 8.50, "value_type": "number"})
    assert r.status_code == 200
    assert client.get("/api/v1/settings/ash_target").json()["value"] == 8.50


def test_inputs_roundtrip():
    client.put("/api/v1/inputs/instrumentInputs", json={"value": {"density": {"manual": 1.45}}})
    got = client.get("/api/v1/inputs").json()
    assert got["instrumentInputs"]["density"]["manual"] == 1.45


def test_records_upsert_idempotent():
    body = {"category": "coarse", "ts": "2026-06-16 09:31:00", "system": "合并",
            "ash_content": 13.86, "coal_amount": 927.0, "sysA": 1}
    r1 = client.post("/api/v1/records", json=body)
    rid = r1.json()["id"]
    body["ash_content"] = 14.0
    r2 = client.post("/api/v1/records", json=body)
    assert r2.json()["id"] == rid          # 同键 upsert 不新增
    assert r2.json()["ash_content"] == 14.0
    assert client.get("/api/v1/records", params={"category": "coarse"}).json()["total"] >= 1


def test_samples_and_manual_entries():
    assert client.post("/api/v1/samples/heavy-ash",
                       json={"ts": "2026-06-16 09:31:00", "rho": 1.45, "ash_content": 8.5}).status_code == 200
    assert client.post("/api/v1/manual-entries",
                       json={"ts": "2026-06-16 09:31:00", "category": "heavy_ash_sample",
                             "values": {"value": 8.5}}).status_code == 200
    assert len(client.get("/api/v1/manual-entries").json()) >= 1
