# -*- coding: utf-8 -*-
"""决策先锋「三指标共振」回测 —— 对照官方 2024.10-2025.10 基准。

## 官方基准(8问8答 问题七)

| 共振状态 | 上涨概率 | 盈亏比 |
|---|---|---|
| 三指标共振向好 | 75.42% | 3.45 |
| 三指标共振拐点 | 73.48% | 3.57 |
| 三指标共振分歧 | 70.55% | 3.37 |
| 三指标共振走坏 | 67.40% | 3.29 |

**成功口径**: 统计期内**最高涨幅 > 3%** 算成功(用户提供口径, 与官方一致)。
**盈亏比**: mean(盈利幅度) / mean(亏损幅度)。

## ⚠️ 数据可得性约束(2026-09-02 实测, 决定本模块能跑出什么)

| 维度 | 历史可得性 |
|------|-----------|
| 趋势(GS) | ✅ 纯 K 线计算, 任意历史日可算 |
| 活跃度   | ✅ 纯 K 线计算, 任意历史日可算 |
| **资金(明盘)** | ❌ **无源**: TQ get_more_info 未连接; ZLJC 历史返空; big_order_flow 仅当日 |
| 资金(暗盘) | 🟡 仅 OHLC 分摊**对照项**可逐日重算(已知大振幅日 23 倍误差) |

因此本回测提供两种口径, **结果一律带 `basis` 标记**:

- `fund_source=None`(默认, **诚实口径**): 只跑 **GS + 活跃度双指标**,
  不含资金维。能验证"趋势+活跃度"的有效性, 但**与官方四态不可直接比较**。
- `fund_source="ohlc"`: 资金维用 OHLC 分摊暗盘, 凑成三指标。
  ⚠️ 该口径下**禁止**与官方 75.42% 做优劣结论 —— 对照项误差可达 23 倍,
  拿它当真值比高低是自欺。它只能看**相对排序**(向好是否 > 走坏)。

明盘历史一旦有源, 把 `fund_source` 换成真实主力净额即可, 结构无需改动。

## 用法
    backtest_resonance(symbols=["002361", ...], start_date="2024-10-01",
                       end_date="2025-10-31", hold_days=5)
⚠️ 逐日滚动重算, 单股 O(n²); 建议股票池 ≤ 200 只、区间 ≤ 2 年, 大样本请后台跑。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional, Sequence

logger = logging.getLogger(__name__)

# 回测参数默认(对齐官方口径)
DEFAULT_HOLD_DAYS = 5
DEFAULT_SUCCESS_PCT = 3.0     # 最高涨幅 > 3% 算成功
MIN_BARS = 60                 # GS 需 ≥28(BB0 要 MA27), 留足余量


def _state_without_fund(trend: str, activity: Optional[float], line: float) -> Optional[str]:
    """缺资金维时的**双指标**降级判定。

    ⚠️ 与官方"三指标四态"**不是同一口径**, 结果必须带 basis 标记。
    """
    if not trend or trend == "无数据" or not isinstance(activity, (int, float)):
        return None
    if trend in ("G信号", "G区间"):
        return "向好" if activity >= line else "分歧"
    if trend == "S信号":
        return "走坏" if activity < line else "分歧"
    return "分歧"


def _outcome(bars: list[dict], i: int, hold_days: int) -> Optional[dict]:
    """第 i 根(信号日)之后 hold_days 内的收益表现。"""
    if i + hold_days >= len(bars):
        return None
    c0 = bars[i].get("close")
    if not isinstance(c0, (int, float)) or c0 <= 0:
        return None
    highs = [b.get("high") for b in bars[i + 1: i + 1 + hold_days]]
    lows = [b.get("low") for b in bars[i + 1: i + 1 + hold_days]]
    highs = [h for h in highs if isinstance(h, (int, float)) and h > 0]
    lows = [l for l in lows if isinstance(l, (int, float)) and l > 0]
    if not highs or not lows:
        return None
    max_gain = (max(highs) - c0) / c0 * 100.0
    max_loss = (min(lows) - c0) / c0 * 100.0
    return {"max_gain": round(max_gain, 4), "max_loss": round(max_loss, 4)}


def _agg(samples: list[dict], success_pct: float) -> dict:
    """一组样本 → 上涨概率 / 盈亏比。"""
    if not samples:
        return {"count": 0, "win_rate": None, "profit_ratio": None,
                "avg_gain": None, "avg_loss": None}
    wins = [s["max_gain"] for s in samples if s["max_gain"] > success_pct]
    losses = [abs(s["max_loss"]) for s in samples if s["max_loss"] < 0]
    win_rate = round(len(wins) / len(samples), 4)
    avg_gain = round(sum(wins) / len(wins), 4) if wins else None
    avg_loss = round(sum(losses) / len(losses), 4) if losses else None
    profit_ratio = round(avg_gain / avg_loss, 2) if (avg_gain and avg_loss) else None
    return {
        "count": len(samples),
        "win_rate": win_rate,
        "profit_ratio": profit_ratio,
        "avg_gain": avg_gain,
        "avg_loss": avg_loss,
    }


def backtest_resonance(
    symbols: Sequence[str],
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    hold_days: int = DEFAULT_HOLD_DAYS,
    success_pct: float = DEFAULT_SUCCESS_PCT,
    fund_source: Optional[str] = None,
    activity_line: float = 3.0,
    bars_days: int = 800,
) -> dict:
    """三指标共振回测。

    Args:
        symbols: 股票池(6 位代码)
        start_date / end_date: 信号日区间("YYYY-MM-DD"), None = 不限
        hold_days: 持有/观察窗口(交易日)
        success_pct: 最高涨幅超过该百分比算成功(官方 3%)
        fund_source: None = 双指标(不含资金); "ohlc" = 资金维用 OHLC 对照项
        activity_line: 活跃度门槛(3.0 强势线 / 6.0 大牛线)
        bars_days: 每股取 K 线根数(需覆盖回测区间 + MIN_BARS 预热)

    Returns:
        {
          "params": {...},
          "basis": "双指标(缺资金维)" | "三指标(资金=OHLC对照项)",
          "sample": {"symbols", "signals"},
          "by_phase": { "向好"/"拐点"/"分歧"/"走坏": {count, win_rate, profit_ratio, ...} },
          "official": 官方基准(供对照),
          "note": 口径与局限说明
        }
    """
    from src.core import ai_activity, gs_strategy, resonance

    samples: dict[str, list[dict]] = {}
    ok_symbols = 0
    total_signals = 0

    for sym in symbols:
        code = (sym or "").strip()
        if not (code.isdigit() and len(code) == 6):
            continue
        try:
            from src.core.decision_pioneer import fetch_bars

            bars = fetch_bars(code, "CN", days=bars_days)
        except Exception as e:  # noqa: BLE001
            logger.warning("回测取 K 线失败 %s: %s", code, e)
            continue
        if len(bars) < MIN_BARS + hold_days + 1:
            continue
        ok_symbols += 1

        for i in range(MIN_BARS - 1, len(bars) - hold_days - 1):
            date = str(bars[i].get("date", ""))[:10]
            if start_date and date < start_date:
                continue
            if end_date and date > end_date:
                break

            window = bars[: i + 1]
            try:
                gs_eval = gs_strategy.eval_gs(window)
                trend = gs_strategy.trend_label(gs_eval)
                activity = (ai_activity.eval_activity(window) or {}).get("activity")

                if fund_source == "ohlc":
                    from src.core.ohlc_dark import ohlc_dark_net

                    fund_net = ohlc_dark_net(window, days=1).get("dark_net")
                    if not isinstance(fund_net, (int, float)):
                        continue
                    st = resonance.evaluate_state(trend, activity, None, fund_net, None)
                    phase = st.get("phase")
                else:
                    phase = _state_without_fund(trend, activity, activity_line)
            except Exception as e:  # noqa: BLE001
                logger.debug("回测 %s@%s 计算失败: %s", code, date, e)
                continue

            if not phase or phase == "无":
                continue
            oc = _outcome(bars, i, hold_days)
            if not oc:
                continue
            samples.setdefault(phase, []).append(oc)
            total_signals += 1

    by_phase = {p: _agg(samples.get(p, []), success_pct)
                for p in ("向好", "拐点", "分歧", "走坏")}

    basis = "双指标(缺资金维)" if fund_source is None else "三指标(资金=OHLC对照项)"
    note = (
        "明盘历史无源(TQ 未连接 / ZLJC 历史返空 / big_order_flow 仅当日), "
        "故完整三指标共振的历史资金维不可得。当前为双指标口径, "
        "与官方四态不可直接比较, 只能看趋势+活跃度本身的有效性。"
        if fund_source is None else
        "资金维用 OHLC 分摊暗盘对照项(已知大振幅日 23 倍误差), "
        "仅可看四态**相对排序**, 禁止与官方 75.42% 做优劣结论。"
    )

    return {
        "params": {
            "start_date": start_date, "end_date": end_date,
            "hold_days": hold_days, "success_pct": success_pct,
            "fund_source": fund_source, "activity_line": activity_line,
        },
        "basis": basis,
        "sample": {"symbols": ok_symbols, "signals": total_signals},
        "by_phase": by_phase,
        "official": resonance.BACKTEST,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "note": note,
    }
