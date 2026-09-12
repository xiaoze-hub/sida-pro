# -*- coding: utf-8 -*-
"""单股 L2 字段接入(v0.5.89, 通达信 get_market_snapshot + get_more_info)。

字段来源(通达信文档 P47-49 snapshot / P55-56 more_info, 离线探针 2026-09-13 确认非交易时段亦有值):
  snapshot: Now/LastClose/Open/Max/Min/Volume/Amount/Inside/Outside/Before5MinNow/Average/
            Buyp/Buyv/Sellp/Sellv(五档)
  more_info: ZTPrice/DTPrice/FCAmo(封单额,万元; >0涨停<0跌停)/FCb/OpenAmo/EverZTCount(连板)/
             fHSL/fLianB(量比)/Wtb/Zjl/Zjl_HB/L2TicNum/L2OrderNum/TotalBVol/TotalSVol/BCancel/SCancel

诚实约束: 任一字段缺失 → None, 不编; 非交易时段返回最后交易日值, 调用方自判时段标注。
单股接口, 仅供 on-demand(工作台/梯队候选池), 不做全市场批量轮询。
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_CST = ZoneInfo("Asia/Shanghai")
_TICK = 0.005
_DIVE_RATIO = 0.98  # now <= before5min*0.98 → 5分钟跌≥2% 判跳水


def _f(v) -> float | None:
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _lst(v) -> list[float]:
    if not isinstance(v, list):
        return []
    return [x for x in (_f(i) for i in v) if x is not None]


def _rpc_snap(symbol: str) -> dict:
    from marketdata.symbol import Symbol as _Symbol
    from marketdata.vendors.tq import to_tq_code as _to_tq, tq_rpc as _tq_rpc

    tqc = _to_tq(_Symbol.parse(symbol, "CN"))
    if not tqc:
        return {}
    v = _tq_rpc("get_market_snapshot", {"stock_code": tqc})
    return v if isinstance(v, dict) else {}


def _rpc_more(symbol: str) -> dict:
    from marketdata.symbol import Symbol as _Symbol
    from marketdata.vendors.tq import to_tq_code as _to_tq, tq_rpc as _tq_rpc

    tqc = _to_tq(_Symbol.parse(symbol, "CN"))
    if not tqc:
        return {}
    v = _tq_rpc("get_more_info", {"stock_code": tqc, "field_list": []})
    return v if isinstance(v, dict) else {}


def fetch_snapshot(symbol: str) -> dict:
    """snapshot 映射; 缺字段 None。"""
    s = _rpc_snap(symbol)
    if not s:
        return {}
    return {
        "now": _f(s.get("Now")), "last_close": _f(s.get("LastClose")),
        "open": _f(s.get("Open")), "high": _f(s.get("Max")), "low": _f(s.get("Min")),
        "volume": _f(s.get("Volume")), "amount": _f(s.get("Amount")),
        "inside": _f(s.get("Inside")), "outside": _f(s.get("Outside")),
        "before5min": _f(s.get("Before5MinNow")), "average": _f(s.get("Average")),
        "buyp": _lst(s.get("Buyp")), "buyv": _lst(s.get("Buyv")),
        "sellp": _lst(s.get("Sellp")), "sellv": _lst(s.get("Sellv")),
    }


def fetch_more(symbol: str) -> dict:
    """more_info 映射(万元→元 仅 FCAmo/OpenAmo); 缺字段 None。"""
    m = _rpc_more(symbol)
    if not m:
        return {}
    fcamo = _f(m.get("FCAmo"))
    openamo = _f(m.get("OpenAmo"))
    return {
        "zt_price": _f(m.get("ZTPrice")), "dt_price": _f(m.get("DTPrice")),
        "fcamo": (fcamo * 1e4) if fcamo is not None else None,
        "fcb": _f(m.get("FCb")),
        "openamo": (openamo * 1e4) if openamo is not None else None,
        "ever_zt_count": _f(m.get("EverZTCount")),
        "fhsl": _f(m.get("fHSL")), "flb": _f(m.get("fLianB")), "wtb": _f(m.get("Wtb")),
        "zjl": _f(m.get("Zjl")), "zjl_hb": _f(m.get("Zjl_HB")),
        "l2_tic": _f(m.get("L2TicNum")), "l2_order": _f(m.get("L2OrderNum")),
        "total_bvol": _f(m.get("TotalBVol")), "total_svol": _f(m.get("TotalSVol")),
        "bcancel": _f(m.get("BCancel")), "scancel": _f(m.get("SCancel")),
        "pe_dynamic": _f(m.get("DynaPE")), "pe_ttm": _f(m.get("StaticPE_TTM")),
        "pb": _f(m.get("PB_MRQ")), "dividend_yield": _f(m.get("DYRatio")),
    }


def fetch_stock_l2(symbol: str) -> dict:
    """单股 L2 汇总(snapshot+more_info); 两源都空 → {}。"""
    snap = fetch_snapshot(symbol)
    more = fetch_more(symbol)
    if not snap and not more:
        return {}
    return {"symbol": symbol, "as_of": datetime.now(_CST).isoformat(timespec="seconds"),
            "snapshot": snap, "more": more}


def fetch_stock_l2_batch(symbols: list[str], concurrency: int = 8) -> dict[str, dict]:
    """候选池批量(线程池并发单股调用); 失败符号省略(不编)。"""
    out: dict[str, dict] = {}
    if not symbols:
        return out
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as ex:
        for sym, res in zip(symbols, ex.map(fetch_stock_l2, symbols)):
            if res:
                out[sym] = res
    return out


def seal_quality_tag(buyp: list[float], buyv: list[float], sellp: list[float],
                     sellv: list[float], zt_price: float | None, now: float | None,
                     ever_sealed: bool = False) -> str | None:
    """封板质量细分(五档盘口): 封死/排队/开板/未封; 五档或涨停价缺失 → None(不猜)。

    开板 vs 未封 需 ever_sealed(今日曾封)上下文: 跌破涨停价且曾封=开板, 从未封=未封。
    """
    if zt_price is None or now is None or not buyp or not buyv:
        return None
    bid1, bid1v = buyp[0], buyv[0] if buyv else 0.0
    ask1 = sellp[0] if sellp else None
    ask1v = sellv[0] if sellv else 0.0
    at_limit = abs(now - zt_price) <= _TICK
    bid_at_limit = abs(bid1 - zt_price) <= _TICK
    if not at_limit and not bid_at_limit:
        return "开板" if ever_sealed else "未封"
    if bid_at_limit and bid1v > 0:
        if ask1 is not None and ask1 <= zt_price + _TICK and ask1v > 0:
            return "排队"  # 涨停价上有卖压
        return "封死"
    return "开板" if ever_sealed else "未封"


def is_dive(now: float | None, before5min: float | None) -> bool | None:
    """跳水: 现价 <= 5分钟前价*0.98(5分钟跌≥2%); 缺任一 → None(不猜)。"""
    if now is None or before5min is None or before5min <= 0:
        return None
    return now <= before5min * _DIVE_RATIO
