"""通知中心 + 外发推送联动。

设计要点(踩坑固化):
- 站内写入永远不能因为外发失败而丢失 → 先落库, 再推送, 推送结果回写 push_status。
- 后台线程调用 → 自带 session 生命周期, 不复用请求 session。
- 无渠道不是错误, 是 push_status='skipped'(站内仍可见), 避免"沉默失败"。

v0.4.36 (P0 派活 1):
- 落库后通过 WS Hub 实时推送 (push_notification → _after_push_hook → broadcast_notification + incr_unread)
- 多用户隔离: WS 按 user_id 分发; 未读计数按 user_id 隔离
"""

from __future__ import annotations

import asyncio
import logging
import time as _time_module

from src.db.session import SessionLocal
from src.db.models import Notification, NotifyChannel, User

logger = logging.getLogger(__name__)

# P1-4 (2026-09-05 28号审计): 后台推送 task 持有集合, 防 GC 中途回收致推送丢失
_BACKGROUND_TASKS: set = set()

# 已启用外发渠道时，站内通知同步外发，避免 info 只出现在右上角。
_PUSH_LEVELS = {"info", "success", "warning", "error"}


def _build_notifier(user_id: str | None = None, channel_ids=None):
    """从 DB 启用渠道构建 NotifierManager，并返回不含密钥的渠道快照。

    多用户(2026-08-10): user_id 指定 → 该用户渠道 + 全局共享(NULL);
    不指定 → 只推全局渠道(NULL)。

    2026-10-29 四审: 真正的**最终外发**必须同时满足「归属可见」与「配置选择集」
    (渠道解析层返回的集合)。旧实现只看 user_id → 用户只勾了 1 个渠道, 实际所有渠道
    都收到(静默超发)。带选择集校验的版本见 `_build_notifier_checked`。
    """
    mgr, records, _audit = _build_notifier_checked(
        user_id=user_id, channel_ids=channel_ids
    )
    return mgr, records


def _build_notifier_checked(*, user_id: str | None = None, channel_ids=None):
    """构建 notifier, 并在**外发前**交叉校验「用户归属 ∩ 选择集」。

    Args:
        user_id: 收件归属(None = 共享桶, 只允许全局渠道)。
        channel_ids: 解析层给出的选择集(`None` = 无配置约束, 用该归属全部启用渠道;
            **空列表 = 明确没有可用渠道 → 一条都不发**, 不能退化成"全部渠道")。

    Returns:
        (mgr | None, records, audit)。audit 记录 requested/granted/denied 供观测;
        denied 非空说明请求里有他人的/已禁用的渠道 —— 一律剔除, 不扩到全渠道。
    """
    from src.core.notifier import NotifierManager
    from sqlalchemy import or_ as _or_

    db = SessionLocal()
    try:
        wanted = None if channel_ids is None else {int(i) for i in channel_ids}
        q = db.query(NotifyChannel).filter(NotifyChannel.enabled.is_(True))
        if user_id is not None:
            q = q.filter(_or_(NotifyChannel.user_id == user_id, NotifyChannel.user_id.is_(None)))
        else:
            q = q.filter(NotifyChannel.user_id.is_(None))
        if wanted is not None:
            if not wanted:
                logger.warning(
                    "[通知中心] 渠道选择集为空(user=%s): 不外发(不退回全渠道)",
                    user_id or "共享桶",
                )
                return None, [], {"requested": [], "granted": [], "denied": []}
            q = q.filter(NotifyChannel.id.in_(sorted(wanted)))
        channels = q.all()
        granted = [int(ch.id) for ch in channels]
        audit = {
            "requested": sorted(wanted) if wanted is not None else None,
            "granted": granted,
            "denied": sorted(wanted - set(granted)) if wanted is not None else [],
        }
        if audit["denied"]:
            logger.warning(
                "[通知中心] user=%s 选择集里的渠道 %s 不属于该归属/未启用, 已剔除"
                "(不会扩到其他渠道)",
                user_id or "共享桶",
                audit["denied"],
            )
        if not channels:
            return None, [], audit
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
        return (mgr if ok else None), records, audit
    except Exception as e:
        logger.warning("[通知中心] 读取渠道失败: %s", e)
        return None, [], {"requested": None, "granted": [], "denied": [], "error": str(e)}
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
    channel_ids=None,
) -> int | None:
    """写一条站内通知, 并按级别决定是否外发。返回 notification id。

    多用户(2026-08-10): user_id 指定 → 推该用户渠道+全局; None → 只推全局渠道。
    绝不抛异常 —— 通知失败不能拖垮业务主流程。

    `channel_ids`(2026-10-29 四审): 调用方若已有「该用户的渠道选择集」(如
    `resolve_notify_channels_ex(...).channels` 的 id), 必须传下来 —— 最终外发会在
    **用户归属 ∩ 选择集**上再校验一次; 选择集为空/全都不可证明 → 一条都不发,
    push_status 如实写原因, 绝不扩到该用户全部渠道或别人的渠道。
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
        nid = int(n.id)  # type: ignore[arg-type]
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
        _after_push_hook(user_id, category, level, title, body, link, source, trace_id, nid)
        return nid

    mgr, channel_records, channel_audit = _build_notifier_checked(
        user_id=user_id, channel_ids=channel_ids
    )
    if mgr is None:
        init_errors = [str(item.get("error") or "") for item in channel_records if item.get("status") == "failed"]
        if channel_records:
            _set_push_status(
                nid,
                "failed",
                "; ".join(err for err in init_errors if err)[:400] or "通知渠道初始化失败",
                channels=channel_records,
            )
        elif channel_ids is not None:
            # 有明确选择集但一条都不可用(空/全被剔除) → 如实记录, 不外发。
            reason = "渠道选择集为空或不属于该归属, 未外发(不扩到其他渠道)"
            if channel_audit.get("denied"):
                reason = (
                    f"渠道选择集 {channel_audit.get('denied')} 不属于该归属/未启用, "
                    "未外发(不扩到其他渠道)"
                )
            logger.warning("[通知中心] id=%s %s", nid, reason)
            _set_push_status(nid, "skipped", reason, channels=[])
        else:
            # 显式状态, 不静默 —— 前端能看到"站内已记录, 未配置外发渠道"
            _set_push_status(nid, "skipped", "未配置通知渠道", channels=[])
        _after_push_hook(user_id, category, level, title, body, link, source, trace_id, nid)
        return nid

    try:
        result = asyncio.run(mgr.notify_with_result(title, body or title))
        final_channels = _finalize_channel_records(channel_records, result)
        final_status, final_error = _push_result_status(result, final_channels)
        _set_push_status(nid, final_status, final_error, channels=final_channels)
    except RuntimeError:
        # 已在事件循环里(异步上下文) → 交给调用方的 loop
        try:
            loop = asyncio.get_running_loop()
            task = loop.create_task(_async_push(nid, mgr, title, body or title, channel_records))
            _BACKGROUND_TASKS.add(task)
            task.add_done_callback(_BACKGROUND_TASKS.discard)
            _set_push_status(nid, "pending", "异步发送中", channels=channel_records)
        except Exception as e:
            _set_push_status(nid, "failed", str(e)[:400], channels=_finalize_channel_records(channel_records, {"success": False, "error": str(e)}))
    except Exception as e:
        logger.warning("[通知中心] 外发失败: %s", e)
        _set_push_status(nid, "failed", str(e)[:400], channels=_finalize_channel_records(channel_records, {"success": False, "error": str(e)}))
    _after_push_hook(user_id, category, level, title, body, link, source, trace_id, nid)
    return nid


def _after_push_hook(
    user_id: str | None,
    category: str,
    level: str,
    title: str,
    body: str,
    link: str,
    source: str,
    trace_id: str,
    nid: int | None,
) -> None:
    """v0.4.36: 落库后通过 WS Hub 实时推送 + 累加未读.

    WS Hub 不可用时静默, 通知中心仍可走轮询拉取 (api/notifications).
    """
    if nid is None:
        return
    try:
        from src.core.notify_sink import (
            broadcast_notification,
            incr_unread,
        )
        payload = {
            "type": "event",
            "id": int(nid),
            "category": category,
            "level": level,
            "title": title[:200],
            "body": (body or "")[:4000],
            "link": link,
            "source": source,
            "trace_id": trace_id,
            "ts": int(_time_module.time()),
        }
        # 广播给该用户的 WS 连接 (按 category 订阅过滤)
        broadcast_notification(user_id, payload, category=category)
        # 累加未读 (站内公告不入未读)
        if user_id:
            incr_unread(user_id, 1)
    except Exception as e:
        logger.debug("[通知中心] WS Hub 钩子失败 (不影响主流程): %s", e)


async def push_notification_async(title: str, body: str = "", **kw) -> int | None:
    """异步上下文里安全调用(把阻塞部分丢线程池)。"""
    return await asyncio.to_thread(push_notification, title, body, **kw)


async def _async_push(nid: int | None, mgr, title: str, body: str, channel_records: list[dict]) -> None:
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


def _set_push_status(nid: int | None, status: str, err: str = "", *, channels: list[dict] | None = None) -> None:
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
    allow_owner_fallback: bool = False,
    channel_ids=None,
) -> int | None:
    """后台任务收尾统一入口: 成功/失败都写站内, 失败额外外发。

    2026-10-29 三审(R4): 兜底只在**确实没有会话**时发生, 且必须留 warning 日志 ——
    此前 `stocks.py` 手动触发线程漏传 user_id, member 的任务结果被静默推给了 owner。
    调用方只要有触发用户就必须显式传(标量 id, 在起线程前取好), 不许依赖兜底。

    2026-10-29 四审: 兜底改成**显式 opt-in**(`allow_owner_fallback=True`), 只给
    "系统自身运维告警"(无触发用户, owner 就是责任人)用。普通任务漏传 user_id 时
    不再自动推 owner —— 那等于把不认识的人的内容塞进 owner 的渠道; 现在按共享桶
    (None) 落库/外发并 warning 留痕, 也就是"未知 → 不发他人"。
    """
    if user_id is None and allow_owner_fallback:
        user_id = _resolve_owner_user_id()
        logger.warning(
            "[通知中心] notify_task_done 未带 user_id, 兜底归属 owner=%s (task=%s, source=%s); "
            "调用方若有触发用户必须显式传, 否则会把他人任务推到 owner",
            user_id or "无",
            task_label,
            source or category,
        )
    elif user_id is None:
        logger.warning(
            "[通知中心] notify_task_done 无可信归属(task=%s, source=%s): 按共享桶落库, "
            "不推给 owner/任何具体用户(调用方应传触发用户)",
            task_label,
            source or category,
        )
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
        # 四审: 若调用方有该 agent/stock_agent 的渠道选择集, 必须在最终外发前带上
        # (用户归属 ∩ 选择集 再校验一次), 否则勾选的渠道约束在通知中心被绕过。
        channel_ids=channel_ids,
    )
