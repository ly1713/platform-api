"""Persist video-understanding self-reported confidence

Revision ID: 0033
Revises: 0032
Create Date: 2026-11-03 10:00:00.000000

视频理解提示词一直要求模型输出 ``confidence``，但服务层解析后直接丢弃，
拆解因此无法区分「画面确实看清了」和「只是猜的」。这一列把自评分落库，
让拆解可以把低置信内容标注成不确定，而不是当成事实引用。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0033"
down_revision: Union[str, Sequence[str], None] = "0032"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # SQLite 不支持 ALTER COLUMN 的完整语义，用 batch 模式保证本地 SQLite 与
    # PostgreSQL 走同一条迁移路径（与 0032 相同的处理）。
    with op.batch_alter_table("video_understanding") as batch:
        batch.add_column(
            sa.Column(
                "understanding_confidence",
                sa.Float(),
                nullable=False,
                server_default="0",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("video_understanding") as batch:
        batch.drop_column("understanding_confidence")
