"""数智决策辅助指标 API(趋势操盘线 / 牛熊线 / 分时突破, P3 补差, 2026-10-10)。

- `GET /api/indicators/trend-line/{symbol}?market=CN`        趋势操盘线(三线+买卖点)
- `GET /api/indicators/niuxiong/{symbol}?market=CN`          牛熊线(金叉/死叉 B/S)
- `GET /api/indicators/minute-breakthrough/{symbol}?market=CN`  分时突破(突/积)

口径: 信号是**证据不是建议** —— 只回客观字段(枚举信号 + 触发时间/价格 + 触发条件),
不含"建议买入/卖出"等主观措辞。缺数据显式 `available=false` + `note`(不编造、不 500)。

诚实降级: minute-breakthrough 需**分钟数据 + DDE 大单序列**两条输入, 现有 DDE 链只有当日
快照 → 生产上多为显式降级, 直到接入逐分钟大单流。

权限: 数智决策档(view_forecast, 与 /api/decision-pioneer 同档)。
30s 结果缓存走 biz_cache(L1 内存 + L2 Redis, 跨 worker 共享 + 重启不丢)。
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException

from src.web.cache.biz_cache import biz_cache

logger = logging.getLogger(__name__)

router = APIRouter()

_CST = ZoneInfo("Asia/Shanghai")

# ──────────── 30s 结果缓存(2026-10-14 缓存收口) ────────────
# 原实现是模块内 dict(_CACHE): 多 worker 各存各的 + 重启即丢, 且违反 AGENTS
# 『缓存一律走 src/web/cache/biz_cache.py, 禁止业务代码自建』。现收口到 biz_cache
# (L1 内存 + L2 Redis), TTL 仍是 30s 语义(盘中多用户/多轮询防重复重算)。
_CACHE_NS = "pioneer_indicators"
_TTL = 30  # 秒(biz_cache TTL 精度为秒, 与原 30.0s 语义一致)
# 本模块写过的键登记(仅用于 clear_* 精准清理 —— biz_cache 无按前缀枚举接口,
# 又不许业务代码裸连 Redis 扫键)。进程内最佳努力, 生产不需要它。
_CACHE_KEYS: set[str] = set()
_CACHE_KEYS_LOCK = threading.Lock()


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
    """30s 结果缓存: biz_cache(L1 内存 + L2 Redis) —— 跨 worker 共享、重启不丢。

    与原进程内 dict 行为差异: 命中语义不变(30s 内同键直接返回), 但多 worker /
    多容器现在读同一份缓存; 降级: Redis 不可达时 biz_cache 自动退回纯 L1,
    等价于原进程内行为(不抛、不阻塞)。
    """
    full_key = f"{_CACHE_NS}:{key}"
    cached = biz_cache.get_json(full_key)
    if cached is not None:
        return cached
    data = fn()
    if data is not None:
        biz_cache.set_json(full_key, data, ttl=_TTL)
        with _CACHE_KEYS_LOCK:
            _CACHE_KEYS.add(full_key)
    return data


def clear_pioneer_indicators_cache() -> None:
    """只清本模块写过的缓存键(不误伤其他业务缓存)。运维/测试用。"""
    with _CACHE_KEYS_LOCK:
        keys = list(_CACHE_KEYS)
        _CACHE_KEYS.clear()
    if keys:
        try:
            biz_cache.delete(*keys)
        except Exception as e:  # noqa: BLE001
            logger.debug("pioneer_indicators 清缓存失败(静默): %s", e)


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


@router.get("/minute-breakthrough/{symbol}")
def get_minute_breakthrough(symbol: str, market: str = "CN"):
    """分时突破: 「突/积」信号 + 触发时间 + 触发条件清单(缺输入显式降级)。"""
    mkt = (market or "CN").upper()
    code = _valid_symbol(symbol, mkt)
    payload = _cached(f"minute:{mkt}:{code}", lambda: _run_minute(code, mkt))
    return {"symbol": code, "market": mkt, "indicator": "minute-breakthrough",
            "data_time": _now(), **payload}


def _run_minute(code: str, mkt: str) -> dict:
    try:
        from src.core.minute_breakthrough import fetch_minute_breakthrough

        return fetch_minute_breakthrough(code, market=mkt)
    except Exception as e:  # noqa: BLE001
        logger.warning("minute-breakthrough %s 失败: %s", code, e)
        return _unavailable("分时突破取数异常, 已降级")
