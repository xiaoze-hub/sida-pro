"""M8(2026-09-10): 悟道 token 读取源回归。

问题: `WudaoMCPClient._db_token()` 原先直读 **sqlite** `/app/data/panwatch.db`, 而应用
跑在 **PG**(设置页写 PG) → 在设置页换 token(如买套餐)后代码仍读到旧 sqlite 值,
表现为"改 key 不生效"。现改为优先读规范库(`src.db.session`), sqlite 仅兜底。
"""
from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.collectors.wudao_mcp_client import WudaoMCPClient
from src.db.models import AppSettings, Base


def _seed_pg_token(value: str):
    eng = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(eng, tables=[AppSettings.__table__])
    S = sessionmaker(bind=eng, autoflush=False, expire_on_commit=False)
    db = S()
    db.add(AppSettings(key="wudao_mcp_token", value=value))
    db.commit()
    db.close()
    return S


def test_db_token_prefers_canonical_db(monkeypatch):
    """规范库(PG)有值 → 用它, 不读 sqlite。"""
    import src.db.session as dbs

    monkeypatch.setattr(dbs, "SessionLocal", _seed_pg_token("PG_TOKEN"))
    # sqlite 兜底给一个不同值, 用来证明"优先 PG"而非"退到 sqlite"
    monkeypatch.setattr(WudaoMCPClient, "_sqlite_token", staticmethod(lambda: "SQLITE_TOKEN"))
    assert WudaoMCPClient._db_token() == "PG_TOKEN"


def test_db_token_pools_multiple(monkeypatch):
    """逗号分隔多 token → 轮换(分摊额度)。"""
    import src.db.session as dbs

    monkeypatch.setattr(dbs, "SessionLocal", _seed_pg_token("T1, T2"))
    monkeypatch.setattr(WudaoMCPClient, "_sqlite_token", staticmethod(lambda: ""))
    WudaoMCPClient._token_idx = -1
    first = WudaoMCPClient._db_token()
    second = WudaoMCPClient._db_token()
    assert {first, second} == {"T1", "T2"}


def test_db_token_falls_back_to_sqlite_when_db_unavailable(monkeypatch):
    import src.db.session as dbs

    def _boom():
        raise RuntimeError("pg unavailable")

    monkeypatch.setattr(dbs, "SessionLocal", _boom)
    monkeypatch.setattr(WudaoMCPClient, "_sqlite_token", staticmethod(lambda: "SQLITE_TOKEN"))
    assert WudaoMCPClient._db_token() == "SQLITE_TOKEN"


def test_db_token_empty_when_nothing_configured(monkeypatch):
    import src.db.session as dbs

    monkeypatch.setattr(dbs, "SessionLocal", _seed_pg_token(""))
    monkeypatch.setattr(WudaoMCPClient, "_sqlite_token", staticmethod(lambda: ""))
    assert WudaoMCPClient._db_token() == ""
