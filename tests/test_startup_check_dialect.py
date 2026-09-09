"""0.4② 方言门禁测试(2026-09-08)。

丢 SIDA_DB_URL 曾导致生产静默回退 SQLite 跑 4 天 —— 门禁要求:
- SIDA_DB_URL 已设置 → 放行;
- 未设置但 SIDA_ALLOW_SQLITE=1 → 放行 + 醒目 WARNING(仅限本地开发);
- 两者皆无 → 拒绝启动。
另覆盖 /api/health 的方言标签(components.database.dialect)。
"""
import logging

from fastapi.testclient import TestClient

from src.core import startup_check as sc


def test_gate_blocks_when_no_env(monkeypatch):
    monkeypatch.delenv("SIDA_DB_URL", raising=False)
    monkeypatch.delenv("SIDA_ALLOW_SQLITE", raising=False)
    ok, msg = sc.check_db_dialect_explicit()
    assert ok is False
    assert "SIDA_DB_URL" in msg
    assert "SIDA_ALLOW_SQLITE" in msg


def test_gate_allows_dev_mode_with_warning_banner(monkeypatch, caplog):
    monkeypatch.delenv("SIDA_DB_URL", raising=False)
    monkeypatch.setenv("SIDA_ALLOW_SQLITE", "1")
    ok, msg = sc.check_db_dialect_explicit()
    assert ok is True
    assert "SQLite" in msg
    assert "SIDA_ALLOW_SQLITE=1" in msg
    banner_records = [
        r
        for r in caplog.records
        if r.name == "src.core.startup_check"
        and r.levelno == logging.WARNING
        and "SIDA_ALLOW_SQLITE=1" in r.getMessage()
    ]
    assert banner_records, "开发模式回退必须打醒目 WARNING 横幅"


def test_gate_allows_explicit_pg_url(monkeypatch):
    monkeypatch.setenv("SIDA_DB_URL", "postgresql+psycopg2://u:p@localhost:5432/db")
    monkeypatch.setattr(sc, "is_postgres", lambda: True)
    ok, msg = sc.check_db_dialect_explicit()
    assert ok is True
    assert "PostgreSQL" in msg


def test_gate_allows_explicit_sqlite_url(monkeypatch):
    monkeypatch.setenv("SIDA_DB_URL", "sqlite:///./data/panwatch.db")
    monkeypatch.setattr(sc, "is_postgres", lambda: False)
    ok, msg = sc.check_db_dialect_explicit()
    assert ok is True
    assert "SQLite" in msg


def test_health_exposes_dialect_field():
    from src.web.app import app

    client = TestClient(app)
    r = client.get("/api/health")
    assert r.status_code == 200
    data = r.json()["data"]  # 标准 envelope: {"code":0, "data": {...}}
    # 测试环境走 SQLite; 生产 PG 部署后此字段应为 postgresql
    assert data["components"]["database"]["dialect"] == "sqlite"
