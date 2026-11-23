"""数据库引擎与会话。

SQLite 与 PostgreSQL 两种后端共用一套模型，但**并发语义差别很大**，桌面版
（api 进程 + worker 进程同时读写同一个 .db 文件）完全落在 SQLite 上，因此这里
对 SQLite 做了三项加固，缺一不可：

1. ``busy_timeout``：默认 5 秒，一旦 worker 有写事务在跑，API 的写入 5 秒后直接
   抛 ``database is locked``。实测 worker 开长事务 8 秒 → API 立即失败；调到 30 秒后
   等待 7.1 秒写入成功。
2. ``journal_mode=WAL``：读写不互斥，读不再被写阻塞（默认 delete 日志模式会）。
3. ``synchronous=NORMAL``：WAL 下的常规安全档，断电最多丢最后一个事务，换来数倍写入
   性能；桌面单机场景不值得为了「最后一个事务」付出 FULL 的 fsync 代价。

外键（``foreign_keys=ON``）也要显式打开：SQLite 默认不启用，测试与桌面版必须与
PostgreSQL 行为一致，否则 ondelete=CASCADE 静默失效。
"""

import logging
from collections.abc import Generator
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings

logger = logging.getLogger("platform.db")

# SQLite 等锁的秒数。必须远大于 worker 单个写事务的持有时长（下载/转录会先 commit
# 再 await，正常情况下事务是毫秒级），30 秒是「绝不误报，又能及时暴露死锁」的折中。
SQLITE_BUSY_TIMEOUT_SECONDS = 30


def _sqlite_connect_args(url: str) -> dict:
    """SQLite 专用连接参数；非 SQLite 返回空字典。"""
    try:
        parsed = make_url(url)
    except Exception:  # noqa: BLE001 - 非法 DSN 交给 create_engine 报清晰的错
        return {}
    if parsed.get_backend_name() != "sqlite":
        return {}
    return {"timeout": SQLITE_BUSY_TIMEOUT_SECONDS}


def make_engine(url: str):
    """按 DSN 建引擎。集中一处，保证 api/worker/测试拿到同样的 SQLite 加固。"""
    return create_engine(url, connect_args=_sqlite_connect_args(url))


class Base(DeclarativeBase):
    pass


def enable_sqlite_foreign_keys(target_engine) -> None:
    """挂 SQLite 专用连接钩子：外键 + WAL + busy_timeout + synchronous。

    这些 PRAGMA 是**连接级**的（journal_mode 除外，它持久化在库文件上，但每次连接
    重设一遍无害且能纠正被外部工具改掉的库），所以必须在每条新连接上执行。
    """
    if target_engine.dialect.name != "sqlite":
        return

    @event.listens_for(target_engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, _record):  # pragma: no cover - 连接钩子
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            # busy_timeout 与 connect_args 的 timeout 是两条独立路径：前者作用于
            # SQLite 内部等待，后者作用于 Python 驱动层。两者都设，避免其中一条被绕过。
            # 放在 WAL 之前：WAL 本身也要抢锁，先有超时更稳。
            cursor.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_SECONDS * 1000}")
            # WAL：读写并发。多进程（api + worker）同时访问时这是必须的。
            #
            # 但 WAL 要求共享内存（-shm）与库文件在**支持共享内存的本地文件系统**上。
            # 网络盘（SMB/NFS）、某些同步盘（OneDrive/Dropbox）会直接抛
            # "unable to open database file" 把整个应用启动打挂。桌面用户完全可能把
            # 数据目录放在 NAS 共享上，所以这里必须容错：WAL 失败就退回默认 delete
            # 模式（并发变差但仍能跑），并记一条警告，而不是让程序起不来。
            try:
                mode = cursor.execute("PRAGMA journal_mode=WAL").fetchone()
                if not mode or str(mode[0]).lower() != "wal":
                    logger.warning("SQLite 无法启用 WAL（当前 %s），并发写入会变慢；请把数据目录放在本地磁盘", mode)
            except Exception as exc:  # noqa: BLE001 - 保命优先，不能因日志模式挡住启动
                logger.warning("SQLite 启用 WAL 失败（%s），已退回默认日志模式；数据目录可能在网络盘上", exc)
            cursor.execute("PRAGMA synchronous=NORMAL")
            # 临时表放内存，排序/分组（如 like_count 排序的 TEMP B-TREE）不必落盘。
            cursor.execute("PRAGMA temp_store=MEMORY")
            # 负值 = KB 为单位的缓存上限（-64000 ≈ 64MB），默认仅 2MB，大表扫描会频繁淘汰。
            cursor.execute("PRAGMA cache_size=-64000")
        finally:
            cursor.close()


engine = make_engine(settings.resolved_database_url)

enable_sqlite_foreign_keys(engine)


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    # 生产由 Alembic 负责建表（见 docker-entrypoint.sh）；关闭自动建表时直接跳过。
    if not settings.auto_create_tables:
        return

    # 若库已由 Alembic 管理（存在 alembic_version 且有记录），create_all 会与迁移链竞争：
    # 它既不写 revision、也不会加列，之后 `alembic upgrade` 会从 0001 重跑撞表。
    # 这种库完全交给 Alembic，init_db 不插手。
    from sqlalchemy import inspect

    if inspect(engine).has_table("alembic_version"):
        with engine.connect() as conn:
            from sqlalchemy import text

            if conn.execute(text("SELECT version_num FROM alembic_version LIMIT 1")).scalar():
                return

    from app import models  # noqa: F401  确保模型注册

    Base.metadata.create_all(engine)

    # create_all 建的是"当前模型"结构，正好对应迁移链 head。这里把版本戳到 head，
    # 使之后的 `alembic upgrade head` 成为空操作，不再撞表（修复审查发现的冲突）。
    from app.core.version import db_head

    head = db_head()
    if head:
        from alembic import command

        from app.core.alembic_config import make_config

        root = Path(__file__).resolve().parent.parent
        # 必须用当前引擎的真实 DSN：桌面版走 SQLite 文件，若这里重新读
        # settings.resolved_database_url，一旦进程间环境变量不一致就会把版本戳写进
        # 另一个库（实测踩过：子进程静默连到默认 PostgreSQL:5433）。以引擎为准，
        # 保证「戳的库」和「建表的库」永远是同一个。
        cfg = make_config(root, engine.url.render_as_string(hide_password=False))
        command.stamp(cfg, head)
