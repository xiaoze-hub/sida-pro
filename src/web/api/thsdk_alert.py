"""thsdk_alert.run() API 化(v0.3.0 阶段 1.4)

把 src/core/thsdk_alert.py::run() 暴露为 REST API。
原 run() 输出:{close_surge, auction, wencai_pool}
"""
from __future__ import annotations

import logging
import time as _time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from src.web.api.auth import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter()

# 进程内缓存(盘中同一symbol 频繁调,缓存节省 thsdk 限频)
_ALERT_CACHE: dict = {}
_ALERT_TTL = 60.0  # 60s 过期


def _to_ths_symbol(symbol: str) -> str:
    """与 thsdk_snapshot 共用的代码转换(本地复制避免 import 链)。"""
    symbol = symbol.strip().upper()
    if symbol.startswith(("US", "UH", "UQ")):
        return symbol
    if not symbol.isdigit() or len(symbol) != 6:
        raise ValueError(f"无效代码: {symbol}")
    if symbol.startswith(("60", "68", "90", "11", "13")):
        return f"USHA{symbol}"
    if symbol.startswith(("30", "00", "20")):
        return f"USZA{symbol}"
    if symbol.startswith(("83", "87", "43")):
        return f"USTM{symbol}"
    return f"USZA{symbol}"


def _run_alert(symbol: str, date: Optional[str] = None) -> dict:
    now = _time.time()
    cache_key = f"{symbol}:{date or 'today'}"
    cached = _ALERT_CACHE.get(cache_key)
    if cached and (now - cached[0]) < _ALERT_TTL:
        return cached[1]

    try:
        from src.core.thsdk_alert import run

        ths_symbol = _to_ths_symbol(symbol)
        result = run(ths_symbol, date=date)
        out = {
            "symbol": symbol,
            "thsdk_symbol": ths_symbol,
            "date": date or "today",
            "close_surge": result.get("close_surge") if isinstance(result, dict) else None,
            "auction": result.get("auction") if isinstance(result, dict) else None,
            "wencai_pool": result.get("wencai_pool") if isinstance(result, dict) else None,
            "warnings": [],
        }
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except Exception as e:  # noqa: BLE001
        logger.warning(f"thsdk_alert 失败 {symbol}: {e}", exc_info=True)
        out = {
            "symbol": symbol,
            "date": date or "today",
            "close_surge": None,
            "auction": None,
            "wencai_pool": None,
            "warnings": [f"thsdk_alert 不可用: {e}"],
        }

    _ALERT_CACHE[cache_key] = (now, out)
    return out


@router.get("/{symbol}")
def alert(
    symbol: str,
    date: Optional[str] = None,
    user=Depends(get_current_user),
) -> dict:
    """个股 thsdk 三大算法输出:尾盘大单突击 + 竞价快照 + wencai 候选池。

    Query:
    - date: YYYYMMDD 历史日期(默认今日)
    """
    return _run_alert(symbol, date)