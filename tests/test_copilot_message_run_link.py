"""消息关联仅接受同用户、同会话、确实存在的 run。"""
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.core import llm_context
from src.db.models import CopilotRun
from src.web.database import Base
from src.web.api.copilot_message_link import verified_current_run_id


def test_chat_message_schema_can_persist_run_link():
    from src.db.models import ChatMessage
    assert 'run_id' in ChatMessage.__table__.c
    assert ChatMessage.__table__.c.run_id.nullable is True


def test_bind_run_conversation_only_for_owner_and_never_overwrites():
    from src.db.models import ChatConversation
    from src.web.api.copilot_message_link import bind_run_conversation
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    try:
        db.add_all([
            ChatConversation(id=12, user_id='u1'),
            ChatConversation(id=13, user_id='u1'),
            ChatConversation(id=14, user_id='u2'),
            CopilotRun(run_id='queued', user_id='u1', conversation_id=None, status='queued'),
            CopilotRun(run_id='foreign', user_id='u2', conversation_id=None, status='queued'),
            CopilotRun(run_id='bound', user_id='u1', conversation_id=99, status='queued'),
        ])
        db.commit()
        assert bind_run_conversation(db, 'queued', 'u1', 12) is True
        assert bind_run_conversation(db, 'queued', 'u1', 12) is True
        assert bind_run_conversation(db, 'queued', 'u1', 13) is False
        assert bind_run_conversation(db, 'queued', 'u1', 14) is False
        assert bind_run_conversation(db, 'foreign', 'u1', 12) is False
        assert bind_run_conversation(db, 'bound', 'u1', 12) is False
        assert bind_run_conversation(db, 'missing', 'u1', 12) is False
        assert db.query(CopilotRun).filter_by(run_id='foreign').one().conversation_id is None
    finally:
        db.close()
        engine.dispose()


def test_current_run_must_exist_and_match_owner_and_conversation():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    try:
        db.add_all([
            CopilotRun(run_id='own', user_id='u1', conversation_id=12, status='ok'),
            CopilotRun(run_id='other-user', user_id='u2', conversation_id=12, status='ok'),
            CopilotRun(run_id='other-conv', user_id='u1', conversation_id=13, status='ok'),
            CopilotRun(run_id='unowned', user_id=None, conversation_id=12, status='ok'),
        ])
        db.commit()
        for rid, expected in [('own', 'own'), ('other-user', None), ('other-conv', None), ('unowned', None), ('missing', None)]:
            token = llm_context.bind_chat(user_id='u1', run_id=rid)
            try:
                assert verified_current_run_id(db, SimpleNamespace(id='u1'), 12) == expected
                assert verified_current_run_id(db, SimpleNamespace(id='u2'), 12) is None
            finally:
                llm_context.unbind(token)
    finally:
        db.close()
        engine.dispose()
