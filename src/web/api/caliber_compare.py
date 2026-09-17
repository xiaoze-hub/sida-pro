"""口径对照(2026-09-18, 老板需求 A2 第一步) —— 三种"主力资金"口径并排看, 消歧不合并。

**为什么要有这一页**: 同一只票、同一时刻, 三个源的"主力净流入"数字**本来就不同** ——
因为它们对"主力"的定义、覆盖范围、时间窗与单位都不同。混用会得出相反结论(AGENTS.md 口径红线)。
这个端点的职责是**把差异显式摆出来**, 而不是合成一个"权威数字"。

三个源(全部只读, 不写库):

| key | 源 | 口径要点 |
|---|---|---|
| `thsdk_l2` | 明盘 L2(TQ/同花顺 `get_more_info` 的 `zjl_hb`) | 按**单笔成交金额分档**汇总的特大/大单净额, 同花顺官方"主力净额"明盘口径 |
| `tencent_dark` | 暗盘(腾讯逐笔 v6, `compute_dark_flow`) | 逐笔主动成交 + **拆单识别**; 字段区分 全量主动净额 / 主力(≥20万) / 超大单(≥100万) |
| `eastmoney_flow` | 东财四档资金流(`capital_flow_collector`) | 公开 Level-1 衍生, **按单金额四档归类**(超大/大/中/小), 盘中可能为 T-1 基准日 |

**诚实口径(硬规则)**: 任一源失败/无数据 → `available=false` + `note` 说明原因, **绝不补 0**;
每个源都带 `unit` 与 `caliber` 说明, 前端照实渲染。
"""
from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query

from src.web.api.auth import get_current_user
from src.web.models import User

logger = logging.getLogger(__name__)

router = APIRouter(tags=["caliber-compare"])

_CST = ZoneInfo("Asia/Shanghai")

#: 三个源的中文名与口径说明(静态文案, 减少前端硬编码; 改口径只改这里)
SOURCE_META: dict[str, dict[str, str]] = {
    "thsdk_l2": {
        "name": "明盘 L2 主力净流入（TQ / 同花顺口径）",
        "caliber": "按单笔成交金额分档汇总的特大单+大单净额；同花顺官方「主力净额」的明盘口径。",
        "unit": "元",
    },
    "tencent_dark": {
        "name": "暗盘资金（腾讯逐笔 v6）",
        "caliber": "腾讯逐笔主动成交，含拆单识别；区分「全量主动净额 / 主力≥20万 / 超大单≥100万」。",
        "unit": "元",
    },
    "eastmoney_flow": {
        "name": "东财四档资金流",
        "caliber": "公开 Level-1 衍生，按单金额四档归类（超大/大/中/小）；盘中可能是 T-1 基准日，看 date 字段。",
        "unit": "元",
    },
}

#: 三条"为什么数字不一样"的静态解释(不编数字, 只讲口径)
DIFFERENCES: list[dict[str, str]] = [
    {
        "topic": "「主力」定义不同",
        "detail": "TQ 按单笔金额分档（特大/大单）、暗盘按逐笔主动成交 + 拆单识别、东财按单金额四档归类 —— "
                  "同一笔大单在三家的归属档位可能不同，所以数字**必然**不等。",
    },
    {
        "topic": "覆盖范围不同",
        "detail": "明盘只看连续竞价成交；暗盘额外含拆单还原与竞价段（`auction_amt` 单列）；"
                  "东财是公开 Level-1 派生的四档，不含逐笔细节。",
    },
    {
        "topic": "时间窗/基准日不同",
        "detail": "TQ 与暗盘为当日累计（盘中实时）；东财盘中常见 T-1 基准日（`date` 字段会标出）—— "
                  "跨基准日对比等于拿两天比一天。",
    },
    {
        "topic": "正确用法",
        "detail": "**不要**取平均或互相校准后当单一结论。先看方向是否一致：方向一致 → 结论稳；"
                  "方向相反 → 停下看明细（谁在拆单、谁把什么算成主力），不要用其中一个去否定另一个。",
    },
]


def _wrap(key: str, *, fields: list[dict] | None, note: str = "", extra: dict | None = None) -> dict:
    """统一的源结果包装: 缺失一律 available=false + note, 不补 0。"""
    meta = SOURCE_META[key]
    has_value = bool(fields) and any(f.get("value") is not None for f in (fields or []))
    out = {
        "key": key,
        "name": meta["name"],
        "caliber": meta["caliber"],
        "unit": meta["unit"],
        "available": has_value,
        "fields": fields or [],
        "note": note if not has_value else note,
    }
    if extra:
        out.update(extra)
    return out


def _thsdk_l2(symbol: str) -> dict:
    from src.core.decision_pioneer import fetch_tq_l2

    try:
        l2 = fetch_tq_l2(symbol, "CN")
    except Exception as exc:  # noqa: BLE001
        logger.warning("caliber-compare thsdk_l2 %s 异常: %r", symbol, exc)
        return _wrap("thsdk_l2", fields=None, note=f"取数异常：{type(exc).__name__}")
    if not l2:
        return _wrap("thsdk_l2", fields=None, note="TQ 未返回数据（未配正式账户 / 非交易时段 / 该票无 L2 摘要）")
    fields = [
        {"label": "主力净流入", "value": l2.get("zjl_hb")},
        {"label": "主力净额（含主动买卖口径）", "value": l2.get("zjl")},
        {"label": "撤买额", "value": l2.get("cancel_buy")},
        {"label": "撤卖额", "value": l2.get("cancel_sell")},
        {"label": "L2 逐笔笔数", "value": l2.get("l2_tick_num"), "unit": "笔"},
        {"label": "L2 委托笔数", "value": l2.get("l2_order_num"), "unit": "笔"},
    ]
    return _wrap("thsdk_l2", fields=fields, note="")


def _tencent_dark(symbol: str) -> dict:
    from marketdata.symbol import Symbol as MDSymbol
    from src.core.dark_flow import compute_dark_flow

    try:
        dark = compute_dark_flow(MDSymbol.parse(symbol, "CN"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("caliber-compare tencent_dark %s 异常: %r", symbol, exc)
        return _wrap("tencent_dark", fields=None, note=f"取数异常：{type(exc).__name__}")
    if not dark:
        return _wrap("tencent_dark", fields=None, note="腾讯逐笔未取到数据（非交易时段 / 数据源不可用）")
    fields = [
        {"label": "全量主动净额", "value": dark.get("dark_net")},
        {"label": "主力净额（≥20万）", "value": dark.get("main_net")},
        {"label": "超大单净额（≥100万）", "value": dark.get("big_net")},
        {"label": "大单净额（20–100万）", "value": dark.get("mid_net")},
        {"label": "散户净额（<20万）", "value": dark.get("small_net")},
        {"label": "主动买额", "value": dark.get("buy_amt")},
        {"label": "主动卖额", "value": dark.get("sell_amt")},
        {"label": "竞价撮合额", "value": dark.get("auction_amt")},
        {"label": "主力参与度", "value": dark.get("main_intensity"), "unit": "%"},
    ]
    note = ""
    if dark.get("data_suspect"):
        # 源自身标了可疑(主力成交额超全日成交额阈值) —— 如实透传, 不吞掉
        note = "⚠️ 该源自标「数据可疑」（主力成交额超全日成交额阈值，历史上多为逐笔重复计数）"
    return _wrap("tencent_dark", fields=fields, note=note)


def _eastmoney_flow(symbol: str) -> dict:
    try:
        from src.collectors.capital_flow_collector import CapitalFlowCollector
        from src.models.market import MarketCode

        summary = CapitalFlowCollector(MarketCode.CN).get_capital_flow_summary(symbol)
    except Exception as exc:  # noqa: BLE001
        logger.warning("caliber-compare eastmoney %s 异常: %r", symbol, exc)
        return _wrap("eastmoney_flow", fields=None, note=f"取数异常：{type(exc).__name__}")
    if not summary or summary.get("error"):
        return _wrap("eastmoney_flow", fields=None, note=str((summary or {}).get("error") or "东财未返回资金流数据"))
    fields = [
        {"label": "主力净流入", "value": summary.get("main_net_inflow")},
        {"label": "主力净占比", "value": summary.get("main_net_inflow_pct"), "unit": "%"},
        {"label": "超大单净额", "value": summary.get("super_net_inflow")},
        {"label": "大单净额", "value": summary.get("big_net_inflow")},
        {"label": "中单净额", "value": summary.get("mid_net_inflow")},
        {"label": "小单净额", "value": summary.get("small_net_inflow")},
    ]
    note = f"基准日 {summary.get('date')}（盘中常见 T-1，跨基准日对比等于拿两天比一天）" if summary.get("date") else ""
    return _wrap("eastmoney_flow", fields=fields, note=note, extra={"date": summary.get("date")})


def build_caliber_compare(symbol: str) -> dict:
    """三源并排(供端点与测试直接调用)。"""
    code = (symbol or "").strip()
    if not code.isdigit() or len(code) != 6:
        raise HTTPException(400, f"非法股票代码: {symbol!r}(需要6位A股代码)")
    # 每个源再包一层: 源函数**自身**抛异常(依赖缺失/网络炸)也只是这一列降级,
    # 不能让整个对照页 500 —— 对照页的价值恰恰在于"哪个源现在不行"也能看见。
    sources: list[dict] = []
    for key, fn in (("thsdk_l2", _thsdk_l2), ("tencent_dark", _tencent_dark), ("eastmoney_flow", _eastmoney_flow)):
        try:
            sources.append(fn(code))
        except Exception as exc:  # noqa: BLE001
            logger.warning("caliber-compare 源 %s 异常: %r", key, exc)
            sources.append(_wrap(key, fields=None, note=f"取数异常：{type(exc).__name__}"))
    return {
        "symbol": code,
        "market": "CN",
        "as_of": datetime.now(_CST).isoformat(timespec="seconds"),
        "sources": sources,
        "available_count": sum(1 for s in sources if s["available"]),
        "differences": DIFFERENCES,
    }


@router.get("/{symbol}")
def caliber_compare(symbol: str, _: User = Depends(get_current_user)):
    """三口径对照: 明盘 L2 / 暗盘逐笔 / 东财四档（同一票同一时刻并排, 不合成为单一数字）。"""
    return build_caliber_compare(symbol)
