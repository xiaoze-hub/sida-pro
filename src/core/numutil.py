"""统一数值工具(audit P1, 2026-09-15)。

消除多处重复实现:
- safe_float: 原 7 处 _safe_float(entry_candidates / strategy_engine / paper_trading_engine /
  price_alert_engine / intraday_event_gate / market_sentiment_collector / board_snapshot)
- clamp: 原 2 处 _clamp(entry_candidates / strategy_engine)
"""
from __future__ import annotations

from typing import Any


def safe_float(value: Any, default: float | None = None) -> float | None:
    """安全转 float。None/非法输入不抛异常, 返回 default(默认 None)。

    需要 0.0 兜底的调用方(如行情字段展示)显式传 default=0.0;
    需要区分"缺失/解析失败"的调用方保持默认 None(下游 is not None 判定)。
    """
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def clamp(value: float, lo: float, hi: float) -> float:
    """将 value 限制在闭区间 [lo, hi]。"""
    return max(lo, min(hi, value))
