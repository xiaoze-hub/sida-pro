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
