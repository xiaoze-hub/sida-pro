"""多用户 AI 配额与用量统计（2026-10-20, P3 治理）。

设计取舍：
- **没有配额行 = 不限**（老用户不受影响）。
- 限额是硬约束：网关层直接 429，不靠提示词"请节约"。
- 计数来源是既有的 `llm_usage`（token）与 `copilot_runs`（次数），
  不额外维护计数器 —— 少一份状态就少一处不一致。
- 统计不到就是 `None`，绝不补 0 假装配额已用完。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import func

from src.db.models import AuditLog, CopilotQuota, CopilotRun, CopilotStep, LLMUsage

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════
# 委派/并发硬限制（P1-1.2，2026-10-25）
#
# 与上面「按天的配额」不是一回事：那边管「一天能用多少」，这边管「同一时刻能并行
# 几个、单个 run 能烧多少次工具调用」。两者都不靠提示词，都是 429 + 审计。
#
# 并发数怎么定：`copilot_jobs.MAX_CONCURRENT` 的全局信号量是 3，而执行体在调 LLM
# 期间必须持有 DB 连接（见 run_copilot_question 的注释）。单用户取 2 —— 留 1 个槽给
# 别人，否则一个人连提 3 个深问就把全站队列占满（2026-10-25 生产就是这种形态）。
#
# 工具预算怎么定：工具层软预算 `harness.runtime.DEFAULT_MAX_TOOL_CALLS=40`（现网实测
# 每轮 2~6 次），配方的 max_tool_calls 会在工具层拒绝继续取数但**仍让模型作答**。
# 这一层是硬兜底：60 = 1.5× 软预算，只用来拦「软预算被绕过/配方把上限调大」的失控，
# 触发即中止 run，不允许截断后继续编答案。
# ══════════════════════════════════════════════════════════════════════════

#: 计入「并发占用」的状态（与 copilot_runtime 的活跃态对齐到真正占连接的两个）
ACTIVE_RUN_STATUSES: tuple[str, ...] = ("queued", "running")

#: 审计 action（写进 `audit_logs`，用现有 `AuditLog` 模型，不新造表）
AUDIT_CONCURRENCY = "copilot_concurrency_exceeded"
AUDIT_TOOL_BUDGET = "copilot_tool_budget_exceeded"


def _env_int(name: str, default: int) -> int:
    """读整数常量；环境变量非法就回落默认值（不让一个手滑的配置把接口打挂）。"""
    import os

    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        val = int(str(raw).strip())
    except (TypeError, ValueError):
        logger.warning("[copilot-quota] 环境变量 %s=%r 不是整数，用默认值 %s", name, raw, default)
        return default
    return val if val > 0 else default


#: 单用户同时排队/执行中的 run 上限
MAX_ACTIVE_RUNS_PER_USER = _env_int("COPILOT_MAX_ACTIVE_RUNS_PER_USER", 2)
#: 单个 run 的工具调用硬预算（超了就中止，不截断继续）
MAX_TOOL_CALLS_PER_RUN = _env_int("COPILOT_MAX_TOOL_CALLS_PER_RUN", 60)


def _day_start() -> datetime:
    now = datetime.utcnow()
    return datetime(now.year, now.month, now.day)


def get_quota(db, user_id) -> CopilotQuota | None:
    uid = str(user_id or "")
    if not uid:
        return None
    return db.query(CopilotQuota).filter(CopilotQuota.user_id == uid).one_or_none()


def set_quota(db, user_id, *, daily_runs=None, daily_tokens=None,
              enabled: bool = True, note: str | None = None,
              updated_by=None) -> CopilotQuota:
    """写/更新一个人的配额（upsert）。值为 None 表示该维度不限。"""
    uid = str(user_id)
    row = get_quota(db, uid)
    if row is None:
        row = CopilotQuota(user_id=uid)
        db.add(row)
    row.daily_runs = daily_runs
    row.daily_tokens = daily_tokens
    row.enabled = bool(enabled)
    row.note = (note or None)
    row.updated_by = str(updated_by) if updated_by is not None else None
    db.commit()
    db.refresh(row)
    return row


def usage_today(db, user_id) -> dict:
    """今天的用量：问答轮数 + token（prompt+completion）。查不到返回 None 而不是 0。"""
    uid = str(user_id or "")
    if not uid:
        return {"runs": None, "tokens": None}
    since = _day_start()
    runs = (
        db.query(func.count(CopilotRun.id))
        .filter(CopilotRun.user_id == uid, CopilotRun.created_at >= since)
        .scalar()
    )
    tokens = (
        db.query(func.coalesce(func.sum(LLMUsage.prompt_tokens + LLMUsage.completion_tokens), 0))
        .filter(LLMUsage.user_id == uid, LLMUsage.created_at >= since)
        .scalar()
    )
    return {"runs": int(runs or 0), "tokens": int(tokens or 0)}


def check(db, user_id) -> tuple[bool, str | None, dict]:
    """是否允许继续用。返回 (ok, reason, usage)。

    无配额行 / enabled=False / 两个上限都是 None → 放行（不限）。
    超限时 reason 是给用户看的中文说明（要能说清"哪个上限、明天几点恢复"）。
    """
    row = get_quota(db, user_id)
    usage = usage_today(db, user_id)
    if row is None or not row.enabled:
        return True, None, usage
    if row.daily_runs is not None and (usage["runs"] or 0) >= row.daily_runs:
        return False, f"今日 AI 问答已达上限（{row.daily_runs} 轮/天），明日 00:00 重置", usage
    if row.daily_tokens is not None and (usage["tokens"] or 0) >= row.daily_tokens:
        return False, (
            f"今日 AI token 用量已达上限（{row.daily_tokens:,} tokens/天），明日 00:00 重置"
        ), usage
    return True, None, usage


def error_message(db, user) -> str | None:
    """流式端点用：返回给人看的一句话，未超限返回 None（不抛异常，避免 SSE 头已发后 500）。"""
    try:
        ok, reason, _ = check(db, getattr(user, "id", None))
    except Exception:  # noqa: BLE001
        return None
    return None if ok else (reason or "已达今日 AI 配额")


def enforce(db, user) -> None:
    """网关层硬拒：超限抛 HTTPException(429)。异常信息直接给用户看。"""
    from fastapi import HTTPException

    try:
        ok, reason, usage = check(db, getattr(user, "id", None))
    except Exception as e:  # noqa: BLE001 - 配额统计失败不能把问答打成 500
        logger.warning("[copilot-quota] 校验失败，放行: %s", e)
        return
    if not ok:
        raise HTTPException(
            status_code=429,
            detail={"code": "copilot_quota_exceeded", "message": reason, "usage": usage},
        )


def audit_limit_hit(db, *, user_id, action: str, detail: str) -> bool:
    """写一条「因限流被拒」的审计（复用现有 `audit_logs` 表 + `AuditLog` 模型）。

    与 `src/core/permissions.py` / `src/core/hv_api_log.py` 的写法一致：`db.add` + `commit`，
    失败先 `safe_rollback` 再吞（审计是 best-effort，不能因为它把 429 变成 500）。
    返回是否写成功，调用方不必依赖。
    """
    from src.core.db_safe import safe_rollback

    uid = str(user_id or "")
    try:
        db.add(AuditLog(
            user_id=uid or None,
            username="",
            action=action,
            detail=(detail or "")[:255],   # 列宽 255，超长会因 PG 报错丢整条
        ))
        db.commit()
        return True
    except Exception as e:  # noqa: BLE001 —— 见 docstring
        safe_rollback(db)
        logger.warning("[copilot-quota] 审计写入失败 action=%s user=%s: %r", action, uid, e)
        return False


def active_run_count(db, user_id) -> int:
    """该用户当前处于 queued/running 的 run 数（并发占用的唯一判据）。

    ⚠️ 僵尸 run（进程死了没改状态）也会被算进去，靠
    `copilot_runtime.reap_stale_runs()` 收尸；这里不做心跳过滤 —— 判据越简单越不会被
    「顺手改一下」改错，且把僵尸当成占用比让用户无限并行安全。
    """
    uid = str(user_id or "")
    if not uid:
        return 0
    return int(
        db.query(func.count(CopilotRun.id))
        .filter(CopilotRun.user_id == uid, CopilotRun.status.in_(ACTIVE_RUN_STATUSES))
        .scalar()
        or 0
    )


def enforce_run_concurrency(db, user) -> None:
    """超并发直接 429（错误体带现有数量与上限），并写审计。

    统计失败放行（fail-open）：限流本身不该把提交打成 500 —— 真出问题时宁可多跑一个。
    """
    from fastapi import HTTPException

    uid = str(getattr(user, "id", "") or "")
    limit = MAX_ACTIVE_RUNS_PER_USER
    if not uid or limit <= 0:
        return
    try:
        active = active_run_count(db, uid)
    except Exception as e:  # noqa: BLE001
        from src.core.db_safe import safe_rollback

        safe_rollback(db)
        logger.warning("[copilot-quota] 并发统计失败，放行: %r", e)
        return
    if active < limit:
        return
    try:
        audit_limit_hit(
            db, user_id=uid, action=AUDIT_CONCURRENCY,
            detail=f"active={active} limit={limit}",
        )
    except Exception as e:  # noqa: BLE001 —— 审计 best-effort：写不进去也必须照常 429
        from src.core.db_safe import safe_rollback

        safe_rollback(db)   # 审计失败会把事务打成 aborted，返回前必须清掉
        logger.warning("[copilot-quota] 并发超限审计写入失败: %r", e)
    raise HTTPException(
        status_code=429,
        detail={
            "code": AUDIT_CONCURRENCY,
            "message": (
                f"同时进行的委派任务已达上限（当前 {active} 个排队/执行中，"
                f"上限 {limit} 个）。请等其中一个完成或取消后再提交。"
            ),
            "active": active,
            "limit": limit,
        },
    )


def run_tool_calls(db, run_id) -> int:
    """该 run 已经落库的工具调用次数。

    口径：`copilot_steps` 行数（每次真实工具调用一行，见 `CopilotTrace.step`），
    与 `copilot_runs.tool_calls` 取大者 —— 后者只有轨迹收尾才写，跑的过程中一直是 0。
    """
    if not run_id:
        return 0
    steps = int(
        db.query(func.count(CopilotStep.id)).filter(CopilotStep.run_id == str(run_id)).scalar() or 0
    )
    row_calls = int(
        db.query(CopilotRun.tool_calls).filter(CopilotRun.run_id == str(run_id)).scalar() or 0
    )
    return max(steps, row_calls)


def check_run_budget(db, run_id) -> tuple[bool, int, int]:
    """(是否还在预算内, 已用次数, 上限)。超预算时调用方必须**中止** run，不许截断继续。"""
    used = run_tool_calls(db, run_id)
    limit = MAX_TOOL_CALLS_PER_RUN
    if limit <= 0:
        return True, used, limit
    return used <= limit, used, limit


def list_all(db) -> list[dict]:
    """全部配额行 + 各自今日用量（管理页用）。"""
    rows = db.query(CopilotQuota).order_by(CopilotQuota.user_id).all()
    out = []
    for r in rows:
        usage = usage_today(db, r.user_id)
        out.append({
            "user_id": r.user_id,
            "daily_runs": r.daily_runs,
            "daily_tokens": r.daily_tokens,
            "enabled": bool(r.enabled),
            "note": r.note,
            "used_runs": usage["runs"],
            "used_tokens": usage["tokens"],
            "updated_at": str(r.updated_at or ""),
        })
    return out


def cost_summary(db, *, days: int = 7, price_per_mtoken: dict | None = None,
                 user_id: str | None = None) -> dict:
    """成本看板数据：按日 / 按模型 / 按用户聚合 token 与估算费用。

    `price_per_mtoken` 是 {模型前缀: 每百万 token 混合单价(元)}；未配置价格的模型
    返回 cost=None 并在 `unpriced_models` 里列出来 —— 不编造单价。
    """
    since = datetime.utcnow() - timedelta(days=max(1, int(days)))
    q = db.query(LLMUsage).filter(LLMUsage.created_at >= since)
    # 非管理员只看自己：by_day/by_model 也必须过滤，否则总量仍会把别人的用量摊出来
    if user_id:
        q = q.filter(LLMUsage.user_id == str(user_id))
    rows = q.all()
    price = price_per_mtoken or {}

    def _price(model: str | None) -> float | None:
        m = (model or "").lower()
        for key, val in price.items():
            if key and key.lower() in m:
                return float(val)
        return None

    by_day: dict[str, dict] = {}
    by_model: dict[str, dict] = {}
    by_user: dict[str, dict] = {}
    unpriced: set[str] = set()
    for r in rows:
        d = (r.created_at or datetime.utcnow()).strftime("%Y-%m-%d")
        toks = (r.prompt_tokens or 0) + (r.completion_tokens or 0)
        pt = _price(r.model_name)
        cost = round(toks / 1_000_000 * pt, 4) if pt is not None else None
        if pt is None and toks:
            unpriced.add(r.model_name or "(unknown)")

        for bucket, key in ((by_day, d), (by_model, r.model_name or "(unknown)"),
                            (by_user, r.user_id or "(unattributed)")):
            node = bucket.setdefault(key, {"tokens": 0, "calls": 0, "cost": 0.0,
                                           "priced": True})
            node["tokens"] += toks
            node["calls"] += 1
            if cost is None:
                node["priced"] = False
            else:
                node["cost"] = round(node["cost"] + cost, 4)

    def _rows(d: dict, key_name: str) -> list[dict]:
        out = []
        for k, v in d.items():
            out.append({
                key_name: k, "tokens": v["tokens"], "calls": v["calls"],
                "cost": (round(v["cost"], 4) if v["priced"] else None),
            })
        return sorted(out, key=lambda x: -x["tokens"])

    return {
        "days": days,
        "total_tokens": sum((r.prompt_tokens or 0) + (r.completion_tokens or 0) for r in rows),
        "total_calls": len(rows),
        "by_day": sorted(_rows(by_day, "date"), key=lambda x: x["date"]),
        "by_model": _rows(by_model, "model"),
        "by_user": _rows(by_user, "user_id"),
        "unpriced_models": sorted(unpriced),
        "price_note": "单价来自 llm_usage 汇总 × 配置单价；未配置价格的模型 cost 为 null",
    }
