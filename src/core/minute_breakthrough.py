"""分时突破「突 / 积」信号(决策先锋辅助指标, P3 补差, 2026-10-10)。

规格 §6(对齐 docs/decision-pioneer-spec.md:18):
- 「突」= 盘整 >15 分钟 + 突然放量异动 + 突破日内高点 + DDE 大单持续流入;
- 「积」= 早盘大单稳健流入积蓄动能(回调买入提示)。

**输入两条链(缺任一即显式降级, 不出信号, 不编造)**:
1. 分钟数据 —— 复用现有 1m 链(`src.core.klines_minute` 落库 / 腾讯
   `fetch_tencent_minute_kline`), `fetch_minute_bars()` 优先读库再落腾讯源;
2. DDE 大单流入 —— 现有链(thsdk `get_dde_flow`/`get_main_flow_official` + TQ
   `get_more_info`)给出的都是**当日快照**, 非逐分钟序列 → `fetch_dde_series()` 只能构造
   单点序列, 无法验证"持续/稳健流入" ⇒ 生产上 `compute_breakthrough` 会**显式降级**并给出
   `reasons`, 直到接入逐分钟大单流(后续任务)。

**信号是证据不是建议**: 输出客观字段(信号枚举 + 触发时间 + 触发条件清单), 无主观措辞。
参数全部落在 `src/core/thresholds` 配置层(env `SIDA_THRESHOLD_MINUTE_*` 覆盖)。
"""

from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Any

logger = logging.getLogger(__name__)

MINUTE_CALIBRATION_NOTE = "阈值逆向近似待校准(参数见 thresholds 配置层)"
_CST = ZoneInfo("Asia/Shanghai")


def _f(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _bar(b: Any) -> dict:
    """统一取 bar 字段(兼容 dict / Bar 对象)。"""
    def gv(*keys: str):
        for k in keys:
            v = b.get(k) if isinstance(b, dict) else getattr(b, k, None)
            if v is not None:
                return v
        return None

    t = gv("time", "date", "ts")
    return {
        "time": None if t is None else str(t),
        "open": _f(gv("open")),
        "high": _f(gv("high", "h")),
        "low": _f(gv("low", "l")),
        "close": _f(gv("close", "c")),
        "volume": _f(gv("volume", "vol")),
    }


def _minutes_of_day(t: str | None) -> int | None:
    """"HH:MM" / ISO 时间串 → 当日分钟数(0..1439); 解析不了返回 None。"""
    if not t:
        return None
    s = str(t)
    # ISO: 2026-10-10T09:35:00 / 2026-10-10 09:35:00
    for sep in ("T", " "):
        if sep in s:
            s = s.split(sep, 1)[1]
            break
        if ":" in s:
            break
    parts = s.split(":")
    if len(parts) < 2:
        return None
    try:
        h, m = int(parts[0]), int(parts[1])
    except (TypeError, ValueError):
        return None
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return None
    return h * 60 + m


def _params(override: dict | None = None) -> dict:
    from src.core import thresholds

    p = {
        "consolidation_min": int(thresholds.value("minute_consolidation_min")),
        "consolidation_amp_pct": thresholds.value("minute_consolidation_amp_pct"),
        "volume_spike_mult": thresholds.value("minute_volume_spike_mult"),
        "dde_consecutive_min": int(thresholds.value("minute_dde_consecutive_min")),
        "dde_min_wan": thresholds.value("minute_dde_min_wan"),
        "early_end_minutes": int(thresholds.value("minute_early_end_minutes")),
        "early_inflow_ratio": thresholds.value("minute_early_inflow_ratio"),
        "breakout_tol_pct": thresholds.value("minute_breakout_tol_pct"),
    }
    if override:
        p.update({k: v for k, v in override.items() if v is not None})
    return p


def _amp_pct(highs: list[float], lows: list[float]) -> float:
    lo = min(lows)
    if lo <= 0:
        return 0.0
    return (max(highs) - lo) / lo * 100.0


def _detect_breakthrough(
    bars: list[dict], dde_by_time: dict[str, float], p: dict
) -> dict | None:
    """在 bars 上找最近一次满足「突」四条件的 bar; 找不到返回 None。"""
    c_min = max(1, p["consolidation_min"])
    need = c_min + 2
    if len(bars) < need:
        return None

    target_wan = p["dde_min_wan"]
    consec = max(1, p["dde_consecutive_min"])
    tol = p["breakout_tol_pct"] / 100.0
    spike = p["volume_spike_mult"]

    best: dict | None = None
    for i in range(c_min, len(bars)):
        win = bars[i - c_min : i]
        highs = [b["high"] for b in win]
        lows = [b["low"] for b in win]
        amp = _amp_pct(highs, lows)
        cond_consolidation = amp <= p["consolidation_amp_pct"]

        avg_vol = sum(b["volume"] for b in win) / len(win)
        cond_volume = avg_vol > 0 and bars[i]["volume"] >= spike * avg_vol

        prior_high = max(b["high"] for b in bars[:i])
        cond_breakout = (
            prior_high > 0 and bars[i]["high"] >= prior_high * (1 + tol)
        )

        # DDE 大单持续流入: 结束于 i 的连续 consec 分钟 main_net 均 > 0, 且累计 >= 下限
        inflow_vals: list[float] = []
        for j in range(i - consec + 1, i + 1):
            t = bars[j]["time"]
            if t is None or t not in dde_by_time:
                inflow_vals = []
                break
            inflow_vals.append(dde_by_time[t])
        cond_dde = bool(inflow_vals) and all(v > 0 for v in inflow_vals) and (
            sum(inflow_vals) / 1e4 >= target_wan
        )

        if cond_consolidation and cond_volume and cond_breakout and cond_dde:
            best = {
                "index": i,
                "conditions": [
                    {"name": "盘整>15分钟", "met": True,
                     "detail": f"前{c_min}分钟振幅{amp:.2f}%≤{p['consolidation_amp_pct']}%"},
                    {"name": "突然放量异动", "met": True,
                     "detail": f"量{bars[i]['volume']:.0f}≥{spike}×均量{avg_vol:.0f}"},
                    {"name": "突破日内高点", "met": True,
                     "detail": f"最高{bars[i]['high']}≥前高{prior_high}×(1+{p['breakout_tol_pct']}%)"},
                    {"name": "DDE大单持续流入", "met": True,
                     "detail": f"连续{consec}分钟净流入累计{sum(inflow_vals) / 1e4:.1f}万≥{target_wan}万"},
                ],
                "trigger_time": bars[i]["time"],
            }
    return best


def _detect_accumulation(
    bars: list[dict], dde_by_time: dict[str, float], p: dict
) -> dict | None:
    """早盘大单稳健流入 + 价格蓄势 → 「积」; 否则 None。"""
    early_end = p["early_end_minutes"]
    # 以最早 bar 的分钟数作为开盘起点(兼容 09:25 竞价/09:30 开盘差异)
    first_mod = next((_minutes_of_day(b["time"]) for b in bars if _minutes_of_day(b["time"]) is not None), None)
    if first_mod is None:
        return None
    cutoff = first_mod + early_end
    early = [b for b in bars if (m := _minutes_of_day(b["time"])) is not None and m <= cutoff]
    if len(early) < 3:
        return None

    matched = [(b["time"], dde_by_time[b["time"]]) for b in early if b["time"] in dde_by_time]
    if not matched:
        return None
    pos = sum(1 for _, v in matched if v > 0)
    ratio = pos / len(matched)
    total_wan = sum(v for _, v in matched) / 1e4

    amp = _amp_pct([b["high"] for b in early], [b["low"] for b in early])
    cond_inflow = ratio >= p["early_inflow_ratio"] and total_wan >= p["dde_min_wan"]
    cond_steady = amp <= p["consolidation_amp_pct"]

    if cond_inflow and cond_steady:
        return {
            "index": len(bars) - 1,
            "conditions": [
                {"name": "早盘大单稳健流入", "met": True,
                 "detail": f"早盘{len(matched)}分钟正流入占比{ratio:.0%}≥{p['early_inflow_ratio']:.0%},"
                           f"累计{total_wan:.1f}万≥{p['dde_min_wan']}万"},
                {"name": "价格蓄势(振幅收敛)", "met": True,
                 "detail": f"早盘振幅{amp:.2f}%≤{p['consolidation_amp_pct']}%"},
            ],
            "trigger_time": early[-1]["time"],
        }
    return None


def compute_breakthrough(
    minute_bars: list | None,
    dde_series: list | None,
    params: dict | None = None,
) -> dict:
    """分时突破「突/积」信号。

    minute_bars: 升序分钟K [{time, open, high, low, close, volume}, ...]
    dde_series:  升序逐分钟大单净流入 [{time, main_net}, ...](元); **必须与分钟时间对齐**

    返回(数据不全时显式降级, signal_type=None):
      {available, signal_type, trigger_time, conditions, met_conditions,
       params, degraded, reasons, calibration}
    """
    p = _params(params)
    reasons: list[str] = []

    bars = [_bar(b) for b in (minute_bars or [])]
    if not bars:
        reasons.append("分钟数据缺失")
    elif len(bars) < p["consolidation_min"] + 2:
        reasons.append(f"分钟数据不足({len(bars)}<{p['consolidation_min'] + 2})")

    dde_by_time: dict[str, float] = {}
    for d in dde_series or []:
        t = d.get("time") if isinstance(d, dict) else getattr(d, "time", None)
        v = d.get("main_net") if isinstance(d, dict) else getattr(d, "main_net", None)
        if t is None or v is None:
            continue
        dde_by_time[str(t)] = _f(v)
    if not dde_by_time:
        reasons.append("DDE大单流入序列缺失")

    base = {
        "params": p,
        "degraded": False,
        "reasons": [],
        "calibration": MINUTE_CALIBRATION_NOTE,
    }

    if reasons:
        return {
            **base,
            "available": False,
            "signal_type": None,
            "trigger_time": None,
            "conditions": [],
            "met_conditions": [],
            "degraded": True,
            "reasons": reasons,
        }

    hit = _detect_breakthrough(bars, dde_by_time, p)
    signal_type = None
    if hit:
        signal_type = "突"
    else:
        hit = _detect_accumulation(bars, dde_by_time, p)
        if hit:
            signal_type = "积"

    conditions = hit["conditions"] if hit else []
    return {
        **base,
        "available": True,
        "signal_type": signal_type,
        "trigger_time": hit["trigger_time"] if hit else None,
        "conditions": conditions,
        "met_conditions": [c["name"] for c in conditions if c["met"]],
    }


# ── 取数链(现有链复用; 失败显式返回空) ───────────────────────────────────
def fetch_minute_bars(symbol: str, market: str = "CN") -> list[dict]:
    """取当日 1m 分钟K: 先读库(klines period='1m'), 再落腾讯源。全空返回 []。"""
    bars = _read_minute_klines_db(symbol, market)
    if bars:
        return bars
    return _fetch_tencent_minute(symbol, market)


def _read_minute_klines_db(symbol: str, market: str) -> list[dict]:
    try:
        from sqlalchemy import text

        from src.db.session import engine

        with engine().connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT ts, open, high, low, close, volume FROM klines "
                    "WHERE symbol=:s AND market=:m AND period='1m' "
                    "AND ts >= :start ORDER BY ts"
                ),
                {"s": symbol, "m": market, "start": datetime.now(_CST).strftime("%Y-%m-%d 00:00:00")},
            ).fetchall()
    except Exception as e:  # noqa: BLE001
        logger.debug("minute_breakthrough 读库失败 %s: %s", symbol, e)
        return []
    out: list[dict] = []
    for r in rows:
        ts = r[0]
        t = ts.strftime("%H:%M") if hasattr(ts, "strftime") else str(ts)[-8:-3]
        out.append({"time": t, "open": _f(r[1]), "high": _f(r[2]),
                    "low": _f(r[3]), "close": _f(r[4]), "volume": _f(r[5])})
    return out


def _fetch_tencent_minute(symbol: str, market: str) -> list[dict]:
    try:
        from marketdata.symbol import Market, Symbol
        from marketdata.vendors.kline import fetch_tencent_minute_kline

        tcode = Symbol(market=Market(market), code=symbol).to_tencent()
        raw = fetch_tencent_minute_kline(tcode, 320, "1m")
    except Exception as e:  # noqa: BLE001
        logger.debug("minute_breakthrough 腾讯 1m 失败 %s: %s", symbol, e)
        return []
    return [_bar(b) for b in (raw or [])]


def fetch_dde_series(symbol: str, market: str = "CN") -> list[dict]:
    """DDE 大单流入序列(现有链只有当日快照 → 单点序列)。

    优先级: thsdk `get_main_flow_official`(同花顺官方口径) → TQ `get_more_info`。
    两者都是**快照**, 无法支撑"持续/稳健流入"判定 ⇒ 单点序列会让 compute 显式降级。
    全失败返回 [](调用方按"无数据"处理)。
    """
    if market.upper() != "CN":
        return []
    net_wan = _fetch_thsdk_dde_wan(symbol)
    if net_wan is None:
        net_wan = _fetch_tq_dde_wan(symbol)
    if net_wan is None:
        return []
    bars = fetch_minute_bars(symbol, market)
    t = bars[-1]["time"] if bars else datetime.now(_CST).strftime("%H:%M")
    return [{"time": t, "main_net": net_wan * 1e4}]  # 万元 → 元


def _fetch_thsdk_dde_wan(symbol: str) -> float | None:
    try:
        from data_source.thsdk_l2 import get_main_flow_official

        o = get_main_flow_official(symbol)
        v = (o or {}).get("main_net_amount_wan")
        return float(v) if v is not None else None
    except Exception as e:  # noqa: BLE001
        logger.debug("minute_breakthrough thsdk DDE 失败 %s: %s", symbol, e)
        return None


def _fetch_tq_dde_wan(symbol: str) -> float | None:
    try:
        from src.core.decision_pioneer import fetch_tq_l2

        l2 = fetch_tq_l2(symbol)
        v = (l2 or {}).get("zjl_hb")
        return float(v) if isinstance(v, (int, float)) else None
    except Exception as e:  # noqa: BLE001
        logger.debug("minute_breakthrough TQ DDE 失败 %s: %s", symbol, e)
        return None


def fetch_minute_breakthrough(symbol: str, market: str = "CN") -> dict:
    """组合分钟数据 + DDE 序列 → compute_breakthrough(生产多为显式降级)。"""
    bars = fetch_minute_bars(symbol, market)
    dde = fetch_dde_series(symbol, market)
    return compute_breakthrough(bars, dde)
