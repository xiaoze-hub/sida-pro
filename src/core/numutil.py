"""统一数值工具(audit P1, 2026-09-15)。

消除多处重复实现:
- safe_float: 原 7 处 _safe_float(entry_candidates / strategy_engine / paper_trading_engine /
  price_alert_engine / intraday_event_gate / market_sentiment_collector / board_snapshot)
- clamp: 原 2 处 _clamp(entry_candidates / strategy_engine)
"""
from __future__ import annotations

import math
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


def finite_float(value: Any, default: float | None = None) -> float | None:
    """转 float 且**只接受有限数**; None/非法/NaN/±Inf 一律返回 default。

    为什么单独一个函数(2026-10-29 三审, R2): `float(nan)`/`float(inf)` 能通过
    所有 `if value:` 判定, 一路流到 `round()` 和 JSON 序列化 —— Python 的 json 会把
    NaN/Infinity 原样写成 `NaN`/`Infinity` (**不是合法 JSON**), 前端 `JSON.parse` 直接抛,
    整个看板白屏。历史库里的脏成本价就是这条路径的来源。
    """
    try:
        if value is None or isinstance(value, bool):
            return default
        f = float(value)
    except (TypeError, ValueError):
        return default
    return f if math.isfinite(f) else default


def json_safe(value: Any) -> Any:
    """递归把结构里的非有限浮点(NaN/±Inf)换成 None, 保证 `json.dumps(allow_nan=False)`
    一定能成功。

    用在**缓存载荷**这种"写进去还要原样吐给 HTTP"的地方(2026-10-29 四审):
    `decision_cache` 的 payload 曾被 `json.dumps` 默认写出 `NaN` 字面量, 读回来仍是
    `float('nan')`, FastAPI 序列化时再写一次 NaN —— 前端 `JSON.parse` 直接失败。
    dict/list/tuple 递归; 其它类型(含 Decimal/datetime)原样返回, 由调用方的 default 处理。
    """
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    return value
