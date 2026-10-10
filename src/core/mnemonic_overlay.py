# -*- coding: utf-8 -*-
"""内外盘七口诀叠加确认(2026-10-10 决策提胜率 B)。

## 为什么
内外盘七口诀(规则预判, 见 `src.core.dark_flow._judge_mnemonic`)此前只活在
L2 主力意图卡的**展示层**。但它与三指标共振是**同源不同维**的确认: 『外盘大+涨+放量
= 真金进攻』若与共振同向, 是真金进攻的叠加确认;『高位诱多出货』则是直接的压制信号。
本模块把口诀结论**信号化**为决策合成的叠加维度, 并入账 `decision_log`
(signal_kind = `bdqk_confirm` / `bdqk_suppress`), 供后续按 regime 分桶统计。

## 口径(纯函数 + 单点 IO)
- `eval_bdqk_effect(mnemonic)` —— 纯函数: 口诀判定结果 → 叠加效应, 可单测;
- `resolve_mnemonic(symbol)` —— 唯一 IO 口(腾讯逐笔 + Quote + tck 主动率), 失败
  一律返回 None(→ 难归集), 绝不编造。

## 诚实纪律(缺数据显式)
- 逐笔数据不足(data_status != ok) → effect="unavailable", 标『难归集』, 不硬凑、不叠加;
- 口诀未命中任何规则(mnemonic=None) → effect="none"(已判定: 无口诀), 不叠加;
- 口诀与主力意图方向**背离**(dark_flow 已标 divergence) → confirm 撤销为 neutral
  (以主力意图为准, 与 `dark_flow` 同立场), 并写 note 说明。
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: 口诀名 → 叠加效应(方向取自 _judge_mnemonic 的 direction)
# 看涨(加仓方向) → 同向确认, 给『动手』加成
BDQK_CONFIRM = {"真金进攻", "压盘吸筹"}
# 看跌/警惕(出货方向) → 压制『动手』
BDQK_SUPPRESS = {"主力撤退", "诱多出货", "对倒造假", "双大单对倒"}
# 观望/关注(方向不明) → 中性, 不叠加
BDQK_NEUTRAL = {"多空平衡", "控盘洗盘", "双小单"}
# 非口径命中(数据不足/异常) → 难归集
BDQK_UNRESOLVED = {"数据不足", "数据异常"}

#: 效应 → 入账 signal_kind(只有可行动的两类才入账)
EFFECT_TO_SIGNAL_KIND = {
    "confirm": "bdqk_confirm",
    "suppress": "bdqk_suppress",
}


def eval_bdqk_effect(mnemonic: dict | None) -> dict[str, Any]:
    """口诀判定结果 → 叠加效应(纯函数)。

    Args:
        mnemonic: `_judge_mnemonic` 的返回 {mnemonic, direction, divergence, detail},
                  或 data_status 不足时的占位 {mnemonic: '数据不足'/'数据异常'}, 或 None。

    Returns:
        {available, name, direction, divergence, effect, signal_kind, note}
        - effect ∈ {confirm, suppress, neutral, none, unavailable};
        - signal_kind: 仅 confirm/suppress 非空(供入账)。
    """
    if not isinstance(mnemonic, dict) or not mnemonic.get("mnemonic"):
        return {
            "available": False,
            "name": None,
            "direction": None,
            "divergence": False,
            "effect": "none",
            "signal_kind": None,
            "note": "七口诀无命中(不叠加)",
        }

    name = str(mnemonic.get("mnemonic")).strip()
    direction = mnemonic.get("direction")
    divergence = bool(mnemonic.get("divergence"))

    if name in BDQK_UNRESOLVED:
        return {
            "available": False,
            "name": name,
            "direction": direction,
            "divergence": False,
            "effect": "unavailable",
            "signal_kind": None,
            "note": f"七口诀难归集({name}: 逐笔数据不足/异常), 不叠加",
        }

    if name in BDQK_CONFIRM:
        effect = "confirm"
    elif name in BDQK_SUPPRESS:
        effect = "suppress"
    elif name in BDQK_NEUTRAL:
        effect = "neutral"
    else:
        # 未列入叠加表的口诀名 → 难归集(不硬凑)
        return {
            "available": False,
            "name": name,
            "direction": direction,
            "divergence": divergence,
            "effect": "unavailable",
            "signal_kind": None,
            "note": f"七口诀『{name}』未列入叠加表(难归集), 不叠加",
        }

    # 方向背离: 口诀与主力资金意图相反 → 以主力意图为准, confirm 撤销为 neutral
    if divergence and effect == "confirm":
        return {
            "available": True,
            "name": name,
            "direction": direction,
            "divergence": True,
            "effect": "neutral",
            "signal_kind": None,
            "note": f"七口诀『{name}』与主力资金意图方向背离, 撤销加成(以主力意图为准)",
        }

    return {
        "available": True,
        "name": name,
        "direction": direction,
        "divergence": divergence,
        "effect": effect,
        "signal_kind": EFFECT_TO_SIGNAL_KIND.get(effect),
        "note": (
            f"七口诀『{name}』({direction})"
            + (" 与主力意图背离" if divergence else "")
        ),
    }


def resolve_mnemonic(symbol: str) -> dict | None:
    """拉当前标的的七口诀判定结果(唯一 IO 口)。失败/非 CN → None(难归集)。

    复用生产链路(与 `/api/dark-flow` 同源): compute_dark_flow → 腾讯 Quote →
    `_judge_mnemonic`; 逐笔不足(data_status != ok) 直接返回『数据不足』占位,
    由 eval_bdqk_effect 归为 unavailable。**绝不编造数字**。
    """
    try:
        from marketdata import Symbol as MDSymbol
        from marketdata.vendors.tencent import TencentQuoteVendor

        from src.core.dark_flow import (
            _judge_mnemonic,
            compute_dark_flow,
            compute_tck_active_ratio,
        )

        md_symbol = MDSymbol.parse(symbol, "CN")
        dark = compute_dark_flow(md_symbol)
        if not dark:
            return None
        if dark.get("data_status") != "ok":
            return {
                "mnemonic": "数据不足",
                "direction": "中性",
                "divergence": False,
                "detail": "逐笔数据不足/异常, 七口诀暂不判定",
            }
        q = TencentQuoteVendor().fetch([md_symbol], {})[0]
        quote = {
            "current_price": q.current_price,
            "high_price": q.high_price,
            "low_price": q.low_price,
            "prev_close": q.prev_close,
            "change_pct": q.change_pct,
            "volume_ratio": q.volume_ratio,
            "volume_outer": q.volume_outer,
            "volume_inner": q.volume_inner,
            "volume": q.volume,
        }
        tck_ratio = compute_tck_active_ratio(symbol)
        return _judge_mnemonic(dark, quote, tck_active_ratio=tck_ratio)
    except Exception as e:  # noqa: BLE001 —— 难归集, 不硬凑
        logger.debug("resolve_mnemonic %s 失败(难归集): %r", symbol, e)
        return None
