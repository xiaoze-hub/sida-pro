"""只在本机隔离 PG 测试库验证 v235：加列幂等、旧行留 NULL、数据类型为 text。"""
import pytest
from sqlalchemy import text

from src.web.database import engine
from src.web.migrations import _m235_chat_message_evidence_audit


def _guard():
    url = engine.url
    if (
        url.get_backend_name() != "postgresql"
        or url.host != "127.0.0.1"
        or url.database != "sida_test"
        or url.username != "sida_test"
    ):
        pytest.skip("仅在本机隔离的 sida_test PG 测试库执行")


def test_pg_migration_adds_nullable_text_and_keeps_legacy_null():
    _guard()
    with engine.begin() as c:
        c.execute(text("ALTER TABLE chat_messages DROP COLUMN IF EXISTS evidence_audit"))
        c.execute(text(
            "INSERT INTO chat_messages (conversation_id, role, content) "
            "VALUES (99999998, 'assistant', 'legacy-no-audit')"
        ))
        _m235_chat_message_evidence_audit(c)
        # 幂等：重复执行不报错
        _m235_chat_message_evidence_audit(c)
        col = c.execute(text(
            "SELECT data_type, is_nullable FROM information_schema.columns "
            "WHERE table_name = 'chat_messages' AND column_name = 'evidence_audit'"
        )).one()
        assert col == ("text", "YES")
        # 旧消息不回填：历史行必须仍是 NULL（不是 '{}'、不是空串）
        legacy = c.execute(text(
            "SELECT evidence_audit FROM chat_messages WHERE conversation_id = 99999998"
        )).one()[0]
        assert legacy is None
        c.execute(text("DELETE FROM chat_messages WHERE conversation_id = 99999998"))


def test_pg_can_store_and_read_audit_json():
    _guard()
    import json

    with engine.begin() as c:
        _m235_chat_message_evidence_audit(c)
        payload = json.dumps({"basis": "number_containment", "rows": [], "checked": 0,
                              "unverified_total": 0, "reason": "no_numeric_rows"},
                             ensure_ascii=False)
        c.execute(text(
            "INSERT INTO chat_messages (conversation_id, role, content, evidence_audit) "
            "VALUES (99999997, 'assistant', 'with-audit', :p)"
        ), {"p": payload})
        got = c.execute(text(
            "SELECT evidence_audit FROM chat_messages WHERE conversation_id = 99999997"
        )).one()[0]
        assert json.loads(got)["basis"] == "number_containment"
        c.execute(text("DELETE FROM chat_messages WHERE conversation_id = 99999997"))
