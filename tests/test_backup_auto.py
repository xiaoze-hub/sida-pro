# -*- coding: utf-8 -*-
"""自动备份/恢复单测: src/core/db_backup_auto.py

覆盖:
  - SQLite 备份: 生成 .sql.gz + gzip 完整性通过
  - 备份文件名格式 backup_YYYYMMDD_HHMMSS.sql.gz
  - cleanup_old_backups 按保留天数删除
  - list_backups 返回结构
  - verify_gzip_file 坏文件返回 False
  - restore_backup: 恢复前自动备份 + 内容一致
  - 备份失败时调用告警(捕获)
"""
from __future__ import annotations

import gzip
import os
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from src.core import db_backup_auto as bak  # noqa: E402


@pytest.fixture()
def sqlite_env(tmp_path, monkeypatch):
    """建一个最小 SQLite 库, 指向 tmp DATA_DIR。"""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = data_dir / "panwatch.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    conn.execute("INSERT INTO t (v) VALUES ('hello')")
    conn.commit()
    conn.close()

    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.setenv("SIDA_DB_URL", f"sqlite:///{db_path}")
    # 方言模块是 import 期读 env 的, 测试直接传 url/路径, 不 reload
    return {"data_dir": data_dir, "db_path": db_path}


def test_backup_creates_gz_and_verifies(sqlite_env, monkeypatch):
    # 绕过 dialect import(测试环境可能已绑定真实 DB): 直接测 _dump_sqlite
    out = sqlite_env["data_dir"] / "backups"
    out.mkdir()
    target = out / "backup_20260101_030000.sql.gz"
    bak._dump_sqlite(str(target), f"sqlite:///{sqlite_env['db_path']}")
    assert target.is_file()
    assert target.stat().st_size > 0
    assert bak.verify_gzip_file(str(target)) is True


def test_backup_filename_format():
    ts = datetime(2026, 9, 18, 3, 0, 0)
    name = bak._backup_filename(ts)
    assert name == "backup_20260918_030000_000000.sql.gz"
    # 带微秒
    ts2 = datetime(2026, 9, 18, 3, 0, 0, 123456)
    assert bak._backup_filename(ts2) == "backup_20260918_030000_123456.sql.gz"


def test_verify_gzip_rejects_corrupt(tmp_path):
    bad = tmp_path / "backup_20260101_000000.sql.gz"
    bad.write_bytes(b"not gzip at all")
    assert bak.verify_gzip_file(str(bad)) is False


def test_verify_gzip_rejects_empty(tmp_path):
    empty = tmp_path / "backup_20260101_000000.sql.gz"
    empty.write_bytes(b"")
    assert bak.verify_gzip_file(str(empty)) is False


def test_list_and_cleanup(sqlite_env):
    backup_dir = sqlite_env["data_dir"] / "backups"
    backup_dir.mkdir()
    # 新备份(今天)
    new_name = bak._backup_filename(datetime.now())
    with gzip.open(backup_dir / new_name, "wb") as f:
        f.write(b"new")
    # 旧备份(40 天前)
    old_dt = datetime.now() - timedelta(days=40)
    old_name = bak._backup_filename(old_dt)
    with gzip.open(backup_dir / old_name, "wb") as f:
        f.write(b"old")

    items = bak.list_backups(str(backup_dir))
    names = [i["name"] for i in items]
    assert new_name in names
    assert old_name in names

    removed = bak.cleanup_old_backups(str(backup_dir), retention_days=30)
    assert removed == 1
    remaining = [i["name"] for i in bak.list_backups(str(backup_dir))]
    assert new_name in remaining
    assert old_name not in remaining


def test_run_backup_sqlite_and_notify_on_failure(sqlite_env, monkeypatch):
    captured = []

    class _FakeMgr:
        def send_backup_failed(self, detail):
            captured.append(detail)
            return True

    monkeypatch.setattr(
        "src.core.alerting.get_alert_manager", lambda: _FakeMgr(), raising=False
    )
    # 让 run_backup 走 sqlite 路径: 绕过 dialect(可能 import 过真实库)
    monkeypatch.setattr(bak, "_db_url", lambda: f"sqlite:///{sqlite_env['db_path']}")

    result = bak.run_backup(backup_dir=str(sqlite_env["data_dir"] / "backups"))
    assert result.ok is True, result.error
    assert result.path.endswith(".sql.gz")
    assert os.path.isfile(result.path)
    assert result.dialect == "sqlite"
    assert bak.verify_gzip_file(result.path) is True


def test_run_backup_failure_triggers_alert(sqlite_env, monkeypatch):
    captured = []

    class _FakeMgr:
        def send_backup_failed(self, detail):
            captured.append(detail)
            return True

    monkeypatch.setattr(
        "src.core.alerting.get_alert_manager", lambda: _FakeMgr(), raising=False
    )
    monkeypatch.setattr(bak, "_db_url", lambda: "sqlite:///nonexistent/nope.db")
    # 指向不存在的库, 且不让 dialect 回落成功
    monkeypatch.setattr(
        bak,
        "_dump_sqlite",
        lambda out_path, url: (_ for _ in ()).throw(RuntimeError("db missing")),
    )

    result = bak.run_backup(backup_dir=str(sqlite_env["data_dir"] / "backups"))
    assert result.ok is False
    assert result.error
    assert captured, "备份失败应触发告警"


def test_restore_roundtrip_sqlite(sqlite_env, monkeypatch):
    monkeypatch.setattr(bak, "_db_url", lambda: f"sqlite:///{sqlite_env['db_path']}")
    backup_dir = str(sqlite_env["data_dir"] / "backups")

    # 备份
    result = bak.run_backup(backup_dir=backup_dir, notify_on_failure=False)
    assert result.ok is True, result.error

    # 改库内容
    conn = sqlite3.connect(str(sqlite_env["db_path"]))
    conn.execute("INSERT INTO t (v) VALUES ('after-backup')")
    conn.commit()
    conn.close()

    # 恢复(会先做 pre-backup)
    r = bak.restore_backup(result.path, pre_backup=True)
    assert r["ok"] is True, r.get("error")

    conn = sqlite3.connect(str(sqlite_env["db_path"]))
    rows = conn.execute("SELECT v FROM t ORDER BY id").fetchall()
    conn.close()
    values = [x[0] for x in rows]
    assert "hello" in values
    assert "after-backup" not in values


def test_restore_missing_file_fails():
    r = bak.restore_backup("/no/such/backup.sql.gz")
    assert r["ok"] is False
    assert "不存在" in r["error"]


def test_get_backup_dir_uses_data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    assert bak.get_backup_dir() == os.path.join(str(tmp_path), "backups")
