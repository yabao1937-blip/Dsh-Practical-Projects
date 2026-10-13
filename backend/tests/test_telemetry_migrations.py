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
        assert db.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "c3a610130001"
        tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {'gpt_decision_evidence_archives','gpt_density_observations','gpt_density_actions','gpt_density_action_entries'} <= tables
        assert 'decision_telemetry_pins' in tables
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


@pytest.mark.parametrize('startup', [False, True])
def test_manual_assay_upgrade_preserves_old_rows_and_constraints(tmp_path, startup):
    path = tmp_path/'manual-upgrade.db'
    env = dict(os.environ, DMCS_DATABASE_URL=f'sqlite:///{path.as_posix()}', DMCS_WRITE_AUTH='off')
    run(env, '-m', 'alembic', 'upgrade', 'c3a610120001')
    old = (42,'coarse_slime_amount','粗精煤泥量',250.25,'t/h','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z','legacy-retry','original-hash')
    with sqlite3.connect(path) as db:
        db.execute('INSERT INTO manual_measurements VALUES (?,?,?,?,?,?,?,?,?)', old)
        db.execute('CREATE INDEX extra_manual_value ON manual_measurements(value)')
    if startup:
        run(env, '-c', 'from app.main import app')
        run(env, '-c', 'from app.main import app')  # Idempotent startup before Alembic catches up.
    run(env, '-m', 'alembic', 'upgrade', 'head')
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT * FROM manual_measurements').fetchall() == [old]
        indexes = {row[1] for row in db.execute('PRAGMA index_list(manual_measurements)')}
        assert {'extra_manual_value','ix_manual_measurement_metric_id'} <= indexes
        new = (43,'coarse_slime_ash','粗精煤泥灰分',8.5,'%',old[5],old[6],'new-retry','new-hash')
        db.execute('INSERT INTO manual_measurements VALUES (?,?,?,?,?,?,?,?,?)', new)
        with pytest.raises(sqlite3.IntegrityError):
            db.execute('INSERT INTO manual_measurements VALUES (?,?,?,?,?,?,?,?,?)', (44,*new[1:7],new[7],new[8]))
        with pytest.raises(sqlite3.IntegrityError):
            db.execute('UPDATE manual_measurements SET value=101 WHERE id=43')
        assert db.execute('SELECT * FROM manual_measurements ORDER BY id').fetchall() == [old,new]
    # A downgrade must refuse to discard the newly archived assays.
    result = subprocess.run([sys.executable,'-X','utf8','-m','alembic','downgrade','c3a610120001'], cwd=BACKEND,
                            env=env, capture_output=True, text=True, encoding='utf-8')
    assert result.returncode != 0 and '已有粗精煤泥灰分记录' in result.stderr
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT * FROM manual_measurements ORDER BY id').fetchall() == [old,new]
        assert db.execute('SELECT version_num FROM alembic_version').fetchone()[0] == 'c3a610130001'


def test_manual_assay_startup_failure_rolls_back_table_replacement(tmp_path):
    from sqlalchemy import create_engine, event
    from app.schema_upgrade import upgrade_schema
    path = tmp_path/'manual-rollback.db'
    env = dict(os.environ, DMCS_DATABASE_URL=f'sqlite:///{path.as_posix()}', DMCS_WRITE_AUTH='off')
    run(env, '-m', 'alembic', 'upgrade', 'c3a610120001')
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO manual_measurements VALUES (7,'coarse_slime_amount','old',250,'t/h','old-time','old-time','old-id','old-hash')")
        before = db.execute('SELECT * FROM manual_measurements').fetchall()
        ddl = db.execute("SELECT sql FROM sqlite_master WHERE name='manual_measurements'").fetchone()[0]
    engine = create_engine(f'sqlite:///{path.as_posix()}')
    @event.listens_for(engine, 'before_cursor_execute')
    def fail_after_old_table_drop(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith('ALTER TABLE manual_measurements_upgrade'):
            raise RuntimeError('injected rename failure')
    try:
        with pytest.raises(RuntimeError, match='injected rename failure'):
            upgrade_schema(engine)
    finally:
        engine.dispose()
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT * FROM manual_measurements').fetchall() == before
        assert db.execute("SELECT sql FROM sqlite_master WHERE name='manual_measurements'").fetchone()[0] == ddl
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='manual_measurements_upgrade'").fetchone()
