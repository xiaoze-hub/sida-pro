"""数智参谋的长期记忆（2026-10-21, P0）。

为什么需要：会话上下文只有 20 条 + 滚动摘要，换个会话就忘了"只做主板"、
忘了"资金流别用东财方向位回答方向"。Hermes 有 MEMORY.md / USER.md，参谋此前没有。

设计取克制路线（不做心理画像）：
- **只在用户明确表达时记**："记住 / 以后都 / 每次 / 我的口径是 / 别再" 这类触发词；
- 每条截断 500 字，不存原始对话；
- 可停用、可删除，UI 一条一条管；
- 注入时只取 pinned(weight>=9) + 最近命中的 6 条，防止把 system prompt 撑爆。

召回不做向量检索（资源紧张且量小）：pinned 优先 + 关键词 LIKE + 最近使用。
"""
from __future__ import annotations

import logging
import re
from datetime import datetime

logger = logging.getLogger(__name__)

KINDS = ("preference", "rule", "fact", "skill")
MAX_CONTENT = 500
MAX_INJECT = 6
PIN_WEIGHT = 9

# 只有这些明确说法才自动记 —— 宁可少记，也不要从闲聊里"推断"偏好
_TRIGGERS = (
    "记住", "记一下", "帮我记", "以后", "今后",
    "每次都", "一直用", "我的偏好", "我的口径", "按我的", "别再", "不要再",
)
# 光有"以后"不够——"以后行情会怎么走"是提问不是指令。必须再带一个祈使/约束词。
_DIRECTIVE = ("别", "不要", "都用", "只用", "只做", "只买", "按", "每次", "一直", "必须", "记得")
_STRIP = re.compile(r"^(请|麻烦|帮我)?(记住|记一下|帮我记一下)[：:，,\s]*")


def _clip(text: str | None) -> str:
    s = (text or "").strip()
    return s if len(s) <= MAX_CONTENT else s[:MAX_CONTENT] + "…"


def extract_candidates(text: str) -> list[tuple[str, str]]:
    """从一句用户话里提取可记忆的条目：[(kind, content)]。

    纯规则，不调模型（省 token，也避免把 AI 的转述当用户原意）。
    """
    t = (text or "").strip()
    if not t or len(t) < 4:
        return []
    if not any(k in t for k in _TRIGGERS):
        return []
    body = _STRIP.sub("", t).strip()
    explicit = any(k in t for k in ("记住", "记一下", "帮我记", "我的偏好", "我的口径"))
    if not explicit and not any(k in body for k in _DIRECTIVE):
        return []
    if len(body) < 4 or len(body) > 200:
        # 太长的一段话通常是在讨论行情，不是在给指令 —— 不记
        return []
    kind = "rule" if any(k in t for k in ("以后", "每次", "一直", "别再", "不要")) else "preference"
    return [(kind, _clip(body))]


def remember(db, user_id, *, kind: str = "preference", content: str,
             source: str = "user", weight: int = 5, run_id: str | None = None) -> dict | None:
    """写一条记忆（同用户同类同内容去重）。失败返回 None，绝不抛。"""
    from src.db.models import CopilotMemory

    uid = str(user_id or "")
    content = _clip(content)
    if not uid or not content:
        return None
    if kind not in KINDS:
        kind = "preference"
    try:
        exists = (
            db.query(CopilotMemory)
            .filter(CopilotMemory.user_id == uid, CopilotMemory.kind == kind,
                    CopilotMemory.content == content)
            .one_or_none()
        )
        if exists:
            exists.enabled = True
            exists.weight = max(int(exists.weight or 5), int(weight))
            db.commit()
            return _row_dict(exists)
        row = CopilotMemory(
            user_id=uid, kind=kind, content=content, source=source,
            weight=max(0, min(10, int(weight))), run_id=run_id,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return _row_dict(row)
    except Exception as e:  # noqa: BLE001 - 记忆是旁路
        logger.warning("[copilot-memory] 写入失败: %s", e)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return None


def recall(db, user_id, *, query: str | None = None, limit: int = MAX_INJECT) -> list[dict]:
    """取要注入的记忆：pinned 优先，再按相关/最近。异常返回 []。"""
    from src.db.models import CopilotMemory

    uid = str(user_id or "")
    if not uid:
        return []
    try:
        q = db.query(CopilotMemory).filter(
            CopilotMemory.user_id == uid, CopilotMemory.enabled.is_(True)
        )
        rows = q.order_by(CopilotMemory.weight.desc(), CopilotMemory.id.desc()).limit(60).all()
        if not rows:
            return []
        pinned = [r for r in rows if (r.weight or 0) >= PIN_WEIGHT]
        rest = [r for r in rows if (r.weight or 0) < PIN_WEIGHT]

        picked: list = list(pinned[:limit])
        remaining = limit - len(picked)
        if remaining > 0 and rest:
            q2 = (query or "").strip()
            if q2:
                # 关键词命中优先（中文不做分词：用 2~4 字滑窗任一片段命中即算相关）
                grams = {q2[i:i + 2] for i in range(len(q2) - 1)} | {q2}
                hit = [r for r in rest if any(g and g in (r.content or "") for g in grams)]
                miss = [r for r in rest if r not in hit]
                rest = hit + miss
            picked.extend(rest[:remaining])

        out = [_row_dict(r) for r in picked]
        for r in picked:
            r.hits = int(r.hits or 0) + 1
            r.last_used_at = datetime.utcnow()
        try:
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
        return out
    except Exception as e:  # noqa: BLE001
        logger.warning("[copilot-memory] 召回失败: %s", e)
        return []


def format_block(items: list[dict]) -> str:
    """把记忆拼成可注入 system prompt 的一段。空 → 空串（不占位）。"""
    if not items:
        return ""
    lines = ["【用户长期偏好（来自本人历史确认，优先遵守；与本次要求冲突时以本次为准）】"]
    for it in items:
        tag = {"rule": "规则", "skill": "流程", "fact": "事实"}.get(it.get("kind"), "偏好")
        lines.append(f"- [{tag}] {it.get('content')}")
    return "\n".join(lines)


def memory_block(db, user_id, question: str | None = None, *, limit: int = MAX_INJECT) -> str:
    """一步到位：自动捕获本次提问里的明确指令 + 召回已有记忆 → 注入文本。

    所有异常都吞掉（返回空串）—— 记忆是旁路，绝不能因为记忆失败把问答搞挂。
    """
    try:
        uid = str(user_id or "")
        if not uid:
            return ""
        for kind, content in extract_candidates(question or ""):
            remember(db, uid, kind=kind, content=content, source="auto")
        return format_block(recall(db, uid, query=question, limit=limit))
    except Exception as e:  # noqa: BLE001
        logger.warning("[copilot-memory] 注入失败(忽略): %s", e)
        return ""


def _row_dict(r) -> dict:
    return {
        "id": r.id, "kind": r.kind, "content": r.content, "weight": r.weight,
        "source": r.source, "enabled": bool(r.enabled), "hits": int(r.hits or 0),
        "created_at": str(r.created_at or ""),
        "last_used_at": str(r.last_used_at or ""),
    }


def list_all(db, user_id, *, include_disabled: bool = False) -> list[dict]:
    from src.db.models import CopilotMemory

    try:
        q = db.query(CopilotMemory).filter(CopilotMemory.user_id == str(user_id))
        if not include_disabled:
            q = q.filter(CopilotMemory.enabled.is_(True))
        return [_row_dict(r) for r in q.order_by(CopilotMemory.weight.desc(),
                                                 CopilotMemory.id.desc()).limit(200).all()]
    except Exception as e:  # noqa: BLE001
        logger.warning("[copilot-memory] 读取失败: %s", e)
        return []


def set_enabled(db, user_id, memory_id: int, enabled: bool) -> bool:
    from src.db.models import CopilotMemory

    row = (
        db.query(CopilotMemory)
        .filter(CopilotMemory.id == memory_id, CopilotMemory.user_id == str(user_id))
        .one_or_none()
    )
    if row is None:
        return False
    row.enabled = bool(enabled)
    db.commit()
    return True


def update(db, user_id, memory_id: int, *, content: str | None = None,
           weight: int | None = None, enabled: bool | None = None) -> dict | None:
    """改一条记忆（内容/权重/启停，任选其一或多项）。

    2026-10-22 修：面板支持「行内编辑内容/权重」，但后端原先只有 `set_enabled`，
    于是编辑看起来很成功（前端乐观更新）却**从没落库**，刷新就回滚 —— 静默丢数据。
    返回更新后的行（dict），不存在/不属于本人 → None。
    """
    from src.db.models import CopilotMemory

    row = (
        db.query(CopilotMemory)
        .filter(CopilotMemory.id == memory_id, CopilotMemory.user_id == str(user_id))
        .one_or_none()
    )
    if row is None:
        return None
    if content is not None:
        cleaned = _clip(content)
        if not cleaned:
            raise ValueError("内容不能为空")
        row.content = cleaned
    if weight is not None:
        row.weight = max(1, min(10, int(weight)))
    if enabled is not None:
        row.enabled = bool(enabled)
    db.commit()
    db.refresh(row)
    return _row_dict(row)


def delete(db, user_id, memory_id: int) -> bool:
    from src.db.models import CopilotMemory

    row = (
        db.query(CopilotMemory)
        .filter(CopilotMemory.id == memory_id, CopilotMemory.user_id == str(user_id))
        .one_or_none()
    )
    if row is None:
        return False
    db.delete(row)
    db.commit()
    return True
