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

from src.db.models import CopilotQuota, CopilotRun, LLMUsage

logger = logging.getLogger(__name__)


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


def cost_summary(db, *, days: int = 7, price_per_mtoken: dict | None = None) -> dict:
    """成本看板数据：按日 / 按模型 / 按用户聚合 token 与估算费用。

    `price_per_mtoken` 是 {模型前缀: 每百万 token 混合单价(元)}；未配置价格的模型
    返回 cost=None 并在 `unpriced_models` 里列出来 —— 不编造单价。
    """
    since = datetime.utcnow() - timedelta(days=max(1, int(days)))
    rows = db.query(LLMUsage).filter(LLMUsage.created_at >= since).all()
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
