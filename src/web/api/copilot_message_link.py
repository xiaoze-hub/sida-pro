"""会话消息的轨迹关联校验。"""
import logging
from sqlalchemy import or_
from sqlalchemy.exc import SQLAlchemyError
from src.core.llm_context import current
from src.db.models import ChatConversation, CopilotRun

logger = logging.getLogger(__name__)


def bind_run_conversation(db, run_id, user_id, conversation_id):
    """原子绑定已有 run；只允许同用户的会话，且不能改写既有关联。"""
    if not run_id or not user_id or conversation_id is None:
        return False
    uid = str(user_id)
    own_conversation = db.query(ChatConversation.id).filter(
        ChatConversation.id == conversation_id, ChatConversation.user_id == uid,
    ).exists()
    changed = db.query(CopilotRun).filter(
        CopilotRun.run_id == run_id, CopilotRun.user_id == uid,
        or_(CopilotRun.conversation_id.is_(None), CopilotRun.conversation_id == conversation_id),
        own_conversation,
    ).update({CopilotRun.conversation_id: conversation_id}, synchronize_session=False)
    db.commit()
    return changed == 1


def verified_current_run_id(db, user, conversation_id):
    ctx = current()
    uid = str(getattr(user, 'id', '') or '')
    rid = ctx.get('run_id')
    if not uid or not rid or ctx.get('user_id') != uid or conversation_id is None:
        return None
    try:
        # 查询锁定主库上的本轮行，避免读副本延迟误判；失败仅丢关联，不丢回答。
        with db.begin_nested():
            row = db.query(CopilotRun.run_id).filter(
                CopilotRun.run_id == rid,
                CopilotRun.user_id == uid,
                CopilotRun.conversation_id == conversation_id,
            ).with_for_update().first()
        return row[0] if row else None
    except SQLAlchemyError:
        logger.warning('验证消息轨迹关联失败，回答仍可保存', exc_info=True)
        return None
