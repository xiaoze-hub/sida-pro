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
    from src.db.klines_repo import load_qfq_bars_cached as load_qfq_bars

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


# -- 圈选区间实算(2026-10-22, P1 原生融合) -------------------------------------
#
# 用户在图上是"圈一段"问「这段为什么跌/涨了多少」，而 compute() 只会算固定的
# 5/20/60/120 日窗口——"这段"没法落成数字，模型只能反问或者猜。这里把圈选区间
# 当成一等公民：给定 [start_date, end_date] 就在**这一段**上实算涨跌/高低/回撤/量比。

def normalize_day(v: Any) -> str:
    """日期归一成 YYYY-MM-DD。只做格式归一，不做任何日期推算。

    认不出（空/乱码/月份越界）就返回空串，让调用方按「区间无效」处理——
    宁可写"不足"，也不能悄悄把两个不存在的日期当成有数据。
    """
    s = str(v or "").strip().replace("/", "-").replace(".", "-")
    if not s:
        return ""
    s = s.split("T", 1)[0].split(" ", 1)[0]
    parts = s.split("-")
    if len(parts) != 3:
        return ""
    try:
        y, m, d = (int(p) for p in parts)
    except (TypeError, ValueError):
        return ""
    if not (1900 <= y <= 2200 and 1 <= m <= 12 and 1 <= d <= 31):
        return ""
    return f"{y:04d}-{m:02d}-{d:02d}"


def compute_range_summary(bars: list[dict], start_date: str, end_date: str) -> dict[str, Any]:
    """圈选区间 [start_date, end_date] 的确定性事实（不做方向推断）。

    bars: [{date, open, high, low, close, volume}] 升序（与 load_qfq_bars 一致）。
    口径：收盘价算涨跌幅，最高/最低取 high/low 的实际极值日，最大回撤按**收盘口径**
    峰谷（只允许先见峰再见谷，不拿未来价回头比）。
    """
    sd, ed = normalize_day(start_date), normalize_day(end_date)
    if not sd or not ed:
        return {"available": False, "reason": "圈选区间起止日期无效"}
    if sd > ed:                      # 用户反着拖（从右往左圈）也要能算
        sd, ed = ed, sd

    clean = [b for b in (bars or []) if b.get("close")]
    rows = [b for b in clean if sd <= normalize_day(b.get("date")) <= ed]
    if len(rows) < 2:
        return {"available": False, "reason": "圈选区间有效日线不足 2 根"}

    closes = [float(r["close"]) for r in rows]
    highs = [float(r.get("high") or r["close"]) for r in rows]
    lows = [float(r.get("low") or r["close"]) for r in rows]
    vols = [float(r.get("volume") or 0) for r in rows]

    hi, lo = max(highs), min(lows)
    peak, mdd = closes[0], 0.0
    for c in closes:
        peak = max(peak, c)
        if peak > 0:
            mdd = min(mdd, (c - peak) / peak * 100)

    avg_vol = round(sum(vols) / len(vols), 0)
    tail = [float(b.get("volume") or 0) for b in clean][-20:]
    avg20 = round(sum(tail) / len(tail), 0) if tail else 0

    return {
        "available": True,
        "bar_count": len(rows),
        "start_date": normalize_day(rows[0].get("date")),
        "end_date": normalize_day(rows[-1].get("date")),
        "start_close": round(closes[0], 2),
        "end_close": round(closes[-1], 2),
        "change_pct": _pct(closes[-1], closes[0]),
        "high": round(hi, 2),
        "high_date": normalize_day(rows[highs.index(hi)].get("date")),
        "low": round(lo, 2),
        "low_date": normalize_day(rows[lows.index(lo)].get("date")),
        "max_drawdown_pct": round(mdd, 2),
        "avg_volume": avg_vol,
        "avg20_volume": avg20,
        "vol_ratio": round(avg_vol / avg20, 2) if avg20 else None,
    }


def render_range_summary(m: dict) -> str:
    """两行紧凑证据串（进 system prompt，不能占版面）。"""
    if not m.get("available"):
        reason = str(m.get("reason") or "无数据").strip()
        # 前缀只留一个：compute() 的 reason 已自带「圈选区间…」，别渲染成
        # 「[圈选区间] 圈选区间有效日线不足 2 根」（重复前缀读起来像 bug）。
        if reason.startswith("圈选区间"):
            reason = reason[len("圈选区间"):].strip() or "无数据"
        return f"[圈选区间] {reason}——不做推断。"
    chg = m.get("change_pct")
    vr = m.get("vol_ratio")
    line1 = (
        f"[圈选区间 {m['start_date']}~{m['end_date']}] "
        f"收盘 {m['start_close']}→{m['end_close']}"
        f"｜区间涨跌 {'--' if chg is None else f'{chg:+.2f}%'}"
        f"｜有效日线 {m['bar_count']} 根"
        "｜[数据源: PG 前复权日线 · 圈选区间实算]"
    )
    line2 = (
        f"区间：高 {m['high']}（{m['high_date']}）/ 低 {m['low']}（{m['low_date']}）"
        f"｜最大回撤 {m['max_drawdown_pct']:.2f}%"
        f"｜区间均量 {'--' if vr is None else f'{vr:.2f}'}×20 日均量"
    )
    return line1 + "\n" + line2


def build_range_summary(symbol: str, start_date: str, end_date: str, market: str = "CN") -> str:
    """读库 + 实算 + 渲染。区间可能早于 250 日窗口，故按 800 天取（库里就有 800 天）。"""
    from src.db.klines_repo import load_qfq_bars_cached as load_qfq_bars

    sym = str(symbol or "").strip()
    if not sym:
        return "[圈选区间] 缺少标的——不做推断。"
    try:
        bars = load_qfq_bars(sym, market, days=800)
    except Exception as exc:  # noqa: BLE001
        return f"[圈选区间] 读日线失败：{type(exc).__name__}——不做推断。"
    return render_range_summary(compute_range_summary(bars, start_date, end_date))
