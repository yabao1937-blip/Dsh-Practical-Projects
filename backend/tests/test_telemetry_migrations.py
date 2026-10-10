"""新建库与兼容建表后的迁移；只在 tmp_path 数据库上执行子进程。"""
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).parents[1]


def run(env, *arguments):
    return subprocess.run([sys.executable, *arguments], cwd=BACKEND, env=env, capture_output=True, text=True, encoding="utf-8", check=True)


@pytest.mark.parametrize("auto_created", [False, True])
def test_migration_preserves_existing_business_and_telemetry(tmp_path, auto_created):
    path = tmp_path / "migration.db"
    env = dict(os.environ, DMCS_DATABASE_URL=f"sqlite:///{path.as_posix()}", DMCS_WRITE_AUTH="off")
    run(env, "-m", "alembic", "upgrade", "b7e2c4d9a001")
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO settings (key, value, value_type, updated_at) VALUES ('migration-test', '8.5', 'float', '2026-10-05 00:00:00')")
    if auto_created:
        run(env, "-c", "from app.main import app; from app.database import engine; from sqlalchemy import text; conn=engine.connect(); conn.execute(text(\"INSERT INTO telemetry_sources (source_id,name,state,retry_seconds) VALUES ('test','test','stopped',0)\")); conn.commit(); conn.close()")
    run(env, "-m", "alembic", "upgrade", "head")
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "c3a610100001"
        tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {'gpt_decision_evidence_archives','gpt_density_observations','gpt_density_actions','gpt_density_action_entries'} <= tables
        assert db.execute("SELECT count(*) FROM gpt_coarse_model_revisions").fetchone()[0] == 0
        assert [r[1] for r in db.execute("PRAGMA table_info(hourly_brief_feedback)")] == ["id", "hour", "content", "created_at", "client_id"]
        assert [r[1] for r in db.execute("PRAGMA table_info(decision_cycle_samples)")] == ["slot", "sampled_at", "context_key", "fingerprint", "snapshot"]
        assert db.execute("SELECT count(*) FROM decision_cycle_hours").fetchone()[0] == 0
        assert [r[1] for r in db.execute("PRAGMA table_info(online_instruments)")] == ["设备", "标签名", "说明", "ItemID", "值", "时间"]
        assert db.execute("SELECT value FROM settings WHERE key='migration-test'").fetchone()[0] == 8.5
        assert db.execute("SELECT count(*) FROM telemetry_sources").fetchone()[0] == int(auto_created)
    run(env, "-m", "alembic", "downgrade", "b7e2c4d9a001")
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT value FROM settings WHERE key='migration-test'").fetchone()[0] == 8.5
        assert not db.execute("SELECT name FROM sqlite_master WHERE name LIKE 'telemetry_%'").fetchall()
        assert not db.execute("SELECT name FROM sqlite_master WHERE name='online_instruments'").fetchall()
        assert not db.execute("SELECT name FROM sqlite_master WHERE name='hourly_brief_feedback'").fetchall()


def test_migration_refuses_incompatible_auto_created_table(tmp_path):
    path = tmp_path / "incompatible.db"
    env = dict(os.environ, DMCS_DATABASE_URL=f"sqlite:///{path.as_posix()}", DMCS_WRITE_AUTH="off")
    run(env, "-m", "alembic", "upgrade", "b7e2c4d9a001")
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE telemetry_sources (source_id TEXT PRIMARY KEY)")
        db.execute("INSERT INTO telemetry_sources VALUES ('preserved')")
    result = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=BACKEND, env=env, capture_output=True, text=True, encoding="utf-8")
    assert result.returncode != 0 and "Existing telemetry schema differs" in result.stderr
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "b7e2c4d9a001"
        assert db.execute("SELECT source_id FROM telemetry_sources").fetchone()[0] == "preserved"
