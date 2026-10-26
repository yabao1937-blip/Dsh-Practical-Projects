"""Persist real hourly-card advice revisions without historical backfill."""
from alembic import op
import sqlalchemy as sa

revision = 'c3a610260002'
down_revision = 'c3a610260001'
branch_labels = None
depends_on = None


def upgrade():
    if not sa.inspect(op.get_bind()).has_table('gpt_hourly_density_advice'):
        op.create_table('gpt_hourly_density_advice',
            sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column('apply_start', sa.String(32), nullable=False),
            sa.Column('generated_at', sa.String(32), nullable=False),
            sa.Column('fingerprint', sa.String(64), nullable=False),
            sa.Column('snapshot', sa.JSON(), nullable=False))
        for key in ('apply_start', 'generated_at'):
            op.create_index('ix_gpt_hourly_density_advice_'+key, 'gpt_hourly_density_advice', [key])


def downgrade():
    if op.get_bind().exec_driver_sql('SELECT 1 FROM gpt_hourly_density_advice LIMIT 1').first():
        raise RuntimeError('已有小时建议历史证据，不能降级删除归档表')
    op.drop_table('gpt_hourly_density_advice')
