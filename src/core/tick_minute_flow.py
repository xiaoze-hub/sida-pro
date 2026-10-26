"""逐笔 → 分钟资金（2026-10-21）。

现有 `get_main_intent` 只给**全天**净额：能说"今天主力净流入 900 万"，
说不出"10:30 那波净流入 300 万是谁买的"。分时判断需要的是后一句。

本模块把逐笔（腾讯口径，字段 d=B/S/M、amt、vol、t）按分钟聚合：
- 每分钟主动买额/主动卖额/净额
- 大单（默认 ≥500 手）净额 —— 拆单伪装的散户单不算大单
- 全天汇总 + 净流入/流出最猛的那一分钟 + 尾盘 30 分钟净额

纯函数 + 一个取数入口，口径写在输出里，缺数据就说缺，不补 0。
"""
from __future__ import annotations

from typing import Any

BIG_LOT_HAND = 500  # 500 手 = 5 万股，约 50~150 万元，属大单门槛


def _hhmm(t: str) -> str | None:
    """'10:31:05' → '1031'；非法返回 None。"""
    s = str(t or "")
    if len(s) < 5 or ":" not in s:
        return None
    hh, _, rest = s.partition(":")
    mm = rest[:2]
    if not (hh.isdigit() and mm.isdigit()):
        return None
    return f"{hh}{mm}"


def aggregate(ticks: list[dict], *, big_lot: int = BIG_LOT_HAND) -> list[dict[str, Any]]:
    """按分钟聚合。方向口径：d=B 主动买 / S 主动卖 / M 中性(不计入净额)。"""
    buckets: dict[str, dict[str, float]] = {}
    for tk in ticks or []:
        key = _hhmm(tk.get("t"))
        if not key:
            continue
        amt = float(tk.get("amt") or 0)
        vol = float(tk.get("vol") or 0)
        d = str(tk.get("d") or "M").upper()
        b = buckets.setdefault(key, {"buy": 0.0, "sell": 0.0, "big_buy": 0.0,
                                     "big_sell": 0.0, "vol": 0.0})
        b["vol"] += vol
        if d == "B":
            b["buy"] += amt
            if vol >= big_lot:
                b["big_buy"] += amt
        elif d == "S":
            b["sell"] += amt
            if vol >= big_lot:
                b["big_sell"] += amt
    rows = []
    for key in sorted(buckets):
        b = buckets[key]
        rows.append({
            "hhmm": key,
            "buy_amt": round(b["buy"], 0),
            "sell_amt": round(b["sell"], 0),
            "net_amt": round(b["buy"] - b["sell"], 0),
            "big_net_amt": round(b["big_buy"] - b["big_sell"], 0),
            "vol": round(b["vol"], 0),
        })
    return rows


def summarize(rows: list[dict], *, top_n: int = 3, tail_from: str = "1430") -> dict[str, Any]:
    """全天汇总 + 极值分钟 + 尾盘。"""
    if not rows:
        return {"available": False}
    buy = sum(r["buy_amt"] for r in rows)
    sell = sum(r["sell_amt"] for r in rows)
    net = buy - sell
    big = sum(r["big_net_amt"] for r in rows)
    # 只列真正的流入/流出分钟，别把反向的也塞进榜单
    inflow = sorted((r for r in rows if r["net_amt"] > 0),
                    key=lambda r: r["net_amt"], reverse=True)[:top_n]
    outflow = sorted((r for r in rows if r["net_amt"] < 0), key=lambda r: r["net_amt"])[:top_n]
    tail = [r for r in rows if r["hhmm"] >= tail_from]
    return {
        "available": True,
        "minutes": len(rows),
        "buy_amt": round(buy, 0),
        "sell_amt": round(sell, 0),
        "net_amt": round(net, 0),
        "active_buy_pct": round(buy / (buy + sell) * 100, 1) if (buy + sell) else None,
        "big_net_amt": round(big, 0),
        "inflow_top": [{"hhmm": r["hhmm"], "net_amt": r["net_amt"]} for r in inflow],
        "outflow_top": [{"hhmm": r["hhmm"], "net_amt": r["net_amt"]} for r in outflow],
        "tail_net_amt": round(sum(r["net_amt"] for r in tail), 0) if tail else None,
        "tail_reached": bool(tail),
    }


def _fmt(v: float | None) -> str:
    if v is None:
        return "--"
    a = abs(v)
    if a >= 1e8:
        return f"{v / 1e8:.2f} 亿"
    if a >= 1e4:
        return f"{v / 1e4:.0f} 万"
    return f"{v:.0f} 元"


def render(summary: dict, *, big_lot: int = BIG_LOT_HAND) -> str:
    if not summary.get("available"):
        return "[分钟资金] 无逐笔数据（非交易时段或源异常）——不做推断。"
    parts = [
        f"[数据源: 腾讯逐笔 → 分钟聚合 · 大单口径 ≥{big_lot} 手] 共 {summary['minutes']} 个分钟",
        f"主动买 {_fmt(summary['buy_amt'])} / 主动卖 {_fmt(summary['sell_amt'])}"
        f"｜全天净额 {_fmt(summary['net_amt'])}｜主动买占比 "
        + (f"{summary['active_buy_pct']}%" if summary.get("active_buy_pct") is not None else "--"),
        f"大单净额 {_fmt(summary['big_net_amt'])}（大单方向与总量方向不一致时以分歧看待）",
    ]
    if summary.get("inflow_top"):
        parts.append("净流入最猛分钟：" + "｜".join(
            f"{r['hhmm']} {_fmt(r['net_amt'])}" for r in summary["inflow_top"]))
    if summary.get("outflow_top"):
        parts.append("净流出最猛分钟：" + "｜".join(
            f"{r['hhmm']} {_fmt(r['net_amt'])}" for r in summary["outflow_top"]))
    parts.append(
        "尾盘 30 分钟净额 " + _fmt(summary["tail_net_amt"]) if summary.get("tail_reached")
        else "尾盘 30 分钟：尚未到该时段"
    )
    return "\n".join(parts)


def build(symbol: str, market: str = "CN") -> str:
    """取数 + 聚合 + 渲染。取数失败返回明确文案，不编。"""
    if market != "CN":
        return "[分钟资金] 目前仅支持 A 股(CN)。"
    code = ("sh" if symbol.startswith(("6", "9")) else "sz") + symbol
    try:
        from src.core.dark_flow import _fetch_all_ticks  # 腾讯逐笔(零配额, 带缓存)

        ticks = _fetch_all_ticks(code)
    except Exception as exc:  # noqa: BLE001
        return f"[分钟资金] 逐笔取数失败：{type(exc).__name__}——不做推断。"
    if not ticks:
        return "[分钟资金] 今日无逐笔数据（非交易日/停牌/源异常）——不做推断。"
    return render(summarize(aggregate(ticks)))
