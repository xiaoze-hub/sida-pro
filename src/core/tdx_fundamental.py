# -*- coding: utf-8 -*-
"""个股基本面/股本/次新(P2, 2026-09-13, 通达信)。

- fetch_gb(symbol): get_gb_info(需 date_list/count) → 最近一条 {date, ltgb(流通股本), zgb(总股本)}。
- fetch_listing(symbol): get_stock_info → {name, listing_date(J_start), unit, ...} → 次新股判定(上市<1年)。
- 基本面 PE/PB/股息: 复用 get_more_info(stock_l2.fetch_more 已含 pe/pb/dy), 不新增 RPC。
- get_report_data MCP 不支持 → 研报卡不做(老板知情)。
离线/失败 → None/空, 不编。
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def _tq(symbol: str):
    from marketdata.symbol import Symbol as _Symbol
    from marketdata.vendors.tq import to_tq_code as _to_tq

    return _to_tq(_Symbol.parse(symbol, "CN"))


def fetch_gb(symbol: str) -> dict | None:
    """最近一条股本数据 {date, ltgb(流通股本), zgb(总股本)}; 失败 None。

    get_gb_info 需 date_list+count 同时给(探针: 只给 date_list → ErrorId=9;
    count+type → 缺 market/list_type; date_list+count → OK)。给近 5 天 + count=1 取最新一条。
    """
    try:
        from datetime import datetime, timedelta

        from marketdata.vendors.tq import tq_rpc

        tqc = _tq(symbol)
        if not tqc:
            return None
        now = datetime.now()
        date_list = [(now - timedelta(days=i)).strftime("%Y%m%d") for i in range(5)]
        v = tq_rpc("get_gb_info", {"stock_code": tqc, "date_list": date_list, "count": 1})
    except Exception as e:  # noqa: BLE001
        logger.warning("TDX 股本失败 %s: %s", symbol, e)
        return None
    if not isinstance(v, list) or not v:
        return None
    row = v[0] if isinstance(v[0], dict) else {}
    return {"date": str(row.get("Date") or ""),
            "ltgb": _f(row.get("Ltgb")), "zgb": _f(row.get("Zgb"))}


def fetch_listing(symbol: str) -> dict | None:
    """上市信息 {name, listing_date, unit}; 失败 None。次新=上市<365天(调用方判)。"""
    try:
        from marketdata.vendors.tq import tq_rpc

        tqc = _tq(symbol)
        if not tqc:
            return None
        v = tq_rpc("get_stock_info", {"stock_code": tqc, "field_list": []})
    except Exception as e:  # noqa: BLE001
        logger.warning("TDX 上市信息失败 %s: %s", symbol, e)
        return None
    if not isinstance(v, dict):
        return None
    return {"name": str(v.get("Name") or ""),
            "listing_date": str(v.get("J_start") or ""),
            "unit": str(v.get("Unit") or "")}


def is_sub_new(listing_date: str, now=None) -> bool | None:
    """上市<365天=次新; listing_date 缺失/解析失败 → None(不猜)。"""
    if not listing_date or len(listing_date) < 8:
        return None
    try:
        from datetime import datetime

        d = datetime.strptime(listing_date[:8], "%Y%m%d")
        now = now or datetime.now()
        return (now - d).days < 365
    except ValueError:
        return None


def _f(v) -> float | None:
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None
