"""分时画像（2026-10-21）：把"分时图怎么读"变成可复算的数字，而不是让模型看图猜。

数据来源分工（都是零配额/已有通道）：
- 分时价量序列：腾讯 `appstock/app/minute/query`（每点含累计量额，增量在这里算）
- 外盘/内盘 + 五档 + 量比：腾讯 `qt.gtimg.cn`（字段 7/8 = 外盘/内盘，9-28 = 五档）
- 撤单率：TQ 会话累计值（另有工具，不在这里重复取）

为什么不让模型直接"看"分时：它会自己编出"放量突破"这种结论。
这里只给**事实**（几点放量、均价线上方占多久、内外盘差多少），结论留给技能手册的读法规则。
"""
from __future__ import annotations

import json
import logging
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

_QT_URL = "https://qt.gtimg.cn/q={code}"
_MIN_URL = "https://web.ifzq.gtimg.cn/appstock/app/minute/query?code={code}"

#: 分时成交段。**半开区间 `[lo, hi)` 会丢掉收尾那一分钟**（11:30 与 15:00 的柱
#: 各自是"上午收盘/全天收盘"那一笔，落在字典外变成「其他」）—— 2026-10-23 审计修复：
#: 判定改为 `lo <= t` 且 `t <= hi`（闭区间），相邻段在 lo 侧不重叠，收尾柱归入本段。
_BUCKETS = (
    ("开盘 30 分钟", "0930", "1000"),
    ("上午其余", "1000", "1130"),
    ("午后前段", "1300", "1430"),
    ("尾盘 30 分钟", "1430", "1500"),
)


def parse_qt(raw: str) -> dict[str, Any]:
    """解析腾讯 qt 单行文本。缺字段给 None，绝不补 0。"""
    txt = (raw or "").strip()
    if "=" not in txt:
        return {}
    payload = txt.split("=", 1)[1].strip().strip(";").strip('"')
    f = payload.split("~")
    if len(f) < 33:
        return {}

    def _num(i: int) -> float | None:
        try:
            v = f[i].strip()
            return float(v) if v not in ("", "-") else None
        except (ValueError, IndexError):
            return None

    bids = [(_num(9 + i * 2), _num(10 + i * 2)) for i in range(5)]
    asks = [(_num(19 + i * 2), _num(20 + i * 2)) for i in range(5)]
    return {
        "name": f[1],
        "price": _num(3),
        "prev_close": _num(4),
        "open": _num(5),
        "total_vol": _num(6),
        "outer": _num(7),      # 外盘(主动买, 手)
        "inner": _num(8),      # 内盘(主动卖, 手)
        "high": _num(33),
        "low": _num(34),
        "change_pct": _num(32),
        "vol_ratio": _num(49),
        "bids": bids,
        "asks": asks,
    }


def _bucket_of(t: str) -> str:
    """时段归属：起始闭、**结束也闭**（收尾柱归本段，见 _BUCKETS 注释）。

    先匹配区间内，再退回「其他」；`1130` 与 `1500` 不再被漏成其他。
    """
    for name, lo, hi in _BUCKETS:
        if lo <= t <= hi:
            return name
    return "其他"


def compute(points: list[dict], qt: dict) -> dict[str, Any]:
    """纯计算：points 每点 {t,price,avg,volume(手)}，qt 为 parse_qt 结果。"""
    pts = [p for p in points or [] if isinstance(p.get("volume"), (int, float))]
    if not pts:
        return {"available": False, "reason": "当日无分时数据(非交易日/停牌/源故障)"}

    prev_close = qt.get("prev_close")
    open_px = pts[0]["price"]
    last = pts[-1]["price"]
    prices = [p["price"] for p in pts]
    day_high, day_low = max(prices), min(prices)
    vwap = pts[-1].get("avg") or 0
    # 均价线量纲守卫：腾讯"累计量"在不同代码上给过 手/股 两种口径，除错 100 倍时
    # avg 会离当日价格一个数量级。落在这个带外一律判为口径异常 → 按"未取到"处理，
    # 绝不拿一个错 100 倍的均价线去算"站上均价线占比"。
    if vwap and (day_low > 0 and day_high > 0):
        if not (day_low * 0.5 <= vwap <= day_high * 1.5):
            logger.debug("[分时] vwap 量纲异常 %s (vwap=%s, 区间 %s~%s)，按未取到处理",
                         qt.get("name"), vwap, day_low, day_high)
            vwap = 0
    total_vol = sum(p["volume"] for p in pts) or 0

    buckets: dict[str, float] = {}
    for p in pts:
        buckets[_bucket_of(p["t"])] = buckets.get(_bucket_of(p["t"]), 0.0) + float(p["volume"])

    peak = max(pts, key=lambda p: float(p["volume"]))
    above = sum(1 for p in pts if p.get("avg") and p["price"] >= p["avg"])

    # 尾盘 30 分钟
    tail = [p for p in pts if p["t"] >= "1430"]
    tail_vol = sum(float(p["volume"]) for p in tail)
    tail_chg = ((tail[-1]["price"] - tail[0]["price"]) / tail[0]["price"] * 100) if len(tail) > 1 else None

    rng = (day_high - day_low) or 0.0
    position = ((last - day_low) / rng * 100) if rng else None  # 0=最低 100=最高

    outer, inner = qt.get("outer"), qt.get("inner")
    # 一字板/封死板会出现 inner==0（全天只成交买盘）→ 原实现 `if (outer and inner)`
    # 把这个**最有信息量的盘口**判成 None（"未取到"）。改成"两侧都有值即可算"，
    # 只有两侧全缺（或全 0）才 None。
    active_buy = None
    if outer is not None and inner is not None and (outer + inner) > 0:
        active_buy = outer / (outer + inner) * 100
    bid5 = sum(q or 0 for _, q in qt.get("bids") or [])
    ask5 = sum(q or 0 for _, q in qt.get("asks") or [])
    # 封板（卖/买一侧全空）时 press_ratio 天然无穷/为零，不能输出成"抛压极大/买压极大"，
    # 单独用 seal_state 表达，press_ratio 留 None。
    seal_state = None
    if ask5 == 0 and bid5 > 0:
        seal_state = "封涨停(无卖盘)"
    elif bid5 == 0 and ask5 > 0:
        seal_state = "封跌停(无买盘)"
    press = (bid5 / ask5) if (ask5 and bid5) else None

    return {
        "available": True,
        "name": qt.get("name") or "",
        "open": open_px,
        "last": last,
        "prev_close": prev_close,
        "gap_pct": round((open_px - prev_close) / prev_close * 100, 2) if prev_close else None,
        "chg_pct": qt.get("change_pct"),
        "high": day_high,
        "low": day_low,
        "position": round(position, 1) if position is not None else None,
        "vwap": vwap,
        "above_vwap_pct": round(above / len(pts) * 100, 1),
        "total_vol": total_vol,
        "buckets": {k: round(v / total_vol * 100, 1) for k, v in buckets.items() if total_vol},
        "peak": {"t": peak["t"], "vol": int(float(peak["volume"]))},
        "tail": {"vol_pct": round(tail_vol / total_vol * 100, 1) if (total_vol and tail) else None,
                 "chg_pct": round(tail_chg, 2) if tail_chg is not None else None,
                 "reached": bool(tail)},
        "active_buy_pct": round(active_buy, 1) if active_buy is not None else None,
        "outer": outer,
        "inner": inner,
        "bid5": bid5,
        "ask5": ask5,
        "press_ratio": round(press, 2) if press is not None else None,
        "seal_state": seal_state,
        "vol_ratio": qt.get("vol_ratio"),
        "points": len(pts),
    }


def render(m: dict) -> str:
    """把指标渲染成工具文本（数字都在这里出，模型只负责读）。"""
    if not m.get("available"):
        return f"[分时画像] {m.get('reason', '无数据')}——不做推断。"
    parts = [f"[数据源: 分时=腾讯 · 内外盘/五档=腾讯qt · 口径=累计量差分] {m['name'] or ''}"]

    def n(v, unit="", nd=2):
        return "--" if v is None else f"{v:.{nd}f}{unit}"

    parts.append(
        f"现价 {n(m['last'])}（涨跌 {n(m['chg_pct'], '%')}）｜开 {n(m['open'])}（缺口 {n(m['gap_pct'], '%')}）"
        f"｜高 {n(m['high'])} 低 {n(m['low'])}｜日内位置 {n(m['position'], '%', 1)}（0=最低 100=最高）"
    )
    parts.append(
        f"均价线(VWAP) {n(m['vwap'])}｜现价{'在均价上方' if (m.get('vwap') and m['last'] >= m['vwap']) else '在均价下方'}"
        f"｜全天站在均价线上方时间占比 {n(m['above_vwap_pct'], '%', 1)}"
    ) if m.get("vwap") else parts.append("均价线(VWAP)：未取到（量纲异常/源缺失，不推断）")
    if m.get("buckets"):
        parts.append("分时段成交量占比：" + "｜".join(f"{k} {v}%" for k, v in m["buckets"].items()))
    _tail = ("尾盘 30 分钟：量占比 " + n(m["tail"]["vol_pct"], "%", 1) +
             "、价格变化 " + n(m["tail"]["chg_pct"], "%")) if m["tail"].get("reached") else "尾盘 30 分钟：尚未到该时段"
    parts.append(f"最大放量分钟 {m['peak']['t']}（{m['peak']['vol']} 手）｜{_tail}")
    if m.get("outer") is not None:
        parts.append(
            f"外盘 {m['outer']:.0f} 手 / 内盘 {m['inner']:.0f} 手｜主动买占比 {n(m['active_buy_pct'], '%', 1)}"
        )
    else:
        parts.append("内外盘：未取到（不推断）")
    if m.get("press_ratio") is not None:
        parts.append(
            f"五档买量 {m['bid5']:.0f} 手 / 卖量 {m['ask5']:.0f} 手｜买卖压比 {m['press_ratio']}"
            f"（>1 买压大，<1 抛压大）｜量比 {n(m['vol_ratio'])}"
        )
    elif m.get("seal_state"):
        parts.append(
            f"五档买量 {m['bid5']:.0f} 手 / 卖量 {m['ask5']:.0f} 手｜{m['seal_state']}"
            f"（压比不可用，不输出）｜量比 {n(m['vol_ratio'])}"
        )
    else:
        parts.append(f"五档：未取到（不推断）｜量比 {n(m['vol_ratio'])}")
    return "\n".join(parts)


def fetch_qt(symbol: str, market: str = "CN") -> dict:
    """腾讯 qt 快照（外盘/内盘/五档/量比）。失败返回空 dict，不编。"""
    prefix = {"CN": "sh" if symbol.startswith(("6", "9")) else "sz", "HK": "hk", "US": "us"}.get(market, "sh")
    if market == "CN" and symbol.startswith("399"):
        prefix = "sz"
    code = symbol if market == "US" else f"{prefix}{symbol}"
    try:
        req = urllib.request.Request(_QT_URL.format(code=code), headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            raw = resp.read().decode("gbk", errors="replace")
        return parse_qt(raw)
    except Exception:  # noqa: BLE001 - 源故障就让工具文本写"未取到"
        return {}


def fetch_minute(symbol: str, market: str = "CN") -> tuple[list[dict] | None, float | None, str | None]:
    """腾讯分时（自含实现：src/core 不许反向依赖 src/web，B4.1）。

    返回 (points, prev_close, note)：
    - points is None → 源故障（note 有值），**不是**"无分时"
    - points == []   → 当日真无分时（非交易日/停牌）
    """
    prefix = {"CN": "sh" if symbol.startswith(("6", "9")) else "sz",
              "HK": "hk", "US": ""}.get(market, "sh")
    if market == "CN":
        if symbol in ("000001", "000300", "000016", "000905", "000852"):
            prefix = "sh"
        elif symbol.startswith("399"):
            prefix = "sz"
    code = f"{prefix}{symbol}" if market != "US" else symbol
    try:
        req = urllib.request.Request(_MIN_URL.format(code=code), headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            d = json.load(resp)
        block = (d.get("data") or {}).get(code) or {}
        rows = (block.get("data") or {}).get("data") or []
        prev_close = None
        qt = block.get("qt") or {}
        qt_list = qt.get(code) if isinstance(qt, dict) else None
        if isinstance(qt_list, list) and len(qt_list) > 4 and qt_list[4]:
            prev_close = float(qt_list[4])
        if not rows:
            return [], prev_close, None
        points, prev_cum = [], 0.0
        for row in rows:
            parts = str(row).split()
            if len(parts) < 4:
                continue
            try:
                t, price = parts[0], float(parts[1])
                cum_vol, cum_amt = float(parts[2]), float(parts[3])  # 累计量(手)/累计额(元)
            except ValueError:
                continue
            # 均价 = 累计额 / (累计量 × 100 股)
            avg = (cum_amt / (cum_vol * 100.0)) if cum_vol > 0 else price
            points.append({"t": t, "price": price, "avg": round(avg, 2),
                           "volume": int(max(cum_vol - prev_cum, 0.0))})
            prev_cum = cum_vol
        return points, prev_close, None
    except Exception as exc:  # noqa: BLE001 - 源故障必须与"真空态"可分
        return None, None, f"分时源(腾讯)暂不可用: {type(exc).__name__}"


def build(symbol: str, market: str = "CN", db=None) -> str:
    """工具入口：分时 + 快照 + 分钟资金 + 撤单趋势，一次给全。

    db 传入时才读撤单采样（工具层有会话）；缺表/缺采样都只是少一段，不抛。
    """
    points, prev_close, note = fetch_minute(symbol, market)
    if points is None:
        return f"[分时画像] 分时源暂不可用：{note or '腾讯分时异常'}——不做推断。"
    qt = fetch_qt(symbol, market)
    if prev_close and not qt.get("prev_close"):
        qt["prev_close"] = prev_close
    blocks = [render(compute(points, qt))]
    try:  # 分钟资金（逐笔聚合）——分时里"这一刻是谁在买"的答案
        from src.core.tick_minute_flow import build as _flow

        blocks.append(_flow(symbol, market))
    except Exception as exc:  # noqa: BLE001
        blocks.append(f"[分钟资金] 取数异常：{type(exc).__name__}")
    if db is not None:
        try:  # 撤单趋势（自采样，非会话累计值）
            from src.collectors.cancel_rate_sampler import cancel_trend

            blocks.append(cancel_trend(db, symbol))
        except Exception as exc:  # noqa: BLE001
            blocks.append(f"[撤单趋势] 读取异常：{type(exc).__name__}")
    return "\n\n".join(blocks)
