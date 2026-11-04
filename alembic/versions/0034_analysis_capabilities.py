"""长视频分块、视听信号与证据类别

Revision ID: 0034
Revises: 0033
Create Date: 2026-11-03 12:00:00.000000

三个能力补齐后需要新的落库位：
- ``evidence_class``：这条素材的爆点靠哪路证据（话/反应/画面）。品类路由用它
  切换提示词侧重与信号权重。
- ``signals``：响度峰值、镜头切换、运动量等客观信号（JSON）。信号是选段的证据，
  不属于模型产出，必须单独存。
- ``moments``：由信号融合出的候选片段（含 drivers，说明靠哪路证据入选）。
- ``chunk_plan`` / ``chunk_count``：长视频被切成几块、每块的原始时间范围。
  没有它就没法解释 timeline 是怎么拼出来的，也没法复算偏移。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0034"
down_revision: Union[str, Sequence[str], None] = "0033"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # SQLite 不支持完整 ALTER COLUMN 语义，统一用 batch 模式，
    # 让本地 SQLite 与生产 PostgreSQL 走同一条迁移路径。
    with op.batch_alter_table("video_understanding") as batch:
        batch.add_column(
            sa.Column("evidence_class", sa.String(length=16), nullable=False, server_default="words")
        )
        batch.add_column(sa.Column("signals", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("moments", sa.JSON(), nullable=True))
        batch.add_column(
            sa.Column("chunk_count", sa.Integer(), nullable=False, server_default="1")
        )
        batch.add_column(sa.Column("chunk_plan", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("video_understanding") as batch:
        batch.drop_column("chunk_plan")
        batch.drop_column("chunk_count")
        batch.drop_column("moments")
        batch.drop_column("signals")
        batch.drop_column("evidence_class")
