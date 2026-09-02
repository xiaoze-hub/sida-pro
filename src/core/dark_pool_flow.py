# -*- coding: utf-8 -*-
"""主力资金聚合: 明盘(权威) + 暗盘(L1 近似) = 主力净额。

对齐《决策先锋8问8答》官方口径:
    主力资金净流入 = 明盘净额 + 暗盘净额
    明盘 = 单笔 > 30 万的大单资金(市场可见)
    暗盘 = AI 识别的私募量化单 / 机构游资对倒拆单 / 大单拆小单
    散户净额 = -主力净额(全市场净流入恒为 0)

## 两个组成部分的来源与置信度(严格区分, 禁止混用)

| 组成 | 数据源 | 置信度 | 说明 |
|------|--------|--------|------|
| 明盘 | `thsdk big_order_flow` 逐笔大单直接汇总 | **官方对齐** | 2026-08-31 交叉验证: 与官方 `market_data_cn 扩展1 主力净流入` 精确到元一致(神剑股份 -3,407,363 元) |
| 暗盘 | `dark_flow._detect_split_orders`(时间间隔聚类拆单识别) | **L1 近似** | v0.4.30 已对齐同花顺暗盘口径(金健米业案例), 但仍是启发式聚类, 非 AI 模型 |

## 实测依据(2026-08-31, 神剑股份 USZA002361)
- big_order_flow 253 行, 单笔金额 min=300,105 元, 100% ≥ 30 万 → 接口本身就是明盘过滤器
- 官方扩展1「主力净流入」 -3,407,363 元 == 自算明盘净额 -3,407,363 元(精确到元)

## 数据源分工定案(Hermes 2026-08-31 13:49 邮件, 硬边界)
| 口径 | 数据源 | 用途 |
|------|--------|------|
| 精确主线 | `tick_super_level1` 自判断方向(价格匹配法) | **暗盘净额**, 真正下单依据 |
| 委托号级增强 | .tck 主动侧拆单(后续叠加) | 拆单识别, 腾讯做不到 |
| 对照项 | OHLC 分摊 | 仅标「对齐同花顺口径」, **实测 23 倍误差**(002361 2026-08-27 算 +2.17亿 vs 真实 +939万) |
| 辅助指标 | big_order_flow | 大单活跃度/参与度, **不用于暗盘金额** |

⚠️ 两条边界, 不得混淆:
1. `big_order_flow` 在本模块只承担**明盘**(= 官方扩展1"主力净流入"口径), 与"暗盘"无关。
   它方向解码对明盘成立(已交叉验证), 但**对暗盘会反转**(多氟多: 公式 -23219 万 vs
   同花顺暗盘 +28124 万), 因为它只有 ≥30万 大单, 根本不含拆散的小单。
2. 本模块暗盘当前用 `dark_flow` 拆单识别(腾讯逐笔)作**过渡实现**;
   `tick_super_level1` 盘中验证通过(逐笔含被动侧委托号 + 委托买卖价字段完整)后,
   暗盘主线切换为"自判断方向", 届时 confidence 从 L1_approx 升级。

## 单位
金额 = 元, 成交量 = 股。缺失一律 None, 由上层显式标注「无数据」。
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

MING_SOURCE = "thsdk_big_order"      # 明盘数据源标识(dark_l2 数据源名)
DARK_SOURCE = "dark_flow_split_v4"   # 暗盘算法标识
MING_CONFIDENCE = "official"         # 已与官方指标交叉验证
DARK_CONFIDENCE = "L1_approx"        # 启发式聚类近似, 非 AI 模型


def _tencent_code(code: str) -> Optional[str]:
    """6 位 A 股代码 → 腾讯风格代码(sz/sh 前缀), 无法识别返回 None。

    与 dark_flow._tencent_code 同口径: 6/9 开头(含 688) → 沪, 0/2/3 开头 → 深。
    本地实现而非跨模块引用私有函数, 避免 dark_flow 内部重构时被连带破坏。
    """
    code = (code or "").strip()
    if code[:2].lower() in ("sz", "sh", "bj"):
        return code.lower()
    if code.isdigit() and len(code) == 6:
        if code[0] in ("6", "9") or code.startswith("688"):
            return f"sh{code}"
        if code[0] in ("0", "2", "3"):
            return f"sz{code}"
    return None


def _ming_flow(tencent_code: str) -> Optional[dict]:
    """明盘净额(权威): thsdk big_order_flow 逐笔大单直接汇总。失败返回 None。"""
    try:
        from src.core import dark_l2, dark_split

        ticks = dark_l2.fetch_l2_ticks(tencent_code, MING_SOURCE)
        r = dark_split.ming_net_from_big_orders(ticks)
        if r["count"] <= 0:
            logger.warning("明盘汇总 0 笔(%s), 返回 None", tencent_code)
            return None
        return {
            "net": r["ming_net"],
            "buy": r["ming_buy"],
            "sell": r["ming_sell"],
            "count": r["count"],
            "source": MING_SOURCE,
            "confidence": MING_CONFIDENCE,
            "skipped": r["skipped"],
        }
    except Exception as e:  # noqa: BLE001
        # 明盘失败不拖垮整体: 暗盘/主力仍可按可得部分返回
        logger.warning("明盘(big_order_flow)获取失败(%s): %s", tencent_code, str(e)[:120])
        return None


def _dark_flow(symbol_str: str) -> Optional[dict]:
    """暗盘净额: **主线 = thsdk L2 逐笔**, 失败回退腾讯逐笔(L1 近似)。

    2026-09-02 切换依据:
      官方暗盘 = 拆单 / 对倒 / 大单拆小单的识别结果。这类"暗"资金**不在大单流里**
      (big_order_flow 只有 ≥30 万的单, 恰恰是拆单要躲开的口径), 只能从**逐笔**认。
      thsdk tick_super_level1 盘中实测 1683 条、委托买入价/卖出价字段完整,
      满足 `dark_flow.py` 注释写明的切换前提, 故由对照项升为**主线**。
      L2 不可达(非交易时段 / 未登录 / 超时)时自动回退腾讯逐笔, confidence 随之降级。
    """
    # ① 主线: **通达信 .tck + 同花顺 thsdk 融合**(互补闭环, 唯一能覆盖被动侧的方案)
    #    .tck 给官方方向(2B/2S 精确)但缺被动 maker; thsdk 逐笔全量含被动但方向是推断。
    #    两者融合 = 主动精确 + 被动覆盖, 单链路在原理上都无法闭环(详见 dark_flow_fusion)。
    try:
        from src.core.dark_flow_fusion import compute_dark_fusion

        f = compute_dark_fusion(symbol_str)
        if f is not None:
            total = f.get("total")
            active = f.get("active")
            split = f.get("split") or {}
            if total:
                # 有全量逐笔 → 用它作净额(含被动侧, 最完整), 精确分量附在明细里
                return {
                    "net": total["net"],
                    "inflow": total["buy"],
                    "outflow": total["sell"],
                    "groups": split.get("count", 0),
                    "source": "fusion_tdx_tck+thsdk",
                    "confidence": "fusion",
                    "tick_count": total.get("count"),
                    # --- 融合明细(新增字段, 供前端分口径展示) ---
                    "active_net": (active or {}).get("net"),
                    "active_confidence": (active or {}).get("confidence"),
                    "split_net": split.get("net"),
                    "passive_est": f.get("passive_est"),
                    "passive_flag": f.get("passive_flag"),
                    "coverage": f.get("coverage"),
                    "fusion_note": f.get("note"),
                }
            if active:
                # 只有 .tck(thsdk 不可达): 主动侧精确但被动缺失 → 显式标注不完整
                return {
                    "net": active["net"],
                    "inflow": active["buy"],
                    "outflow": active["sell"],
                    "groups": split.get("count", 0),
                    "source": "tdx_tck_active_only",
                    "confidence": "official_exact_partial",
                    "tick_count": active.get("count"),
                    "active_net": active["net"],
                    "split_net": split.get("net"),
                    "passive_est": None,
                    "passive_flag": None,
                    "coverage": "tck_only",
                    "fusion_note": f.get("note"),
                }
    except Exception as e:  # noqa: BLE001
        logger.warning("暗盘融合失败(%s): %s, 降级 L2 逐笔", symbol_str, str(e)[:120])

    # ② 次选: 纯 thsdk L2 逐笔(委托买卖价自判断方向)
    try:
        from src.core.dark_flow_l2 import compute_dark_flow_l2

        r = compute_dark_flow_l2(symbol_str)
        if r is not None:
            return r
    except Exception as e:  # noqa: BLE001
        logger.warning("暗盘 L2 主线失败(%s): %s, 回退腾讯逐笔", symbol_str, str(e)[:120])

    # ② 回退: 腾讯逐笔(L1 近似)
    try:
        from marketdata.symbol import Symbol as MDSymbol
        from src.core.dark_flow import compute_dark_flow

        mdsym = MDSymbol.parse(symbol_str, "CN")
        dark = compute_dark_flow(mdsym)
        if not dark:
            return None
        split = dark.get("split_order")
        if not split:
            logger.warning("拆单识别无结果(%s), 暗盘显式标无数据", symbol_str)
            return None
        return {
            "net": round(split.get("net") or 0.0, 2),
            "inflow": round(split.get("buy_amt") or 0.0, 2),    # 暗盘流入(买入簇)
            "outflow": round(split.get("sell_amt") or 0.0, 2),  # 暗盘流出(卖出簇)
            "groups": len(split.get("groups") or []),
            "source": DARK_SOURCE,
            "confidence": DARK_CONFIDENCE,
            "dark_flow_status": dark.get("data_status"),   # insufficient/suspect/ok
            "tick_count": dark.get("tick_count"),
        }
    except Exception as e:  # noqa: BLE001
        logger.warning("暗盘(拆单识别)计算失败(%s): %s", symbol_str, str(e)[:120])
        return None


def compute_pool_flow(symbol: str) -> Optional[dict]:
    """聚合主力资金: 明盘(权威) + 暗盘(L1 近似) = 主力净额。

    Args:
        symbol: A 股代码, 支持 "002361" / "sz002361" / thsdk 风格由下游转换

    Returns:
        {
          "symbol": str,
          "ming":  {...} | None,     # 明盘, confidence="official"
          "dark":  {...} | None,     # 暗盘, confidence="L1_approx"
          "main_net": float | None,  # 主力净额 = 明盘 + 暗盘; 任一缺失则为 None
          "retail_net": float | None,# 散户净额 = -主力净额(官方口径: 全市场净流入=0)
          "coverage": str,           # "full"(两者都有) / "ming_only" / "dark_only" / "none"
        }

    ⚠️ main_net 只在明盘与暗盘**同时可得**时才输出。
       只拿得到一边时硬算主力净额 = 把"L1 近似"冒充成完整口径, 违反"不编造"红线。

    明盘与暗盘各自失败互不拖垮: 一个挂了另一个照常返回, coverage 标明覆盖情况。
    """
    code = (symbol or "").strip()
    tencent_code = _tencent_code(code)
    if not tencent_code:
        logger.warning("无法识别 A 股代码: %r", symbol)
        return None

    ming = _ming_flow(tencent_code)
    dark = _dark_flow(code)

    main_net: Optional[float] = None
    retail_net: Optional[float] = None
    if ming is not None and dark is not None:
        main_net = round(ming["net"] + dark["net"], 2)
        retail_net = round(-main_net, 2)

    if ming is None and dark is None:
        coverage = "none"
    elif dark is None:
        coverage = "ming_only"
    elif ming is None:
        coverage = "dark_only"
    else:
        coverage = "full"

    return {
        "symbol": code,
        "tencent_code": tencent_code,
        "ming": ming,
        "dark": dark,
        "main_net": main_net,
        "retail_net": retail_net,
        "coverage": coverage,
    }
