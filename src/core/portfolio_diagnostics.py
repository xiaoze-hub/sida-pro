"""组合诊断(Phase 4):只读分析模拟盘持仓的集中度 / 分布 / 风险。

对标 PortfolioPilot 的「只读不下单」诊断 —— 纯读取持仓,**绝不下单**,只产出诊断与提示。
纯函数 diagnose_positions 可单测;diagnose_paper_portfolio 读 DB。

2026-10-29 缺行情缺陷: 没有可用现价时**没有市值** —— 不得用成本/入场价顶替、不得记 0。
市值与浮盈只在「已定价子集」上统计, 缺失数与覆盖度单独返回, 消费方据此判断可信范围。
"""

from __future__ import annotations

import logging
import math

from src.db.session import SessionLocal
from src.db.models import PaperTradingPosition

logger = logging.getLogger(__name__)

# 风险阈值(可后续配置化)
MAX_SINGLE_WEIGHT = 0.40   # 单仓占比上限
HIGH_HHI = 0.50            # HHI 集中度高线
MAX_MARKET_WEIGHT = 0.70   # 单市场占比上限
MIN_POSITIONS = 3          # 最少分散持仓数


def herfindahl(values: list[float]) -> float:
    """HHI 集中度 = Σ(w_i)²(w 为归一化权重)。范围 [1/n, 1],越大越集中。"""
    total = sum(values)
    if total <= 0:
        return 0.0
    return sum((v / total) ** 2 for v in values)


def _finite(value) -> float | None:
    """数值化; None/非数值/NaN/inf 一律视为"未知"。0 是有效数值, 不动它。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def diagnose_positions(positions: list[dict]) -> dict:
    """纯函数诊断。

    positions: [{symbol, market, strategy_code, market_value, unrealized_pnl}]
    market_value / unrealized_pnl 为 None = 现价不可用(未知), 会被排除在统计之外,
    并在 missing_quote_count / quotes_coverage_pct 里显式声明。
    """
    if not positions:
        return {
            "position_count": 0,
            "priced_position_count": 0,
            "missing_quote_count": 0,
            "quotes_coverage_pct": 100.0,
            "has_complete_quotes": True,
            "total_market_value": 0.0,
            "hhi": 0.0,
            "max_weight": 0.0,
            "by_market": {},
            "by_strategy": {},
            "total_unrealized_pnl": 0.0,
            "alerts": [],
        }

    priced = [p for p in positions if _finite(p.get("market_value")) is not None]
    missing = len(positions) - len(priced)

    values = [max(0.0, _finite(p.get("market_value"))) for p in priced]  # type: ignore[arg-type]
    total = sum(values)
    hhi = herfindahl(values)
    max_w = (max(values) / total) if total > 0 else 0.0

    by_market: dict[str, float] = {}
    by_strategy: dict[str, float] = {}
    for p, v in zip(priced, values):
        m = p.get("market") or "?"
        s = p.get("strategy_code") or "?"
        by_market[m] = by_market.get(m, 0.0) + v
        by_strategy[s] = by_strategy.get(s, 0.0) + v

    # 浮盈同理: 只汇总已定价持仓; 全都没价时是"未知"而不是 0。
    upnl_values = [u for u in (_finite(p.get("unrealized_pnl")) for p in priced) if u is not None]
    upnl = round(sum(upnl_values), 2) if upnl_values else None

    coverage_pct = round(len(priced) / len(positions) * 100, 2)

    alerts: list[str] = []
    if missing:
        alerts.append(
            f"{missing} 只持仓缺行情(无有效现价), 市值/浮盈仅统计已定价的 {len(priced)} 只"
            f"(覆盖 {coverage_pct:.0f}%)"
        )
    if max_w >= MAX_SINGLE_WEIGHT:
        alerts.append(f"单仓集中度过高:最大持仓占 {max_w * 100:.0f}%")
    if hhi >= HIGH_HHI:
        alerts.append(f"组合高度集中(HHI={hhi:.2f})")
    if len(positions) < MIN_POSITIONS and total > 0:
        alerts.append(f"持仓数过少({len(positions)}),分散不足")
    if total > 0:
        for m, v in by_market.items():
            if v / total >= MAX_MARKET_WEIGHT:
                alerts.append(f"{m} 市场占比过高({v / total * 100:.0f}%)")

    return {
        "position_count": len(positions),
        "priced_position_count": len(priced),
        "missing_quote_count": missing,
        "quotes_coverage_pct": coverage_pct,
        "has_complete_quotes": missing == 0,
        # 无任何已定价持仓时不冒充 0 市值 —— 直接声明未知。
        "total_market_value": round(total, 2) if priced else None,
        "hhi": round(hhi, 4),
        "max_weight": round(max_w, 4),
        "by_market": {k: round(v, 2) for k, v in by_market.items()},
        "by_strategy": {k: round(v, 2) for k, v in by_strategy.items()},
        "total_unrealized_pnl": upnl,
        "alerts": alerts,
    }


def diagnose_paper_portfolio(user_id: str | None = None) -> dict:
    """读模拟盘 open 持仓 → 组合诊断(只读)。2026-09-08 T6: user_id 限定归属用户。"""
    db = SessionLocal()
    try:
        query = db.query(PaperTradingPosition).filter(PaperTradingPosition.status == "open")
        if user_id is None:
            query = query.filter(PaperTradingPosition.user_id.is_(None))
        else:
            query = query.filter(PaperTradingPosition.user_id == user_id)
        rows = query.all()
        positions: list[dict] = []
        for p in rows:
            # 2026-10-29: 原来 current_price 缺失时用 entry_price 兜底 = 用成本冒充市值。
            # 现改为"无现价即无市值", 交给 diagnose_positions 统计缺失与覆盖度。
            price = _finite(p.current_price)
            if price is not None and price <= 0:
                price = None
            qty = int(p.quantity or 0)
            positions.append(
                {
                    "symbol": p.stock_symbol,
                    "market": p.stock_market,
                    "strategy_code": p.strategy_code or "",
                    "market_value": (float(price) * qty) if price is not None else None,
                    "unrealized_pnl": (
                        float(p.unrealized_pnl or 0.0) if price is not None else None
                    ),
                }
            )
        return diagnose_positions(positions)
    finally:
        db.close()
