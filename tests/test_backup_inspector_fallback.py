# -*- coding: utf-8 -*-
"""KI-014: pg_dump 缺失时 inspector 降级快照。"""
from unittest.mock import MagicMock, patch

from src.db import backup


def test_inspector_fallback_when_no_pg_dump(tmp_path, monkeypatch):
    monkeypatch.setattr(backup.shutil, "which", lambda name: None)
    # 伪 dialect: is_postgres True, DB_PATH 落 tmp
    fake_dialect = MagicMock()
    fake_dialect.is_postgres.return_value = True
    fake_dialect.DB_PATH = str(tmp_path / "x.db")
    monkeypatch.setattr("src.db.dialect.is_postgres", lambda: True)
    monkeypatch.setattr("src.db.dialect.DB_PATH", str(tmp_path / "x.db"))
    monkeypatch.setattr("src.db.dialect.DB_URL", "postgresql+psycopg2://u:p@h:5432/db")

    fake_insp = MagicMock()
    fake_insp.get_table_names.return_value = ["stocks", "klines"]
    fake_insp.get_columns.side_effect = lambda t: [
        {"name": "id", "type": "INTEGER"},
        {"name": "symbol", "type": "VARCHAR"},
    ]
    fake_engine = MagicMock()

    with patch("src.db.session.engine", fake_engine), \
         patch("sqlalchemy.inspect", return_value=fake_insp):
        backup.backup_pg_schema_before_migration()

    files = list((tmp_path / "migrations_backup").glob("schema_inspect_*.txt"))
    assert len(files) == 1
    text = files[0].read_text(encoding="utf-8")
    assert "TABLE stocks" in text
    assert "TABLE klines" in text
