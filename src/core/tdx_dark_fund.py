# -*- coding: utf-8 -*-
"""通达信 TQ 暗盘资金接口(2026-09-15)。

## 数据源分工

| 维度 | 来源 | 说明 |
|------|------|------|
| L2 汇总 | TQ `get_more_info` | Zjl_HB(主力净额) / L2TicNum / L2OrderNum / BCancel / SCancel |
| 四档分档 | TQ `L2_AMO` 公式 | 超大/大/中/小 单买入卖出额(万元) |
| 暗盘拆单 | `dark_flow._detect_split_orders` | 腾讯逐笔时间间隔聚类(现有) |
| 明盘净额 | `dark_split.ming_net_from_big_orders` | thsdk big_order_flow ≥30万(现有) |

## 官方口径(决策先锋8问8答)

    主力资金净流入 = 明盘净额 + 暗盘净额
    明盘 = 单笔 > 30 万的大单资金(市场可见)
    暗盘 = AI 识别的私募量化单 / 机构游资对倒拆单 / 大单拆小单
    散户净额 = -主力净额

## L2_AMO 公式(需 TQ 客户端已定义)

```
超B:=L2_AMO(0,0)/10000.0;
大B:=L2_AMO(1,0)/10000.0;
中B:=L2_AMO(2,0)/10000.0;
小B:=L2_AMO(3,0)/10000.0;
超S:=L2_AMO(0,1)/10000.0;
大S:=L2_AMO(1,1)/10000.0;
中S:=L2_AMO(2,1)/10000.0;
小S:=L2_AMO(3,1)/10000.0;
主力净额:(超B+大B)-(超S+大S),NODRAW;
```

## 单位
- 金额 = 元(除 L2_AMO 原始返回为万元, 本模块统一转元)
- 成交量 = 股
- 缺失一律 None, 不编造
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

# L2_AMO 四档阈值(万元, 官方口径)
TIER_LABELS = {
    0: "超大单",  # ≥100万
    1: "大单",    # 20-100万
    2: "中单",    # 5-20万
    3: "小单",    # <5万
}


def _to_tq_code(symbol: str) -> Optional[str]:
    """6位A股代码 → TQ格式(000001.SZ / 600519.SH)。"""
    s = (symbol or "").strip()
    if not s.isdigit() or len(s) != 6:
        return None
    if s[0] in ("6", "9") or s.startswith("688"):
        return f"{s}.SH"
    if s[0] in ("0", "2", "3"):
        return f"{s}.SZ"
    return None


def _rpc(method: str, params: dict, timeout: float = 4.0):
    """TQ JSON-RPC 调用(复用 tq.py 的网关发现)。"""
    from marketdata.vendors.tq import _rpc as tq_rpc

    return tq_rpc(method, params, timeout=timeout)


def fetch_tq_l2_summary(symbol: str) -> Optional[dict]:
    """TQ get_more_info → L2 汇总数据。

    Returns:
        {
            "zjl_hb": 主力净额(万元),          # Zjl_HB —— 单位万元(见 mainflow_tri 文件头)
            "l2_tick_num": L2逐笔成交数,
            "l2_order_num": L2逐笔委托数,
            "cancel_buy": 总撤买量(股),
            "cancel_sell": 总撤卖量(股),
            "total_buy_vol": 总买量(股),
            "total_sell_vol": 总卖量(股),
        }
        TQ 不可达 → None
    """
    tqc = _to_tq_code(symbol)
    if not tqc:
        return None
    try:
        raw = _rpc("get_more_info", {"stock_code": tqc})
        if not isinstance(raw, dict) or raw.get("ErrorId") not in ("0", None, ""):
            return None

        def _f(key):
            v = raw.get(key)
            try:
                return float(v) if v not in (None, "", "0.00") else None
            except (TypeError, ValueError):
                return None

        def _i(key):
            v = raw.get(key)
            try:
                return int(float(v)) if v not in (None, "") else None
            except (TypeError, ValueError):
                return None

        return {
            "zjl_hb": _f("Zjl_HB"),
            "l2_tick_num": _i("L2TicNum"),
            "l2_order_num": _i("L2OrderNum"),
            "cancel_buy": _f("BCancel"),
            "cancel_sell": _f("SCancel"),
            "total_buy_vol": _f("TotalBVol"),
            "total_sell_vol": _f("TotalSVol"),
        }
    except Exception as e:  # noqa: BLE001
        logger.debug("TQ get_more_info 失败 %s: %s", symbol, e)
        return None


def fetch_tq_l2_amo(symbol: str) -> Optional[dict]:
    """TQ L2_AMO 公式 → 四档资金分档(万元→元)。

    Returns:
        {
            "xl_buy": 超大单买入(元), "xl_sell": 超大单卖出(元), "xl_net": 超大单净额(元),
            "large_buy": ..., "large_sell": ..., "large_net": ...,
            "mid_buy": ..., "mid_sell": ..., "mid_net": ...,
            "small_buy": ..., "small_sell": ..., "small_net": ...,
            "main_net": 主力净额(元) = (超大+大)净额,
        }
        公式未定义/TQ 不可达 → None
    """
    tqc = _to_tq_code(symbol)
    if not tqc:
        return None
    try:
        # L2_AMO 是公式函数, 需客户端已定义同名指标公式
        # 公式内容: 超B/大B/中B/小B/超S/大S/中S/小S + 主力净额
        raw = _rpc(
            "formula_process_mul_zb",
            {
                "formula_name": "L2_AMO",
                "stock_list": [tqc],
                "stock_period": "1d",
                "periodstr": "1d",
                "count": 1,
                "return_count": 1,
            },
            timeout=6.0,
        )
        if not isinstance(raw, dict) or not raw:
            return None
        metrics = raw.get(tqc)
        if not isinstance(metrics, dict):
            return None

        def _get(name: str) -> Optional[float]:
            vals = metrics.get(name)
            if isinstance(vals, list) and vals:
                try:
                    # L2_AMO 原始返回万元, 转元
                    return float(vals[-1]) * 1e4
                except (TypeError, ValueError):
                    return None
            return None

        xl_buy = _get("超B")
        xl_sell = _get("超S")
        large_buy = _get("大B")
        large_sell = _get("大S")
        mid_buy = _get("中B")
        mid_sell = _get("中S")
        small_buy = _get("小B")
        small_sell = _get("小S")

        # 主力净额 = (超大+大)净额(官方口径)
        main_net = None
        if all(v is not None for v in (xl_buy, xl_sell, large_buy, large_sell)):
            main_net = (xl_buy + large_buy) - (xl_sell + large_sell)

        def _net(b, s):
            if b is not None and s is not None:
                return b - s
            return None

        return {
            "xl_buy": xl_buy, "xl_sell": xl_sell, "xl_net": _net(xl_buy, xl_sell),
            "large_buy": large_buy, "large_sell": large_sell, "large_net": _net(large_buy, large_sell),
            "mid_buy": mid_buy, "mid_sell": mid_sell, "mid_net": _net(mid_buy, mid_sell),
            "small_buy": small_buy, "small_sell": small_sell, "small_net": _net(small_buy, small_sell),
            "main_net": main_net,
        }
    except Exception as e:  # noqa: BLE001
        logger.debug("TQ L2_AMO 失败 %s: %s", symbol, e)
        return None


def compute_tdx_dark_fund(symbol: str) -> Optional[dict]:
    """通达信暗盘资金完整接口: TQ L2 汇总 + 四档分档 + 暗盘拆单融合。

    Returns:
        {
            "symbol": "000001",
            "available": True,
            "source": "tdx_tq",
            "date": "2026-09-15",
            # L2 汇总
            "l2_summary": {zjl_hb, l2_tick_num, ...},
            # 四档分档(元)
            "tiers": {
                "xl": {buy, sell, net},
                "large": {buy, sell, net},
                "mid": {buy, sell, net},
                "small": {buy, sell, net},
            },
            "main_net": 主力净额(元),
            # 暗盘拆单(腾讯逐笔)
            "dark_flow": {buy_amt, sell_amt, net, groups},
            # 明盘(thsdk big_order_flow)
            "ming_net": 明盘净额(元),
            # 融合
            "main_net_fused": 明盘+暗盘(元),
            "confidence": "tdx_l2+split_v4",
        }
        TQ 不可达 → None(调用方回退)
    """
    # 1. TQ L2 汇总
    l2_summary = fetch_tq_l2_summary(symbol)
    if not l2_summary:
        logger.info("TQ L2 汇总不可用 %s, 回退", symbol)
        return None

    # 2. TQ 四档分档(L2_AMO 公式, 可能未定义)
    tiers_raw = fetch_tq_l2_amo(symbol)
    tiers = None
    main_net = None
    if tiers_raw:
        tiers = {
            "xl": {"buy": tiers_raw["xl_buy"], "sell": tiers_raw["xl_sell"], "net": tiers_raw["xl_net"]},
            "large": {"buy": tiers_raw["large_buy"], "sell": tiers_raw["large_sell"], "net": tiers_raw["large_net"]},
            "mid": {"buy": tiers_raw["mid_buy"], "sell": tiers_raw["mid_sell"], "net": tiers_raw["mid_net"]},
            "small": {"buy": tiers_raw["small_buy"], "sell": tiers_raw["small_sell"], "net": tiers_raw["small_net"]},
        }
        main_net = tiers_raw["main_net"]

    # 3. 暗盘拆单(腾讯逐笔, 复用现有)
    dark_flow = None
    try:
        from src.core.dark_flow import compute_dark_flow

        df = compute_dark_flow(symbol)
        if df and isinstance(df, dict):
            dark_flow = {
                "buy_amt": df.get("buy_amt"),
                "sell_amt": df.get("sell_amt"),
                "net": df.get("net"),
                "groups": len(df.get("groups") or []),
            }
    except Exception as e:  # noqa: BLE001
        logger.debug("暗盘拆单失败 %s: %s", symbol, e)

    # 4. 明盘(thsdk big_order_flow, 可选)
    ming_net = None
    try:
        from src.core.dark_split import ming_net_from_big_orders

        ming_net = ming_net_from_big_orders(symbol)
    except Exception as e:  # noqa: BLE001
        logger.debug("明盘计算失败 %s: %s", symbol, e)

    # 5. 融合主力净额 = 明盘 + 暗盘
    main_net_fused = None
    if ming_net is not None and dark_flow and dark_flow.get("net") is not None:
        main_net_fused = ming_net + dark_flow["net"]
    elif main_net is not None:
        main_net_fused = main_net
    elif l2_summary.get("zjl_hb") is not None:
        main_net_fused = l2_summary["zjl_hb"]

    return {
        "symbol": symbol,
        "available": True,
        "source": "tdx_tq",
        "l2_summary": l2_summary,
        "tiers": tiers,
        "main_net": main_net,
        "dark_flow": dark_flow,
        "ming_net": ming_net,
        "main_net_fused": main_net_fused,
        "confidence": "tdx_l2+split_v4",
    }
