"""参谋回答落库与依据行对账的收尾（chat.py 有 2310 行硬护栏，逻辑收到这里）。

三件事本来都散在 chat.py 的两个端点里（流式/非流式各一份）：
1. 保存助手消息：`run_id` 必须经服务端校验（`verified_current_run_id`），不是客户端给的；
2. 依据行对账：只能在**生成时**算（需要本轮工具返回原文），历史消息无法重算；
3. 助手消息的对外形状：只在这里拼一次，避免"加一个字段要改三处、漏一处前端就少渲染"。

对账用 ContextVar 按请求传递（生成器里 `record*()`，端点落库时 `take()`，取一次即清空），
并把**被对账正文的指纹**一起存下：零工具拒答会把正文整段换掉、断流可能只留半截，落库时指纹
对不上就丢弃 —— 绝不给用户没见过的正文贴一块"依据行"。
"""
from __future__ import annotations

import hashlib
import json
import logging
from contextvars import ContextVar

from src.core.evidence_row_audit import evidence_section, rows_from_audit
from src.web.api.copilot_message_link import verified_current_run_id

logger = logging.getLogger(__name__)

_PENDING: ContextVar[dict | None] = ContextVar("copilot_evidence_row_audit", default=None)


def _fingerprint(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


def _stage(text: str, audit_result: dict) -> None:
    try:
        section = evidence_section(text or "")
        if section is None:
            _PENDING.set(None)
            return
        rows, truncated = rows_from_audit(audit_result or {}, section)
        if not rows:
            _PENDING.set(None)
            return
        _PENDING.set({
            "basis": "number_containment",
            "rows": rows,
            "checked": len({c for r in rows for c in r["claims"]}),
            "unverified_total": sum(len(r["unverified"]) for r in rows),
            "truncated": truncated,
            "source_sha256": _fingerprint(text),
        })
    except Exception as e:  # noqa: BLE001 - 对账是旁路，绝不让它毁掉回答
        logger.debug("依据行对账计算失败(忽略): %s", e)
        _PENDING.set(None)


def record_with_audit(text: str, audit_result: dict) -> str:
    """用**已经算好的**整段 audit 结论派生行级结论；原样返回 text（不再付 O(池²) 代价）。"""
    _stage(text, audit_result)
    return text


def record(text: str, evidence: str, question: str = "") -> str:
    """便利入口：没有现成 audit 结果时自己算一次（测试/离线用）。"""
    try:
        from src.core.number_audit import audit as _audit_numbers

        result = _audit_numbers(text or "", evidence or "", question or "")
    except Exception as e:  # noqa: BLE001
        logger.debug("依据行对账计算失败(忽略): %s", e)
        _PENDING.set(None)
        return text
    _stage(text, result)
    return text


def take(content: str | None = None) -> str | None:
    """取出本轮对账并清空。

    - 没有可展示的行 ⇒ None（NULL 表示"未计算"，不是"零问题"）；
    - 给了 `content` 且与被对账正文不一致（拒答替换/断流半截）⇒ 丢弃，不贴到不相干的正文上。
    """
    value = _PENDING.get()
    _PENDING.set(None)
    if not value or not value.get("rows"):
        return None
    if content is not None and value.get("source_sha256") != _fingerprint(content):
        logger.debug("对账对应的正文与落库正文不一致，丢弃对账结果")
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


def to_payload(msg) -> dict:
    """助手消息的对外形状（历史接口与两个 done 事件共用，加字段只改这里）。"""
    return {
        "id": msg.id,
        "role": msg.role,
        "content": msg.content,
        "created_at": str(msg.created_at or ""),
        "run_id": msg.run_id,
        "evidence_audit": load(msg.evidence_audit),
    }


def save_assistant_message(db, conversation_id: int, user, content: str):
    """落库助手消息：run_id 走服务端校验，对账结果取本轮 ContextVar（正文不一致则丢弃）。"""
    from src.db.models import ChatMessage

    msg = ChatMessage(
        conversation_id=conversation_id,
        role="assistant",
        content=content,
        run_id=verified_current_run_id(db, user, conversation_id),
        evidence_audit=take(content),
    )
    db.add(msg)
    return msg
