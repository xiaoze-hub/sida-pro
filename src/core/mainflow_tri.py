"""明盘主力净额三源交叉验证(P1, 2026-09-04)。

腾讯四档(元) + 同花顺DDE官方(万元) + TQ Zjl_HB(万元) → 统一万元。
n_ok>=2且同号 → agree; 符号不一或 spread>50% → 分歧标记(不断主链路)。
单源失败静默 None; 整体 8s 超时兜底。
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)

_TIMEOUT_S = 8.0
_SPREAD_PCT = 50.0


def _tencent_net(symbol6: str) -> dict | None:
    """腾讯四档主力净(万元) + 超大/大/中/小 + 5日。失败 None。"""
    try:
        from marketdata.symbol import Symbol
        from marketdata.vendors.tencent_fundflow import TencentFundflowVendor
        flows = TencentFundflowVendor().fetch([Symbol.parse(symbol6, "CN")], {})
        if not flows:
            return None
        c = flows[0]
        if c.main_net_inflow is None:
            return None
        return {
            "net_wan": round(c.main_net_inflow / 1e4, 1),
            "super_wan": round(c.super_net_inflow / 1e4, 1) if c.super_net_inflow is not None else None,
            "big_wan": round(c.big_net_inflow / 1e4, 1) if c.big_net_inflow is not None else None,
            "mid_wan": round(c.mid_net_inflow / 1e4, 1) if c.mid_net_inflow is not None else None,
            "small_wan": round(c.small_net_inflow / 1e4, 1) if c.small_net_inflow is not None else None,
            "net_5d_wan": round(c.main_net_5d / 1e4, 1) if c.main_net_5d is not None else None,
        }
    except Exception as e:  # noqa: BLE001
        logger.debug(f"mainflow_tri tencent失败 {symbol6}: {e}")
        return None


def _thsdk_net(symbol6: str) -> dict | None:
    """同花顺DDE官方主力净(万元)。失败 None。"""
    try:
        from data_source.thsdk_l2 import get_main_flow_official
        o = get_main_flow_official(symbol6)
        net = o.get("main_net_amount_wan")
        if net is None:
            return None
        return {"net_wan": round(float(net), 1)}
    except Exception as e:  # noqa: BLE001
        logger.debug(f"mainflow_tri thsdk失败 {symbol6}: {e}")
        return None


def _tq_net(symbol6: str) -> dict | None:
    """TQ Zjl_HB主力净(万元)。网关挂则 None。"""
    try:
        from src.core.decision_pioneer import fetch_tq_l2
        l2 = fetch_tq_l2(symbol6)
        if not l2:
            return None
        net = l2.get("zjl_hb")
        if not isinstance(net, (int, float)):
            return None
        return {"net_wan": round(float(net), 1)}
    except Exception as e:  # noqa: BLE001
        logger.debug(f"mainflow_tri tq失败 {symbol6}: {e}")
        return None


def judge_agree(nets: list[float | None]) -> dict:
    """纯函数: 多源净额一致性判定(可单测)。"""
    vals = [v for v in nets if v is not None]
    if len(vals) < 2:
        return {"agree": None, "consensus_wan": vals[0] if vals else None,
                "spread_pct": None, "n_ok": len(vals)}
    signs = {1 if v > 0 else (-1 if v < 0 else 0) for v in vals}
    signs.discard(0)
    same_sign = len(signs) == 1
    mx, mn = max(vals), min(vals)
    denom = max(abs(mx), abs(mn), 1e-9)
    spread = abs(mx - mn) / denom * 100
    agree = bool(same_sign and spread <= _SPREAD_PCT)
    sv = sorted(vals)
    consensus = sv[len(sv) // 2]
    return {"agree": agree, "consensus_wan": round(consensus, 1),
            "spread_pct": round(spread, 1), "n_ok": len(vals)}


def triangulate(symbol6: str) -> dict:
    """三源明盘交叉验证。永不抛异常(失败源记None)。"""
    with ThreadPoolExecutor(max_workers=3) as ex:
        fut = {
            "tencent": ex.submit(_tencent_net, symbol6),
            "thsdk": ex.submit(_thsdk_net, symbol6),
            "tq": ex.submit(_tq_net, symbol6),
        }
        sources = {}
        for k, f in fut.items():
            try:
                sources[k] = f.result(timeout=_TIMEOUT_S)
            except Exception:  # noqa: BLE001
                sources[k] = None
    nets = [ (sources[k] or {}).get("net_wan") for k in ("tencent", "thsdk", "tq")]
    out = judge_agree(nets)
    out["sources"] = sources
    return out
