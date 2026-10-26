"""SQLite 原位升级：保留历史行和索引，事务失败则完整回滚。"""
import re

BASE_METRICS = ['heavy_clean_coal_ash', 'raw_coal_slime_content', 'coarse_slime_amount']
# Every catalog state this code generation may find in the wild, oldest first.
KNOWN_METRIC_SETS = [
    BASE_METRICS,
    BASE_METRICS + ['coarse_slime_ash'],
    BASE_METRICS + ['coarse_slime_ash', 'float_clean_coal_ash'],
]
METRIC_LABELS = {'coarse_slime_ash': '粗精煤泥灰分', 'float_clean_coal_ash': '浮精灰分'}


def update_manual_metric_constraint(conn, include_ash=True, include_float_ash=False):
    """Expand the SQLite catalog without losing old readings or their identities.

    Called inside the startup/Alembic transaction. A downgrade never drops assays.
    """
    ddl = conn.exec_driver_sql("SELECT sql FROM sqlite_master WHERE type='table' AND name='manual_measurements'").scalar()
    if not ddl:
        return
    match = re.search(r'CONSTRAINT ck_manual_measurement_metric CHECK\s*\(metric IN \(([^)]*)\)\)', ddl)
    if not match:
        raise RuntimeError('无法识别手工补录指标约束，未修改数据库')
    allowed = re.findall(r"'([^']+)'", match[1])
    if sorted(allowed) not in [sorted(s) for s in KNOWN_METRIC_SETS]:
        raise RuntimeError('手工补录指标约束与预期不符，未修改数据库')
    wanted = BASE_METRICS + (['coarse_slime_ash'] if include_ash else []) \
        + (['float_clean_coal_ash'] if include_float_ash else [])
    if set(wanted) == set(allowed):
        return
    for metric in set(allowed) - set(wanted):
        if conn.exec_driver_sql(f"SELECT 1 FROM manual_measurements WHERE metric='{metric}' LIMIT 1").first():
            raise RuntimeError(f'已有{METRIC_LABELS.get(metric, metric)}记录，不能降级删除该指标')
    new_ddl = ddl[:match.start(1)] + ', '.join(repr(key) for key in wanted) + ddl[match.end(1):]
    new_ddl, count = re.subn(r'^CREATE TABLE "?manual_measurements"?\s*\(',
                           'CREATE TABLE manual_measurements_upgrade (', new_ddl, count=1)
    if count != 1:
        raise RuntimeError('无法识别手工补录表结构，未修改数据库')
    extras = conn.exec_driver_sql("SELECT sql FROM sqlite_master WHERE tbl_name='manual_measurements' AND type IN ('index','trigger') AND sql IS NOT NULL").scalars().all()
    conn.exec_driver_sql(new_ddl)
    conn.exec_driver_sql('INSERT INTO manual_measurements_upgrade SELECT * FROM manual_measurements')
    conn.exec_driver_sql('DROP TABLE manual_measurements')
    conn.exec_driver_sql('ALTER TABLE manual_measurements_upgrade RENAME TO manual_measurements')
    for sql in extras:
        conn.exec_driver_sql(sql)


def upgrade_schema(engine):
    if engine.dialect.name != "sqlite":
        return
    with engine.connect() as conn:
        conn.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            ddl = conn.exec_driver_sql("SELECT sql FROM sqlite_master WHERE type='table' AND name='coal_records'").scalar()
            if ddl and 'uq_coal_cat_ts_sys' in ddl:
                # SQLite 无法 DROP CONSTRAINT；使用同一事务复制原表，保留所有列和行。
                indexes = conn.exec_driver_sql("SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name='coal_records' AND sql IS NOT NULL").scalars().all()
                new_ddl = re.sub(r',?\s*CONSTRAINT uq_coal_cat_ts_sys UNIQUE \(category, ts, system\)', '', ddl)
                if new_ddl == ddl:
                    raise RuntimeError('无法识别旧煤样唯一约束，未修改数据库')
                new_ddl = new_ddl.replace('CREATE TABLE coal_records', 'CREATE TABLE coal_records_upgrade', 1)
                conn.exec_driver_sql(new_ddl)
                conn.exec_driver_sql('INSERT INTO coal_records_upgrade SELECT * FROM coal_records')
                conn.exec_driver_sql('DROP TABLE coal_records')
                conn.exec_driver_sql('ALTER TABLE coal_records_upgrade RENAME TO coal_records')
                for sql in indexes:
                    conn.exec_driver_sql(sql)
            conn.exec_driver_sql("CREATE UNIQUE INDEX IF NOT EXISTS uq_coal_measurement ON coal_records (category, ts, system, coalesce(belt, ''))")
            cols = {r[1] for r in conn.exec_driver_sql('PRAGMA table_info(heavy_samples)')}
            if 'client_id' not in cols:
                conn.exec_driver_sql('ALTER TABLE heavy_samples ADD COLUMN client_id VARCHAR(64)')
            conn.exec_driver_sql('CREATE UNIQUE INDEX IF NOT EXISTS uq_heavy_client_id ON heavy_samples(client_id)')
            update_manual_metric_constraint(conn, include_float_ash=True)
            cols = {r[1] for r in conn.exec_driver_sql('PRAGMA table_info(manual_measurements)')}
            if cols and 'time_basis' not in cols:
                conn.exec_driver_sql("ALTER TABLE manual_measurements ADD COLUMN time_basis VARCHAR(32) NOT NULL DEFAULT 'legacy-lag-2h'")
            if cols:
                conn.exec_driver_sql('CREATE INDEX IF NOT EXISTS ix_manual_measurement_effective_latest ON manual_measurements(metric,time_basis,observed_at,id)')
            cols = {r[1] for r in conn.exec_driver_sql('PRAGMA table_info(decision_telemetry_pins)')}
            if cols and 'last_slot' not in cols:
                # Metadata-only additive upgrade. Historical JSON scanning runs
                # later, one hour at a time, before retention may release pins.
                conn.exec_driver_sql('ALTER TABLE decision_telemetry_pins ADD COLUMN last_slot INTEGER')
            conn.commit()
        except Exception:
            conn.rollback()
            raise
