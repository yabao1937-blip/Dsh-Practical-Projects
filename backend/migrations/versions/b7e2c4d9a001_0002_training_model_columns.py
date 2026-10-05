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
    # SQLite 不支持 ALTER TABLE ADD CONSTRAINT，使用 Alembic batch 保留原数据。
    with op.batch_alter_table('coarse_models') as batch:
        batch.add_column(sa.Column('train_run_id', sa.String(length=36), nullable=True))
        batch.create_unique_constraint('uq_coarse_model_method', ['method'])
    op.add_column('coarse_model_history', sa.Column('train_run_id', sa.String(length=36), nullable=True))
    op.add_column('coarse_model_history', sa.Column('detail', sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column('coarse_model_history', 'detail')
    op.drop_column('coarse_model_history', 'train_run_id')
    with op.batch_alter_table('coarse_models') as batch:
        batch.drop_constraint('uq_coarse_model_method', type_='unique')
        batch.drop_column('train_run_id')
