"""0002_training_model_columns

Revision ID: b7e2c4d9a001
Revises: 27cd61d87298
Create Date: 2026-09-09

粗灰模型训练后移：coarse_models 加 train_run_id + method 唯一约束；
coarse_model_history 加 train_run_id + detail（完整历史快照）。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b7e2c4d9a001'
down_revision: Union[str, Sequence[str], None] = '27cd61d87298'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('coarse_models', sa.Column('train_run_id', sa.String(length=36), nullable=True))
    op.create_unique_constraint('uq_coarse_model_method', 'coarse_models', ['method'])
    op.add_column('coarse_model_history', sa.Column('train_run_id', sa.String(length=36), nullable=True))
    op.add_column('coarse_model_history', sa.Column('detail', sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column('coarse_model_history', 'detail')
    op.drop_column('coarse_model_history', 'train_run_id')
    op.drop_constraint('uq_coarse_model_method', 'coarse_models', type_='unique')
    op.drop_column('coarse_models', 'train_run_id')
