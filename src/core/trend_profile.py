"""多周期走势画像（2026-10-21）。

为什么需要它：全景诊断原来只有"今天"——技术面是一句话（空头排列/MACD 死叉/RSI 中性），
没有 5/20/60/120 日涨跌、没有阶段高低点、没有相对强弱。用户看不到"这票是怎么走到今天的"。

数据来源：PG klines 前复权日线（`src/db/klines_repo.load_qfq_bars`，已有 800 天历史，零新增配额）。
口径：全部用**收盘价**计算，涨跌幅不跨复权、不做未来数据；数据不足就写「样本不足」，不硬算。
"""
from __future__ import annotations

from typing import Any

_PERIODS = (5, 20, 60, 120)


def _pct(cur: float, base: float) -> float | None:
    if not base:
        return None
    return round((cur - base) / base * 100, 2)


def _ma(closes: list[float], n: int) -> float | None:
    if len(closes) < n:
        return None
    return round(sum(closes[-n:]) / n, 2)


def compute(bars: list[dict], index_bars: list[dict] | None = None,
            index_label: str = "沪深300") -> dict[str, Any]:
    """bars: [{date, open, high, low, close, volume}] 升序。"""
    bars = [b for b in bars or [] if b.get("close")]
    if len(bars) < 21:
        return {"available": False, "reason": f"日线样本不足（{len(bars)} 根，至少 21 根）"}
    closes = [float(b["close"]) for b in bars]
    vols = [float(b.get("volume") or 0) for b in bars]
    last = closes[-1]

    rets = {}
    for p in _PERIODS:
        rets[p] = _pct(last, closes[-p - 1]) if len(closes) > p else None

    window = bars[-120:] if len(bars) >= 120 else bars
    hi = max(float(b["high"]) for b in window)
    lo = min(float(b["low"]) for b in window)
    pos = round((last - lo) / (hi - lo) * 100, 1) if hi > lo else None

    mas = {n: _ma(closes, n) for n in (5, 10, 20, 60)}
    ma_slope = {}
    for n in (5, 20, 60):
        cur, prev = _ma(closes, n), _ma(closes[:-5], n) if len(closes) > n + 5 else None
        ma_slope[n] = (round(cur - prev, 2) if (cur is not None and prev is not None) else None)

    avg20 = round(sum(vols[-20:]) / 20, 0) if len(vols) >= 20 else None
    vol_ratio = round(vols[-1] / avg20, 2) if (avg20 and vols[-1]) else None
    # 放量日：当天量 > 前 5 日均量 ×1.5（只看最近 20 个交易日）
    big_days = 0
    for i in range(max(len(vols) - 20, 5), len(vols)):
        prev = vols[i - 5:i]
        base = sum(prev) / len(prev) if prev else 0
        if base and vols[i] > base * 1.5:
            big_days += 1

    rs20 = None
    if index_bars and len(index_bars) > 20 and len(closes) > 20:
        ic = [float(b["close"]) for b in index_bars if b.get("close")]
        if len(ic) > 20:
            stock_r = _pct(last, closes[-21])
            idx_r = _pct(ic[-1], ic[-21])
            if stock_r is not None and idx_r is not None:
                rs20 = round(stock_r - idx_r, 2)

    return {
        "available": True,
        "bars": len(bars),
        "last": round(last, 2),
        "rets": rets,
        "high": round(hi, 2),
        "low": round(lo, 2),
        "position": pos,
        "dist_high": _pct(last, hi),
        "dist_low": _pct(last, lo),
        "mas": mas,
        "ma_slope": ma_slope,
        "avg20_vol": avg20,
        "vol_ratio": vol_ratio,
        "big_vol_days": big_days,
        "rs20": rs20,
        "index_label": index_label,
    }


def _fmt(v, unit="%", nd=2):
    return "--" if v is None else f"{v:+.{nd}f}{unit}" if unit else f"{v:.{nd}f}"


def render(m: dict) -> str:
    if not m.get("available"):
        return f"[多周期走势] {m.get('reason', '无数据')}——不做推断。"
    r = m["rets"]
    parts = [
        f"[数据源: PG 前复权日线 · 共 {m['bars']} 根] 现价 {m['last']}",
        "涨跌幅：" + "｜".join(
            f"{p}日 {'--' if r[p] is None else f'{r[p]:+.2f}%'}" for p in _PERIODS),
        f"阶段区间：高 {m['high']} / 低 {m['low']}｜当前位置 {m['position']}%（0=区间最低）"
        f"｜距高点 {_fmt(m['dist_high'])}｜距低点 {_fmt(m['dist_low'])}",
        "均线：" + "｜".join(
            "MA{} {}".format(n, "--" if m["mas"][n] is None else m["mas"][n])
            for n in (5, 10, 20, 60))
        + "（5 日斜率：" + "｜".join(
            "MA{} {}".format(n, "--" if m["ma_slope"][n] is None else "{:+.2f}".format(m["ma_slope"][n]))
            for n in (5, 20, 60)) + "）",
        f"量能：近 20 日均量 {m['avg20_vol'] or '--'}｜今日量比(对 20 日) {m['vol_ratio'] or '--'}"
        f"｜近 20 日放量日 {m['big_vol_days']} 天",
        "相对强弱：20 日 " + ("--（指数数据未取到）" if m["rs20"] is None
                          else "{:+.2f}pp（vs {}）".format(m["rs20"], m.get("index_label") or "指数")),
    ]
    return "\n".join(parts)


def build(symbol: str, market: str = "CN", days: int = 250) -> str:
    """工具入口：读库算指标渲染。"""
    from src.db.klines_repo import load_qfq_bars

    try:
        bars = load_qfq_bars(symbol, market, days=days)
    except Exception as exc:  # noqa: BLE001
        return f"[多周期走势] 读日线失败：{type(exc).__name__}——不做推断。"
    # 指数兜底链：沪深300 → 上证指数（库里通常只有 000001，别因为缺 300 就丢掉 RS）
    index_bars, index_label = None, "沪深300"
    for code, label in (("000300", "沪深300"), ("000001", "上证指数")):
        try:
            got = load_qfq_bars(code, "CN", days=days)
        except Exception:  # noqa: BLE001
            got = []
        if len(got) > 20:
            index_bars, index_label = got, label
            break
    return render(compute(bars, index_bars, index_label))
