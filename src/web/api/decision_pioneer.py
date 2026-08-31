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
    return data


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
