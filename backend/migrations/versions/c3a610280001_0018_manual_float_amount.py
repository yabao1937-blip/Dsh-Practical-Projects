"""Allow decision-participating manual flotation clean coal amount entries."""
from alembic import op
from app.schema_upgrade import update_manual_metric_constraint

revision = 'c3a610280001'
down_revision = 'c3a610260002'
branch_labels = None
depends_on = None


def change(include):
    conn = op.get_bind()
    if conn.dialect.name == 'sqlite':
        # SQLite's legacy transaction mode does not begin a transaction for DDL.
        if not conn.connection.driver_connection.in_transaction:
            conn.exec_driver_sql('BEGIN IMMEDIATE')
        update_manual_metric_constraint(conn, include_ash=True, include_float_ash=True, include_float_amount=include)
    else:
        if not include and conn.exec_driver_sql("SELECT 1 FROM manual_measurements WHERE metric='float_clean_coal_amount' LIMIT 1").first():
            raise RuntimeError('已有浮精量记录，不能降级删除该指标')
        op.drop_constraint('ck_manual_measurement_metric', 'manual_measurements', type_='check')
        values = "'heavy_clean_coal_ash', 'raw_coal_slime_content', 'coarse_slime_amount', 'coarse_slime_ash', 'float_clean_coal_ash'"
        if include:
            values += ", 'float_clean_coal_amount'"
        op.create_check_constraint('ck_manual_measurement_metric', 'manual_measurements', f'metric IN ({values})')


def upgrade():
    change(True)


def downgrade():
    change(False)
