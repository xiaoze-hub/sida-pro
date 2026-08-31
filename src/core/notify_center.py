"""站内消息中心 + 外发推送联动。

设计要点(踩坑固化):
- 站内写入永远不能因为外发失败而丢失 → 先落库, 再推送, 推送结果回写 push_status。
- 后台线程调用 → 自带 session 生命周期, 不复用请求 session。
- 无渠道不是错误, 是 push_status='skipped'(站内仍可见), 避免"沉默失败"。
"""

from __future__ import annotations

import asyncio
import logging

from src.web.database import SessionLocal
from src.web.models import Notification, NotifyChannel, User

logger = logging.getLogger(__name__)

# 已启用外发渠道时，站内通知同步外发，避免 info 只出现在右上角。
_PUSH_LEVELS = {"info", "success", "warning", "error"}


def _build_notifier(user_id: str | None = None):
    """从 DB 启用渠道构建 NotifierManager，并返回不含密钥的渠道快照。

    多用户(2026-08-10): user_id 指定 → 该用户渠道 + 全局共享(NULL);
    不指定 → 只推全局渠道(NULL)。
    """
    from src.core.notifier import NotifierManager
    from sqlalchemy import or_ as _or_

    db = SessionLocal()
    try:
        q = db.query(NotifyChannel).filter(NotifyChannel.enabled.is_(True))
        if user_id is not None:
            q = q.filter(_or_(NotifyChannel.user_id == user_id, NotifyChannel.user_id.is_(None)))
        else:
            q = q.filter(NotifyChannel.user_id.is_(None))
        channels = q.all()
        if not channels:
            return None, []
        mgr = NotifierManager()
        ok = 0
        records: list[dict] = []
        for ch in channels:
            record = {
                "id": int(ch.id),
                "name": str(ch.name or ch.type or "未命名渠道"),
                "type": str(ch.type or ""),
                "status": "pending",
                "error": "",
            }
            try:
                if mgr.add_channel(ch.type, ch.config or {}):
                    ok += 1
            except Exception as e:
                record["status"] = "failed"
                record["error"] = str(e)[:400]
                logger.warning("[通知中心] 渠道 %s 初始化失败: %s", ch.type, e)
            records.append(record)
        return (mgr if ok else None), records
    except Exception as e:
        logger.warning("[通知中心] 读取渠道失败: %s", e)
        return None, []
    finally:
        db.close()


def push_notification(
    title: str,
    body: str = "",
    *,
    category: str = "system",
    level: str = "info",
    link: str = "",
    source: str = "",
    trace_id: str = "",
    also_push: bool | None = None,
    user_id: str | None = None,
) -> int | None:
    """写一条站内通知, 并按级别决定是否外发。返回 notification id。

    多用户(2026-08-10): user_id 指定 → 推该用户渠道+全局; None → 只推全局渠道。
    绝不抛异常 —— 通知失败不能拖垮业务主流程。
    """
    nid = None
    db = SessionLocal()
    try:
        n = Notification(
            user_id=user_id,
            category=category,
            level=level,
            title=title[:200],
            body=(body or "")[:4000],
            link=link,
            source=source,
            trace_id=trace_id,
            push_status="pending",
        )
        db.add(n)
        db.commit()
        db.refresh(n)
        nid = n.id
    except Exception as e:
        logger.exception("[通知中心] 站内写入失败: %s", e)
        db.rollback()
        db.close()
        return None
    finally:
        try:
            db.close()
        except Exception:
            pass

    do_push = also_push if also_push is not None else (level in _PUSH_LEVELS)
    if not do_push:
        _set_push_status(nid, "skipped", "级别不外发")
        return nid

    mgr, channel_records = _build_notifier(user_id=user_id)
    if mgr is None:
        init_errors = [str(item.get("error") or "") for item in channel_records if item.get("status") == "failed"]
        if channel_records:
            _set_push_status(
                nid,
                "failed",
                "; ".join(err for err in init_errors if err)[:400] or "通知渠道初始化失败",
                channels=channel_records,
            )
        else:
            # 显式状态, 不静默 —— 前端能看到"站内已记录, 未配置外发渠道"
            _set_push_status(nid, "skipped", "未配置通知渠道", channels=[])
        return nid

    try:
        result = asyncio.run(mgr.notify_with_result(title, body or title))
        final_channels = _finalize_channel_records(channel_records, result)
        final_status, final_error = _push_result_status(result, final_channels)
        _set_push_status(nid, final_status, final_error, channels=final_channels)
    except RuntimeError:
        # 已在事件循环里(异步上下文) → 交给调用方的 loop
        try:
            loop = asyncio.get_event_loop()
            loop.create_task(_async_push(nid, mgr, title, body or title, channel_records))
            _set_push_status(nid, "pending", "异步发送中", channels=channel_records)
        except Exception as e:
            _set_push_status(nid, "failed", str(e)[:400], channels=_finalize_channel_records(channel_records, {"success": False, "error": str(e)}))
    except Exception as e:
        logger.warning("[通知中心] 外发失败: %s", e)
        _set_push_status(nid, "failed", str(e)[:400], channels=_finalize_channel_records(channel_records, {"success": False, "error": str(e)}))
    return nid


async def push_notification_async(title: str, body: str = "", **kw) -> int | None:
    """异步上下文里安全调用(把阻塞部分丢线程池)。"""
    return await asyncio.to_thread(push_notification, title, body, **kw)


async def _async_push(nid: int, mgr, title: str, body: str, channel_records: list[dict]) -> None:
    try:
        result = await mgr.notify_with_result(title, body)
        final_channels = _finalize_channel_records(channel_records, result)
        final_status, final_error = _push_result_status(result, final_channels)
        _set_push_status(nid, final_status, final_error, channels=final_channels)
    except Exception as e:
        _set_push_status(
            nid,
            "failed",
            str(e)[:400],
            channels=_finalize_channel_records(channel_records, {"success": False, "error": str(e)}),
        )


def _finalize_channel_records(records: list[dict], result: dict) -> list[dict]:
    """将 NotifierManager 回执映射到配置渠道，只保留可展示字段。"""
    result_items = list(result.get("channels") or [])
    exact: dict[str, list[dict]] = {}
    apprise_result = None
    for item in result_items:
        item_type = str(item.get("type") or "")
        if item_type == "apprise":
            apprise_result = item
        else:
            exact.setdefault(item_type, []).append(item)

    finalized: list[dict] = []
    for original in records:
        record = {
            "id": int(original.get("id") or 0),
            "name": str(original.get("name") or original.get("type") or "未命名渠道"),
            "type": str(original.get("type") or ""),
            "status": str(original.get("status") or "pending"),
            "error": str(original.get("error") or "")[:400],
        }
        if record["status"] == "failed":
            finalized.append(record)
            continue

        queue = exact.get(record["type"], [])
        receipt = queue.pop(0) if queue else apprise_result
        if receipt is not None:
            record["status"] = "sent" if receipt.get("success") else "failed"
            record["error"] = str(receipt.get("error") or "")[:400]
        else:
            record["status"] = "sent" if result.get("success") else "failed"
            record["error"] = str(result.get("error") or result.get("skipped") or "")[:400]
        finalized.append(record)
    return finalized


def _push_result_status(result: dict, channels: list[dict]) -> tuple[str, str]:
    """任一配置渠道失败时，整体状态应进入“推送失败”筛选。"""
    failed = [item for item in channels if item.get("status") == "failed"]
    if result.get("success") and not failed:
        return "sent", ""
    error = str(result.get("error") or result.get("skipped") or "")
    if not error and failed:
        error = "; ".join(str(item.get("error") or f"{item.get('name') or item.get('type')} 发送失败") for item in failed)
    return "failed", error[:400]


def _set_push_status(nid: int, status: str, err: str = "", *, channels: list[dict] | None = None) -> None:
    if not nid:
        return
    db = SessionLocal()
    try:
        n = db.query(Notification).filter(Notification.id == nid).first()
        if n:
            n.push_status = status
            n.push_error = err
            if channels is not None:
                n.push_channels = channels
            db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()


# ── 便捷封装: 后台任务生命周期 ─────────────────────────────────────

_CATEGORY_LINKS = {
    "agent_run": "/agents",
    "report": "/reports",
    "strategy": "/opportunities",
}


def _resolve_owner_user_id() -> str | None:
    """解析 owner(管理员) 的 user_id, 供后台任务无用户上下文时兜底推送。"""
    db = SessionLocal()
    try:
        u = (
            db.query(User)
            .filter(User.role == "owner", User.is_active.is_(True))
            .order_by(User.id.asc())
            .first()
        )
        return str(u.id) if u else None
    except Exception as e:
        logger.warning("[通知中心] 解析 owner 失败: %s", e)
        return None
    finally:
        db.close()


def notify_task_done(
    task_label: str,
    *,
    ok: bool,
    detail: str = "",
    category: str = "agent_run",
    source: str = "",
    trace_id: str = "",
    duration_ms: int | None = None,
    link: str = "",
    status: str = "",
    user_id: str | None = None,
) -> int | None:
    """后台任务收尾统一入口: 成功/失败都写站内, 失败额外外发。

    user_id: 多用户改造后渠道全归属 user_id(无全局渠道), 后台任务无用户上下文时
    兜底推给 owner(role=owner), 否则站内通知永远 skipped("未配置通知渠道")。
    """
    if user_id is None:
        user_id = _resolve_owner_user_id()
    dur = f"（耗时 {duration_ms / 1000:.1f}s）" if duration_ms else ""
    if status == "skipped":
        title = f"⏭️ {task_label} 已跳过{dur}"
        level = "warning"
    elif ok:
        title = f"✅ {task_label} 已完成{dur}"
        level = "success"
    else:
        title = f"❌ {task_label} 执行失败{dur}"
        level = "error"
    return push_notification(
        title,
        detail,
        category=category,
        level=level,
        link=link or _CATEGORY_LINKS.get(category, ""),
        source=source or task_label,
        trace_id=trace_id,
        user_id=user_id,
    )
