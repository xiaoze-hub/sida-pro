"""趋势操盘线(决策先锋辅助指标, P3 补差, 2026-10-10)。

规格 §5(对齐 docs/decision-pioneer-spec.md:17):
    红/黄/绿三线; 红黄带 = 多方带 / 空方带;
    买点 = 升势中回踩多方带收阳 或 回踩绿线收阳;
    卖点 = 跌势中反弹绿线无力突破。

⚠️ **逆向近似待校准**: 同花顺官方未公开三线的精确均线类型与周期, 本实现用 EMA(指数
均线)作可调近似(红线=快线 / 黄线=中 / 绿线=长), 参数全部落在 `src/core/thresholds`
配置层(默认红线 10 / 黄线 20 / 绿线 60, 支持 env `SIDA_THRESHOLD_TREND_PILOT_*` 覆盖)。
截图/逆向校准后只改配置, 不发版。红≥黄=多方带(快线在慢线上方, 多头排列)。

**信号是证据不是建议**: 输出客观字段(枚举信号 + 触发规则 + 触发条件清单 + 价格/时间),
不含"建议买入/卖出"等主观措辞。

数据缺失(bar 不足)一律返回 None/available=False, 禁止推测编造。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# 逆向近似标注(与前端/文档共用同一常量)
CALIBRATION_NOTE = "逆向近似待校准(EMA 近似, 参数见 thresholds 配置层)"


def _bar_ohlc(b: Any) -> tuple[float, float, float, float, str | None]:
    """统一取 bar 的 (open, high, low, close, time), 兼容 dict / 对象属性。"""
    def g(*keys: str) -> float:
        for k in keys:
            v = b.get(k) if isinstance(b, dict) else getattr(b, k, None)
            if v is None:
                continue
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
        return 0.0

    t = None
    for k in ("time", "date", "ts"):
        v = b.get(k) if isinstance(b, dict) else getattr(b, k, None)
        if v is not None:
            t = str(v)
            break
    return g("open"), g("high", "h"), g("low", "l"), g("close", "c"), t


def _ema_series(values: list[float], period: int) -> list[float]:
    """EMA 序列(首值播种, k=2/(period+1)); 空/非法周期返回 []。"""
    if not values or period <= 0:
        return []
    out = [values[0]]
    k = 2 / (period + 1)
    for v in values[1:]:
        out.append((v - out[-1]) * k + out[-1])
    return out


def _params(override: dict | None = None) -> dict:
    """生效参数(默认取 thresholds 配置层; override 供测试/显式传参)。"""
    from src.core import thresholds

    p = {
        "red_period": int(thresholds.value("trend_pilot_red_period")),
        "yellow_period": int(thresholds.value("trend_pilot_yellow_period")),
        "green_period": int(thresholds.value("trend_pilot_green_period")),
        "band_tol_pct": thresholds.value("trend_pilot_band_tol_pct"),
    }
    if override:
        p.update({k: v for k, v in override.items() if v is not None})
    return p


def compute_trend_line(bars: list, params: dict | None = None) -> dict | None:
    """趋势操盘线单快照(仅评估**最后一根** bar 上的买卖点)。

    bars: 按时间升序的日K(dict / Bar 对象均可)。至少需 max(红,黄,绿)周期 + 1 根,
    否则返回 None(数据不足, 不编造)。

    返回:
      {
        available, lines:{red,yellow,green}, band:{state,low,high},
        trend, close, bar_time,
        buy_points:[{signal:'buy',rule,trigger,price,time}, ...],
        sell_points:[{signal:'sell',rule,trigger,price,time}, ...],
        signals:[买卖点合并], params, calibration
      }
    """
    if not bars:
        return None
    p = _params(params)
    need = max(p["red_period"], p["yellow_period"], p["green_period"]) + 1
    if len(bars) < need:
        return None

    closes: list[float] = []
    for b in bars:
        closes.append(_bar_ohlc(b)[3])
    if any(c <= 0 for c in closes):
        return None

    red_s = _ema_series(closes, p["red_period"])
    yel_s = _ema_series(closes, p["yellow_period"])
    grn_s = _ema_series(closes, p["green_period"])
    if not red_s or not yel_s or not grn_s:
        return None

    red = round(red_s[-1], 4)
    yellow = round(yel_s[-1], 4)
    green = round(grn_s[-1], 4)

    band_low = min(red, yellow)
    band_high = max(red, yellow)
    band_state = "多方带" if red >= yellow else "空方带"

    o, h, low_px, c, t = _bar_ohlc(bars[-1])
    tol = p["band_tol_pct"] / 100.0
    is_yang = c >= o
    has_range = h > low_px  # 一字板(h==l)不产生任何买卖点(避免零振幅假信号)

    # ── 趋势判定(规格: 升势/跌势) ────────────────────────────────────────
    green_prev = grn_s[-2] if len(grn_s) >= 2 else green
    if c > green and green >= green_prev:
        trend = "升势"
    elif c < green and green <= green_prev:
        trend = "跌势"
    else:
        trend = "震荡"

    buy_points: list[dict] = []
    sell_points: list[dict] = []

    def _bp(rule: str, trigger: str) -> dict:
        return {"signal": "buy", "rule": rule, "trigger": trigger,
                "price": round(c, 4), "time": t}

    def _sp(rule: str, trigger: str) -> dict:
        return {"signal": "sell", "rule": rule, "trigger": trigger,
                "price": round(c, 4), "time": t}

    # ── 买点①: 升势中回踩多方带收阳 ──────────────────────────────────────
    if band_state == "多方带" and c > green and has_range:
        touched_band = low_px <= band_high * (1 + tol)
        held_above = c >= band_low * (1 - tol)
        if touched_band and held_above and is_yang:
            buy_points.append(_bp(
                "pullback_bull_band_yang",
                f"多方带(红{red}≥黄{yellow})中回踩带区[{band_low},{band_high}]"
                f"且收阳(收{c}≥开{o})",
            ))

    # ── 买点②: 回踩绿线收阳 ───────────────────────────────────────────────
    if c >= green and low_px <= green * (1 + tol) and is_yang and has_range:
        buy_points.append(_bp(
            "pullback_green_yang",
            f"回踩绿线(最低{low_px}≤绿{green})后收阳(收{c}≥开{o})且收于绿线上方",
        ))

    # ── 卖点: 跌势中反弹绿线无力突破 ─────────────────────────────────────
    if c < green and has_range:
        rebounded = h >= green * (1 - tol)
        failed_break = c < green
        if rebounded and failed_break:
            sell_points.append(_sp(
                "rebound_green_fail",
                f"跌势中反弹触绿线(最高{h}≥绿{green})但无力突破(收{c}<绿{green})",
            ))

    return {
        "available": True,
        "lines": {"red": red, "yellow": yellow, "green": green},
        "band": {"state": band_state, "low": round(band_low, 4), "high": round(band_high, 4)},
        "trend": trend,
        "close": round(c, 4),
        "bar_time": t,
        "buy_points": buy_points,
        "sell_points": sell_points,
        "signals": buy_points + sell_points,
        "params": p,
        "calibration": CALIBRATION_NOTE,
    }


def fetch_trend_line(symbol: str, market: str = "CN", days: int = 120) -> dict | None:
    """取日K(复用 decision_pioneer.fetch_bars 兜底链) → compute_trend_line。

    取数失败/不足 → None(调用方按"无数据"处理; 禁止编造)。
    """
    try:
        from src.core.decision_pioneer import fetch_bars

        bars = fetch_bars(symbol, market=market, days=days)
    except Exception as e:  # noqa: BLE001
        logger.warning("trend_line fetch_bars %s 失败: %s", symbol, e)
        bars = []
    return compute_trend_line(bars)
