"""SQLite 原位升级：保留历史行和索引，事务失败则完整回滚。"""
import re


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
            conn.commit()
        except Exception:
            conn.rollback()
            raise
