"""Independent, delayed total-ash settlements; original decision history is retained."""
from alembic import op
from app.decision_hourly.models import DecisionAshSettlement

revision = 'c3a610140001'
down_revision = 'c3a610130001'
branch_labels = None
depends_on = None


def upgrade():
    DecisionAshSettlement.__table__.create(op.get_bind(), checkfirst=True)


def downgrade():
    if op.get_bind().exec_driver_sql('SELECT 1 FROM gpt_ash_settlements LIMIT 1').first():
        raise RuntimeError('已有小时灰分结算记录，不能降级删除')
    DecisionAshSettlement.__table__.drop(op.get_bind(), checkfirst=True)
