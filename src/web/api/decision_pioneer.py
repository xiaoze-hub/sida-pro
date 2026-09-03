"""决策先锋三指标 API(盘中实时)。

GET /api/decision-pioneer/002361?market=CN
→ {symbol, market, institution_activity, gs, l2, main_intent, data_time}

进程内 30s 缓存(盘中多用户/多轮询防重复重算)。
"""
from __future__ import annotations

import logging
import time

from fastapi import APIRouter, HTTPException

from src.core.decision_pioneer import fetch_decision_pioneer

logger = logging.getLogger(__name__)

router = APIRouter()

# 进程内缓存: {key: (ts, data)}, TTL 30s
_CACHE: dict[str, tuple[float, dict]] = {}
_TTL = 30.0


def _valid_symbol(raw: str) -> str:
    code = (raw or "").strip()
    if not code.isdigit() or len(code) != 6:
        raise HTTPException(400, f"非法股票代码: {raw!r}(需要6位A股代码)")
    return code


def _get(symbol: str, market: str) -> dict:
    key = f"{market}:{symbol}"
    now = time.time()
    hit = _CACHE.get(key)
    if hit and now - hit[0] < _TTL:
        return hit[1]
    data = fetch_decision_pioneer(symbol, market)
    _CACHE[key] = (now, data)
    # 09-03: 新鲜快照落库(历史回查用; best-effort, 失败不影响读链路)
    try:
        from src.core.history_store import record_dp_snapshot

        record_dp_snapshot(symbol, market, data)
    except Exception:  # noqa: BLE001
        pass
    return data


@router.get("/{symbol}/history")
def get_decision_pioneer_history(symbol: str, market: str = "CN", days: int = 30):
    """决策先锋历史快照(09-03 落库回查; 空=该股尚无落库, 不编造)。"""
    code = _valid_symbol(symbol)
    if market.upper() not in ("CN",):
        raise HTTPException(400, "decision-pioneer 仅支持 CN 市场")
    from src.core.history_store import query_dp_history

    return {"symbol": code, "market": market.upper(), "rows": query_dp_history(code, market.upper(), days)}


@router.get("/{symbol}")
def get_decision_pioneer(symbol: str, market: str = "CN"):
    """决策先锋三指标 + L2 主力净流入 + 主力意图(盘中实时)。"""
    code = _valid_symbol(symbol)
    if market.upper() not in ("CN",):
        raise HTTPException(400, "decision-pioneer 仅支持 CN 市场")
    try:
        return _get(code, market.upper())
    except Exception as e:  # noqa: BLE001
        logger.warning("decision-pioneer API %s failed: %s", code, e)
        raise HTTPException(502, f"决策先锋数据获取失败: {e}") from e
