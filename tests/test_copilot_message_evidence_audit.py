"""依据行对账的请求内传递与落库：没有取数原文就不落库，历史 NULL 表示未计算。"""
import json
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.core import llm_context
from src.db.models import ChatMessage, ChatConversation, CopilotRun
from src.web.database import Base
from src.web.api.copilot_message_save import load, record, save_assistant_message, take

ANSWER = """## 【依据】
- 现价 9.65 元
- 主力净流入 1.23 亿
"""
EVIDENCE = "002361 现价 9.65 成交额 163278.31万元"


def test_schema_has_nullable_evidence_audit_column():
    assert "evidence_audit" in ChatMessage.__table__.c
    assert ChatMessage.__table__.c.evidence_audit.nullable is True


def test_record_returns_text_unchanged_and_take_clears():
    assert record(ANSWER, EVIDENCE, "") == ANSWER, "对账只旁路计算，绝不改正文"
    raw = take()
    assert raw and json.loads(raw)["unverified_total"] == 1
    assert take() is None, "取过一次必须清空，否则会串到下一条消息"


def test_nothing_to_show_is_not_persisted_as_zero_problems():
    record("## 【依据】\n- 该股走弱，资金分歧", EVIDENCE, "")
    assert take() is None, "没有可展示的行 ⇒ 落 NULL(未计算)，不许写 0"


def test_record_failure_never_breaks_the_answer(monkeypatch):
    import src.web.api.copilot_message_save as mod

    def boom(*a, **k):
        raise RuntimeError("audit exploded")

    monkeypatch.setattr(mod, "audit_rows", boom)
    assert record("正文照旧", EVIDENCE, "") == "正文照旧"
    assert take() is None


def test_load_tolerates_garbage_and_absence():
    assert load(None) is None
    assert load("") is None
    assert load("{not json") is None
    assert load('["a list"]') is None
    assert load('{"rows": []}') == {"rows": []}


def _db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine)()


def test_save_assistant_message_persists_verified_run_and_audit():
    engine, db = _db()
    try:
        db.add_all([
            ChatConversation(id=21, user_id="u1"),
            CopilotRun(run_id="own", user_id="u1", conversation_id=21, status="ok"),
        ])
        db.commit()
        user = SimpleNamespace(id="u1")
        record(ANSWER, EVIDENCE, "")
        token = llm_context.bind_chat(user_id="u1", run_id="own")
        try:
            msg = save_assistant_message(db, 21, user, ANSWER)
        finally:
            llm_context.unbind(token)
        db.commit()
        db.refresh(msg)
        assert msg.run_id == "own"
        assert msg.evidence_audit and json.loads(msg.evidence_audit)["unverified_total"] == 1
        assert take() is None
    finally:
        db.close()
        engine.dispose()


def test_save_without_audit_leaves_null_instead_of_empty_json():
    engine, db = _db()
    try:
        db.add(ChatConversation(id=22, user_id="u1"))
        db.commit()
        msg = save_assistant_message(db, 22, SimpleNamespace(id="u1"), "无取数的回答")
        db.commit()
        assert msg.evidence_audit is None
        # 会话不属于该用户 ⇒ run_id 不落（服务端校验，不猜）
        assert msg.run_id is None
    finally:
        db.close()
        engine.dispose()
