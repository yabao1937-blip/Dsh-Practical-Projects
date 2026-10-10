"""GPT operating evidence archives and independent density tracking."""
from alembic import op
from app.decision_hourly.models import DecisionEvidenceArchive, DensityObservation, DensityAction, DensityActionEntry

revision = 'c3a610100001'
down_revision = 'c3a610090003'
branch_labels = None
depends_on = None


def upgrade():
    for model in (DecisionEvidenceArchive,DensityObservation,DensityAction,DensityActionEntry):
        model.__table__.create(op.get_bind(),checkfirst=True)


def downgrade():
    for model in (DensityActionEntry,DensityAction,DensityObservation,DecisionEvidenceArchive):
        model.__table__.drop(op.get_bind(),checkfirst=True)
