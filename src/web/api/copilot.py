"""src/web/api/copilot.py

AI 数智参谋（Copilot）API 路由。
提供 /api/copilot/brief/{symbol} 与 /api/copilot/presets。
契约源: docs/research/AI数智参谋全链路落地设计方案.md §4
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import or_

from src.core.copilot_brief import build_copilot_brief
from src.core.trade_phase import phase_meta
from src.web.api.auth import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/copilot", tags=["copilot"])

_TTL = {"intraday": 20, "closed": 300, "non_trading": 1800}

# 简易进程内缓存与单飞锁
_cache: dict[str, tuple[float, int, dict]] = {}
_locks: dict[str, threading.Lock] = {}
_global_lock = threading.Lock()


def _get_flight_lock(key: str) -> threading.Lock:
    with _global_lock:
        if key not in _locks:
            _locks[key] = threading.Lock()
        return _locks[key]


@router.get("/brief/{symbol}")
def get_copilot_brief(
    symbol: str,
    market: str = Query("CN", pattern="^(CN|HK|US)$"),
    refresh: bool = Query(False, description="true=跳过进程缓存"),
    user: Any = Depends(get_current_user),
) -> dict:
    meta = phase_meta()
    phase = meta.get("trade_phase", "closed")
    is_live = bool(meta.get("is_live", False))

    ttl = _TTL["intraday"] if is_live else (_TTL["closed"] if phase == "closed" else _TTL["non_trading"])
    cache_key = f"{symbol}:{market}:{phase}"

    if not refresh and cache_key in _cache:
        ts, hit_ttl, payload = _cache[cache_key]
        if time.time() - ts < hit_ttl:
            res = dict(payload)
            res["cache"] = {"hit": True, "ttl_s": hit_ttl, "key": cache_key}
            return res

    lock = _get_flight_lock(cache_key)
    with lock:
        # 双重检查
        if not refresh and cache_key in _cache:
            ts, hit_ttl, payload = _cache[cache_key]
            if time.time() - ts < hit_ttl:
                res = dict(payload)
                res["cache"] = {"hit": True, "ttl_s": hit_ttl, "key": cache_key}
                return res

        brief = build_copilot_brief(symbol, market=market)
        # 若不是全降级，写缓存
        evidence_used = brief.get("verdict", {}).get("evidence_used", [])
        if len(evidence_used) >= 1:
            _cache[cache_key] = (time.time(), ttl, brief)

        res = dict(brief)
        res["cache"] = {"hit": False, "ttl_s": ttl, "key": cache_key}
        return res


@router.get("/presets")
def get_copilot_presets(
    user: Any = Depends(get_current_user),
) -> list[dict]:
    return [
        {
            "id": "full_dossier",
            "icon": "🧬",
            "label": "全景数智深度诊断(多维Harness)",
            "prompt": "请调用标的全景档案工具，对我当前查看的股票进行全面深度分析：整合实时量价、K线形态、腾讯逐笔主力意图、120日底仓筹码沉淀、数智决策GS多空色带、东方财富机构研报、近期公司公告与利好/利空消息面，给出明确结论与实战作战预案。",
            "group": "dossier",
            "requires_perm": None,
        },
        {
            "id": "auction_strong",
            "icon": "🔥",
            "label": "扫描今日 9:25 爆量抢筹",
            "prompt": "扫描今天集合竞价里爆量抢筹(竞价金额>2000万且量比>2.5)的股票，按竞价金额降序给出前 10 只，并标注每只的五态判定与竞价撤单率。",
            "group": "auction",
            "requires_perm": "view_auction",
        },
        {
            "id": "chip_cost",
            "icon": "🎯",
            "label": "诊断持仓股底仓成本与获利盘",
            "prompt": "用我的持仓列表，逐只给出底仓成本(COST50)、70%筹码区间与当前获利盘比例，标出已跌破 70% 筹码下沿的票。",
            "group": "chip",
            "requires_perm": None,
        },
        {
            "id": "resonance_first",
            "icon": "🛡️",
            "label": "筛三指标首次共振股",
            "prompt": "筛出今天三指标首次共振(🔥首次共振·机会挖掘)的股票，并按机构活跃度降序排列，说明每只的 GS 区间与 L2 主力净额。",
            "group": "resonance",
            "requires_perm": "view_forecast",
        },
        {
            "id": "demon_mainline",
            "icon": "👑",
            "label": "检索主线中的妖股候选",
            "prompt": "找出当前主线 Top5 板块里、同时进入妖股因子库 Top50 的个股，给出因子分、分级与连板身位。",
            "group": "demon",
            "requires_perm": "view_opportunities",
        },
        {
            "id": "defense_check",
            "icon": "🧯",
            "label": "检查持仓的防守位是否破位",
            "prompt": "对每只持仓，给出第一防守位与核心生命线(70%筹码下沿)，并明确哪些已经破位、哪些还在上方。",
            "group": "defense",
            "requires_perm": None,
        },
    ]


# ─────────────────────────────── 内置智能体（Harness 配方） ───────────────────────────────
@router.get("/agents")
def get_copilot_agents(user: Any = Depends(get_current_user)) -> list[dict]:
    """内置智能体元信息（前端 @ 面板用）。配方正文不下发。"""
    from src.harness.recipes import list_recipes

    return [r.public() for r in list_recipes()]


@router.get("/mentions")
def get_copilot_mentions(
    q: str = Query("", description="代码 / 名称 / 拼音首字母"),
    limit: int = Query(8, ge=1, le=30),
    user: Any = Depends(get_current_user),
) -> dict:
    """@ 面板候选：自选股（含全局）+ 内置智能体。

    自选股按当前用户隔离（与 get_watchlist 同口径：本人 + user_id IS NULL 的全局自选）。
    """
    from src.db.models import Stock
    from src.harness.recipes import list_recipes
    from src.web.database import SessionLocal

    kw = (q or "").strip().lower()
    stocks: list[dict] = []
    db = SessionLocal()
    try:
        query = db.query(Stock)
        if getattr(user, "id", None) is not None:
            query = query.filter(or_(Stock.user_id == user.id, Stock.user_id.is_(None)))
        rows = query.order_by(Stock.sort_order.asc()).limit(200).all()
        for s in rows:
            name = s.name or ""
            py = _pinyin_initials(name)
            hay = f"{s.symbol} {name} {py}".lower()
            if kw and kw not in hay:
                continue
            stocks.append({
                "type": "stock",
                "symbol": s.symbol,
                "name": name,
                "market": s.market or "CN",
                "py": py,
            })
            if len(stocks) >= limit:
                break
    except Exception as e:  # 自选表异常不应打断 @ 面板
        logger.warning("mentions 自选股查询失败: %s", e)
    finally:
        db.close()

    agents = []
    for r in list_recipes():
        hay = f"{r.id} {r.label} {r.desc} {' '.join(r.when)}"
        if kw and kw not in hay.lower():
            continue
        item = r.public()
        item["type"] = "agent"
        agents.append(item)
        if len(agents) >= limit:
            break
    return {"stocks": stocks, "agents": agents}


def _pinyin_initials(name: str) -> str:
    """名称 → 拼音首字母（"北方稀土" → "bfxt"）。pypinyin 缺失时退化为空串。"""
    if not name:
        return ""
    try:
        from pypinyin import Style, lazy_pinyin

        return "".join(lazy_pinyin(name, style=Style.FIRST_LETTER, errors="ignore")).lower()
    except Exception:
        return ""


# ──────────────── P1: 页面即上下文 / 动作回写 / P3: 轨迹只读 ────────────────

from fastapi import Body, HTTPException  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from src.harness import actions as actions_mod  # noqa: E402
from src.harness.viewstate import ViewState, load_view_state, save_view_state  # noqa: E402
from src.web.database import get_db  # noqa: E402


@router.post("/context")
def post_copilot_context(
    payload: dict = Body(...),
    user: Any = Depends(get_current_user),
) -> dict:
    """前端上报当前视图（K 线页的标的/周期/圈选区）。

    刻意用裸 dict 而不是直接绑定 ViewState：前端字段演进时不该 422，
    未知字段一律忽略，缺字段一律可选。
    """
    conversation_id = payload.get("conversation_id")
    try:
        state = ViewState(**{k: v for k, v in payload.items() if k in ViewState.model_fields})
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=f"视图状态不合法：{exc}") from exc
    ok = save_view_state(user.id, conversation_id, state)
    return {"saved": ok, "ttl_seconds": 1800}


@router.get("/context")
def get_copilot_context(
    conversation_id: int | None = Query(default=None),
    user: Any = Depends(get_current_user),
) -> dict:
    return {"state": load_view_state(user.id, conversation_id)}


@router.get("/actions")
def get_copilot_actions(
    db: Session = Depends(get_db),
    user: Any = Depends(get_current_user),
) -> list[dict]:
    """待确认动作列表（AI 提议、等用户点确认）。"""
    return actions_mod.list_pending(db, user.id)


@router.post("/actions/{action_id}/apply")
def apply_copilot_action(
    action_id: int,
    db: Session = Depends(get_db),
    user: Any = Depends(get_current_user),
) -> dict:
    try:
        return actions_mod.apply_action(db, user_id=user.id, action_id=action_id)
    except actions_mod.ActionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/actions/{action_id}/reject")
def reject_copilot_action(
    action_id: int,
    db: Session = Depends(get_db),
    user: Any = Depends(get_current_user),
) -> dict:
    try:
        return actions_mod.reject_action(db, user_id=user.id, action_id=action_id)
    except actions_mod.ActionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/runs")
def get_copilot_runs(
    limit: int = Query(default=20, le=100),
    conversation_id: int | None = Query(default=None),
    db: Session = Depends(get_db),
    user: Any = Depends(get_current_user),
) -> list[dict]:
    """最近若干轮问答的骨架（P3 治理/回放入口）。"""
    from src.db.models import CopilotRun

    q = db.query(CopilotRun).filter(CopilotRun.user_id == str(user.id))
    if conversation_id is not None:
        q = q.filter(CopilotRun.conversation_id == conversation_id)
    rows = q.order_by(CopilotRun.id.desc()).limit(limit).all()
    run_ids = [r.run_id for r in rows]
    tokens: dict[str, int] = {}
    if run_ids:
        from sqlalchemy import func

        from src.db.models import LLMUsage

        for rid, tot in (
            db.query(LLMUsage.run_id,
                     func.coalesce(func.sum(LLMUsage.prompt_tokens + LLMUsage.completion_tokens), 0))
            .filter(LLMUsage.run_id.in_(run_ids))
            .group_by(LLMUsage.run_id)
            .all()
        ):
            tokens[rid] = int(tot or 0)
    return [{
        "run_id": r.run_id, "conversation_id": r.conversation_id, "recipe": r.recipe,
        "status": r.status, "tool_calls": r.tool_calls, "elapsed_ms": r.elapsed_ms,
        "tokens": tokens.get(r.run_id),
        "question": r.question, "created_at": str(r.created_at or ""),
    } for r in rows]


@router.get("/runs/{run_id}")
def get_copilot_run_detail(
    run_id: str,
    db: Session = Depends(get_db),
    user: Any = Depends(get_current_user),
) -> dict:
    """单轮回放：骨架 + 每步工具调用（含摘要与耗时）。"""
    from src.db.models import CopilotRun, CopilotStep

    run = (
        db.query(CopilotRun)
        .filter(CopilotRun.run_id == run_id, CopilotRun.user_id == str(user.id))
        .one_or_none()
    )
    if run is None:
        raise HTTPException(status_code=404, detail="轨迹不存在")
    steps = (
        db.query(CopilotStep).filter(CopilotStep.run_id == run_id)
        .order_by(CopilotStep.seq).all()
    )
    return {
        "run": {
            "run_id": run.run_id, "status": run.status, "recipe": run.recipe,
            "question": run.question, "tool_calls": run.tool_calls,
            "elapsed_ms": run.elapsed_ms, "created_at": str(run.created_at or ""),
        },
        "steps": [{
            "seq": s.seq, "tool": s.tool, "ok": s.ok, "elapsed_ms": s.elapsed_ms,
            "args": s.args, "result_digest": s.result_digest,
        } for s in steps],
    }


@router.get("/metrics")
def get_copilot_metrics(
    days: int = Query(default=7, le=30),
    db: Session = Depends(get_db),
    user: Any = Depends(get_current_user),
) -> dict:
    """成本/质量看板数据：运行数、成功率、平均工具数、P95 耗时、按配方分组。"""
    from datetime import timedelta

    from sqlalchemy import func

    from src.db.models import CopilotRun

    since = __import__("datetime").datetime.utcnow() - timedelta(days=days)
    rows = (
        db.query(CopilotRun)
        .filter(CopilotRun.user_id == str(user.id), CopilotRun.created_at >= since)
        .all()
    )
    if not rows:
        return {"days": days, "runs": 0, "ok_rate": None, "avg_tools": None,
                "p95_elapsed_ms": None, "by_recipe": []}
    elapsed = sorted([r.elapsed_ms for r in rows if r.elapsed_ms])
    p95 = elapsed[int(len(elapsed) * 0.95) - 1] if elapsed else None
    by_recipe: dict[str, int] = {}
    for r in rows:
        by_recipe[r.recipe or "(默认)"] = by_recipe.get(r.recipe or "(默认)", 0) + 1
    return {
        "days": days,
        "runs": len(rows),
        "ok_rate": round(sum(1 for r in rows if r.status == "ok") / len(rows), 3),
        "avg_tools": round(sum(r.tool_calls or 0 for r in rows) / len(rows), 1),
        "p95_elapsed_ms": p95,
        "by_recipe": [{"recipe": k, "runs": v} for k, v in sorted(by_recipe.items(), key=lambda x: -x[1])],
    }


@router.get("/annotations")
def get_copilot_annotations(
    symbol: str = Query(...),
    market: str = Query(default="CN"),
    period: str = Query(default="1d"),
    db: Session = Depends(get_db),
    user: Any = Depends(get_current_user),
) -> list[dict]:
    """K 线图层标注（P1 收尾，2026-10-20）。

    只返回本人 + 全局(user_id IS NULL)的标注；AI 与用户画的都在这张表里，
    用 `source` 区分。前端按 symbol/period 取，直接画到图上。
    """
    from sqlalchemy import or_

    from src.db.models import KlineAnnotation

    # 2026-10-20 线上实测: AI 提议里 market 默认写 "CN"，而 K 线页传 "SH"/"SZ"，
    # 严格等值匹配会导致"标注确认成功但图上查不到"。A 股市场代码统一按一组处理。
    _A_SHARE = {"CN", "SH", "SZ", "BJ"}
    markets = sorted(_A_SHARE) if (market or "").upper() in _A_SHARE else [market]
    rows = (
        db.query(KlineAnnotation)
        .filter(
            KlineAnnotation.symbol == symbol,
            KlineAnnotation.market.in_(markets),
            KlineAnnotation.period == period,
            or_(KlineAnnotation.user_id == str(user.id), KlineAnnotation.user_id.is_(None)),
        )
        .order_by(KlineAnnotation.created_at.desc())
        .limit(200)
        .all()
    )
    return [{
        "id": r.id, "price": r.price, "ts": r.ts, "label": r.label,
        "note": r.note, "color": r.color, "source": r.source,
        "created_at": str(r.created_at or ""),
    } for r in rows]


@router.get("/governance/cost")
def get_copilot_cost(
    days: int = Query(default=7, ge=1, le=90),
    db: Session = Depends(get_db),
    user: Any = Depends(get_current_user),
) -> dict:
    """成本看板：按日 / 按模型 / 按用户聚合 token 与估算费用。

    未配置单价的模型 cost 返回 null 并列进 `unpriced_models` —— 不编造金额。
    普通用户只能看自己那份（by_user 只剩自己），owner/admin 看全量。
    """
    from src.core.copilot_quota import cost_summary

    data = cost_summary(db, days=days, price_per_mtoken=_llm_prices())
    if not _is_admin(user):
        uid = str(getattr(user, "id", ""))
        data["by_user"] = [r for r in data["by_user"] if r.get("user_id") == uid]
        data["scope"] = "self"
    else:
        data["scope"] = "all"
    return data


@router.get("/governance/quota")
def get_copilot_quota(
    db: Session = Depends(get_db),
    user: Any = Depends(get_current_user),
) -> dict:
    """配额列表 + 各自今日用量。普通用户只看自己。"""
    from src.core.copilot_quota import get_quota, list_all, usage_today

    if _is_admin(user):
        return {"scope": "all", "items": list_all(db)}
    uid = str(getattr(user, "id", ""))
    row = get_quota(db, uid)
    return {
        "scope": "self",
        "items": [{
            "user_id": uid,
            "daily_runs": getattr(row, "daily_runs", None),
            "daily_tokens": getattr(row, "daily_tokens", None),
            "enabled": bool(getattr(row, "enabled", True)) if row else True,
            "note": getattr(row, "note", None),
            "used_runs": usage_today(db, uid)["runs"],
            "used_tokens": usage_today(db, uid)["tokens"],
            "updated_at": str(getattr(row, "updated_at", "") or ""),
        }],
    }


@router.put("/governance/quota/{user_id}")
def put_copilot_quota(
    user_id: str,
    body: dict,
    db: Session = Depends(get_db),
    user: Any = Depends(get_current_user),
) -> dict:
    """设置某人配额（仅 owner/admin）。body: {daily_runs, daily_tokens, enabled, note}。

    `null` = 该维度不限；两个都 null 等于不限。
    """
    if not _is_admin(user):
        raise HTTPException(status_code=403, detail="只有管理员可以修改配额")
    from src.core.copilot_quota import set_quota

    def _int_or_none(v):
        if v is None or v == "":
            return None
        n = int(v)
        if n < 0:
            raise HTTPException(status_code=400, detail="配额不能为负数")
        return n

    row = set_quota(
        db, user_id,
        daily_runs=_int_or_none(body.get("daily_runs")),
        daily_tokens=_int_or_none(body.get("daily_tokens")),
        enabled=bool(body.get("enabled", True)),
        note=(body.get("note") or None),
        updated_by=getattr(user, "id", None),
    )
    return {
        "user_id": row.user_id, "daily_runs": row.daily_runs,
        "daily_tokens": row.daily_tokens, "enabled": bool(row.enabled),
        "note": row.note, "updated_at": str(row.updated_at or ""),
    }


def _is_admin(user) -> bool:
    return str(getattr(user, "role", "") or "").lower() in ("owner", "admin")


def _llm_prices() -> dict:
    """模型单价表（元 / 百万 token，混合口径）。

    来源：`settings` 表的 `llm_price_per_mtoken`（JSON，可热改），缺省用内置保守值。
    没有配置的模型一律 cost=None —— 宁可显示"—"，不编一个假金额。
    """
    import json

    try:
        from src.db.models import AppSettings
        from src.db.session import SessionLocal

        with SessionLocal() as db:
            row = db.query(AppSettings).filter(AppSettings.key == "llm_price_per_mtoken").one_or_none()
            if row and row.value:
                return json.loads(row.value)
    except Exception:  # noqa: BLE001 - 没有配置表/解析失败都退回内置值
        pass
    return {
        "gemini": 0.0,      # 订阅制，无按量计费
        "deepseek": 2.0,
        "glm": 1.0,
        "kimi": 1.0,
        "gpt-4o-mini": 1.0,
    }
