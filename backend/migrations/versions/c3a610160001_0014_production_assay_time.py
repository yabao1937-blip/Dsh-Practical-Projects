"""Actual production-time assays and durable hour-first MD_SET evidence."""
from alembic import op
import sqlalchemy as sa
from app.decision_hourly.models import GptHourlyDensityAnchor

revision = 'c3a610160001'
down_revision = 'c3a610140001'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    if 'time_basis' not in {c['name'] for c in sa.inspect(bind).get_columns('manual_measurements')}:
        op.add_column('manual_measurements', sa.Column('time_basis', sa.String(32), nullable=False,
                                                     server_default='legacy-lag-2h'))
    if 'ix_manual_measurement_effective_latest' not in {i['name'] for i in sa.inspect(bind).get_indexes('manual_measurements')}:
        op.create_index('ix_manual_measurement_effective_latest', 'manual_measurements',
                        ['metric','time_basis','observed_at','id'])
    GptHourlyDensityAnchor.__table__.create(bind, checkfirst=True)


def downgrade():
    bind = op.get_bind()
    if (bind.exec_driver_sql('SELECT 1 FROM gpt_hourly_density_anchors LIMIT 1').first()
            or bind.exec_driver_sql("SELECT 1 FROM manual_measurements WHERE metric='heavy_clean_coal_ash' AND time_basis='production-time-v1' LIMIT 1").first()):
        raise RuntimeError('已有生产时间化验或小时设定基准，不能降级丢失归属证据')
    GptHourlyDensityAnchor.__table__.drop(bind, checkfirst=True)
    op.drop_index('ix_manual_measurement_effective_latest', table_name='manual_measurements')
    op.drop_column('manual_measurements', 'time_basis')
