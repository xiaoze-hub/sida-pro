"""依据行对账的请求内传递与落库：没有取数原文就不落库，历史 NULL 表示未计算。"""
import json
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.core import llm_context
from src.db.models import ChatMessage, ChatConversation, CopilotRun
from src.web.database import Base
from src.web.api.copilot_message_save import (
    load, record, record_with_audit, save_assistant_message, take,
)

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
    """对账炸了只能丢对账，正文必须原样返回。"""
    import src.web.api.copilot_message_save as mod

    def boom(*a, **k):
        raise RuntimeError("audit exploded")

    monkeypatch.setattr(mod, "rows_from_audit", boom)
    assert record(ANSWER, EVIDENCE, "") == ANSWER
    assert take(ANSWER) is None


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


def test_record_with_audit_reuses_supplied_result_instead_of_recomputing(monkeypatch):
    """生产路径已有整段 audit 结果：再算一遍就是白付一次 O(池²)。"""
    import src.core.number_audit as na

    def boom(*a, **k):
        raise AssertionError("不该重复计算整段 audit")

    monkeypatch.setattr(na, "audit", boom)
    result = {"checked": 1, "unverified": ["1.23亿"], "ratio": 1.0, "severe": True}
    assert record_with_audit(ANSWER, result) == ANSWER
    raw = take(ANSWER)
    assert raw and json.loads(raw)["unverified_total"] == 1


def test_audit_is_dropped_when_saved_text_differs_from_audited_text():
    """零工具拒答会把正文整段换掉：对账结果不许贴到用户没见过的正文上。"""
    record(ANSWER, EVIDENCE, "")
    assert take("拒答：问具体标的但没有取到数据。") is None


def test_audit_is_kept_when_text_matches():
    record(ANSWER, EVIDENCE, "")
    raw = take(ANSWER)
    assert raw and json.loads(raw)["rows"]
    # 取过一次就清空，不会落到下一条消息
    assert take(ANSWER) is None


def test_to_payload_shape_is_shared_by_history_and_done():
    engine, db = _db()
    try:
        db.add(ChatConversation(id=23, user_id="u1"))
        db.commit()
        msg = save_assistant_message(db, 23, SimpleNamespace(id="u1"), "无取数回答")
        db.commit()
        db.refresh(msg)
        from src.web.api.copilot_message_save import to_payload

        payload = to_payload(msg)
        assert set(payload) == {"id", "role", "content", "created_at", "run_id", "evidence_audit"}
        assert payload["role"] == "assistant" and payload["evidence_audit"] is None
    finally:
        db.close()
        engine.dispose()
