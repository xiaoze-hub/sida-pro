"""站内消息中心 API。"""

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from src.core.notify_center import push_notification
from src.core.timezone import beijing_now_naive
from src.web.api.auth import get_current_user
from src.web.database import get_db
from src.web.models import AgentRun, Notification, NotifyChannel, User

router = APIRouter()


class NotificationOut(BaseModel):
    id: int
    category: str
    level: str
    title: str
    body: str = ""
    link: str = ""
    source: str = ""
    trace_id: str = ""
    push_status: str = ""
    push_error: str = ""
    push_channels: list[dict] = Field(default_factory=list)
    read: bool = False
    created_at: str = ""


class AgentRunDetail(BaseModel):
    status: str = ""
    result: str = ""
    error: str = ""
    duration_ms: int = 0
    model_label: str = ""
    trigger_source: str = ""
    created_at: str = ""


class NotificationDetailOut(NotificationOut):
    task: AgentRunDetail | None = None


def _normalize_link(link: str | None) -> str:
    """兼容旧版个股通知链接。

    前端从未提供 ``/stocks`` 路由，该链接会只显示应用外壳。保留原有
    query string 并转到持仓页，使数据库中已存的通知也能正常打开。
    """
    value = str(link or "")
    if value == "/stocks" or value.startswith("/stocks?"):
        return f"/portfolio{value[len('/stocks') :]}"
    return value


def _run_status(run: AgentRun | None) -> str:
    if not run:
        return ""
    result = str(run.result or "")
    if run.status == "success" and "跳过" in result and "交易时段" in result:
        return "skipped"
    return str(run.status or "")


def _to_out(n: Notification, run: AgentRun | None = None) -> NotificationOut:
    title = n.title or ""
    body = n.body or ""
    level = n.level or "info"
    if _run_status(run) == "skipped":
        title = title.replace("✅ ", "⏭️ ", 1).replace(" 已完成", " 已跳过", 1)
        body = str(run.result or body)
        level = "warning"
    return NotificationOut(
        id=n.id,
        category=n.category or "system",
        level=level,
        title=title,
        body=body,
        link=_normalize_link(n.link),
        source=n.source or "",
        trace_id=n.trace_id or "",
        push_status=n.push_status or "",
        push_error=n.push_error or "",
        push_channels=list(n.push_channels or []),
        read=n.read_at is not None,
        created_at=n.created_at.isoformat() if n.created_at else "",
    )


def _configured_channels(db: Session) -> list[dict]:
    """只向前端暴露安全的渠道标识，不返回 config 中的任何密钥。"""
    rows = (
        db.query(NotifyChannel)
        .filter(NotifyChannel.enabled.is_(True))
        .order_by(NotifyChannel.id.asc())
        .all()
    )
    return [
        {"id": int(row.id), "name": str(row.name or row.type or "未命名渠道"), "type": str(row.type or "")}
        for row in rows
    ]


@router.get("/unread-count")
def unread_count(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """S4(2026-08-23): 红点计数按用户过滤, 避免账号间串号。"""
    n = (
        db.query(Notification)
        .filter(Notification.read_at.is_(None), Notification.user_id == user.id)
        .count()
    )
    return {"unread": n}


@router.get("")
def list_notifications(
    limit: int = Query(30, ge=1, le=200),
    only_unread: bool = Query(False),
    category: str | None = Query(None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """S4(2026-08-23): 通知中心按用户过滤, 多账号各自只看自己的通知。"""
    q = db.query(Notification).filter(Notification.user_id == user.id)
    if only_unread:
        q = q.filter(Notification.read_at.is_(None))
    if category:
        q = q.filter(Notification.category == category)
    rows = q.order_by(Notification.id.desc()).limit(limit).all()
    trace_ids = [str(row.trace_id) for row in rows if row.trace_id]
    run_by_trace: dict[str, AgentRun] = {}
    if trace_ids:
        runs = (
            db.query(AgentRun)
            .filter(AgentRun.trace_id.in_(trace_ids))
            .order_by(AgentRun.id.desc())
            .all()
        )
        for run in runs:
            run_by_trace.setdefault(str(run.trace_id or ""), run)
    unread = (
        db.query(Notification)
        .filter(Notification.read_at.is_(None), Notification.user_id == user.id)
        .count()
    )
    return {
        "items": [
            _to_out(row, run_by_trace.get(str(row.trace_id or ""))).model_dump()
            for row in rows
        ],
        "unread": unread,
        "configured_channels": _configured_channels(db),
    }


@router.get("/{nid}", response_model=NotificationDetailOut)
def get_notification_detail(
    nid: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """S4(2026-08-23): 仅返回当前用户的通知详情; 否则 404 防账号探测。"""
    notification = (
        db.query(Notification)
        .filter(Notification.id == nid, Notification.user_id == user.id)
        .first()
    )
    if not notification:
        raise HTTPException(status_code=404, detail="通知不存在")

    run = None
    if notification.trace_id:
        run = (
            db.query(AgentRun)
            .filter(AgentRun.trace_id == notification.trace_id)
            .order_by(AgentRun.id.desc())
            .first()
        )

    payload = _to_out(notification, run).model_dump()
    payload["task"] = (
        AgentRunDetail(
            status=_run_status(run),
            result=run.result or "",
            error=run.error or "",
            duration_ms=int(run.duration_ms or 0),
            model_label=run.model_label or "",
            trigger_source=run.trigger_source or "",
            created_at=run.created_at.isoformat() if run.created_at else "",
        ).model_dump()
        if run
        else None
    )
    return payload


@router.post("/{nid}/read")
def mark_read(
    nid: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """S4(2026-08-23): 仅允许当前用户标记自己的通知为已读; 否则返回 ok=False 防账号探测。"""
    n = (
        db.query(Notification)
        .filter(Notification.id == nid, Notification.user_id == user.id)
        .first()
    )
    if not n:
        return {"ok": False, "error": "not found"}
    if n.read_at is None:
        n.read_at = beijing_now_naive()
        db.commit()
    return {"ok": True}


@router.post("/read-all")
def mark_all_read(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """S4(2026-08-23): 仅标记当前用户的未读通知, 不影响其他账号。"""
    now = beijing_now_naive()
    cnt = (
        db.query(Notification)
        .filter(Notification.read_at.is_(None), Notification.user_id == user.id)
        .update({Notification.read_at: now}, synchronize_session=False)
    )
    db.commit()
    return {"ok": True, "marked": cnt}


@router.delete("/clear")
def clear_read(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """S4(2026-08-23): 仅清空当前用户已读通知, 不影响其他账号。"""
    cnt = (
        db.query(Notification)
        .filter(Notification.read_at.isnot(None), Notification.user_id == user.id)
        .delete(synchronize_session=False)
    )
    db.commit()
    return {"ok": True, "deleted": cnt}


@router.post("/test")
def send_test(db: Session = Depends(get_db)):
    """自检: 写一条站内通知并尝试外发, 返回外发状态便于排查渠道。"""
    nid = push_notification(
        "🔔 通知中心测试",
        "这是一条测试消息。若 push_status=skipped 说明未配置外发渠道（站内仍可见）。",
        category="system",
        level="success",
        source="manual_test",
    )
    n = db.query(Notification).filter(Notification.id == nid).first()
    return {
        "ok": True,
        "id": nid,
        "push_status": n.push_status if n else "",
        "push_error": n.push_error if n else "",
    }
