# -*- coding: utf-8 -*-
"""暗盘资金(L2 逐笔版)—— 决策先锋"主力暗盘资金"的主线实现。

## 为什么要有这个模块

官方口径(决策先锋8问8答 问题三):
    主力明盘资金 = 单笔 > 30 万的大单资金(市场可见, **可被拆单伪装**, 不能代表真主力)
    主力暗盘资金 = AI 模拟识别的 **私募量化单 / 机构游资对倒拆单 / 大单拆小单**
                   → 代表机构、游资的**真正意图**
    判定: 明盘 + 暗盘 > 0 = 流入; 暗盘进出在一定程度上代表主力"真正"想法

即: 暗盘才是决策先锋的核心, 明盘只是参考。而暗盘的"暗"恰恰在于**它不在大单里**
—— 大单拆成小单后才看不见, 所以只能从**逐笔**里认, 不能从大单流里算。

## 与 `dark_flow.py`(腾讯逐笔)的关系

两者共用**同一套拆单识别内核** `dark_flow._detect_split_orders`, 只换数据源:

| 模块 | 数据源 | 方向判定 | 置信度 |
|------|--------|----------|--------|
| `dark_flow`(旧) | 腾讯免费逐笔 | 价格自解析 | `L1_approx` |
| **本模块(新)** | thsdk L2 逐笔 `tick_super_level1` | **委托买入价/卖出价** | `l2_thsdk` |

切换的依据(2026-09-02 盘中实测, 真凭据):
    tick_super_level1 → 1683 条逐笔(3 秒条差分还原), 0.8s 返回,
    委托买入价/卖出价字段完整 → 满足 `dark_flow.py` 注释里写的切换前提
    ("逐笔含被动侧委托号 + 委托买卖价字段完整"后暗盘主线切换为自判断方向)。

## 硬规则(与全仓库一致)
- 金额 = 元, 成交量 = 股。
- 数据源不可达 → 返回 None, **由调用方回退腾讯逐笔**, 不在这里编造 0。
- 集合竞价(9:15-9:30)方向一律标中性 M(`dark_l2._fetch_thsdk` 已处理), 不污染多空。
"""

from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

# 暗盘主线开关(运维可关: 置 0 则整体回退腾讯逐笔)
DARK_L2_ENABLED = os.environ.get("PANWATCH_DARK_L2", "1").strip() == "1"

# 置信度标识: 与 dark_pool_flow.DARK_CONFIDENCE("L1_approx") 区分
DARK_CONFIDENCE_L2 = "l2_thsdk"
DARK_SOURCE_L2 = "thsdk_tick_super_level1"


def _tencent_code(symbol: str) -> Optional[str]:
    """6 位 A 股代码 → 腾讯风格(sz002361 / sh600519); 无法识别返回 None。

    与 `dark_pool_flow._tencent_code` 同口径, 本地实现避免跨模块引用私有函数。
    """
    s = (symbol or "").strip().lower()
    if s.startswith(("sz", "sh", "bj")):
        return s
    if s.isdigit() and len(s) == 6:
        if s[0] in ("6", "9") or s.startswith("688"):
            return "sh" + s
        if s[0] in ("0", "2", "3"):
            return "sz" + s
    return None


def _prev_close(symbol: str) -> Optional[float]:
    """取昨收(供拆单簇的逆势/顺势展示标记用; 取不到返回 None, 不影响暗盘总额)。"""
    try:
        from src.core.decision_pioneer import fetch_bars

        bars = fetch_bars(symbol, "CN", days=10)
        if len(bars) >= 2:
            return float(bars[-2].get("close"))
    except Exception as e:  # noqa: BLE001
        logger.debug("取昨收失败 %s: %s", symbol, e)
    return None


def compute_dark_flow_l2(symbol: str, source: str = "thsdk") -> Optional[dict]:
    """基于 thsdk L2 逐笔计算暗盘资金。

    Args:
        symbol: A 股代码("002361" / "sz002361")
        source: `dark_l2.fetch_l2_ticks` 的数据源标识, 默认 "thsdk"(tick_super_level1)

    Returns:
        与 `dark_flow.compute_dark_flow()["split_order"]` **同构** 的 dict:
        {
          "net": 暗盘净额(元, 正=流入),
          "buy_amt": 暗盘流入(元),
          "sell_amt": 暗盘流出(元),
          "groups": 拆单簇数量,
          "source": DARK_SOURCE_L2,
          "confidence": DARK_CONFIDENCE_L2,
          "tick_count": 逐笔条数,
          "contractarian": ...(透传, 仅展示)
        }
        数据源不可达 / 逐笔为空 → 返回 None(调用方回退, 不编造)。
    """
    if not DARK_L2_ENABLED:
        return None

    code = _tencent_code(symbol)
    if not code:
        logger.warning("dark_flow_l2: 无法识别代码 %r", symbol)
        return None

    try:
        from src.core import dark_l2
        from src.core.dark_flow import _detect_split_orders

        ticks = dark_l2.fetch_l2_ticks(code, source)
    except Exception as e:  # noqa: BLE001
        # 逐笔拉不到(非交易时段 / thsdk 未登录 / 超时) → 显式 None, 交给上层回退
        logger.warning("L2 逐笔获取失败(%s, source=%s): %s", code, source, str(e)[:120])
        return None

    if not ticks:
        logger.warning("L2 逐笔为空(%s)", code)
        return None

    try:
        split = _detect_split_orders(ticks, prev_close=_prev_close(symbol))
    except Exception as e:  # noqa: BLE001
        logger.warning("拆单识别失败(%s): %s", code, str(e)[:120])
        return None

    if not split:
        return None

    net = split.get("net")
    if net is None:
        return None

    return {
        "net": round(float(net), 2),
        "inflow": round(float(split.get("buy_amt") or 0.0), 2),
        "outflow": round(float(split.get("sell_amt") or 0.0), 2),
        "groups": len(split.get("groups") or []),
        "source": DARK_SOURCE_L2,
        "confidence": DARK_CONFIDENCE_L2,
        "tick_count": len(ticks),
    }
