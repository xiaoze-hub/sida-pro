"""只允许本机隔离 PG 测试库，验证关联与迁移；禁止对生产库执行。"""
import uuid

import pytest
from sqlalchemy import text
from src.core import llm_context
from src.db.models import ChatConversation, CopilotRun, User
from src.web.database import SessionLocal, engine
from src.web.api.copilot_message_link import bind_run_conversation, verified_current_run_id
from src.web.migrations import _m234_chat_message_run_id
from types import SimpleNamespace


def _guard():
    url = engine.url
    if url.get_backend_name() != 'postgresql' or url.host != '127.0.0.1' or url.database != 'sida_test' or url.username != 'sida_test':
        pytest.skip('仅在本机隔离的 sida_test PG 测试库执行')


def test_pg_migration_preserves_legacy_and_is_idempotent():
    _guard()
    with engine.begin() as c:
        c.execute(text('ALTER TABLE chat_messages DROP COLUMN run_id'))
        c.execute(text("INSERT INTO chat_messages (conversation_id, role, content) VALUES (99999999, 'assistant', 'legacy')"))
        _m234_chat_message_run_id(c)
        _m234_chat_message_run_id(c)
        assert c.execute(text("SELECT run_id FROM chat_messages WHERE content='legacy' AND conversation_id=99999999")).scalar_one() is None
        c.execute(text("DELETE FROM chat_messages WHERE content='legacy' AND conversation_id=99999999"))


def test_pg_lookup_error_does_not_abort_message_transaction(monkeypatch):
    _guard()
    db = SessionLocal()
    token = llm_context.bind_chat(user_id='u1', run_id='missing')
    try:
        def broken_query(*args, **kwargs):
            db.execute(text('SELECT 1 / 0'))
        monkeypatch.setattr(db, 'query', broken_query)
        assert verified_current_run_id(db, SimpleNamespace(id='u1'), 12) is None
        assert db.execute(text('SELECT 1')).scalar_one() == 1
    finally:
        llm_context.unbind(token)
        db.close()


def test_pg_background_new_conversation_links_message_to_run(monkeypatch):
    _guard()
    import asyncio
    from src.db.models import ChatMessage
    from src.web.api import chat as chat_api, copilot_jobs
    uid, rid = str(uuid.uuid4()), str(uuid.uuid4())
    db = SessionLocal()
    try:
        db.add(User(id=uid, username='job-'+uid, password_hash='x'))
        db.add(CopilotRun(run_id=rid, user_id=uid, conversation_id=None, status='queued', question='hello'))
        db.commit()
    finally:
        db.close()

    async def messages(*args, **kwargs):
        return []

    async def loop(*args, **kwargs):
        yield 'text', 'test answer'

    monkeypatch.setattr(chat_api, 'enforce_copilot_quota', lambda *_: None)
    monkeypatch.setattr(chat_api, '_build_ai_messages', messages)
    monkeypatch.setattr(chat_api, '_get_ai_client', lambda *a, **kw: object())
    monkeypatch.setattr(chat_api, '_run_tool_loop_inner', loop)
    monkeypatch.setattr(chat_api.gap_collector, 'spawn', lambda *a, **kw: None)
    cid = asyncio.run(copilot_jobs.run_copilot_question(user_id=uid, conversation_id=None, question='hello', run_id=rid))
    db = SessionLocal()
    try:
        row = db.query(ChatMessage).filter_by(conversation_id=cid, role='assistant').one()
        assert row.run_id == rid
        assert db.query(CopilotRun).filter_by(run_id=rid).one().conversation_id == cid
    finally:
        db.close()


def test_pg_concurrent_bind_has_exactly_one_winner():
    _guard()
    import threading
    from concurrent.futures import ThreadPoolExecutor
    uid, rid = str(uuid.uuid4()), str(uuid.uuid4())
    db = SessionLocal()
    try:
        db.add(User(id=uid, username='race-'+uid, password_hash='x'))
        db.flush()
        a, b = ChatConversation(user_id=uid), ChatConversation(user_id=uid)
        db.add_all([a, b]); db.flush()
        ids = [a.id, b.id]
        db.add(CopilotRun(run_id=rid, user_id=uid, conversation_id=None, status='queued'))
        db.commit()
    finally:
        db.close()
    barrier = threading.Barrier(2)
    def claim(cid):
        session = SessionLocal()
        try:
            barrier.wait(timeout=10)
            return bind_run_conversation(session, rid, uid, cid)
        finally:
            session.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, ids))
    assert sorted(results) == [False, True]
    db = SessionLocal()
    try:
        assert db.query(CopilotRun).filter_by(run_id=rid).one().conversation_id == ids[results.index(True)]
    finally:
        db.close()


def test_pg_owner_and_conversation_match_for_real_rows():
    _guard()
    uid, other = str(uuid.uuid4()), str(uuid.uuid4())
    rid = str(uuid.uuid4())
    db = SessionLocal()
    try:
        db.add_all([User(id=uid, username='link-'+uid, password_hash='x'), User(id=other, username='link-'+other, password_hash='x')])
        db.flush()
        a, b = ChatConversation(user_id=uid), ChatConversation(user_id=other)
        db.add_all([a, b]); db.flush()
        db.add(CopilotRun(run_id=rid, user_id=uid, conversation_id=None, status='queued'))
        db.commit()
        assert not bind_run_conversation(db, rid, uid, b.id)
        assert bind_run_conversation(db, rid, uid, a.id)
        token = llm_context.bind_chat(user_id=uid, run_id=rid)
        try:
            assert verified_current_run_id(db, SimpleNamespace(id=uid), a.id) == rid
            assert verified_current_run_id(db, SimpleNamespace(id=other), a.id) is None
            assert verified_current_run_id(db, SimpleNamespace(id=uid), b.id) is None
        finally:
            llm_context.unbind(token)
    finally:
        db.close()
