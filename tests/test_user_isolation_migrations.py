"""Migrations v122 / v123 (2026-08-23): user_id 列添加 + 存量回填 + 索引

覆盖:
- v122: chat_conversations / notifications / stock_suggestions / analysis_history 加 user_id 索引
- v123: price_alert_rules 加 user_id 列 + 索引
- 幂等: 重复运行不报错, 不重复加列
- 存量回填: 旧数据归最早 owner 用户
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from src.web.database import SessionLocal, engine
from src.web.migrations import (
    MIGRATIONS,
    _m122_user_id_columns_chat_notif_suggestion,
    _m123_user_id_on_price_alerts,
)


def _has_column(conn, table: str, column: str) -> bool:
    rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
    for r in rows:
        if len(r) > 1 and str(r[1]) == column:
            return True
    return False


def _has_index(conn, table: str, index_name: str) -> bool:
    rows = conn.execute(
        text("SELECT name FROM sqlite_master WHERE type='index' AND name=:n"),
        {"n": index_name},
    ).fetchall()
    return len(rows) > 0


def _owner_id(conn) -> str | None:
    row = conn.execute(
        text(
            "SELECT id FROM users WHERE role='owner' AND is_active=1 "
            "ORDER BY created_at ASC, id ASC LIMIT 1"
        )
    ).first()
    return str(row[0]) if row else None


class TestMigrationV122V123:
    """v122/v123: user_id 列添加 + 索引 + 回填"""

    def test_chat_conv_has_user_id_column(self):
        """chat_conversations 应该有 user_id 列"""
        with engine.begin() as conn:
            _m122_user_id_columns_chat_notif_suggestion(conn)
            assert _has_column(conn, "chat_conversations", "user_id")

    def test_notifications_has_user_id_column(self):
        with engine.begin() as conn:
            _m122_user_id_columns_chat_notif_suggestion(conn)
            assert _has_column(conn, "notifications", "user_id")

    def test_stock_suggestions_has_user_id_column(self):
        with engine.begin() as conn:
            _m122_user_id_columns_chat_notif_suggestion(conn)
            assert _has_column(conn, "stock_suggestions", "user_id")

    def test_price_alert_rules_has_user_id_column(self):
        with engine.begin() as conn:
            _m123_user_id_on_price_alerts(conn)
            assert _has_column(conn, "price_alert_rules", "user_id")

    def test_chat_conv_index_created(self):
        with engine.begin() as conn:
            _m122_user_id_columns_chat_notif_suggestion(conn)
            assert _has_index(conn, "chat_conversations", "ix_chat_conv_user")

    def test_notifications_index_created(self):
        with engine.begin() as conn:
            _m122_user_id_columns_chat_notif_suggestion(conn)
            assert _has_index(conn, "notifications", "ix_notifications_user")

    def test_stock_suggestions_index_created(self):
        with engine.begin() as conn:
            _m122_user_id_columns_chat_notif_suggestion(conn)
            assert _has_index(conn, "stock_suggestions", "ix_stock_suggestions_user")

    def test_price_alert_index_created(self):
        with engine.begin() as conn:
            _m123_user_id_on_price_alerts(conn)
            assert _has_index(conn, "price_alert_rules", "ix_price_alert_user")

    def test_analysis_history_index_created(self):
        with engine.begin() as conn:
            _m122_user_id_columns_chat_notif_suggestion(conn)
            assert _has_index(conn, "analysis_history", "ix_analysis_history_user_date")

    def test_idempotent(self):
        """重复运行迁移, 不应抛错"""
        with engine.begin() as conn:
            _m122_user_id_columns_chat_notif_suggestion(conn)
            # 再跑一次
            _m122_user_id_columns_chat_notif_suggestion(conn)
            _m123_user_id_on_price_alerts(conn)
            _m123_user_id_on_price_alerts(conn)
        # 不抛错即通过

    def test_backfill_to_owner(self):
        """存量行(user_id=NULL)应被回填到最早 owner"""
        # 先插入一行 user_id=NULL 的 chat_conversation
        db = SessionLocal()
        try:
            owner_id = _owner_id(db)
            if owner_id is None:
                pytest.skip("无 owner 用户, 跳过回填测试")

            # 清理之前的测试数据
            db.execute(text("DELETE FROM chat_conversations WHERE title = 'iso_backfill_test'"))
            db.execute(
                text(
                    "INSERT INTO chat_conversations (title, stock_symbol, stock_market, user_id, created_at, updated_at) "
                    "VALUES ('iso_backfill_test', NULL, NULL, NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                )
            )
            db.commit()
            null_id = db.execute(
                text("SELECT id FROM chat_conversations WHERE title = 'iso_backfill_test'")
            ).scalar()

            # 运行迁移
            with engine.begin() as conn:
                _m122_user_id_columns_chat_notif_suggestion(conn)

            # 验证: 该行的 user_id 已变为 owner_id
            db2 = SessionLocal()
            try:
                row = db2.execute(
                    text("SELECT user_id FROM chat_conversations WHERE id = :id"),
                    {"id": null_id},
                ).first()
                assert row is not None
                assert str(row[0]) == owner_id, f"期望回填到 {owner_id}, 实际 {row[0]}"
            finally:
                db2.close()
        finally:
            db.execute(text("DELETE FROM chat_conversations WHERE title = 'iso_backfill_test'"))
            db.commit()
            db.close()

    def test_migrations_registered(self):
        """v122/v123 应该在 MIGRATIONS 元组里注册"""
        versions = [m.version for m in MIGRATIONS]
        assert 122 in versions
        assert 123 in versions
