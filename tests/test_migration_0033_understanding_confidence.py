"""迁移 0033：视频理解自评置信度落库，升降级都可逆。"""

import base64
import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect


def _cfg(db_url: str) -> Config:
    root = Path(__file__).resolve().parent.parent
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "alembic"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


def _columns(engine, table: str) -> dict[str, dict]:
    return {col["name"]: col for col in inspect(engine).get_columns(table)}


@pytest.fixture(autouse=True)
def _configured_key(monkeypatch):
    """迁移会读加密密钥，显式提供避免被结构检查挡住。"""
    from app.config import settings

    monkeypatch.setattr(settings, "credential_enc_key", base64.b64encode(os.urandom(32)).decode())
    monkeypatch.setattr(settings, "credential_key_file", None)
    import app.core.crypto as crypto

    crypto._key = None
    yield
    crypto._key = None


def test_migration_0033_adds_understanding_confidence(tmp_path):
    url = "sqlite:///" + str(tmp_path / "mig33.db")
    command.upgrade(_cfg(url), "0032")
    engine = create_engine(url)
    try:
        assert "understanding_confidence" not in _columns(engine, "video_understanding")
    finally:
        engine.dispose()

    command.upgrade(_cfg(url), "0033")
    engine = create_engine(url)
    try:
        column = _columns(engine, "video_understanding")["understanding_confidence"]
        assert column["nullable"] is False
        # 历史行必须有确定值，不能让拆解读到 NULL 后崩。
        assert str(column["default"]).strip("'\"") in ("0", "0.0")
    finally:
        engine.dispose()

    # 回滚可逆：降回 0032 后列消失。
    command.downgrade(_cfg(url), "0032")
    engine = create_engine(url)
    try:
        assert "understanding_confidence" not in _columns(engine, "video_understanding")
    finally:
        engine.dispose()
