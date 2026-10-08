"""0005: independent manual process measurements."""
from alembic import op
import sqlalchemy as sa

revision = "c3a610070001"
down_revision = "c3a610060001"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("manual_measurements"):
        expected = {"id", "metric", "name", "value", "unit", "observed_at", "created_at", "client_id", "request_hash"}
        columns = inspector.get_columns("manual_measurements")
        unique = inspector.get_unique_constraints("manual_measurements")
        types = {"id": sa.Integer, "value": sa.Float}
        lengths = {"metric": 32, "name": 64, "unit": 16, "observed_at": 32,
                   "created_at": 32, "client_id": 64, "request_hash": 64}
        valid_types = all(isinstance(column["type"], types.get(column["name"], sa.String))
                          and (column["name"] not in lengths or column["type"].length == lengths[column["name"]])
                          and column["nullable"] == (column["name"] == "client_id") for column in columns)
        checks = {constraint["name"] for constraint in inspector.get_check_constraints("manual_measurements")}
        if ({column["name"] for column in columns} != expected
                or not valid_types
                or inspector.get_pk_constraint("manual_measurements")["constrained_columns"] != ["id"]
                or not any(constraint["column_names"] == ["client_id"] for constraint in unique)
                or not {"ck_manual_measurement_nonnegative", "ck_manual_measurement_metric", "ck_manual_measurement_percentage"}.issubset(checks)):
            raise RuntimeError("Existing manual measurement schema differs; migration cancelled")
        if not any(index["column_names"] == ["metric", "id"] for index in inspector.get_indexes("manual_measurements")):
            op.create_index("ix_manual_measurement_metric_id", "manual_measurements", ["metric", "id"])
        return
    op.create_table("manual_measurements",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("metric", sa.String(32), nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("value", sa.Float(), nullable=False),
        sa.Column("unit", sa.String(16), nullable=False),
        sa.Column("observed_at", sa.String(32), nullable=False),
        sa.Column("created_at", sa.String(32), nullable=False),
        sa.Column("client_id", sa.String(64), nullable=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.UniqueConstraint("client_id", name="uq_manual_measurements_client_id"),
        sa.CheckConstraint("value >= 0", name="ck_manual_measurement_nonnegative"),
        sa.CheckConstraint("metric IN ('heavy_clean_coal_ash', 'raw_coal_slime_content', 'coarse_slime_amount')", name="ck_manual_measurement_metric"),
        sa.CheckConstraint("metric = 'coarse_slime_amount' OR value <= 100", name="ck_manual_measurement_percentage"))
    op.create_index("ix_manual_measurement_metric_id", "manual_measurements", ["metric", "id"])


def downgrade():
    op.drop_index("ix_manual_measurement_metric_id", table_name="manual_measurements")
    op.drop_table("manual_measurements")
