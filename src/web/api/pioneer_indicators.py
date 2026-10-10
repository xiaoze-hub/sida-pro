"""决策先锋辅助指标 API(趋势操盘线 / 牛熊线, P3 补差, 2026-10-10)。

- `GET /api/indicators/trend-line/{symbol}?market=CN`  趋势操盘线(三线+买卖点)
- `GET /api/indicators/niuxiong/{symbol}?market=CN`    牛熊线(金叉/死叉 B/S)

口径: 信号是**证据不是建议** —— 只回客观字段(枚举信号 + 触发时间/价格 + 触发条件),
不含"建议买入/卖出"等主观措辞。缺数据显式 `available=false` + `note`(不编造、不 500)。

权限: 数智决策档(view_forecast, 与 /api/decision-pioneer 同档)。
进程内 30s 缓存(盘中多用户/多轮询防重复重算)。
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException

logger = logging.getLogger(__name__)

router = APIRouter()

_CST = ZoneInfo("Asia/Shanghai")
_CACHE: dict[str, tuple[float, dict]] = {}
_TTL = 30.0


def _valid_symbol(raw: str, market: str = "CN") -> str:
    """按市场校验股票代码: CN=6位数字 / HK=5位数字 / 其余(US)字母数字放宽。"""
    code = (raw or "").strip()
    mkt = (market or "CN").upper()
    if mkt == "CN":
        ok = code.isdigit() and len(code) == 6
    elif mkt == "HK":
        ok = code.isdigit() and len(code) == 5
    else:
        ok = bool(code) and all(ch.isalnum() or ch in ".-" for ch in code)
    if not ok:
        raise HTTPException(400, f"非法股票代码: {raw!r}(市场 {mkt})")
    return code


def _now() -> str:
    return datetime.now(_CST).isoformat(timespec="seconds")


def _cached(key: str, fn):
    now = time.time()
    hit = _CACHE.get(key)
    if hit and now - hit[0] < _TTL:
        return hit[1]
    data = fn()
    _CACHE[key] = (now, data)
    return data


def _unavailable(note: str, extra: dict | None = None) -> dict:
    out = {"available": False, "degraded": True, "note": note}
    if extra:
        out.update(extra)
    return out


@router.get("/trend-line/{symbol}")
def get_trend_line(symbol: str, market: str = "CN"):
    """趋势操盘线: 红/黄/绿三线 + 多空带 + 买卖点信号(升势回踩买 / 跌势反弹卖)。"""
    mkt = (market or "CN").upper()
    code = _valid_symbol(symbol, mkt)
    payload = _cached(f"trend:{mkt}:{code}", lambda: _run_trend(code, mkt))
    return {"symbol": code, "market": mkt, "indicator": "trend-line",
            "data_time": _now(), **payload}


def _run_trend(code: str, mkt: str) -> dict:
    try:
        from src.core.trend_pilot_line import fetch_trend_line

        r = fetch_trend_line(code, market=mkt)
    except Exception as e:  # noqa: BLE001
        logger.warning("trend-line %s 失败: %s", code, e)
        r = None
    if not r:
        return _unavailable("日K不足或无数据, 无法计算趋势操盘线")
    return r


@router.get("/niuxiong/{symbol}")
def get_niuxiong(symbol: str, market: str = "CN"):
    """牛熊线: 牛线(加权)/马线/买卖线 + 金叉死叉 B/S 信号(买红卖绿)。"""
    mkt = (market or "CN").upper()
    code = _valid_symbol(symbol, mkt)
    payload = _cached(f"niuxiong:{mkt}:{code}", lambda: _run_niuxiong(code, mkt))
    return {"symbol": code, "market": mkt, "indicator": "niuxiong",
            "data_time": _now(), **payload}


def _run_niuxiong(code: str, mkt: str) -> dict:
    try:
        from src.core.niuxiong_line import fetch_niuxiong

        r = fetch_niuxiong(code, market=mkt)
    except Exception as e:  # noqa: BLE001
        logger.warning("niuxiong %s 失败: %s", code, e)
        r = None
    if not r:
        return _unavailable("日K不足或无数据, 无法计算牛熊线")
    return r
