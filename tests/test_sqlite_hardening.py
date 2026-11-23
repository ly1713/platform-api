"""SQLite 加固回归：桌面版（api + worker 双进程共用同一个 .db）的地基。

三项都必须守住，任何一项退回去，用户看到的是「点了没反应 / 转圈后报错 /
同一个视频被下载两遍」，而不是一条清晰的错误——所以这里逐条钉死：

1. **PRAGMA 全开**：busy_timeout / WAL / foreign_keys / synchronous。
   少任何一个，并发表现都会退化（见 app/db.py 顶部注释里的实测数字）。
2. **并发领取不重复**：SQLite 没有 FOR UPDATE SKIP LOCKED，若退化成普通查询，
   多进程会抢到同一个任务（实测 20 个任务被领 30 次）。
3. **索引存在**：点赞排序与队列轮询的复合索引，且 alembic 与 create_all
   两条建库路径必须一致——桌面首次启动走 create_all，升级走 alembic。
"""

import base64
import multiprocessing as mp
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, text

from app.db import SQLITE_BUSY_TIMEOUT_SECONDS, make_engine


# ------------------------------------------------------------------ PRAGMA


def test_sqlite_pragmas_are_applied(tmp_path):
    """新连接必须带上加固 PRAGMA，否则并发会退化为默认行为。"""
    from app.db import enable_sqlite_foreign_keys

    db_path = tmp_path / "pragma.db"
    engine = make_engine(f"sqlite:///{db_path}")
    enable_sqlite_foreign_keys(engine)

    with engine.connect() as conn:
        # journal_mode 需要真正写一次库才会从 delete 变成 wal
        conn.execute(text("CREATE TABLE IF NOT EXISTS t (id INTEGER PRIMARY KEY, v TEXT)"))
        conn.commit()
        assert conn.execute(text("PRAGMA journal_mode")).scalar().lower() == "wal"
        assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 1
        assert conn.execute(text("PRAGMA busy_timeout")).scalar() == SQLITE_BUSY_TIMEOUT_SECONDS * 1000
        # NORMAL(1) 是 WAL 下的常规档；FULL(2) 会让每次提交都 fsync。
        assert conn.execute(text("PRAGMA synchronous")).scalar() == 1
    engine.dispose()


def test_busy_timeout_is_long_enough_for_worker_transactions():
    """30 秒是有依据的下限，不是随手取的数。

    实测：worker 持写事务 8 秒时，默认 5 秒超时的 API 写入直接
    `database is locked`；调到 30 秒后等 7.1 秒写入成功。低于 10 秒等于
    把「慢」变成「失败」。
    """
    assert SQLITE_BUSY_TIMEOUT_SECONDS >= 10


def test_non_sqlite_url_gets_no_sqlite_connect_args():
    """PostgreSQL 不能被塞 SQLite 的 connect_args（会让 psycopg2 报错）。"""
    from app.db import _sqlite_connect_args

    assert _sqlite_connect_args("postgresql+psycopg2://u:p@localhost:5432/db") == {}
    assert _sqlite_connect_args("sqlite:////tmp/x.db") == {"timeout": SQLITE_BUSY_TIMEOUT_SECONDS}
    # 引擎本身仍应按 PG 方言创建（不是被误判成 sqlite）
    engine = make_engine("postgresql+psycopg2://u:p@localhost:5432/db")
    assert engine.dialect.name == "postgresql"
    engine.dispose()


# ------------------------------------------------------- 并发领取不重复


def _claim_worker(db_path: str, rounds: int, out):
    """子进程：反复 claim，把抢到的 job id 回传。

    这里**自建 engine**而不是用 ``app.db.engine``：后者是模块级单例，spawn 子进程
    重新 import 本模块时（模块顶部有 ``from app.db import ...``）它就已经按当时的
    env 建好了，再改 env 无效。自建 engine 让这个测试不依赖全局状态，跑得更稳。
    """
    from sqlalchemy.orm import sessionmaker

    from app.core.queue import claim
    from app.db import enable_sqlite_foreign_keys, make_engine

    engine = make_engine(f"sqlite:///{db_path}")
    enable_sqlite_foreign_keys(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    got = []
    for _ in range(rounds):
        with Session() as db:
            job = claim(db, ["sync"])
            if job is not None:
                got.append(job.id)
        time.sleep(0.002)
    engine.dispose()
    out.put(got)


def test_concurrent_claim_never_hands_out_same_job_twice(tmp_path):
    """多进程并发 claim 时，同一个任务绝不能被执行两次。

    这是桌面版最贵的一类 bug：重复下载、重复调用大模型（按次计费）。
    SQLite 分支用条件 UPDATE（WHERE status='queued'）做乐观锁，命中 0 行
    即视为被别人抢走。
    """
    db_path = tmp_path / "claim.db"
    url = f"sqlite:///{db_path}"

    # 用 app 的真实模型建表，保证列与生产一致
    from app import models  # noqa: F401
    from app.db import Base, enable_sqlite_foreign_keys

    seed_engine = make_engine(url)
    enable_sqlite_foreign_keys(seed_engine)
    Base.metadata.create_all(seed_engine)

    from sqlalchemy.orm import Session

    now = datetime.utcnow()
    with Session(seed_engine) as s:
        for i in range(20):
            s.add(
                models.JobQueue(
                    job_type="sync",
                    payload={"i": i},
                    status="queued",
                    priority=5,
                    attempts=0,
                    max_attempts=3,
                    error="",
                    run_after=now,
                    created_at=now,
                    updated_at=now,
                )
            )
        s.commit()
    seed_engine.dispose()

    ctx = mp.get_context("spawn")  # spawn 更接近真实独立进程，避免 fork 继承连接
    out = ctx.Queue()
    procs = [ctx.Process(target=_claim_worker, args=(str(db_path), 15, out)) for _ in range(3)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=90)
        assert p.exitcode == 0, f"子进程异常退出: {p.exitcode}"

    claimed: list[int] = []
    while not out.empty():
        claimed.extend(out.get())

    assert claimed, "没有任何进程领到任务，测试无效"
    duplicates = {jid for jid in claimed if claimed.count(jid) > 1}
    assert not duplicates, f"同一任务被重复领取（会导致重复下载/重复计费）: {sorted(duplicates)}"

    # 库里的最终状态必须与领取记录一致：全部 running，没有残留 queued
    conn = sqlite3.connect(db_path)
    try:
        running = conn.execute("SELECT COUNT(*) FROM job_queue WHERE status='running'").fetchone()[0]
        queued = conn.execute("SELECT COUNT(*) FROM job_queue WHERE status='queued'").fetchone()[0]
    finally:
        conn.close()
    assert running == len(set(claimed))
    assert running + queued == 20


# --------------------------------------------------------------- 索引


def _alembic_env(tmp_path: Path, db_path: Path) -> dict[str, str]:
    """给子进程 alembic 一份自洽、与开发机状态无关的运行环境。

    ``alembic upgrade head`` 会一路跑到 ``0018_model_access``，那条迁移要求加密
    密钥已就绪，否则拒绝建库（防止自动生成新 secret.key 覆盖已有凭证）。本机
    之所以能过，是因为仓库里躺着 ``data/secret.key``——一个被 gitignore 的文件。
    CI 上没有它，于是同一份代码在 CI 挂、在本机绿，是典型的"只在本机能过的测试"。

    所以这里显式注入一次性密钥，并把 ``PLATFORM_DATA_DIR`` 指到 tmp_path：
    迁移既不该依赖开发机的密钥，也不该往仓库的 data/ 里写任何东西。
    """
    return {
        **os.environ,
        "PLATFORM_DATABASE_URL": f"sqlite:///{db_path}",
        "PLATFORM_AUTO_CREATE_TABLES": "false",
        "PLATFORM_CREDENTIAL_ENC_KEY": base64.b64encode(os.urandom(32)).decode(),
        "PLATFORM_DATA_DIR": str(tmp_path / "data"),
    }


def _run_alembic_upgrade(tmp_path: Path, db_path: Path) -> subprocess.CompletedProcess:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=root,
        env=_alembic_env(tmp_path, db_path),
        capture_output=True,
        text=True,
        timeout=600,
    )


def test_desktop_perf_indexes_exist_after_migration(tmp_path):
    """点赞排序 / 队列轮询的复合索引必须由迁移建出来。"""
    db_path = tmp_path / "mig.db"
    result = _run_alembic_upgrade(tmp_path, db_path)
    assert result.returncode == 0, f"alembic upgrade 失败:\n{result.stderr[-2000:]}"

    conn = sqlite3.connect(db_path)
    try:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    finally:
        conn.close()

    for expected in (
        "ix_cms_content_captured",  # 点赞排序：覆盖索引，避免回表
        "ix_job_queue_status_run_after",  # worker 轮询
        "ix_media_asset_deleted_id",  # 素材列表首页
    ):
        assert expected in names, f"缺少性能索引 {expected}"


def test_create_all_and_migration_indexes_match(tmp_path):
    """两条建库路径的索引集必须一致。

    桌面版首次启动走 ``create_all``（PLATFORM_AUTO_CREATE_TABLES=true），
    而升级走 alembic。若某个索引只在迁移里声明，全新安装的桌面用户就永远
    没有它 —— 表现为「同样数据量，别人快我慢」，极难排查。
    """
    from app import models  # noqa: F401

    # 迁移建库
    mig_db = tmp_path / "mig_idx.db"
    r = _run_alembic_upgrade(tmp_path, mig_db)
    assert r.returncode == 0, r.stderr[-2000:]

    # create_all 建库
    ca_db = tmp_path / "createall_idx.db"
    ca_engine = create_engine(f"sqlite:///{ca_db}")
    from app.db import Base

    Base.metadata.create_all(ca_engine)
    ca_engine.dispose()

    def _indexes(path):
        conn = sqlite3.connect(path)
        try:
            return {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'"
                )
            }
        finally:
            conn.close()

    mig_idx = _indexes(mig_db)
    ca_idx = _indexes(ca_db)

    missing_in_create_all = sorted(mig_idx - ca_idx)
    assert not missing_in_create_all, (
        f"这些索引只存在于迁移里，全新安装的桌面用户会缺: {missing_in_create_all}"
    )
