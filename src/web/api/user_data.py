"""用户数据管理 API(GDPR, P3 2026-09-18): 导出 / 删除。

路由(挂 /api/user 前缀, 全部需登录 JWT):
- GET  /api/user/data/export          导出当前用户全部数据为 JSON 下载
- POST /api/user/data/delete          请求删除用户数据(软删除, 需密码确认)
- GET  /api/user/data/delete/status   查询删除状态与剩余宽限期

删除语义:
1. 软删除: users.is_deleted=True + deleted_at=now, 账号立即无法登录/调 API
2. 宽限期 30 天: 期间可通过管理员恢复(误操作兜底); 30 天后由
   purge_expired_deletions() 物理清除用户行及关联数据(FK CASCADE + 显式清理)
3. 需密码确认: 防止会话被劫持后一键注销

导出范围(只含本人数据, user_id 隔离):
- user: 账号基本信息
- watchlist: 自选列表
- positions: 持仓
- api_keys: Skill API Key(不含明文/hash)
- usage: Skill 调用计量
- notifications: 站内通知
- paper_trading: 模拟盘账户/持仓/成交
- chat_conversations: 对话会话标题与消息
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from src.db.models import (
    ChatConversation,
    ChatMessage,
    Notification,
    PaperTradingAccount,
    PaperTradingPosition,
    PaperTradingTrade,
    Position,
    SkillApiKey,
    SkillUsage,
    Stock,
    StockSuggestion,
    User,
)
from src.web.api.auth import get_current_user, verify_password
from src.web.database import get_db

logger = logging.getLogger(__name__)
router = APIRouter(tags=["user-data"])

# GDPR 宽限期: 软删除后 30 天可反悔, 期满物理清除
DELETE_GRACE_DAYS = 30


class DeleteRequest(BaseModel):
    """请求删除用户数据。password 必须为当前账号密码。"""

    password: str = Field(..., min_length=1, description="当前账号密码(确认身份)")
    confirm: bool = Field(False, description="须显式 true, 防误触")


def _iso(dt) -> str | None:
    return dt.isoformat() if dt else None


def _collect_user_payload(db: Session, user: User) -> dict:
    """组装当前用户全部可导出数据(只读本人, 不触碰他人行)。"""
    uid = user.id

    watchlist = (
        db.query(Stock).filter(Stock.user_id == uid).order_by(Stock.sort_order.asc()).all()
    )
    positions = (
        db.query(Position).filter(Position.user_id == uid).order_by(Position.id.asc()).all()
    )
    api_keys = (
        db.query(SkillApiKey)
        .filter(SkillApiKey.user_id == uid)
        .order_by(SkillApiKey.created_at.desc())
        .all()
    )
    usages = (
        db.query(SkillUsage)
        .filter(SkillUsage.user_id == uid)
        .order_by(SkillUsage.created_at.desc())
        .limit(5000)
        .all()
    )
    notifications = (
        db.query(Notification)
        .filter(Notification.user_id == uid)
        .order_by(Notification.created_at.desc())
        .limit(2000)
        .all()
    )
    suggestions = (
        db.query(StockSuggestion)
        .filter(StockSuggestion.user_id == uid)
        .order_by(StockSuggestion.created_at.desc())
        .limit(1000)
        .all()
    )
    paper_accounts = (
        db.query(PaperTradingAccount).filter(PaperTradingAccount.user_id == uid).all()
    )
    paper_positions = (
        db.query(PaperTradingPosition)
        .filter(PaperTradingPosition.user_id == uid)
        .all()
    )
    paper_trades = (
        db.query(PaperTradingTrade).filter(PaperTradingTrade.user_id == uid).all()
    )
    conversations = (
        db.query(ChatConversation)
        .filter(ChatConversation.user_id == uid)
        .order_by(ChatConversation.updated_at.desc())
        .all()
    )
    conv_ids = [c.id for c in conversations]
    messages = (
        db.query(ChatMessage)
        .filter(ChatMessage.conversation_id.in_(conv_ids))
        .order_by(ChatMessage.created_at.asc())
        .all()
        if conv_ids
        else []
    )

    return {
        "export_meta": {
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "source": "sida-user-data-export",
            "schema_version": 1,
        },
        "user": {
            "id": user.id,
            "username": user.username,
            "email": user.email,
            "nickname": user.nickname,
            "role": user.role,
            "is_active": user.is_active,
            "provider": user.provider,
            "created_at": _iso(user.created_at),
            "updated_at": _iso(user.updated_at),
            # 安全: 导出不含 password_hash / avatar 二进制 / permissions 细节
        },
        "watchlist": [
            {
                "symbol": s.symbol,
                "name": s.name,
                "market": s.market,
                "sort_order": s.sort_order,
                "created_at": _iso(s.created_at),
            }
            for s in watchlist
        ],
        "positions": [
            {
                "stock_id": p.stock_id,
                "account_id": p.account_id,
                "cost_price": p.cost_price,
                "quantity": p.quantity,
                "invested_amount": p.invested_amount,
                "trading_style": p.trading_style,
                "created_at": _iso(p.created_at),
            }
            for p in positions
        ],
        "api_keys": [
            {
                # 导出不含 key_hash; prefix 仅供辨识
                "key_prefix": k.key_prefix,
                "owner_label": k.owner_label,
                "tier": k.tier,
                "status": k.status,
                "daily_limit": k.daily_limit,
                "created_at": _iso(k.created_at),
                "last_used_at": _iso(k.last_used_at),
                "expires_at": _iso(k.expires_at),
            }
            for k in api_keys
        ],
        "usage": [
            {
                "skill_name": u.skill_name,
                "status_code": u.status_code,
                "duration_ms": u.duration_ms,
                "input_tokens": u.input_tokens,
                "output_tokens": u.output_tokens,
                "channel": u.channel,
                "created_at": _iso(u.created_at),
            }
            for u in usages
        ],
        "notifications": [
            {
                "category": n.category,
                "level": n.level,
                "title": n.title,
                "body": n.body,
                "source": n.source,
                "read_at": _iso(n.read_at),
                "created_at": _iso(n.created_at),
            }
            for n in notifications
        ],
        "suggestions": [
            {
                "stock_symbol": s.stock_symbol,
                "stock_market": s.stock_market,
                "action": s.action,
                "action_label": s.action_label,
                "signal": s.signal,
                "reason": s.reason,
                "agent_name": s.agent_name,
                "created_at": _iso(s.created_at),
            }
            for s in suggestions
        ],
        "paper_trading": {
            "accounts": [
                {
                    "initial_capital": a.initial_capital,
                    "current_capital": a.current_capital,
                    "total_pnl": a.total_pnl,
                    "total_trades": a.total_trades,
                    "created_at": _iso(a.created_at),
                }
                for a in paper_accounts
            ],
            "positions": [
                {
                    "stock_symbol": p.stock_symbol,
                    "stock_market": p.stock_market,
                    "quantity": p.quantity,
                    "entry_price": p.entry_price,
                    "created_at": _iso(p.created_at),
                }
                for p in paper_positions
            ],
            "trades": [
                {
                    "stock_symbol": getattr(t, "stock_symbol", ""),
                    "stock_market": getattr(t, "stock_market", ""),
                    "side": getattr(t, "side", ""),
                    "price": getattr(t, "price", None),
                    "quantity": getattr(t, "quantity", None),
                    "created_at": _iso(getattr(t, "created_at", None)),
                }
                for t in paper_trades
            ],
        },
        "chat_conversations": [
            {
                "title": c.title,
                "stock_symbol": c.stock_symbol,
                "stock_market": c.stock_market,
                "created_at": _iso(c.created_at),
                "messages": [
                    {"role": m.role, "content": m.content, "created_at": _iso(m.created_at)}
                    for m in messages
                    if m.conversation_id == c.id
                ],
            }
            for c in conversations
        ],
    }


@router.get("/data/export")
def export_user_data(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """导出当前用户全部数据(JSON 下载)。"""
    payload = _collect_user_payload(db, user)
    filename = f"sida-user-data-{user.username}-{datetime.now().strftime('%Y%m%d%H%M%S')}.json"
    body = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    return JSONResponse(
        content=json.loads(body),
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


@router.post("/data/delete")
def request_data_delete(
    data: DeleteRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """请求删除用户数据(软删除)。

    - 需密码确认
    - 立即: is_deleted=True, 登录被拒
    - 30 天后: purge_expired_deletions() 物理清除
    """
    if not data.confirm:
        raise HTTPException(400, "请显式确认删除(confirm=true)")
    if not user.password_hash or not verify_password(data.password, user.password_hash):
        raise HTTPException(401, "密码错误, 无法确认身份")
    if getattr(user, "is_deleted", False):
        raise HTTPException(409, "账号已处于删除申请状态")

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    user.is_deleted = True
    user.deleted_at = now
    user.is_active = False
    # 踢下线: token_version 递增使存量 JWT 全部失效
    user.token_version = int(user.token_version or 1) + 1
    db.commit()

    try:
        from src.web.api.audit import log_audit

        log_audit(db, user, "export", detail="用户申请删除数据(软删除)", ip="")
    except Exception:  # noqa: BLE001
        pass

    purge_at = now + timedelta(days=DELETE_GRACE_DAYS)
    logger.info("用户 %s 申请数据删除, 预计 %s 物理清除", user.id[:8], purge_at.date())
    return {
        "status": "pending",
        "message": f"删除申请已受理, {DELETE_GRACE_DAYS} 天后物理清除; 期间账号无法登录",
        "deleted_at": now.isoformat(),
        "purge_after": purge_at.isoformat(),
        "grace_days": DELETE_GRACE_DAYS,
    }


@router.get("/data/delete/status")
def data_delete_status(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """查询当前账号删除状态。

    注: 软删除后 get_current_user 已拒 403; 此端点主要服务
    「尚未删除」状态展示与管理员排查。删除后应由管理员侧查询。
    """
    is_deleted = bool(getattr(user, "is_deleted", False))
    deleted_at = getattr(user, "deleted_at", None)
    if not is_deleted:
        return {"status": "none", "is_deleted": False, "message": "账号未申请删除"}
    purge_after = None
    days_left = None
    if deleted_at is not None:
        # deleted_at 存 naive UTC; 与当前 UTC 比较
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        purge_dt = deleted_at + timedelta(days=DELETE_GRACE_DAYS)
        purge_after = purge_dt.isoformat()
        days_left = max(0, (purge_dt - now).days)
    return {
        "status": "pending" if (days_left is None or days_left > 0) else "expired",
        "is_deleted": True,
        "deleted_at": _iso(deleted_at),
        "purge_after": purge_after,
        "days_left": days_left,
        "message": (
            f"删除申请处理中, 约 {days_left} 天后物理清除"
            if days_left is not None and days_left > 0
            else "宽限期已满, 等待物理清除"
        ),
    }


def purge_expired_deletions(db: Session | None = None) -> dict:
    """物理清除软删除满 30 天的用户及其关联数据。

    由调度任务(每日)调用; 返回 {"purged": n}。永不抛异常。
    """
    from src.web.database import SessionLocal as _SL

    own_session = db is None
    session = db or _SL()
    purged = 0
    try:
        cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
            days=DELETE_GRACE_DAYS
        )
        expired = (
            session.query(User)
            .filter(User.is_deleted.is_(True), User.deleted_at.isnot(None), User.deleted_at <= cutoff)
            .all()
        )
        for u in expired:
            uid = u.id
            try:
                # 显式清理无 FK 或 FK 为 SET NULL 的关联表
                session.query(SkillUsage).filter(SkillUsage.user_id == uid).delete(
                    synchronize_session=False
                )
                session.query(SkillApiKey).filter(SkillApiKey.user_id == uid).delete(
                    synchronize_session=False
                )
                session.query(Notification).filter(Notification.user_id == uid).delete(
                    synchronize_session=False
                )
                session.query(StockSuggestion).filter(
                    StockSuggestion.user_id == uid
                ).delete(synchronize_session=False)
                session.query(PaperTradingPosition).filter(
                    PaperTradingPosition.user_id == uid
                ).delete(synchronize_session=False)
                session.query(PaperTradingTrade).filter(
                    PaperTradingTrade.user_id == uid
                ).delete(synchronize_session=False)
                session.query(PaperTradingAccount).filter(
                    PaperTradingAccount.user_id == uid
                ).delete(synchronize_session=False)
                # chats: 先消息后会话
                conv_ids = [
                    row.id
                    for row in session.query(ChatConversation.id).filter(
                        ChatConversation.user_id == uid
                    )
                ]
                if conv_ids:
                    session.query(ChatMessage).filter(
                        ChatMessage.conversation_id.in_(conv_ids)
                    ).delete(synchronize_session=False)
                    session.query(ChatConversation).filter(
                        ChatConversation.user_id == uid
                    ).delete(synchronize_session=False)
                # 其余 user_id FK CASCADE 表(自选/持仓/分析历史等)随 user 行删除
                session.delete(u)
                session.commit()
                purged += 1
                logger.info("物理清除已注销用户 %s", uid[:8])
            except Exception as e:  # noqa: BLE001
                session.rollback()
                logger.warning("物理清除用户 %s 失败: %s", str(uid)[:8], e)
    except Exception as e:  # noqa: BLE001
        logger.warning("用户数据物理清除任务失败: %s", e)
    finally:
        if own_session:
            session.close()
    return {"purged": purged}
