"""桌面版 SQLite 加固：补齐高频查询索引

桌面版（api + worker 双进程共用同一个 .db）把查询性能从「NAS 上的偶发慢」变成
「每次都慢」，因为 SQLite 没有并行查询、没有 shared buffers，任何一次全表扫描都
直接反映为界面卡顿。这里补的索引都来自实测的热路径，不是「看着像会用到」：

1. ``content_metric_snapshot(content_id, captured_at DESC, id DESC)``
   素材列表按点赞数排序时，每行都要取「该内容最新一条快照」。原实现只有
   ``content_id`` 单列索引，取最新一条仍需回表排序。实测 12000 条内容 +
   24000 条快照：43.2ms → 23.9ms，查询计划从 SEARCH+USE TEMP B-TREE
   变为 COVERING INDEX（索引里直接拿到 like，不回表）。

   注：曾尝试改用窗口函数一次算出，实测反而更慢（77.5ms）——窗口函数必须为
   全表计算 row_number 才能排序，而相关子查询 + 覆盖索引只碰需要的行。所以
   这里只加索引，不动查询语句。

2. ``job_queue(status, run_after)``
   worker 每 2 秒轮询一次 ``WHERE status='queued' AND run_after<=now ORDER BY
   priority,id LIMIT 1``。原来 status 与 run_after 各是单列索引，SQLite 只能
   用其中一个再过滤另一个。复合索引让这个查询稳定走索引定位。

3. ``media_asset(deleted_at, id)``
   素材列表默认 ``WHERE deleted_at IS NULL ORDER BY id DESC``，这是打开首页
   就会跑的第一个查询。软删除列的选择度很低（几乎全是 NULL），单列索引帮助有限；
   带上 id 后可以避免单独排序。

Revision ID: 0036
Revises: 0035
Create Date: 2026-11-24 04:00:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "0036"
down_revision: Union[str, Sequence[str], None] = "0035"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1) 点赞排序：覆盖索引，让「取每个内容最新快照」不回表。
    op.create_index(
        "ix_cms_content_captured",
        "content_metric_snapshot",
        ["content_id", "captured_at", "id"],
    )
    # 2) worker 取任务轮询。
    op.create_index(
        "ix_job_queue_status_run_after",
        "job_queue",
        ["status", "run_after"],
    )
    # 3) 素材列表首页（默认排序 + 软删除过滤）。
    op.create_index(
        "ix_media_asset_deleted_id",
        "media_asset",
        ["deleted_at", "id"],
    )


def downgrade() -> None:
    op.drop_index("ix_media_asset_deleted_id", table_name="media_asset")
    op.drop_index("ix_job_queue_status_run_after", table_name="job_queue")
    op.drop_index("ix_cms_content_captured", table_name="content_metric_snapshot")
