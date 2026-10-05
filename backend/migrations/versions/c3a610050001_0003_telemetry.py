"""0003: independent read-only telemetry tables.

Revision ID: c3a610050001
Revises: b7e2c4d9a001
"""
from alembic import context, op
import sqlalchemy as sa

revision = "c3a610050001"
down_revision = "b7e2c4d9a001"
branch_labels = None
depends_on = None


def tables():
    metadata = sa.MetaData()
    sa.Table("telemetry_sources", metadata,
        sa.Column("source_id", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("owner", sa.String(36)), sa.Column("lease_until", sa.String(32)),
        sa.Column("heartbeat_at", sa.String(32)), sa.Column("last_sample_at", sa.String(32)),
        sa.Column("error_code", sa.String(64)), sa.Column("retry_seconds", sa.Float(), nullable=False))
    sa.Table("telemetry_points", metadata,
        sa.Column("source_id", sa.String(64), primary_key=True),
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("config", sa.JSON(), nullable=False), sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("latest", sa.JSON()), sa.Column("last_history_at", sa.String(32)))
    readings = sa.Table("telemetry_readings", metadata,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("source_id", sa.String(64), nullable=False), sa.Column("point_key", sa.String(64), nullable=False),
        sa.Column("batch_id", sa.String(36), nullable=False), sa.Column("received_at", sa.String(32), nullable=False),
        sa.Column("sample", sa.JSON(), nullable=False),
        sa.UniqueConstraint("source_id", "point_key", "batch_id", name="uq_telemetry_batch_point"))
    sa.Index("ix_telemetry_point_time", readings.c.source_id, readings.c.point_key, readings.c.received_at)
    sa.Index("ix_telemetry_retention", readings.c.received_at)
    return metadata


def upgrade():
    metadata = tables()
    bind = op.get_bind()
    if context.is_offline_mode():
        metadata.create_all(bind, checkfirst=False)
        return
    inspector = sa.inspect(bind)
    existing = set(inspector.get_table_names())
    # HTTP/collector 保留旧版自动建表兼容。先核对已有表，再接纳，不能静默 stamp 异构表。
    for table in metadata.sorted_tables:
        if table.name not in existing:
            continue
        actual = {column["name"]: column for column in inspector.get_columns(table.name)}
        if set(actual) != set(table.columns.keys()) or any(
            actual[column.name]["type"]._type_affinity is not column.type._type_affinity
            or (not column.primary_key and actual[column.name]["nullable"] != column.nullable)
            for column in table.columns
        ):
            raise RuntimeError(f"Existing telemetry schema differs: {table.name}; migration cancelled")
        if inspector.get_pk_constraint(table.name)["constrained_columns"] != [c.name for c in table.primary_key]:
            raise RuntimeError(f"Existing telemetry primary key differs: {table.name}; migration cancelled")
        for unique in (c for c in table.constraints if isinstance(c, sa.UniqueConstraint)):
            if not any(row["column_names"] == [c.name for c in unique.columns] for row in inspector.get_unique_constraints(table.name)):
                raise RuntimeError(f"Existing telemetry uniqueness differs: {table.name}; migration cancelled")
        indexes = {row["name"]: row for row in inspector.get_indexes(table.name)}
        for index in table.indexes:
            if index.name in indexes and (indexes[index.name]["column_names"] != [c.name for c in index.columns] or indexes[index.name]["unique"]):
                raise RuntimeError(f"Existing telemetry index differs: {table.name}; migration cancelled")
    metadata.create_all(bind, checkfirst=True)
    for table in metadata.sorted_tables:
        for index in table.indexes:
            index.create(bind, checkfirst=True)


def downgrade():
    op.drop_table("telemetry_readings")
    op.drop_table("telemetry_points")
    op.drop_table("telemetry_sources")
