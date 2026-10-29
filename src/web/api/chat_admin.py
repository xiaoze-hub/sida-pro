"""会话管理接口(2026-10-22): 重命名 / 置顶 / 搜索 / 删单条消息 / 会话轨迹。

为什么单独开文件: `src/web/api/chat.py` 已到 2300 行护栏上限(见
tests/test_w33_chat_registry.py::test_chat_py_line_shrink), 本次只加不改 ——
新端点一律放这里, 由 `src/web/app.py` 以 prefix="/api/chat" 注册。

权限口径(与既有 chat 端点一致, 见 S2 隔离约定):
- 一切读/写都按 `conversation.user_id == 当前用户` 过滤; 不存在与"他人会话"
  统一 404(不泄露资源存在性, 防账号探测)。
- 搜索先按 user_id 收敛到本人会话, 再在标题/消息正文里模糊匹配 ——
  绝不跨账号命中。
- 删单条消息: 先校验会话归属, 再校验消息属于该会话(两步都 404)。

路由顺序注意(app.py 里 chat_admin 必须挂在 chat 之前):
`GET /chat/conversations` 既有列表端点在 chat.py 里, 本模块的搜索端点复用
同一路径(多一个 q 参数)。FastAPI 按注册顺序匹配, 后注册的 search 端点会被
旧的列表端点遮住 —— 故 app.py 先挂 chat_admin。搜索端点返回**超集**字段
(含旧端点原有的 stock_market/created_at), 前端旧调用不受影响。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from src.web.api.auth import get_current_user
from src.web.database import get_db
from src.web.models import (
    ChatConversation,
    ChatMessage,
    CopilotRun,
    User,
)

logger = logging.getLogger(__name__)
router = APIRouter()

# 重命名长度上限(字符, 非字节)
TITLE_MAX_LEN = 80
# 搜索关键词上限(防止超长 like 拖垮库)
QUERY_MAX_LEN = 100


class UpdateConversationBody(BaseModel):
    """重命名 / 置顶请求体。两个字段都可选, 至少给一个。"""

    title: str | None = None
    pinned: bool | None = None


def _like_pattern(raw: str) -> str:
    """把用户输入转成 LIKE pattern, 转义 % _ \\ 通配符。

    不转义的话, 用户搜 "50%" 会匹配全部会话 —— 看着像 bug, 实际是注入。
    """
    escaped = (
        raw.replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )
    return f"%{escaped}%"


def _conversation_dict(conv: ChatConversation, message_count: int = 0) -> dict[str, Any]:
    """会话列表/详情的统一序列化(字段名与既有 chat 端点保持兼容)。"""
    return {
        "id": conv.id,
        "title": conv.title or "",
        "stock_symbol": conv.stock_symbol,
        "stock_market": conv.stock_market,
        "pinned": bool(conv.pinned),
        "created_at": str(conv.created_at or ""),
        "updated_at": str(conv.updated_at or ""),
        "message_count": int(message_count),
    }


@router.patch("/conversations/{conversation_id}")
def update_conversation(
    conversation_id: int,
    body: UpdateConversationBody,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """重命名 / 置顶会话。

    - 标题: 去首尾空白后必须 1~80 字; 空串或超长 → 400。
    - 置顶: pinned 布尔, 与标题互不影响, 可只传其一。
    - 非本人会话 → 404(防账号探测, 与既有 chat 端点一致)。
    """
    conv = (
        db.query(ChatConversation)
        .filter(
            ChatConversation.id == conversation_id,
            ChatConversation.user_id == user.id,
        )
        .first()
    )
    if not conv:
        raise HTTPException(404, "对话不存在")

    new_title: str | None = None
    if body.title is not None:
        new_title = body.title.strip()
        if not new_title:
            raise HTTPException(400, "标题不能为空")
        if len(new_title) > TITLE_MAX_LEN:
            raise HTTPException(400, f"标题最长 {TITLE_MAX_LEN} 字")

    if new_title is None and body.pinned is None:
        raise HTTPException(400, "未提供要更新的字段(title / pinned)")

    if new_title is not None:
        conv.title = new_title
    if body.pinned is not None:
        conv.pinned = bool(body.pinned)

    db.commit()
    db.refresh(conv)

    count = (
        db.query(func.count(ChatMessage.id))
        .filter(ChatMessage.conversation_id == conv.id)
        .scalar()
    )
    return _conversation_dict(conv, int(count or 0))


@router.get("/conversations")
def search_conversations(
    q: str = Query("", max_length=QUERY_MAX_LEN),
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[dict[str, Any]]:
    """搜索本人会话: 标题或消息正文模糊匹配(ilike)。

    ⚠️ 路径是 `/conversations/search` 而不是 `/conversations` —— 后者与 chat.py 的列表端点
    同路径同方法，FastAPI 只按注册顺序取第一个命中，**谁先注册谁是 winner**（实测 openapi 里
    显示的是后注册的那个，与实际路由不一致）。独立路径没有这种歧义。

    - q 为空 → 等价于"列出我的会话"(按 pinned desc, updated_at desc)。
    - 命中消息正文时, 该会话也算命中。
    - 排序: pinned desc, updated_at desc —— 置顶的永远在前。
    """
    query = db.query(ChatConversation).filter(ChatConversation.user_id == user.id)

    keyword = (q or "").strip()
    if keyword:
        pattern = _like_pattern(keyword)
        # 命中消息正文的会话。子查询 join 回 chat_conversations 并按 user_id 收敛 ——
        # 否则会全表扫所有人的消息再靠外层过滤(慢, 且命中集合依赖全库)。
        msg_conv_ids = (
            db.query(ChatMessage.conversation_id)
            .join(ChatConversation, ChatMessage.conversation_id == ChatConversation.id)
            .filter(
                ChatConversation.user_id == user.id,
                ChatMessage.content.ilike(pattern, escape="\\"),
            )
        )
        query = query.filter(
            or_(
                ChatConversation.title.ilike(pattern, escape="\\"),
                ChatConversation.id.in_(msg_conv_ids),
            )
        )

    rows = (
        query.order_by(
            ChatConversation.pinned.desc(),
            ChatConversation.updated_at.desc(),
            ChatConversation.id.desc(),
        )
        .limit(limit)
        .all()
    )

    # 消息数: 一次 GROUP BY 拿全部, 不做 N+1
    counts: dict[int, int] = {}
    ids = [c.id for c in rows]
    if ids:
        for cid, cnt in (
            db.query(ChatMessage.conversation_id, func.count(ChatMessage.id))
            .filter(ChatMessage.conversation_id.in_(ids))
            .group_by(ChatMessage.conversation_id)
            .all()
        ):
            counts[int(cid)] = int(cnt or 0)

    return [_conversation_dict(c, counts.get(c.id, 0)) for c in rows]


@router.delete("/conversations/{conversation_id}/messages/{message_id}")
def delete_conversation_message(
    conversation_id: int,
    message_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """删单条消息(会话级: 只能删自己会话里的消息)。

    两步校验: ① 会话属于当前用户; ② 消息属于该会话。任一不满足 → 404。
    """
    conv = (
        db.query(ChatConversation)
        .filter(
            ChatConversation.id == conversation_id,
            ChatConversation.user_id == user.id,
        )
        .first()
    )
    if not conv:
        raise HTTPException(404, "对话不存在")

    msg = (
        db.query(ChatMessage)
        .filter(
            ChatMessage.id == message_id,
            ChatMessage.conversation_id == conversation_id,
        )
        .first()
    )
    if not msg:
        raise HTTPException(404, "消息不存在")

    db.delete(msg)
    db.commit()
    return {"ok": True, "deleted_id": message_id}


@router.get("/conversations/{conversation_id}/runs")
def list_conversation_runs(
    conversation_id: int,
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[dict[str, Any]]:
    """该会话的问答轨迹(供前端画工具时间线)。

    数据来自 copilot_runs(与 /api/copilot/runs?conversation_id= 同一张表),
    但这里先做会话归属校验 —— 他人会话一律 404。
    """
    conv = (
        db.query(ChatConversation)
        .filter(
            ChatConversation.id == conversation_id,
            ChatConversation.user_id == user.id,
        )
        .first()
    )
    if not conv:
        raise HTTPException(404, "对话不存在")

    rows = db.query(CopilotRun).filter(  # scoped-check: allow — conversation_id 只来自上面已校验归属的 ChatConversation(他人会话已 404), 本查询按该 id 取 run
        CopilotRun.conversation_id == conversation_id
    ).order_by(
        CopilotRun.created_at.desc(), CopilotRun.id.desc()
    ).limit(limit).all()
    return [
        {
            "run_id": r.run_id,
            "recipe": r.recipe,
            "status": r.status,
            "tool_calls": r.tool_calls,
            "elapsed_ms": r.elapsed_ms,
            "created_at": str(r.created_at or ""),
        }
        for r in rows
    ]
