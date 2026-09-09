"""P5 后端侧：静态托管 + /state 整库快照"""
import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)
SEED = json.loads((Path(__file__).resolve().parent.parent / "data" / "seed_store.json").read_text(encoding="utf-8"))


def test_static_serves_index():
    r = client.get("/")
    assert r.status_code == 200
    assert "重介密控系统" in r.text


def test_state_roundtrip():
    r = client.put("/api/v1/state", json=SEED)
    assert r.status_code == 200
    got = client.get("/api/v1/state").json()
    assert len(got["coarseCoal"]) == 113
    assert len(got["floatCoal"]) == 5
    assert len(got["calcLogs"]) == 124


def test_state_feeds_dashboard():
    client.put("/api/v1/state", json=SEED)
    d = client.get("/api/v1/overview/dashboard").json()
    assert abs(d["totalAsh"] - 8.8956) < 1e-6
    assert abs(d["coarseAsh"] - 13.67) < 1e-6
