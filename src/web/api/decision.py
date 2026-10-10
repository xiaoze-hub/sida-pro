"""决策合成口(2026-09-08 方向2): GET /api/decision/{symbol} → 动手/看看/别碰 + 一行理由。

权限(2026-09-15): view_forecast — member 3次/天试用, pro/owner 全开。

个性化(2026-10-10 P1-2): 端点按**当前用户**组装决策上下文(自选/持仓/风险偏好),
经 `decide(..., user_context)` 传入合成层做透明微调。user_id 隔离是 AGENTS 硬约束 ——
上下文只读当前用户(自己的 + 全局共享), 禁跨用户混用; 读不到上下文 → 传 None,
合成层走全局口径(向后兼容零破坏)。
跨市场(2026-10-10 P2-7): market=HK/US 时合成层自动走双维诚实降级(资金维无数据(非CN))。
"""
import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from src.web.api._scope import scoped
from src.web.api.auth import get_current_user
from src.web.database import get_db
from src.web.models import Position, Stock, User

logger = logging.getLogger(__name__)

router = APIRouter()

# 交易风格 → 风险偏好档位(现有用户数据源: 持仓 trading_style; 无持仓 → None, 不硬套)
_STYLE_TO_RISK = {"short": "aggressive", "swing": "balanced", "long": "conservative"}


def _attach_evidence(out: dict) -> dict:
    """证据化(2026-10-10): 给决策响应**追加**证据链 + 历史相似情形(**不改 verdict/理由**)。

    触发条件/as_of/失效条件由 `src.core.evidence_chain` 从决策自身**确定性字段**拼出(不编);
    相似情形只读决策账本(decision_log.stats)聚合。任何失败一律**旁路降级**(缺字段), 绝不影响
    verdict 与理由。**只读** —— 不改 `src/core/decision.py`(合成本体)与 `decision_log.py`(账本本体)。
    """
    try:
        from src.core.evidence_chain import build_decision_evidence, load_stats

        extra = build_decision_evidence(out, stats=load_stats())
        out.setdefault("evidence", extra.get("evidence"))
        out.setdefault("similar", extra.get("similar"))
    except Exception as e:  # noqa: BLE001 —— 证据是旁路, 失败不阻断决策
        logger.debug("decision evidence 装配失败(跳过): %r", e)
    return out


def _infer_risk_profile(styles: list[str]) -> str | None:
    """由用户持仓的交易风格推断风险偏好档位。

    多数派规则: 某档位需**过半**才定档, 否则 balanced(不硬套); 无持仓风格 → None
    (合成层不做阈值调整)。这是现有用户数据源(Position.trading_style)的口径,
    非独立风险问卷(那属新产品面, 不在本次范围)。
    """
    styles = [s for s in styles if s]
    if not styles:
        return None
    from collections import Counter

    top, n = Counter(styles).most_common(1)[0]
    if n * 2 <= len(styles):
        return "balanced"
    return _STYLE_TO_RISK.get(top, "balanced")


def _build_user_context(db: Session, user: User, symbol: str, market: str) -> dict | None:
    """组装当前用户决策上下文(自选/持仓/风险偏好)。user_id 严格隔离。

    任何异常/读取失败 → 返回 None(合成层走全局口径, 零破坏)。
    """
    code = (symbol or "").strip()
    mkt = (market or "CN").upper()
    try:
        stocks = scoped(db.query(Stock), user).all()
        positions = scoped(db.query(Position), user).all()
    except Exception as e:  # noqa: BLE001
        logger.debug("decision user-context 读取失败: %r", e)
        return None

    by_id = {s.id: s for s in stocks}
    in_watchlist = any(
        s.symbol == code and (s.market or "").upper() == mkt for s in stocks
    )
    holds = False
    cost_price = None
    quantity = None
    for p in positions:
        s = by_id.get(p.stock_id)
        if s and s.symbol == code and (s.market or "").upper() == mkt:
            holds = True
            cost_price = p.cost_price
            quantity = p.quantity
            break
    return {
        "risk_profile": _infer_risk_profile([p.trading_style for p in positions]),
        "holds": holds,
        "cost_price": cost_price,
        "quantity": quantity,
        "in_watchlist": in_watchlist,
    }


@router.get("/{symbol}")
async def get_decision(
    symbol: str,
    market: str = "CN",
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """三信号合成决策。永不 500: 算不出就看看 + 理由。

    冗余设计(2026-10-10): **缓存优先** —— `decision_cache` 表存**全局基底**(盘后批算
    或端上短 TTL 写入)。命中直返, 免重复现算; 用户维度(自选/持仓/风险偏好)在**读取时**
    用 `apply_user_overlay` 叠加, 不污染全局缓存行。响应带 `cached` / `computed_at`
    语义字段 —— **不用缓存冒充实时**(`computed_at` 即该结论的计算时刻)。
    """
    from src.core.permissions import PERM_VIEW_FORECAST, enforce_perm

    enforce_perm(user, PERM_VIEW_FORECAST, db)
    from src.core import decision_cache

    mkt = (market or "CN").upper()
    ctx = _build_user_context(db, user, symbol, market)

    # ① 缓存优先(预落库基底 / 端上 TTL 同表): 命中直接返, 不重算。
    rec = await asyncio.to_thread(decision_cache.get_cached_decision, symbol, mkt)
    if rec is not None:
        out = decision_cache.apply_user_overlay(rec["payload"], ctx)
        out["cached"] = True
        out["computed_at"] = rec["computed_at"]
        out["cache_source"] = rec["source"]
        return _attach_evidence(out)

    # ② miss: 算**全局基底**(无用户上下文) → 落库(短 TTL) → 读取时叠加个性化。
    try:
        base = await asyncio.to_thread(decision_cache.compute_base, symbol, mkt, 120)
    except Exception as e:  # noqa: BLE001
        logger.warning("decision endpoint %s failed: %s", symbol, e)
        raise HTTPException(500, f"决策计算失败({symbol})")
    written = await asyncio.to_thread(
        decision_cache.put_cached_decision, symbol, mkt, base,
        decision_cache.DECISION_TTL_S, decision_cache.SOURCE_TTL,
    )
    out = decision_cache.apply_user_overlay(base, ctx)
    out["cached"] = False
    out["computed_at"] = written or decision_cache.iso_utc(decision_cache.now_utc_naive())
    out["cache_source"] = decision_cache.SOURCE_TTL
    return _attach_evidence(out)


@router.post("/precompute")
def trigger_precompute(
    limit: int | None = Query(None, ge=1, le=2000),
    user: User = Depends(get_current_user),
):
    """手动触发盘后批算(自选/持仓标的预落库)。后台线程执行, 立即返回。

    2026-10-10 冗余设计: 平时由 `DecisionPrecomputeScheduler`(工作日 15:45)自动跑,
    本端点供运维/验证手动补跑。仅 owner/admin 可用(全库批算较重)。
    """
    if str(getattr(user, "role", "") or "").lower() not in ("owner", "admin"):
        raise HTTPException(403, "仅 owner/admin 可触发决策预落库")

    import threading

    from src.core import decision_precompute

    def _runner() -> None:
        try:
            decision_precompute.run_precompute_job()
        except Exception as e:  # noqa: BLE001
            logger.warning("决策预落库手动触发失败: %s", e)

    threading.Thread(target=_runner, name="decision-precompute-manual", daemon=True).start()
    return {"started": True, "limit": limit}
