"""参谋回答落库与依据行对账的收尾（chat.py 有 2310 行硬护栏，逻辑收到这里）。

两件事本来都散在 chat.py 的两个端点里（流式/非流式各一份）：
1. 保存助手消息：`run_id` 必须经服务端校验（`verified_current_run_id`），不是客户端给的；
2. 依据行数字对账：只能在**生成时**算（需要本轮工具返回原文），历史消息无法重算。

因此对账结果用手写三次就够的 ContextVar 传递：生成器里 `record()`，端点落库时 `take()`。
同一次请求同一条 task，不会串到别的用户/会话。
"""
from __future__ import annotations

import json
import logging
from contextvars import ContextVar

from src.core.evidence_row_audit import audit_rows
from src.web.api.copilot_message_link import verified_current_run_id

logger = logging.getLogger(__name__)

_PENDING: ContextVar[dict | None] = ContextVar("copilot_evidence_row_audit", default=None)


def record(text: str, evidence: str, question: str = "") -> str:
    """旁路计算行级对账；**原样返回 text**（不改正文，失败也不影响回答）。"""
    try:
        _PENDING.set(audit_rows(text, evidence or "", question or ""))
    except Exception as e:  # noqa: BLE001 - 对账是旁路，绝不让它毁掉回答
        logger.debug("依据行对账计算失败(忽略): %s", e)
    return text


def take() -> str | None:
    """取出本轮对账并清空。没有可展示的行 ⇒ None（NULL 表示"未计算"，不是"零问题"）。"""
    value = _PENDING.get()
    _PENDING.set(None)
    if not value or not value.get("rows"):
        return None
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return None


def load(raw: str | None) -> dict | None:
    """历史消息：只认能解析的 JSON；解析不了当未计算，不猜。"""
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def save_assistant_message(db, conversation_id: int, user, content: str):
    """落库助手消息：run_id 走服务端校验，对账结果取本轮 ContextVar。"""
    from src.db.models import ChatMessage

    msg = ChatMessage(
        conversation_id=conversation_id,
        role="assistant",
        content=content,
        run_id=verified_current_run_id(db, user, conversation_id),
        evidence_audit=take(),
    )
    db.add(msg)
    return msg
