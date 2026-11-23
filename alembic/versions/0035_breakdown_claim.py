"""拆解外移：认领租约表 + 报告产出方

拆解从「平台自己跑模型」变成「外部 agent 认领后回传」后，需要两样以前不存在的
落库位：

- ``breakdown_claim``：谁在做这条素材、租约到什么时候、被认领过几次。独立成表
  而不是复用 ``viral_breakdown.status``，是为了不把「外部租约」这组概念塞进
  ``workflow_engine`` / ``run_reconciler`` / 前端都要认识的报告状态机。
- ``viral_breakdown.produced_by`` / ``contract_version``：这份报告是谁产出的、
  按哪版契约产出的。同一条素材可能先后经过内部/外部两条路径，不记就解释不了
  「这次的结论为什么和上次不一样」。

新列为空字符串默认值，历史行不需要回填语义（``produced_by`` 为空即「本次改造
之前的报告，由平台内部产出」）。

Revision ID: 0035
Revises: 0034
Create Date: 2026-11-23 10:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0035"
down_revision: Union[str, Sequence[str], None] = "0034"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("viral_breakdown") as batch:
        batch.add_column(
            sa.Column("produced_by", sa.String(length=64), nullable=False, server_default="")
        )
        batch.add_column(
            sa.Column("contract_version", sa.String(length=32), nullable=False, server_default="")
        )

    op.create_table(
        "breakdown_claim",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("asset_id", sa.Integer(), nullable=False),
        sa.Column("agent_name", sa.String(length=120), nullable=False, server_default=""),
        sa.Column("lease_token", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("claimed_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("lease_until", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["asset_id"], ["media_asset.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    # 一素材一租约：唯一约束是「重复认领返回同一租约」的实现基础，
    # 也让两个 agent 同时认领时由数据库而不是应用层判定谁赢。
    op.create_index("ix_breakdown_claim_asset_id", "breakdown_claim", ["asset_id"], unique=True)
    op.create_index("ix_breakdown_claim_lease_until", "breakdown_claim", ["lease_until"])


def downgrade() -> None:
    op.drop_index("ix_breakdown_claim_lease_until", table_name="breakdown_claim")
    op.drop_index("ix_breakdown_claim_asset_id", table_name="breakdown_claim")
    op.drop_table("breakdown_claim")
    with op.batch_alter_table("viral_breakdown") as batch:
        batch.drop_column("contract_version")
        batch.drop_column("produced_by")
