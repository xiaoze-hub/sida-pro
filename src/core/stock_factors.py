"""个股因子读取与渲染（2026-10-21）。

只做两件事：读当日因子 + 横截面分位，读近 20 日变化（有历史才有）。
没有数据就明说「未生成」，绝不用近似值填充。
"""
from __future__ import annotations

from typing import Any

_LABELS = {
    "momentum_20": ("20 日动量", "%"),
    "momentum_60": ("60 日动量", "%"),
    "vol_20": ("20 日年化波动", "%"),
    "volume_z20": ("量能 z(60日)", ""),
    "position_120": ("120 日区间位置", ""),
}


def load(db, symbol: str, market: str = "CN") -> dict[str, Any]:
    from src.db.models import StockFactorDaily

    rows = (
        db.query(StockFactorDaily)
        .filter(StockFactorDaily.symbol == str(symbol))
        .order_by(StockFactorDaily.trade_date.desc())
        .limit(21)
        .all()
    )
    if not rows:
        return {"available": False}
    cur = rows[0]
    hist = rows[1:]
    changes: dict[str, float] = {}
    if len(hist) >= 20:
        base = hist[19]
        for key in _LABELS:
            a, b = getattr(cur, key, None), getattr(base, key, None)
            if a is not None and b is not None:
                changes[key] = round(a - b, 2)
    return {
        "available": True,
        "trade_date": cur.trade_date,
        "values": {k: getattr(cur, k, None) for k in _LABELS},
        "ranks": {
            "momentum_20": cur.rank_momentum_20,
            "momentum_60": cur.rank_momentum_60,
            "vol_20": cur.rank_vol_20,
            "volume_z20": cur.rank_volume_z20,
            "position_120": cur.rank_position_120,
        },
        "changes_20d": changes,
        "history_days": len(hist) + 1,
    }


def render(m: dict) -> str:
    if not m.get("available"):
        return "[量化因子] 该标的当日因子尚未生成（因子表按日累积）——不做推断。"
    lines = [f"[数据源: stock_factor_daily · {m['trade_date']} · 横截面分位=同日全市场排名]"]
    for key, (label, unit) in _LABELS.items():
        v = m["values"].get(key)
        if v is None:
            lines.append(f"{label}：暂无")
            continue
        rank = m["ranks"].get(key)
        txt = f"{label}：{v:+.2f}{unit}" if unit == "%" else f"{label}：{v:.2f}"
        if rank is not None:
            txt += f"（全市场 {rank:.0f} 分位）"
        if key in m["changes_20d"]:
            txt += f"｜20 日变化 {m['changes_20d'][key]:+.2f}"
        lines.append(txt)
    if not m["changes_20d"]:
        lines.append(f"（20 日变化需 {20 - (m['history_days'] - 1)} 个交易日后可用，当前仅 {m['history_days']} 天历史）")
    lines.append("口径：动量/波动/量能/位置均来自 PG 前复权日线；换手率与主力净额暂缺（无历史股本/逐笔），不填近似值。")
    return "\n".join(lines)


def build(db, symbol: str, market: str = "CN") -> str:
    try:
        return render(load(db, symbol, market))
    except Exception as exc:  # noqa: BLE001
        return f"[量化因子] 读取失败：{type(exc).__name__}——不做推断。"
