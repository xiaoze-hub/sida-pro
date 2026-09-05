"""封单成色检测器(批次A, 2026-09-06 28号)。

场景: 涨停封单持续被撤 = 假封单, 撤单率异动比炸板早几分钟。
数据基座: seal_sampler 盘中 60s 采样 fetch_tq_l2 的累计字段
(BCancel/SCancel 买卖撤单累计、TotalBVol/TotalSVol 买卖总量累计,
TQ9 实测收盘后不归零 → 全部走时序差分口径)。

指标(全为纯函数, 数据不足显式 available=False + reason, 绝不编造):
- cancel_rate_5m   近5min窗口撤单率 = Δ撤 / (Δ买 + Δ卖 + Δ撤)
  (口径对齐 decision_pioneer._l2_summary 的 P2 撤单率, 但做在窗口差分上)
- seal_quality     封单成色 = 1 - cancel_rate_5m
- cancel_bias_5m   撤单方向差 = (Δ买撤-Δ卖撤)/(Δ买撤+Δ卖撤)
  (口径对齐 decision_pioneer._l2_summary: >0 买撤多=托单虚偏空, <0 卖撤多=压单虚偏多)
- cancel_zscore    撤单率异动: 本窗口撤单率相对当日历史窗口的 z-score
  (窗口数 < 10 → None, 不硬算)
- seal_success_rate 封板成功率: 窗口内 is_sealed=True 占比(现价>=涨停价)
- seal_amount      封单额(FCAmo, 元; 字段语义周一盘中实测校准, 仅展示不改指标)
"""
from __future__ import annotations

from datetime import datetime, timedelta

_WINDOW_MIN = 5
_MIN_INTERVALS_FOR_Z = 10


def _to_ts(v) -> datetime | None:
    if isinstance(v, datetime):
        return v
    try:
        return datetime.fromisoformat(str(v))
    except (TypeError, ValueError):
        return None


def _f(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _window_deltas(
    samples: list[dict],
    window_min: int = _WINDOW_MIN,
    now: datetime | None = None,
) -> dict | None:
    """取窗口两端样本(最新一条 + 窗口起点前最近一条)的累计字段差分。

    样本须按 ts 升序。任一字段两端缺失/负差分(跨日重置) → None 由上层标注。
    """
    rows = []
    for s in samples or []:
        ts = _to_ts(s.get("ts"))
        if ts is None:
            continue
        rows.append((ts, s))
    if len(rows) < 2:
        return None
    rows.sort(key=lambda x: x[0])
    now = now or rows[-1][0]
    start = now - timedelta(minutes=window_min)
    latest_ts, latest = rows[-1]
    if (now - latest_ts).total_seconds() > 180:
        return None  # 最新样本停滞 3min 以上, 窗口失效
    base = None
    for ts, s in rows:
        if ts <= start:
            base = (ts, s)
        else:
            break
    if base is None:
        base = rows[0]
        if (latest_ts - base[0]).total_seconds() < 120:
            return None  # 窗口不足 2 分钟, 差分无意义

    def _pair(field: str) -> tuple[float, float] | None:
        a = _f(base[1].get(field))
        b = _f(latest.get(field))
        if a is None or b is None:
            return None
        return a, b

    out: dict = {"window_min": (latest_ts - base[0]).total_seconds() / 60.0, "base_ts": base[0]}
    for f in ("cancel_buy", "cancel_sell", "total_buy_vol", "total_sell_vol"):
        p = _pair(f)
        if p is None:
            return None
        out[f"delta_{f}"] = p[1] - p[0]
        if out[f"delta_{f}"] < 0:
            return None  # 累计字段回退 = 跨日重置/数据异常
    out["latest"] = latest
    return out


def compute_from_series(
    samples: list[dict],
    window_min: int = _WINDOW_MIN,
    now: datetime | None = None,
) -> dict:
    """封单成色主入口。samples: [{ts, cancel_buy, cancel_sell, total_buy_vol,
    total_sell_vol, is_sealed, seal_amount, price, limit_price}] (当日, 升序)。

    永远返回 dict; 不可算时 available=False + reason(显式无数据, 不编造)。
    """
    empty = {"available": False, "reason": None}
    d = _window_deltas(samples, window_min, now)
    if d is None:
        return {**empty, "reason": "采样不足或累计字段重置(需当日≥2个间隔≥2min的有效样本)"}

    dcb = d["delta_cancel_buy"]
    dcs = d["delta_cancel_sell"]
    dtb = d["delta_total_buy_vol"]
    dts = d["delta_total_sell_vol"]
    dcancel = dcb + dcs
    dtotal = dtb + dts + dcb + dcs
    if dtotal <= 0:
        return {**empty, "reason": "窗口内买卖+撤单总量为零(停牌或无L2流量)"}

    cancel_rate = dcancel / dtotal
    seal_quality = 1.0 - cancel_rate
    bias = None
    if dcancel > 0:
        bias = (dcb - dcs) / dcancel

    # 撤单率异动 z-score: 基线=窗口起点之前的全部逐对撤单率
    # (不含窗口内样本, 避免暴增自身污染基线)
    cancel_zscore = None
    base_ts = d.get("base_ts")
    if base_ts is not None:
        hist = _pairwise_rates([s for s in (samples or []) if (_to_ts(s.get("ts")) or base_ts) <= base_ts])
    if len(hist) >= _MIN_INTERVALS_FOR_Z:
        cur = cancel_rate
        mean = sum(hist) / len(hist)
        var = sum((x - mean) ** 2 for x in hist) / len(hist)
        std = var ** 0.5
        if std > 1e-9:
            cancel_zscore = round((cur - mean) / std, 2)

    # 封板成功率: 窗口内 is_sealed 已知样本占比
    sealed_known = [
        s.get("is_sealed")
        for ts, s in [( _to_ts(x.get("ts")), x) for x in samples or []]
        if ts is not None
        and (now or _to_ts(samples[-1].get("ts"))) - ts <= timedelta(minutes=window_min * 2)
        and s.get("is_sealed") is not None
    ]
    seal_success_rate = None
    if sealed_known:
        seal_success_rate = round(sum(1 for v in sealed_known if v) / len(sealed_known), 4)

    latest = d["latest"] or {}
    return {
        "available": True,
        "reason": None,
        "window_min": round(d["window_min"], 1),
        "n_samples": len(samples or []),
        "cancel_rate_5m": round(cancel_rate, 4),
        "seal_quality": round(seal_quality, 4),
        "cancel_bias_5m": round(bias, 4) if bias is not None else None,
        "cancel_zscore": cancel_zscore,
        "seal_success_rate": seal_success_rate,
        "seal_amount": _f(latest.get("seal_amount")),
        "price": _f(latest.get("price")),
        "limit_price": _f(latest.get("limit_price")),
        "is_sealed": latest.get("is_sealed"),
    }


def _pairwise_rates(samples: list[dict]) -> list[float]:
    """相邻样本对的撤单率序列(逐对差分), 用于 z-score 基线。"""
    rows = []
    for s in samples or []:
        ts = _to_ts(s.get("ts"))
        if ts is None:
            continue
        rows.append((ts, s))
    rows.sort(key=lambda x: x[0])
    rates: list[float] = []
    for (t0, s0), (t1, s1) in zip(rows, rows[1:]):
        if (t1 - t0).total_seconds() <= 0:
            continue
        vals = []
        for f in ("cancel_buy", "cancel_sell", "total_buy_vol", "total_sell_vol"):
            a = _f(s0.get(f))
            b = _f(s1.get(f))
            if a is None or b is None:
                vals = []
                break
            vals.append(b - a)
        if not vals or any(v < 0 for v in vals):
            continue  # 重置/缺失对不入基线
        dcb, dcs, dtb, dts = vals
        denom = dtb + dts + dcb + dcs
        if denom > 0:
            rates.append((dcb + dcs) / denom)
    return rates
