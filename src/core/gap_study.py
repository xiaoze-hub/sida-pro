# -*- coding: utf-8 -*-
"""高开幅度 → 后续收益 的实证分档(v0.5.80 A7, 借鉴 tick-stock-panel 的"把回测结论标在盘前榜单上")。

## 为什么做
盘前榜单上"高开 7%"这种数字, 用户自己看不出该不该追 —— 要么不标, 要么抄别人的结论。
本模块**用我们自己的日线**算出每个高开档位的实际后果, 只有证据够硬才允许贴徽标。

## 口径(三条都是刻意的)
1. **买入可成交性**: 开盘就涨停的票买不进去(W0.2 已确立"涨跌停不可成交"), 这类样本
   直接剔除并单独计数 —— 留着会把"追高必死"算成一个根本做不到的交易的收益。
   涨停价按**非 ST** 比例算(`limit_ratio(sym, is_st=False)`): 少数 ST 股会漏剔,
   方向上只会让高档位样本偏多, 不改变符号; 漏剔数在 `excluded_limit_up` 里如实暴露。
2. **收益定义**: `r_day = 开盘买入 → 当日收盘`, `r_next = 开盘买入 → 次日收盘`(隔夜另算)。
   全部以百分比返回, 缺次日数据 → None(不补 0)。
3. **样本不足就不下结论**: 每档 `n < MIN_SAMPLES` → `insufficient`;
   还要过**前后半段一致性**(`stable`): 把窗口按日期对半切, 两半的均值符号必须一致
   且各自 `n ≥ MIN_SAMPLES/2` —— 单段好看可能是行情β, 两半都同向才敢贴到盘前。

统计只做描述性: n / 均值 / 中位数 / 胜率 / 标准差 / t=均值/(std/√n)。
**不做多重比较校正**, 所以 `verdict` 只在 `stable=True` 时给"提示", 不给"结论式"措辞。
"""
from __future__ import annotations

import logging
import math
from statistics import mean, median

logger = logging.getLogger(__name__)

MIN_SAMPLES = 60          # 单档最少样本, 低于此不下判断
MIN_HALF_SAMPLES = 30     # 前后半段各自最少样本(≈MIN_SAMPLES/2)

# 高开档位(左闭右开, 单位 %)。下界 -100 覆盖全部低开情形。
GAP_BUCKETS: tuple[tuple[str, float, float], ...] = (
    ("低开", -100.0, 0.0),
    ("平开", 0.0, 2.0),
    ("小幅高开", 2.0, 3.0),
    ("中幅高开", 3.0, 5.0),
    ("大幅高开", 5.0, 7.0),
    ("强高开", 7.0, 9.5),
    ("一字附近高开", 9.5, 100.0),
)


def bucket_of(gap_pct: float | None) -> str | None:
    """gap% → 档位名; 越界/缺数 → None(不硬塞进两端档)。"""
    if gap_pct is None or not math.isfinite(gap_pct):
        return None
    for name, lo, hi in GAP_BUCKETS:
        if lo <= gap_pct < hi:
            return name
    return None


def is_unfillable(symbol: str, prev_close: float | None, open_price: float | None) -> bool:
    """开盘即涨停 = 买不进去。比例取非 ST 档(见模块 docstring 的偏差说明)。"""
    from src.core.limit_rules import limit_ratio

    if not prev_close or prev_close <= 0 or not open_price or open_price <= 0:
        return False
    ratio = limit_ratio((symbol or "")[:6], is_st=False)
    if ratio is None:
        return False
    limit_price = round(prev_close * (1.0 + ratio), 2)
    return open_price >= limit_price - 0.011   # 容一分内的四舍五入误差


def _stats(values: list[float]) -> dict:
    """描述性统计; n<2 时 std/t 为 None(不假装有波动率)。"""
    n = len(values)
    if n == 0:
        return {"n": 0, "mean": None, "median": None, "win_rate": None, "std": None, "t": None}
    m = mean(values)
    out = {
        "n": n,
        "mean": round(m, 3),
        "median": round(median(values), 3),
        "win_rate": round(sum(1 for v in values if v > 0) / n, 3),
        "std": None,
        "t": None,
    }
    if n >= 2:
        var = sum((v - m) ** 2 for v in values) / (n - 1)
        std = math.sqrt(var)
        out["std"] = round(std, 3)
        if std > 0:
            out["t"] = round(m / (std / math.sqrt(n)), 2)
    return out


def _halves(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """按日期排序后对半切(前段/后段), 而不是按标的切 —— 检验的是时间稳定性。"""
    ordered = sorted(rows, key=lambda r: (r.get("date") or "", r.get("symbol") or ""))
    mid = len(ordered) // 2
    return ordered[:mid], ordered[mid:]


def _sign(v: float | None) -> int:
    if v is None or not math.isfinite(v) or abs(v) < 1e-9:
        return 0
    return 1 if v > 0 else -1


def summarize(observations: list[dict], *, min_samples: int = MIN_SAMPLES) -> dict:
    """纯函数: 观测行 → 每档统计 + 是否可贴徽标。

    `observations` 每行需含 `symbol/date/gap_pct/r_day/r_next`(r_* 允许 None)。
    """
    usable = [o for o in observations if bucket_of(o.get("gap_pct")) is not None]
    buckets: list[dict] = []
    for name, lo, hi in GAP_BUCKETS:
        rows = [o for o in usable if bucket_of(o["gap_pct"]) == name]
        day = [o["r_day"] for o in rows if o.get("r_day") is not None]
        nxt = [o["r_next"] for o in rows if o.get("r_next") is not None]
        sd, sn = _stats(day), _stats(nxt)
        h1, h2 = _halves(rows)
        s1 = _stats([o["r_day"] for o in h1 if o.get("r_day") is not None])
        s2 = _stats([o["r_day"] for o in h2 if o.get("r_day") is not None])
        enough = sd["n"] >= min_samples
        stable = (
            s1["n"] >= MIN_HALF_SAMPLES and s2["n"] >= MIN_HALF_SAMPLES
            and s1["mean"] is not None and s2["mean"] is not None
            and _sign(s1["mean"]) == _sign(s2["mean"]) != 0
        )
        verdict = "insufficient"
        if enough:
            if not stable:
                verdict = "unstable"       # 样本够, 但前后半段不同向 → 不敢贴徽标
            elif sd["mean"] is not None:
                verdict = "negative" if sd["mean"] < 0 else "positive"
        buckets.append({
            "bucket": name,
            "gap_min": lo,
            "gap_max": hi,
            "day": sd,
            "next": sn,
            "halves": {"first": s1, "second": s2},
            "enough_samples": enough,
            "stable": stable,
            "verdict": verdict,
        })
    return {"buckets": buckets, "observations": len(usable)}


def run_study(days: int = 250, *, market: str = "CN", min_samples: int = MIN_SAMPLES) -> dict:
    """从已入库日线算一次分档统计。永不抛异常(数据不可用时 available=False + 原因)。

    宇宙 = **库里已有 1d/qfq 日线的标的**(目前偏自选/扫描池, 不是全 A)——
    这个偏差必须随结果一起返回, 否则用户会把它当成全市场规律用。
    """
    from datetime import date, timedelta

    try:
        since = (date.today() - timedelta(days=int(days) * 2)).isoformat()  # 自然日×2 换交易日
        rows, excluded, symbols = _load_observations(market, since)
        usable = [r for r in rows if r["r_day"] is not None]
        out = summarize(usable, min_samples=min_samples)
        latest = max((r["date"] for r in usable), default=None)
        empty_reason = None if out["observations"] else (
            f"窗口内无可用日线(库里 period=1d/adjust=qfq 在 {since} 之后没有样本)")
        return {
            "available": out["observations"] > 0,
            "reason": empty_reason,
            "window_days": int(days),
            "since": since,
            "latest_date": latest,
            "universe": {
                "symbols": symbols,
                "observations": len(usable),
                "note": "样本=已入库日线的标的(自选/扫描池为主), 非全 A 股; 结论只在这些标的内成立",
            },
            "excluded_limit_up": excluded,
            "min_samples": min_samples,
            "buckets": out["buckets"],
        }
    except Exception as e:  # noqa: BLE001 - 统计不可用要如实说, 不能返回空表冒充"无风险"
        logger.warning("高开分档统计失败: %s", e)
        return {"available": False, "reason": str(e), "buckets": [], "window_days": int(days)}


def _load_observations(market: str, since: str) -> tuple[list[dict], int, int]:
    """读日线并用窗口函数取昨收/次收, 在 Python 侧算 gap 与两段收益。

    返回 (观测行, 因"开盘即涨停买不进"被剔除的行数, 参与标的数)。
    """
    from sqlalchemy import text

    from src.db.session import SessionLocal

    sql = text(
        "SELECT symbol, ts, open, close, "
        "  LAG(close) OVER (PARTITION BY symbol ORDER BY ts) AS prev_close, "
        "  LEAD(close) OVER (PARTITION BY symbol ORDER BY ts) AS next_close "
        "FROM klines "
        "WHERE period = '1d' AND adjust = 'qfq' AND market = :mkt AND ts >= :since"
    )
    db = SessionLocal()
    try:
        raw = db.execute(sql, {"mkt": market, "since": since}).fetchall()
    finally:
        db.close()

    kept: list[dict] = []
    excluded = 0
    for r in raw:
        m = dict(r._mapping)
        prev_c, op, cl, nxt_c = m.get("prev_close"), m.get("open"), m.get("close"), m.get("next_close")
        if not prev_c or not op or not cl or prev_c <= 0 or op <= 0:
            continue
        ts = m.get("ts")
        sym = str(m.get("symbol") or "")
        if is_unfillable(sym, float(prev_c), float(op)):
            excluded += 1
            continue
        kept.append({
            "symbol": sym,
            "date": str(ts)[:10] if ts else "",
            "gap_pct": round((float(op) / float(prev_c) - 1.0) * 100.0, 2),
            "r_day": round((float(cl) / float(op) - 1.0) * 100.0, 2),
            "r_next": round((float(nxt_c) / float(op) - 1.0) * 100.0, 2) if nxt_c and float(nxt_c) > 0 else None,
        })
    return kept, excluded, len({r["symbol"] for r in kept})
