"""因子 IC/IR（2026-10-21，本地计算版）。

原来 `get_factor_ic_report` 走 LLM 归因、且要 OpenAI key（实测 `Missing credentials` 后静默降级成
"样本不足"，等于废掉）。这里改成**纯本地方差计算**，不需要任何外部数据：

    IC(T) = Spearman( T 日横截面因子值 , T→T+N 日收益 )
    IR    = mean(IC) / std(IC)

未来收益由日线自算（close(T+N)/close(T)-1），**不需要用户的交割单或调仓记录**。
样本不足（<20 只票 或 <5 个交易日）直接说不足，不编数字。
"""
from __future__ import annotations

import logging
import math
from collections import defaultdict

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

FACTOR_LABELS = {
    "momentum_20": "20 日动量",
    "momentum_60": "60 日动量",
    "vol_20": "20 日年化波动",
    "volume_z20": "量能 z",
    "position_120": "120 日区间位置",
    "concentration": "CYQ 集中度",
}


def _ranks(vals: list[float]) -> list[float]:
    order = sorted(range(len(vals)), key=lambda i: vals[i])
    ranks = [0.0] * len(vals)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and vals[order[j + 1]] == vals[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def _spearman(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 10:
        return None
    rx, ry = _ranks(xs), _ranks(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    vx = math.sqrt(sum((a - mx) ** 2 for a in rx))
    vy = math.sqrt(sum((b - my) ** 2 for b in ry))
    if not vx or not vy:
        return None
    return cov / (vx * vy)


def compute(db: Session, horizon: int = 5, max_dates: int = 60) -> dict:
    from src.db.klines_repo import load_qfq_bars
    from src.db.models import StockFactorDaily
    from sqlalchemy import text

    rows = (
        db.query(StockFactorDaily)
        .order_by(StockFactorDaily.trade_date.desc())
        .limit(60000)
        .all()
    )
    if not rows:
        return {"available": False, "reason": "因子表为空（先跑回填或等待每日采集）"}

    by_date: dict[str, list] = defaultdict(list)
    for r in rows:
        by_date[r.trade_date].append(r)
    dates = sorted(by_date)[-max_dates:]

    # 每只票的收盘价序列：只取因子表里出现过的票
    symbols = {r.symbol for r in rows}
    close_map: dict[str, list[tuple[str, float]]] = {}
    for sym in symbols:
        try:
            bars = load_qfq_bars(sym, "CN", days=260)
        except Exception:  # noqa: BLE001
            continue
        close_map[sym] = [(str(b["date"]).replace("-", ""), float(b["close"])) for b in bars]

    def fwd_ret(sym: str, day: str) -> float | None:
        series = close_map.get(sym)
        if not series:
            return None
        idx = next((i for i, (d, _) in enumerate(series) if d == day), None)
        if idx is None or idx + horizon >= len(series):
            return None
        base = series[idx][1]
        return (series[idx + horizon][1] / base - 1) if base else None

    results = {}
    for factor, label in FACTOR_LABELS.items():
        ics = []
        for day in dates:
            xs, ys = [], []
            for r in by_date[day]:
                v = getattr(r, factor, None)
                if v is None:
                    continue
                y = fwd_ret(r.symbol, day)
                if y is None:
                    continue
                xs.append(float(v))
                ys.append(y)
            ic = _spearman(xs, ys)
            if ic is not None:
                ics.append(ic)
        if len(ics) < 5:
            results[factor] = {"label": label, "ic_mean": None, "days": len(ics)}
            continue
        mean = sum(ics) / len(ics)
        var = sum((i - mean) ** 2 for i in ics) / max(len(ics) - 1, 1)
        std = math.sqrt(var)
        results[factor] = {
            "label": label,
            "ic_mean": round(mean, 4),
            "ic_std": round(std, 4),
            "ir": round(mean / std, 2) if std else None,
            "win_rate": round(sum(1 for i in ics if i > 0) / len(ics) * 100, 1),
            "days": len(ics),
        }
    return {"available": True, "horizon": horizon, "dates": [dates[0], dates[-1]],
            "factors": results}


def render(m: dict) -> str:
    if not m.get("available"):
        return f"[因子 IC] {m.get('reason', '无数据')}——不做推断。"
    lines = [f"[数据源: 本地计算 · stock_factor_daily × 前复权日线] 预测期 {m['horizon']} 个交易日"
             f"｜样本区间 {m['dates'][0]}~{m['dates'][1]}"]
    for _k, v in m["factors"].items():
        if v.get("ic_mean") is None:
            lines.append(f"- {v['label']}：样本不足（{v['days']} 个有效交易日，至少需 5 个）")
            continue
        lines.append(
            f"- {v['label']}：IC 均值 {v['ic_mean']:+.4f}｜IR {v.get('ir')}｜"
            f"胜率 {v['win_rate']}%｜样本 {v['days']} 天"
        )
    lines.append("口径：Spearman 秩相关；未来收益由日线自算，未使用任何外部调仓数据。")
    return "\n".join(lines)
