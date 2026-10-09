"""Independent versioned GPT expert model.

Revision ID: c3a610090003
Revises: c3a610090002
"""
from alembic import op
from app.gpt_coarse.models import GptCoarseRevision

revision = 'c3a610090003'
down_revision = 'c3a610090002'
branch_labels = None
depends_on = None


def upgrade():
    GptCoarseRevision.__table__.create(op.get_bind(), checkfirst=True)


def downgrade():
    GptCoarseRevision.__table__.drop(op.get_bind(), checkfirst=True)
