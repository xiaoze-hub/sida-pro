"""牛熊线(决策先锋辅助指标, P3 补差, 2026-10-10)。

规格 §7(对齐 docs/decision-pioneer-spec.md:19):
    牛线 = 20日加权均线; 马线 = 5日均线; 买卖线;
    牛线金叉买卖线 = B 买入信号, 死叉 = S 卖出信号。

⚠️ **逆向近似待校准**: 官方"买卖线"精确公式未公开, 本实现用均线作可调近似; 牛线按
规格用**加权均线(WMA, 线性权重 1..n)**, 马线用简单均线。参数落在 `src/core/thresholds`
配置层(默认牛 20 / 马 5 / 买卖线 10, env `SIDA_THRESHOLD_NIUXIONG_*` 覆盖)。

**信号色约定**: B(买)=red, S(卖)=green(与同花顺买红卖绿一致)。
**信号是证据不是建议**: 输出客观字段(信号枚举 + 金叉/死叉时点 + 线值), 无主观措辞。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

CALIBRATION_NOTE = "买卖线口径逆向近似待校准(参数见 thresholds 配置层)"
BUY_COLOR = "red"    # 买红
SELL_COLOR = "green"  # 卖绿


def _close(b: Any) -> float:
    v = b.get("close") if isinstance(b, dict) else getattr(b, "close", None)
    if v is None:
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _time(b: Any) -> str | None:
    for k in ("time", "date", "ts"):
        v = b.get(k) if isinstance(b, dict) else getattr(b, k, None)
        if v is not None:
            return str(v)
    return None


def wma_series(values: list[float], period: int) -> list[float]:
    """加权均线序列(线性权重 1..period, 最近值权重最大); 不足 period 位置返回 0.0。

    长度与 values 对齐(前 period-1 个占位 0.0), 便于与其它均线按索引对齐求交叉。
    """
    out: list[float] = []
    if period <= 0:
        return out
    denom = period * (period + 1) / 2
    for i in range(len(values)):
        if i + 1 < period:
            out.append(0.0)
            continue
        window = values[i - period + 1 : i + 1]
        acc = sum((j + 1) * v for j, v in enumerate(window))
        out.append(acc / denom)
    return out


def sma_series(values: list[float], period: int) -> list[float]:
    """简单均线序列(前 period-1 个占位 0.0)。"""
    out: list[float] = []
    if period <= 0:
        return out
    for i in range(len(values)):
        if i + 1 < period:
            out.append(0.0)
            continue
        out.append(sum(values[i - period + 1 : i + 1]) / period)
    return out


def ema_series(values: list[float], period: int) -> list[float]:
    """EMA 序列(首值播种)。"""
    if not values or period <= 0:
        return []
    out = [values[0]]
    k = 2 / (period + 1)
    for v in values[1:]:
        out.append((v - out[-1]) * k + out[-1])
    return out


def _params(override: dict | None = None) -> dict:
    from src.core import thresholds

    p = {
        "bull_period": int(thresholds.value("niuxiong_bull_period")),
        "horse_period": int(thresholds.value("niuxiong_horse_period")),
        "trade_period": int(thresholds.value("niuxiong_trade_period")),
    }
    if override:
        p.update({k: v for k, v in override.items() if v is not None})
    return p


def compute_niuxiong(bars: list, params: dict | None = None) -> dict | None:
    """牛熊线快照: 牛线(加权) vs 买卖线 的金叉/死叉。

    bars: 按时间升序日K。至少需 max(牛,买卖线) + 2 根(要比较前后两日判交叉),
    否则 None(数据不足)。

    返回:
      {
        available, lines:{bull,horse,trade}, close, bar_time,
        signal:"B"/"S"/None, color:"red"/"green"/None,
        cross:{type:"golden"/"death"|None, direction:"B"/"S"|None, bars_ago, time},
        state:"牛线上方"/"牛线下方", params, calibration
      }
    """
    if not bars:
        return None
    p = _params(params)
    need = max(p["bull_period"], p["trade_period"]) + 2
    if len(bars) < need:
        return None

    closes = [_close(b) for b in bars]
    if any(c <= 0 for c in closes):
        return None

    bull_s = wma_series(closes, p["bull_period"])
    trade_s = sma_series(closes, p["trade_period"])
    horse_s = sma_series(closes, p["horse_period"])
    if not bull_s or not trade_s:
        return None

    # 找最近一次交叉(牛线 上穿/下穿 买卖线)
    cross_type: str | None = None
    cross_dir: str | None = None
    cross_idx = -1
    for i in range(1, len(bull_s)):
        if bull_s[i - 1] == 0.0 or trade_s[i - 1] == 0.0:
            continue
        prev_diff = bull_s[i - 1] - trade_s[i - 1]
        cur_diff = bull_s[i] - trade_s[i]
        if prev_diff <= 0 < cur_diff:
            cross_type, cross_dir, cross_idx = "golden", "B", i
        elif prev_diff >= 0 > cur_diff:
            cross_type, cross_dir, cross_idx = "death", "S", i

    bull_v = round(bull_s[-1], 4)
    trade_v = round(trade_s[-1], 4)
    horse_v = round(horse_s[-1], 4) if horse_s else None
    state = "牛线上方" if bull_s[-1] >= trade_s[-1] else "牛线下方"

    signal = cross_dir if cross_idx >= 0 else None
    color = BUY_COLOR if signal == "B" else (SELL_COLOR if signal == "S" else None)
    bars_ago = (len(bars) - 1 - cross_idx) if cross_idx >= 0 else None
    cross_time = _time(bars[cross_idx]) if cross_idx >= 0 else None

    return {
        "available": True,
        "lines": {"bull": bull_v, "horse": horse_v, "trade": trade_v},
        "close": round(closes[-1], 4),
        "bar_time": _time(bars[-1]),
        "signal": signal,
        "color": color,
        "state": state,
        "cross": {
            "type": cross_type,
            "direction": cross_dir,
            "bars_ago": bars_ago,
            "time": cross_time,
        },
        "params": p,
        "calibration": CALIBRATION_NOTE,
    }


def fetch_niuxiong(symbol: str, market: str = "CN", days: int = 120) -> dict | None:
    """取日K(复用 decision_pioneer.fetch_bars 兜底链) → compute_niuxiong。失败/不足 → None。"""
    try:
        from src.core.decision_pioneer import fetch_bars

        bars = fetch_bars(symbol, market=market, days=days)
    except Exception as e:  # noqa: BLE001
        logger.warning("niuxiong fetch_bars %s 失败: %s", symbol, e)
        bars = []
    return compute_niuxiong(bars)
