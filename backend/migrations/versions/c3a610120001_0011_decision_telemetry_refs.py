"""Authoritative telemetry references, retained independently of hot decision rows."""
from alembic import op
from app.decision_hourly.models import DecisionTelemetryPin

revision = 'c3a610120001'
down_revision = 'c3a610100001'
branch_labels = None
depends_on = None


def upgrade():
    DecisionTelemetryPin.__table__.create(op.get_bind(), checkfirst=True)


def downgrade():
    # Dropping pins would allow pruning records still used by compressed evidence.
    if op.get_bind().exec_driver_sql('SELECT 1 FROM decision_telemetry_pins LIMIT 1').first():
        raise RuntimeError('Decision telemetry references exist; preserve the reference table and evidence')
    DecisionTelemetryPin.__table__.drop(op.get_bind(), checkfirst=True)
