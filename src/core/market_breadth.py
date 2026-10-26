"""市场广度 / 情绪周期量化 —— 自研口径。

设计取舍（重要）：
    通达信客户端公式库里有同名指标（ADL/ADR/ARMS/BTI/MCL/STIX），但客户端公式
    必须先在客户端注册才能被 TQ 网关调用（实测 ErrorId=9: no find formula setting），
    且无法用 API 注册。因此这里按**教科书标准定义**自行实现，口径完全透明，
    与客户端同名指标**可能存在细微差异**——对外展示时须标注"自研口径"。

输入只需每日四个市场级原始量：上涨家数 / 下跌家数 / 上涨股成交量合计 / 下跌股成交量合计。
指标定义（教科书）：
    ADL  腾落指标    : ADL_t = ADL_(t-1) + (上涨家数 - 下跌家数)            累计
    ADR  涨跌比率    : MA(上涨家数,10) / MA(下跌家数,10)
    ARMS 阿姆氏(TRIN): (上涨家数/下跌家数) / (上涨股成交量/下跌股成交量)
    BTI  广量冲力    : MA(上涨家数/(上涨家数+下跌家数)*100, 10)；>=61.5 且前 10 日曾 <=40 记为 thrust
    MCL  麦克连      : EMA(涨跌差,19) - EMA(涨跌差,39)，另附累计 Summation
    STIX 指数平滑广量: EMA(上涨家数/(上涨家数+下跌家数)*100, 6)

原则：任何缺失/不可算的分母一律返回 None，绝不用推测值填充。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

ADR_WINDOW = 10
BTI_WINDOW = 10
MCL_FAST = 19
MCL_SLOW = 39
STIX_WINDOW = 6
BTI_THRUST_HIGH = 61.5
BTI_THRUST_LOW = 40.0


@dataclass(frozen=True)
class BreadthBar:
    """一个交易日的市场宽度原始数据。"""

    trade_date: str
    up: int
    down: int
    flat: int = 0
    up_volume: float = 0.0
    down_volume: float = 0.0


def _sma(values: Sequence[float], window: int) -> list[float | None]:
    out: list[float | None] = []
    for i in range(len(values)):
        if i + 1 < window:
            out.append(None)
            continue
        chunk = values[i + 1 - window : i + 1]
        out.append(sum(chunk) / window)
    return out


def _ema(values: Sequence[float], window: int) -> list[float | None]:
    """标准 EMA：alpha = 2/(window+1)，首值取序列首元素。"""
    if not values:
        return []
    alpha = 2.0 / (window + 1)
    out: list[float | None] = [float(values[0])]
    prev = float(values[0])
    for v in values[1:]:
        prev = alpha * float(v) + (1 - alpha) * prev
        out.append(prev)
    return out


def _ratio(num: float, den: float) -> float | None:
    if den in (0, 0.0) or den is None:
        return None
    return num / den


def _pct_up(bar: BreadthBar) -> float | None:
    total = bar.up + bar.down
    if total <= 0:
        return None
    return bar.up * 100.0 / total


def percentile_rank(series: Sequence[float], value: float) -> float | None:
    """value 在 series 中的历史分位（0-100）。样本少于 20 不给分位。"""
    clean = [float(v) for v in series if v is not None]
    if len(clean) < 20 or value is None:
        return None
    below = sum(1 for v in clean if v < value)
    equal = sum(1 for v in clean if v == value)
    return round((below + 0.5 * equal) * 100.0 / len(clean), 1)


def compute_series(bars: Sequence[BreadthBar]) -> list[dict[str, Any]]:
    """逐日计算结果（按输入顺序，需按交易日升序）。"""
    if not bars:
        return []
    ups = [b.up for b in bars]
    downs = [b.down for b in bars]
    diffs = [b.up - b.down for b in bars]
    pct = [_pct_up(b) for b in bars]
    pct_safe = [0.0 if p is None else p for p in pct]

    adr = _sma([float(x) for x in ups], ADR_WINDOW)
    adr_d = _sma([float(x) for x in downs], ADR_WINDOW)
    bti = _sma(pct_safe, BTI_WINDOW)
    mcl_fast = _ema([float(x) for x in diffs], MCL_FAST)
    mcl_slow = _ema([float(x) for x in diffs], MCL_SLOW)
    stix = _ema(pct_safe, STIX_WINDOW)

    out: list[dict[str, Any]] = []
    adl = 0.0
    summ = 0.0
    for i, b in enumerate(bars):
        adl += b.up - b.down
        arms = None
        r1 = _ratio(b.up, b.down)
        r2 = _ratio(b.up_volume, b.down_volume)
        if r1 is not None and r2 is not None and r2 != 0:
            arms = r1 / r2
        adr_v = None
        if adr[i] is not None and adr_d[i] not in (None, 0):
            adr_v = adr[i] / adr_d[i]
        bti_v = bti[i]
        mcl_v = None
        if mcl_fast[i] is not None and mcl_slow[i] is not None:
            mcl_v = mcl_fast[i] - mcl_slow[i]
            summ += mcl_v
        thrust = False
        if bti_v is not None and bti_v >= BTI_THRUST_HIGH:
            window = [x for x in bti[max(0, i - BTI_WINDOW + 1) : i] if x is not None]
            if window and min(window) <= BTI_THRUST_LOW:
                thrust = True
        out.append(
            {
                "trade_date": b.trade_date,
                "up": b.up,
                "down": b.down,
                "flat": b.flat,
                "up_volume": b.up_volume,
                "down_volume": b.down_volume,
                "adl": round(adl, 2),
                "adr": None if adr_v is None else round(adr_v, 4),
                "arms": None if arms is None else round(arms, 4),
                "bti": None if bti_v is None else round(bti_v, 2),
                "bti_thrust": thrust,
                "mcl": None if mcl_v is None else round(mcl_v, 4),
                "mcl_summation": round(summ, 2) if mcl_v is not None else None,
                "stix": None if stix[i] is None else round(stix[i], 2),
                "up_ratio": None if pct[i] is None else round(pct[i], 2),
            }
        )
    return out


def compute_series_with_percentile(bars: Sequence[BreadthBar]) -> list[dict[str, Any]]:
    """逐日结果 + 每个指标相对自身历史的滚动分位（用于画"情绪温度"曲线）。"""
    rows = compute_series(bars)
    if not rows:
        return []
    metrics = ("adl", "adr", "arms", "bti", "mcl", "stix")
    hist: dict[str, list[float]] = {m: [] for m in metrics}
    for r in rows:
        for m in metrics:
            v = r.get(m)
            r[f"{m}_pct"] = percentile_rank(hist[m], v) if v is not None else None
            if v is not None:
                hist[m].append(float(v))
        # 逐日合成温度（落库需要每一天的值，不能只在最新一日算）
        r["sentiment_score"] = sentiment_score(r)
    return rows


def sentiment_score(row: dict[str, Any]) -> int | None:
    """合成情绪温度（0-100）：各指标历史分位的均值。缺失项不参与，无一项可算则 None。"""
    vals = [row.get(f"{m}_pct") for m in ("adl", "adr", "arms", "bti", "mcl", "stix")]
    clean = [float(v) for v in vals if v is not None]
    if not clean:
        return None
    return int(round(sum(clean) / len(clean)))


def latest_summary(bars: Sequence[BreadthBar]) -> dict[str, Any]:
    """给 AI 工具/接口用的最新一日摘要（含温度与逐项分位）。"""
    rows = compute_series_with_percentile(bars)
    if not rows:
        return {"ok": False, "reason": "no_data"}
    last = dict(rows[-1])
    last["sentiment_score"] = sentiment_score(last)
    last["bars_used"] = len(rows)
    last["caliber"] = "自研口径（教科书定义）；非通达信客户端公式，数值可能有差异"
    return last
