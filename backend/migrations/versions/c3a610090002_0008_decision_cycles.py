"""Forward decision observations and hourly aggregates.

Revision ID: c3a610090002
Revises: c3a610090001
"""
from alembic import op
from app.decision_hourly.models import DecisionCycleContext, DecisionCycleSample, DecisionCycleHour

revision = 'c3a610090002'
down_revision = 'c3a610090001'
branch_labels = None
depends_on = None


def upgrade():
    for model in (DecisionCycleContext,DecisionCycleSample,DecisionCycleHour):
        model.__table__.create(op.get_bind(),checkfirst=True)


def downgrade():
    for model in (DecisionCycleHour,DecisionCycleSample,DecisionCycleContext):
        model.__table__.drop(op.get_bind(),checkfirst=True)
