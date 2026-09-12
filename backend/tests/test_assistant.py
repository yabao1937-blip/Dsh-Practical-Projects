# -*- coding: utf-8 -*-
"""AI 解读助手(只读)测试:快照构建 / 状态接口 / 未配置拒绝 / 只读性(无写表)。"""
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.routers.assistant import MAX_QUESTION, build_snapshot

client = TestClient(app)


def test_snapshot_contains_expected_fields():
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        snap = build_snapshot(db)
    finally:
        db.close()
    for key in ("server_time", "record_counts", "density_current", "heavy_ash",
                "total_ash_actual", "guidance", "coarse_model", "k_gain", "decision_log_tail"):
        assert key in snap, f"快照缺少 {key}"
    # 种子数据下的具体值(回归锚点)
    assert snap["record_counts"]["coarse"] == 113
    assert snap["guidance"]["scheme"] in ("total", "heavy")
    assert snap["k_gain"]["source"] in ("per_system", "pooled", "none")


def test_status_endpoint_shape():
    r = client.get("/api/v1/assistant/status")
    assert r.status_code == 200
    j = r.json()
    assert "configured" in j and isinstance(j["configured"], bool)


def test_ask_unconfigured_returns_503(monkeypatch):
    for k in ("ASSISTANT_API_KEY", "ZAI_API_KEY", "DEEPSEEK_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    r = client.post("/api/v1/assistant/ask", json={"question": "什么是Q²?", "history": []})
    assert r.status_code == 503
    assert "ZAI_API_KEY" in r.json()["detail"]


def test_ask_validates_input():
    r = client.post("/api/v1/assistant/ask", json={"question": "x" * (MAX_QUESTION + 1)})
    assert r.status_code == 422


def test_ask_is_readonly():
    """只读性回归:一次(被拒绝的)ask 前后,三表计数不变。"""
    from sqlalchemy import text as sql
    from app.database import SessionLocal

    def counts():
        db = SessionLocal()
        try:
            return tuple(db.execute(sql(
                "SELECT (SELECT COUNT(*) FROM coal_records), (SELECT COUNT(*) FROM calc_logs), "
                "(SELECT COUNT(*) FROM settings), (SELECT COUNT(*) FROM auto_state)")).fetchone())
        finally:
            db.close()

    before = counts()
    client.post("/api/v1/assistant/ask", json={"question": "解释一下"})
    assert counts() == before
