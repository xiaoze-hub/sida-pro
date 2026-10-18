"""分时突破「突 / 积」信号(决策先锋辅助指标, P3 补差, 2026-10-10)。

规格 §6(对齐 docs/decision-pioneer-spec.md:18):
- 「突」= 盘整 >15 分钟 + 突然放量异动 + 突破日内高点 + DDE 大单持续流入;
- 「积」= 早盘大单稳健流入积蓄动能(回调买入提示)。

**输入两条链(缺任一即显式降级, 不出信号, 不编造)**:
1. 分钟数据 —— 复用现有 1m 链(`src.core.klines_minute` 落库 / 腾讯
   `fetch_tencent_minute_kline`), `fetch_minute_bars()` 优先读库再落腾讯源;
2. DDE 大单流入 —— 多级取数(全部失败才显式降级, 不编造):
   ① `dde_minute_flow` 盘中采样序列表(全市场, 5 分钟节拍; 样本够判「持续」才直接用);
   ② **腾讯逐笔按分钟聚合**(2026-10-18 接入): 与 `get_main_intent` 同口径
      (单笔 ≥20 万或 ≥600 手, 剔除 09:25-09:30 竞价), 逐分钟主力净流入 ——
      采样不足/为空时用它补全, 使「突」在单票维度真可判;
   ③ thsdk/TQ 当日快照单点(仅能证明"有流入", 不足以证明"持续" → 仍降级)。
   全空才 `available=false` + `reasons`, 并附 `dde_source` 说明实际口径。

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


def _sustained_vals(
    dde_pts: list[tuple[int, float]], at_min: int | None, consec: int, min_wan: float
) -> list[float] | None:
    """截止 at_min(含)最近 consec 个 DDE 采样: 全部 > 0 且累计 >= min_wan(万元) → 返回该批值。

    采样序列已按分钟升序。**按样本数判定"持续流入"**(允许采样节拍粗于 1m —— 默认 5min),
    不要求与分钟K逐点对齐; 样本不足 consec 个 → None(尚未形成"持续", 不编造)。
    """
    if at_min is None:
        return None
    upto = [v for (m, v) in dde_pts if m <= at_min]
    vals = upto[-max(1, consec):]
    if len(vals) < max(1, consec):
        return None
    if all(v > 0 for v in vals) and sum(vals) / 1e4 >= min_wan:
        return vals
    return None


def _detect_breakthrough(
    bars: list[dict], dde_pts: list[tuple[int, float]], p: dict
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

        # DDE 大单持续流入: 截止 i 最近 consec 个采样均 > 0, 且累计 >= 下限
        # (采样节拍可能粗于 1m, 按样本判定"持续", 见 _sustained_vals)
        inflow_vals = _sustained_vals(
            dde_pts, _minutes_of_day(bars[i]["time"]), consec, target_wan
        )
        cond_dde = inflow_vals is not None

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
                     "detail": f"连续{consec}个采样净流入累计{sum(inflow_vals) / 1e4:.1f}万≥{target_wan}万"},
                ],
                "trigger_time": bars[i]["time"],
            }
    return best


def _detect_accumulation(
    bars: list[dict], dde_pts: list[tuple[int, float]], p: dict
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

    # 早盘窗口内的 DDE 采样(按样本, 不要求与分钟K逐点对齐)
    matched = [v for (m, v) in dde_pts if first_mod <= m <= cutoff]
    if not matched:
        return None
    pos = sum(1 for v in matched if v > 0)
    ratio = pos / len(matched)
    total_wan = sum(matched) / 1e4

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

    dde_pts: list[tuple[int, float]] = []
    for d in dde_series or []:
        t = d.get("time") if isinstance(d, dict) else getattr(d, "time", None)
        v = d.get("main_net") if isinstance(d, dict) else getattr(d, "main_net", None)
        if t is None or v is None:
            continue
        m = _minutes_of_day(str(t))
        if m is None:
            continue
        dde_pts.append((m, _f(v)))
    dde_pts.sort(key=lambda x: x[0])
    if not dde_pts:
        reasons.append("DDE大单流入序列缺失(采样表与腾讯逐笔均空)")

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

    hit = _detect_breakthrough(bars, dde_pts, p)
    signal_type = None
    if hit:
        signal_type = "突"
    else:
        hit = _detect_accumulation(bars, dde_pts, p)
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

        with engine.connect() as conn:
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
    """DDE 大单流入序列(元) —— 兼容入口(只要序列, 不要来源标签)。"""
    return fetch_dde_series_with_source(symbol, market)[0]


def fetch_dde_series_with_source(
    symbol: str, market: str = "CN"
) -> tuple[list[dict], str | None]:
    """DDE 大单流入序列(元) + 实际口径标签。

    取数顺序(先精确、先便宜, 全失败才空):
      ① `dde_minute_flow` 盘中采样序列(全市场逐 N 分钟区间净流入), 且样本数已够判
         「持续」(≥ `minute_dde_consecutive_min`, 默认 3) —— 够用就不再多翻逐笔;
      ② 采样不足/为空 → 腾讯逐笔逐分钟聚合(与 `get_main_intent` 同口径);
      ③ 逐笔也不可用 → 仍用采样(哪怕只有 1-2 点, 显式降级但不丢证据);
      ④ thsdk/TQ 当日快照单点(只为「有流入」留证, 单点无法证明持续 → 仍降级)。
    全失败返回 ([], None)(调用方按"无数据"处理)。
    """
    if market.upper() != "CN":
        return [], None

    series = _read_dde_series_db(symbol, market)
    # 采样点不够判「持续」(早盘刚开盘常见) → 先用逐分钟逐笔补全, 避免明明有逐笔却降级。
    need = max(3, int(_params()["dde_consecutive_min"]))
    if len(series) >= need:
        return series, "minute_sample"

    tick_series = _fetch_tencent_tick_dde_series(symbol, market)
    if tick_series:
        return tick_series, "tencent_tick"
    if series:
        return series, "minute_sample"

    net_wan = _fetch_thsdk_dde_wan(symbol)
    if net_wan is None:
        net_wan = _fetch_tq_dde_wan(symbol)
    if net_wan is None:
        return [], None
    bars = fetch_minute_bars(symbol, market)
    t = bars[-1]["time"] if bars else datetime.now(_CST).strftime("%H:%M")
    return [{"time": t, "main_net": net_wan * 1e4}], "intraday_snapshot"  # 万元 → 元


def _fetch_tencent_tick_dde_series(symbol: str, market: str = "CN") -> list[dict]:
    """腾讯逐笔 → 逐分钟主力净流入序列(元)。

    **与 `get_main_intent` 同口径**(`src/core/dark_flow.py`): 单笔成交额 ≥20 万或
    股数 ≥600 手视为主力单, 按主动方向 B/S 计 ±金额, 09:25-09:30 集合竞价撮合
    方向不可信 → 剔除。逐分钟聚合后**该分钟无主力单则记 0**(序列完整, 不虚增"持续流入")。

    为什么这里是**唯一可做逐笔归因的兜底**: 通达信 TQ 网关未开放深交所叫买/叫卖
    委托号与逐笔成交明细, 无法重建主动买卖方向; 腾讯逐笔有方向, 故主力意图与
    本序列共用同一口径。

    - 非交易日直接返回 []: 腾讯逐笔接口按"最近交易日"返回, 套到节假日会把上一
      交易日的序列伪装成今日(宁可显式缺失, 不给错日序列);
    - 任何异常返回 [](调用方继续走快照/降级, 不让指标端点 500)。
    """
    if market.upper() != "CN":
        return []
    try:
        from src.core.trading_calendar import is_trading_day

        if not is_trading_day(datetime.now(_CST).date()):
            return []
    except Exception as e:  # noqa: BLE001 — 日历不可用 → 不给可能错日的序列
        logger.debug("minute_breakthrough 交易日历不可用, 跳过逐笔 DDE: %s", e)
        return []

    try:
        from marketdata.symbol import Market, Symbol
        from src.core.dark_flow import (
            MAIN_AMOUNT,
            MAIN_VOLUME,
            _fetch_all_ticks,
            _tencent_code,
        )

        code = _tencent_code(Symbol(market=Market(market), code=symbol))
        ticks = _fetch_all_ticks(code) if code else []
    except Exception as e:  # noqa: BLE001
        logger.debug("minute_breakthrough 腾讯逐笔 DDE 失败 %s: %s", symbol, e)
        return []

    minutes: set[str] = set()
    net: dict[str, float] = {}
    for tk in ticks or []:
        t = str(tk.get("t") or "")
        if len(t) < 5 or t < "09:30":  # 竞价撮合不计(与 dark_flow 一致)
            continue
        hm = t[:5]
        minutes.add(hm)
        amt = _f(tk.get("amt"))
        vol = _f(tk.get("vol"))
        if not (amt >= MAIN_AMOUNT or vol >= MAIN_VOLUME):
            continue  # 非主力单不进 DDE 大单序列
        d = tk.get("d")
        if d == "B":
            net[hm] = net.get(hm, 0.0) + amt
        elif d == "S":
            net[hm] = net.get(hm, 0.0) - amt

    return [{"time": hm, "main_net": round(net.get(hm, 0.0), 2)} for hm in sorted(minutes)]


def _read_dde_series_db(symbol: str, market: str = "CN") -> list[dict]:
    """读当日 DDE 采样序列表 → 逐样本区间净流入(元)。无行/异常返回 []。"""
    try:
        from sqlalchemy import text

        from src.db.session import engine

        today = datetime.now(_CST).strftime("%Y-%m-%d")
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT sample_ts, delta_net_wan FROM dde_minute_flow "
                    "WHERE symbol = :s AND market = :m AND trade_date = :d "
                    "ORDER BY sample_ts"
                ),
                {"s": symbol, "m": market.upper(), "d": today},
            ).fetchall()
    except Exception as e:  # noqa: BLE001
        logger.debug("minute_breakthrough 读 DDE 采样失败 %s: %s", symbol, e)
        return []
    out: list[dict] = []
    for r in rows:
        if r[1] is None:
            continue
        out.append({"time": str(r[0]), "main_net": _f(r[1]) * 1e4})  # 万元 → 元
    return out


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
    """组合分钟数据 + DDE 序列 → compute_breakthrough。

    额外回传 `dde_source`(实际采用的 DDE 口径: minute_sample / tencent_tick /
    intraday_snapshot / null)与 `note`(降级时的**事实**说明, 不猜原因),
    供前端把「为什么不出信号」讲清楚, 而不是笼统一句"未接入"。
    """
    bars = fetch_minute_bars(symbol, market)
    dde, source = fetch_dde_series_with_source(symbol, market)
    out = compute_breakthrough(bars, dde)
    out["dde_source"] = source
    if out.get("degraded") and not dde:
        out["note"] = (
            "逐分钟 DDE 序列为空(采样表与腾讯逐笔均无数据): "
            "非交易时段或数据源暂不可用; 按铁律不出信号"
        )
    return out
