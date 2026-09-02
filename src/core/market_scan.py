# -*- coding: utf-8 -*-
"""全市场三榜扫描(P2.3)。

产出:
  1. 今日新出 G 点榜    —— 最后一根 K 线产生 A0 上穿 BB0(G 买信号)的股票
  2. 暗盘资金 TOP 榜    —— OHLC 分摊暗盘净额排序(**对照项, approximation 硬标记**)
  3. 机构活跃度 TOP 榜  —— AI 机构活跃度(7 因子 × 1.2, 阈值 1.56/3/6)排序
  附: ZLJC 主力进出三档批量(md_main_flow_zljc, 明盘参考)

## 数据链路(全部复用既有模块, 不新造轮子)
  K 线:        `decision_pioneer.fetch_bars`(marketdata Engine, TQ 优先, 含当日)
  GS:          `decision_pioneer.compute_gs_signal`(BB0/A0 交叉, 与前端 InteractiveKline 已等价验证)
  活跃度:      `decision_pioneer.compute_institution_activity`(7 因子 MAX × 1.2)
  暗盘(对照):  `ohlc_dark.ohlc_dark_net`(APZJ 对照项, 已知大阳线日失真, 仅排序参考)
  ZLJC:        `marketdata_client.md_main_flow_zljc`(formula_process_mul_zb 批量)

## 硬规则
  - 暗盘榜是**对照项**, 每条记录带 `approximation=True`, 禁止当作真实暗盘意图。
    真实暗盘 = L2 逐笔 + 拆单识别(主线, 见 dark_pool_flow.py 注释)。
  - 金额 = 元(输出附 `_wan` 万元字段仅展示用), 成交量 = 股。
  - 单股失败不拖垮全局: 计入 skipped, 不编造。
  - 缺失显式 None, 由上层标「无数据」。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional, Sequence

logger = logging.getLogger(__name__)

DEFAULT_TOP_N = 20
DEFAULT_BARS_DAYS = 60


def _valid_symbols(raw: Sequence[str]) -> list[str]:
    """6 位数字 A 股代码, 去重保序。"""
    out: list[str] = []
    for s in raw:
        code = (s or "").strip()
        if code.isdigit() and len(code) == 6 and code not in out:
            out.append(code)
    return out


def _per_stock_metrics(symbol: str, bars_days: int, dark_days: int = 1) -> Optional[dict]:
    """单股三指标计算。bars 为空返回 None。

    Args:
        symbol: 6 位代码
        bars_days: K 线根数(GS 需 ≥27, 建议 ≥60)
        dark_days: 暗盘榜口径窗口(默认 1 = 今日; 官方支持 1/3/5 日)。
                   ⚠️ 不要用全部 bars 累计——60 日 OHLC 分摊会把净额放大到失真量级。

    返回:
        gs_signal / gs_state / new_g(bool) / activity / activity_level /
        dark_net(元, 对照) / close
    """
    from src.core.decision_pioneer import (
        compute_gs_signal,
        compute_institution_activity,
        fetch_bars,
    )
    from src.core.ohlc_dark import ohlc_dark_net

    bars = fetch_bars(symbol, "CN", days=bars_days)
    if not bars:
        return None

    gs = compute_gs_signal(bars)
    act = compute_institution_activity(bars)
    dark = ohlc_dark_net(bars, days=dark_days)

    n = len(bars)
    new_g = bool(
        gs
        and gs.get("signal") == "G"
        and gs.get("last_cross_idx") == n - 1   # 交叉就发生在最后一根(今日)
    )

    return {
        "symbol": symbol,
        "close": bars[-1].get("close"),
        "gs_signal": gs.get("signal") if gs else None,
        "gs_state": gs.get("state") if gs else None,
        "new_g": new_g,
        "activity": act.get("activity") if act else None,
        "activity_level": act.get("level") if act else None,
        "dark_net": dark.get("dark_net"),          # 元, 对照项
        "dark_bars_used": dark.get("bars_used"),
    }


def _fund_net_of(symbol: str, bars: Sequence[dict], source: str) -> tuple[Optional[float], bool]:
    """按口径取"资金"维净额(元)。返回 (净额, 是否近似对照项)。

    | source | 口径 | 速度 | 精度 |
    |--------|------|------|------|
    | "ohlc" | 逐日 OHLC 分摊暗盘(对照项) | 快(纯计算) | 低(已知大振幅日 23 倍误差) |
    | "l2"   | thsdk L2 逐笔拆单暗盘(主线) | 中(~1s/只) | 高 |
    | "full" | 明盘 + 暗盘 = 主力净额(官方口径) | 慢(需 thsdk 两次调用) | 最高 |

    ⚠️ 全市场 5000+ 只逐只调 thsdk 不现实(单只 ~1s ≈ 83 分钟),
    故推荐 **ohlc 粗筛 → 小候选池用 l2/full 复核**的两段式。
    """
    from src.core.ohlc_dark import ohlc_dark_net

    if source == "ohlc":
        d = ohlc_dark_net(list(bars), days=1)
        return d.get("dark_net"), True
    if source == "l2":
        try:
            from src.core.dark_flow_l2 import compute_dark_flow_l2

            r = compute_dark_flow_l2(symbol)
            return ((r or {}).get("net"), False) if r else (None, False)
        except Exception as e:  # noqa: BLE001
            logger.warning("L2 暗盘失败 %s: %s", symbol, e)
            return None, False
    if source == "full":
        try:
            from src.core.dark_pool_flow import compute_pool_flow

            pool = compute_pool_flow(symbol) or {}
            # main_net 仅在明盘+暗盘**同时可得**时才有值(coverage 机制, 不编造)
            return pool.get("main_net"), False
        except Exception as e:  # noqa: BLE001
            logger.warning("主力净额失败 %s: %s", symbol, e)
            return None, False
    raise ValueError(f"未知资金口径 {source!r}(支持 ohlc/l2/full)")


def resonance_pick(
    symbols: Optional[Sequence[str]] = None,
    top_n: int = DEFAULT_TOP_N,
    bars_days: int = DEFAULT_BARS_DAYS,
    activity_line: float = 3.0,
    fund_source: str = "ohlc",
    require_new_g: bool = False,
    with_prev: bool = False,
) -> dict:
    """**三指标共振选股**(官方问题六「策略选股」): 三条件 **AND** 联合筛选。

    与 `scan()` 的区别: `scan()` 出的是三张**独立榜**(各自排序、互不约束);
    本函数是官方意义上的"策略选股" —— 三个条件**同时满足**才入选:

        ① 趋势: GS 出现 G 信号(或处于 G 区间)
        ② 活跃度: AI 机构活跃度站上强势线(3.00)或大牛线(6.00)
        ③ 资金  : 主力资金净额 > 0(流入)

    官方原文(问题六): "在 5000 多只股票中快速筛选……此类股票位置低且有足够的
    资金关注, 后市上涨概率较大"。

    Args:
        symbols: 股票池; None = 全市场(⚠️ 5000+ 只, 建议后台跑或用 ohlc 口径)
        top_n: 最多返回条数
        bars_days: 每股 K 线根数(GS 需 ≥28)
        activity_line: 活跃度门槛, 3.0 = 强势线(三步战法) / 6.0 = 大牛线(官方选股示例)
        fund_source: 资金口径 "ohlc" / "l2" / "full"(见 `_fund_net_of`)
        require_new_g: True = 只要**今日新出** G 信号(当日交叉), False = G 区间也算
        with_prev: 是否多算一日前值以判定"较前一日翻倍"(拐点态); 会翻倍计算量

    Returns:
        {
          "generated_at", "universe", "computed", "skipped",
          "filters": {...},                 # 本次实际使用的筛选条件(便于复盘)
          "picks": [ {symbol, trend, activity, fund_net, state, phase,
                      action, backtest, approximation} ... ],
          "note": 口径说明
        }
    """
    from src.core import ai_activity, gs_strategy, resonance

    if symbols is None:
        from src.web.stock_list import get_stock_list

        raw = [s.get("code") for s in get_stock_list() if isinstance(s, dict)]
        symbols = _valid_symbols(raw)
    else:
        symbols = _valid_symbols(symbols)

    picks: list[dict] = []
    skipped = 0
    computed = 0
    for sym in symbols:
        try:
            from src.core.decision_pioneer import fetch_bars

            bars = fetch_bars(sym, "CN", days=bars_days)
            if not bars:
                skipped += 1
                continue

            gs_eval = gs_strategy.eval_gs(bars)
            trend = gs_strategy.trend_label(gs_eval)
            if trend in ("无数据",):
                skipped += 1
                continue
            if require_new_g and trend != "G信号":
                computed += 1
                continue
            if not require_new_g and trend not in ("G信号", "G区间"):
                computed += 1
                continue

            act_eval = ai_activity.eval_activity(bars)
            activity = act_eval.get("activity")
            if not isinstance(activity, (int, float)) or activity < activity_line:
                computed += 1
                continue

            fund_net, approx = _fund_net_of(sym, bars, fund_source)
            if not isinstance(fund_net, (int, float)):
                computed += 1
                continue

            # 前一日值(判"较前一日翻倍" → 拐点态); 关掉时传 None, 按不满足处理
            act_prev = None
            fund_prev = None
            if with_prev and len(bars) >= 2:
                try:
                    act_prev = (ai_activity.eval_activity(bars[:-1]) or {}).get("activity")
                    fund_prev, _ = _fund_net_of(sym, bars[:-1], fund_source)
                except Exception:  # noqa: BLE001
                    act_prev = fund_prev = None

            st = resonance.evaluate_state(trend, activity, act_prev, fund_net, fund_prev)
            if st.get("phase") not in ("向好", "拐点"):
                computed += 1
                continue

            computed += 1
            picks.append({
                "symbol": sym,
                "close": bars[-1].get("close"),
                "trend": trend,
                "activity": round(activity, 2),
                "activity_line": activity_line,
                "fund_net": round(fund_net, 2),
                "fund_net_wan": round(fund_net / 1e4, 2),
                "state": st.get("state"),
                "phase": st.get("phase"),
                "action": st.get("action"),
                "backtest": st.get("backtest"),
                "approximation": approx,   # True = 资金维用的是 OHLC 对照项(非真值)
            })
        except Exception as e:  # noqa: BLE001
            logger.warning("resonance_pick %s failed: %s", sym, e)
            skipped += 1
            continue

    # 排序: 资金净额降序(同等共振下, 资金更强的靠前)
    picks.sort(key=lambda r: -r["fund_net"])

    note = None
    if fund_source == "ohlc":
        note = ("资金维用 OHLC 分摊暗盘**对照项**(已知大振幅日 23 倍误差), "
                "仅作粗筛; 入选后建议用 fund_source='l2'/'full' 复核")

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "universe": len(symbols),
        "computed": computed,
        "skipped": skipped,
        "filters": {
            "trend": "G信号" if require_new_g else "G信号/G区间",
            "activity_line": activity_line,
            "fund": "净额 > 0(流入)",
            "fund_source": fund_source,
            "phase": "向好/拐点",
        },
        "picks": picks[:top_n],
        "note": note,
    }


def scan(
    symbols: Optional[Sequence[str]] = None,
    top_n: int = DEFAULT_TOP_N,
    bars_days: int = DEFAULT_BARS_DAYS,
    dark_days: int = 1,
    with_zljc: bool = True,
) -> dict:
    """全市场/指定股票池三榜扫描。

    Args:
        symbols: 股票池(6 位代码); None = 全市场(get_stock_list)
        top_n:   各榜保留条数
        bars_days: 每股取 K 线根数(GS 需 ≥27, 建议 ≥60)
        dark_days: 暗盘榜口径窗口(默认 1 = 今日; 官方支持 1/3/5 日)
        with_zljc: 是否附带 ZLJC 主力进出批量(明盘参考)

    Returns:
        {
          "generated_at": iso,
          "universe": 输入股票数,
          "computed": 成功计算数,
          "skipped": 失败/无数据数,
          "new_g_points": [...],   # 今日新出 G 点榜(按 symbol 升序)
          "dark_top": [...],       # 暗盘资金 TOP 榜(净额降序, approximation=True)
          "activity_top": [...],   # 机构活跃度 TOP 榜(活跃度降序)
          "zljc": {...} | None,    # ZLJC 主力进出(明盘参考)
        }
    """
    if symbols is None:
        from src.web.stock_list import get_stock_list

        raw = [s.get("code") for s in get_stock_list() if isinstance(s, dict)]
        symbols = _valid_symbols(raw)
    else:
        symbols = _valid_symbols(symbols)

    metrics: list[dict] = []
    skipped = 0
    for sym in symbols:
        try:
            m = _per_stock_metrics(sym, bars_days, dark_days)
        except Exception as e:  # noqa: BLE001
            logger.warning("market_scan %s failed: %s", sym, e)
            m = None
        if m is None:
            skipped += 1
            continue
        metrics.append(m)

    # 榜1: 今日新出 G 点(最后一根产生 G 交叉)
    new_g_points = [
        {
            "symbol": m["symbol"],
            "close": m["close"],
            "gs_state": m["gs_state"],
        }
        for m in metrics
        if m["new_g"]
    ]
    new_g_points.sort(key=lambda r: r["symbol"])

    # 榜2: 暗盘资金 TOP(对照项, 降序; None 值排最后)
    dark_valid = [m for m in metrics if isinstance(m["dark_net"], (int, float))]
    dark_valid.sort(key=lambda m: -m["dark_net"])
    dark_top = [
        {
            "symbol": m["symbol"],
            "dark_net": round(m["dark_net"], 2),                    # 元
            "dark_net_wan": round(m["dark_net"] / 1e4, 2),          # 万元(展示)
            "approximation": True,   # 硬标记: OHLC 分摊对照项, 非真实暗盘意图
        }
        for m in dark_valid[:top_n]
    ]

    # 榜3: 机构活跃度 TOP(降序)
    act_valid = [m for m in metrics if isinstance(m["activity"], (int, float))]
    act_valid.sort(key=lambda m: -m["activity"])
    activity_top = [
        {
            "symbol": m["symbol"],
            "activity": round(m["activity"], 2),
            "level": m["activity_level"],
        }
        for m in act_valid[:top_n]
    ]

    zljc: Optional[dict] = None
    if with_zljc and symbols:
        try:
            from src.core.marketdata_client import md_main_flow_zljc

            zljc = md_main_flow_zljc(list(symbols)) or None
        except Exception as e:  # noqa: BLE001
            logger.warning("market_scan zljc failed: %s", e)
            zljc = None

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "universe": len(symbols),
        "computed": len(metrics),
        "skipped": skipped,
        "dark_days": dark_days,
        "new_g_points": new_g_points,
        "dark_top": dark_top,
        "activity_top": activity_top,
        "zljc": zljc,
    }
