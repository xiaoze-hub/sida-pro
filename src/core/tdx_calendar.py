# -*- coding: utf-8 -*-
"""通达信交易日历/复权因子交叉校验(P1, 2026-09-13)。

- tdx_trading_dates(start,end): 通达信交易日历(权威), 与自建 trading_calendar 交叉校验,
  返回差异列表(本地有TDX无 / TDX有本地无), 供数据质量告警(不自动改本地日历)。
- tdx_divid_factors(symbol): 分红送配/复权因子(权威), 供 qfq 复权交叉校验(复权污染是历史坑 KI-011)。
离线/失败 → None/空, 不编。
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def tdx_trading_dates(start: str, end: str) -> list[str] | None:
    """通达信交易日历 [start,end] (yyyymmdd 列表); 失败 None。"""
    try:
        from marketdata.vendors.tq import tq_rpc

        v = tq_rpc("get_trading_dates", {"start_date": start, "end_date": end})
    except Exception as e:  # noqa: BLE001
        logger.warning("TDX 交易日历失败: %s", e)
        return None
    if not isinstance(v, dict):
        return None
    dates = v.get("Date") or []
    return [str(d) for d in dates if str(d).strip()]


def calendar_mismatch(start: str, end: str) -> dict:
    """本地 trading_calendar vs 通达信 差异; 任一源不可用 → {"ok": False}。"""
    tdx = tdx_trading_dates(start, end)
    if not tdx:
        return {"ok": False, "reason": "tdx_unavailable"}
    try:
        from src.core.trading_calendar import is_trading_day

        from datetime import datetime, timedelta

        local = []
        d = datetime.strptime(start, "%Y%m%d")
        end_dt = datetime.strptime(end, "%Y%m%d")
        while d <= end_dt:
            if is_trading_day(d):
                local.append(d.strftime("%Y%m%d"))
            d += timedelta(days=1)
    except Exception as e:  # noqa: BLE001
        logger.warning("本地交易日历失败: %s", e)
        return {"ok": False, "reason": f"local_error:{e}"}
    tdx_set, local_set = set(tdx), set(local)
    return {
        "ok": True,
        "local_only": sorted(local_set - tdx_set),
        "tdx_only": sorted(tdx_set - local_set),
    }


def tdx_divid_factors(symbol: str) -> list[list[str]] | None:
    """通达信分红送配/复权因子(每行一组因子); 失败 None。"""
    try:
        from marketdata.symbol import Symbol as _Symbol
        from marketdata.vendors.tq import to_tq_code as _to_tq, tq_rpc as _tq_rpc

        tqc = _to_tq(_Symbol.parse(symbol, "CN"))
        if not tqc:
            return None
        v = _tq_rpc("get_divid_factors", {"stock_code": tqc})
    except Exception as e:  # noqa: BLE001
        logger.warning("TDX 复权因子失败 %s: %s", symbol, e)
        return None
    if not isinstance(v, list):
        return None
    return [list(row) for row in v if isinstance(row, (list, tuple))]
