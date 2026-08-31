"""thsdk L2 综合快照 API（v0.3.0 阶段 1.3）

一次性返回个股 L2 全貌:
- 基础行情(最新价/开盘/最高/最低/成交量)
- 20档盘口
- 大单净额+主力拆分
- 板块归属
- 异动信号

包装 thsdk_l2.get_comprehensive_snapshot() + 增量查询。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from src.web.api.auth import get_current_user

logger = logging.getLogger(__name__)

# 模块层 import 让 monkeypatch.setattr 能替换(否则 lazy import 是局部变量)
try:
    from data_source.thsdk_l2 import get_comprehensive_snapshot  # noqa: F401
except Exception:
    get_comprehensive_snapshot = None  # type: ignore[assignment]

router = APIRouter()

# 进程内缓存,30s 过期(避免重复拉 thsdk)
_SNAP_CACHE: dict = {}
_SNAP_TTL = 30.0


def _to_ths_symbol(symbol: str) -> str:
    """6 位代码 → thsdk 格式(USZA/USHK/UNQ)。当前只支持 A 股。"""
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
    # 默认深市
    return f"USZA{symbol}"


def _fetch_snapshot(symbol: str) -> dict:
    """调 thsdk 综合快照(带缓存+容错)。"""
    import time as _time

    now = _time.time()
    cached = _SNAP_CACHE.get(symbol)
    if cached and (now - cached[0]) < _SNAP_TTL:
        return cached[1]

    try:
        # 模块层属性优先(测试 mock 用),否则 lazy import
        fn = globals().get("get_comprehensive_snapshot")
        if fn is None:
            from data_source.thsdk_l2 import get_comprehensive_snapshot

            fn = get_comprehensive_snapshot

        ths_symbol = _to_ths_symbol(symbol)
        snap = fn(ths_symbol)
        # 标准化返回
        out = {
            "symbol": symbol,
            "thsdk_symbol": ths_symbol,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "quote": snap.get("quote") if isinstance(snap, dict) else None,
            "depth": snap.get("depth") if isinstance(snap, dict) else None,
            "main_flow": snap.get("main_flow") if isinstance(snap, dict) else None,
            "sectors": snap.get("sectors", []) if isinstance(snap, dict) else [],
            "warnings": [],
        }
    except Exception as e:  # noqa: BLE001
        logger.warning(f"thsdk snapshot 失败 {symbol}: {e}", exc_info=True)
        out = {
            "symbol": symbol,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "quote": None,
            "depth": None,
            "main_flow": None,
            "sectors": [],
            "warnings": [f"thsdk 数据不可用: {e}"],
        }

    _SNAP_CACHE[symbol] = (now, out)
    return out


@router.get("/{symbol}")
def snapshot(symbol: str, user=Depends(get_current_user)) -> dict:
    """个股 L2 综合快照。

    返回字段:
    - symbol / thsdk_symbol / fetched_at
    - quote: 最新行情 dict
    - depth: 20档盘口 {bid: [...], ask: [...]}
    - main_flow: 主力净额+大单拆分
    - sectors: 板块归属 list
    - warnings: 异常说明 list(降级时填)
    """
    try:
        return _fetch_snapshot(symbol)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except Exception as e:  # noqa: BLE001
        logger.error(f"snapshot 失败 {symbol}: {e}", exc_info=True)
        raise HTTPException(500, f"snapshot 失败: {e}") from e


@router.post("/cache/invalidate")
def invalidate_cache(symbol: Optional[str] = None, user=Depends(get_current_user)) -> dict:
    """手动失效缓存(测试用)。不传 symbol 清全部。"""
    if symbol:
        _SNAP_CACHE.pop(symbol, None)
        return {"invalidated": [symbol]}
    _SNAP_CACHE.clear()
    return {"invalidated": "all"}