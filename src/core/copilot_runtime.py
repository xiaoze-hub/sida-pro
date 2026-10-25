"""数智参谋 Durable Run 运行时（P0-0.1，2026-10-25）。

**要解决的真问题**：`copilot_runs` 只有 `status`，没有心跳。进程重启 / 容器发布 /
worker 崩溃后，库里会留下永久 `running` 的"幽灵运行"——用户看到"正在分析"卡在那，
既不会失败也不会重试，也没人去收尸。

**状态机（兼容历史词汇，不重命名）**：
```
queued ──start──> running ──ok/degraded──> 终态
                     │  └──error/timeout/cancelled──> 终态
                     └──pause──> paused ──resume──> running
```
历史数据里 `status` 实际写的是 `ok / degraded / error`（见 `trace._infer_status`），
计划书里写的是 `succeeded / failed`。**两者都算终态，都认**，不迁移存量数据 ——
改词汇的代价是历史轨迹全部变成未知状态，收益为零。

**接管判据**：`heartbeat_at` 超过 120s 未更新 = 进程已死（心跳 30s 一次，容忍 4 次丢失）。

**为什么不自动重跑**：重跑会消耗额度、且用户可能已经看到半截答案。
这里只把状态改成 `failed` 并写明"可重试"，**重试由用户点**（幂等入口在
`POST /history/{id}/retry` 的同一套语义里）。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Iterable

logger = logging.getLogger(__name__)

# ── 状态词汇 ─────────────────────────────────────────────────────────────
QUEUED = "queued"
RUNNING = "running"
PAUSED = "paused"

#: 终态（含历史词汇 ok/degraded/error —— 不改存量数据，两种都认）
TERMINAL_STATES: frozenset[str] = frozenset(
    {"ok", "degraded", "error", "succeeded", "failed", "cancelled"}
)

#: 活跃态（需要心跳、可能被接管）
ACTIVE_STATES: frozenset[str] = frozenset({QUEUED, RUNNING, PAUSED})

#: 合法迁移；不在表里的一律拒绝（防止 ok→running 这种把结果改回去的操作）
TRANSITIONS: dict[str, frozenset[str]] = {
    QUEUED: frozenset({RUNNING, "failed", "cancelled"}),
    RUNNING: frozenset({PAUSED, "ok", "degraded", "error", "succeeded", "failed", "cancelled"}),
    PAUSED: frozenset({RUNNING, "failed", "cancelled"}),
}

#: 心跳间隔与接管阈值（秒）
HEARTBEAT_INTERVAL_S = 30
STALE_AFTER_S = 120


def is_terminal(status: str | None) -> bool:
    return (status or "") in TERMINAL_STATES


def is_active(status: str | None) -> bool:
    return (status or "") in ACTIVE_STATES


def can_transition(src: str | None, dst: str) -> bool:
    """状态机校验。终态不可再变（除了幂等写同一个终态）。"""
    if not src:
        return dst in (QUEUED, RUNNING)
    if src == dst:
        return True
    if is_terminal(src):
        return False
    return dst in TRANSITIONS.get(src, frozenset())


def stale_cutoff(now: datetime | None = None, *, stale_after_s: int = STALE_AFTER_S) -> datetime:
    return (now or datetime.utcnow()) - timedelta(seconds=stale_after_s)


def takeover_error(now: datetime | None = None) -> str:
    """接管时写进 `error` 的说明 —— 用户要能看懂发生了什么。"""
    return (
        f"运行中断：心跳超过 {STALE_AFTER_S}s 未更新（进程重启或任务被终止）。"
        "结果不完整，可点「重试」重新执行。"
    )


def reap_stale_runs(
    db,
    *,
    now: datetime | None = None,
    stale_after_s: int = STALE_AFTER_S,
    dry_run: bool = False,
) -> list[str]:
    """把心跳超时的活跃 run 标成 failed，返回被接管的 run_id 列表。

    **为什么按心跳而不是按进程**：多 worker 部署时，"这个进程还活着"不代表
    "它负责的那个 run 还在跑"。心跳是 run 级的事实。

    **为什么 heartbeat 为 NULL 也算超时**：老数据没有心跳列，若它们卡在 running，
    说明写入它的那次进程早就不在了 —— 这正是要收的尸。
    """
    from src.db.models import CopilotRun

    cutoff = stale_cutoff(now, stale_after_s=stale_after_s)
    rows: Iterable[CopilotRun] = (
        db.query(CopilotRun)
        .filter(CopilotRun.status.in_(tuple(ACTIVE_STATES)))
        .filter(
            (CopilotRun.heartbeat_at.is_(None)) | (CopilotRun.heartbeat_at < cutoff)
        )
        .all()
    )

    reaped: list[str] = []
    for row in rows:
        # queued 的还没开始跑，不能因为"没心跳"就判死（它本来就还没开始）
        if row.status == QUEUED and row.heartbeat_at is None:
            continue
        if not can_transition(row.status, "failed"):
            continue
        reaped.append(row.run_id)
        if dry_run:
            continue
        row.status = "failed"
        row.error = takeover_error(now)
        row.finished_at = now or datetime.utcnow()

    if reaped and not dry_run:
        try:
            db.commit()
        except Exception:
            from src.core.db_safe import safe_rollback

            safe_rollback(db)
            raise
        logger.warning("[copilot_runtime] 接管 %d 个中断 run: %s", len(reaped), reaped[:10])
    return reaped


def heartbeat(db, run_id: str, *, now: datetime | None = None) -> bool:
    """刷新心跳。终端 run 不再刷（返回 False，不报错）。"""
    from src.db.models import CopilotRun

    row = db.query(CopilotRun).filter(CopilotRun.run_id == run_id).first()
    if row is None or is_terminal(row.status):
        return False
    row.heartbeat_at = now or datetime.utcnow()
    db.commit()
    return True
