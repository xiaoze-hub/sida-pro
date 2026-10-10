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

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from src.web.api._scope import scoped
from src.web.api.auth import get_current_user
from src.web.database import get_db
from src.web.models import Position, Stock, User

logger = logging.getLogger(__name__)

router = APIRouter()

# 交易风格 → 风险偏好档位(现有用户数据源: 持仓 trading_style; 无持仓 → None, 不硬套)
_STYLE_TO_RISK = {"short": "aggressive", "swing": "balanced", "long": "conservative"}


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
    """三信号合成决策。永不 500: 算不出就看看 + 理由。"""
    from src.core.permissions import PERM_VIEW_FORECAST, enforce_perm

    enforce_perm(user, PERM_VIEW_FORECAST, db)
    from src.core.decision import decide

    ctx = _build_user_context(db, user, symbol, market)
    try:
        return await asyncio.to_thread(decide, symbol, market, 120, ctx)
    except Exception as e:  # noqa: BLE001
        logger.warning("decision endpoint %s failed: %s", symbol, e)
        raise HTTPException(500, f"决策计算失败({symbol})")
