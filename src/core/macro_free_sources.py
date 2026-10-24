"""宏观指标免费取数（2026-10-25）。

**为什么用 Yahoo 而不是 iFinD**：iFinD EDB 数据更权威（财政部/IMF 口径、月度、200+ 指标），
但**后端没有它的客户端**（生产容器只有 THS_* 三个变量，无 IFIND/WUDAO/TUSHARE）。
Yahoo Finance chart API 免 key、免鉴权、支持日频历史，实测 `^TNX`(美债10Y)、`BZ=F`(布伦特)、
`DX-Y.NYB`(美元指数) 全部可用 —— 足够回答「美债升了没、油涨了没、美元强了没」。
要更权威的宏观口径（GDP/CPI/社融），接 iFinD EDB 是下一步，本模块不假装能覆盖那些。

口径纪律：
  · `^TNX` 报价 = 收益率 × 10（31.5 表示 3.15%），必须 /10 才是百分数；
  · 一律返回 `source` + `as_of` + 单位，缺失返回空列表 + reason，**不补 0、不插值**。
"""

from __future__ import annotations

import json
import logging
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

_UA = "Mozilla/5.0 (compatible; sida-pro macro reader)"
_BASE = "https://query1.finance.yahoo.com/v8/finance/chart/"

# key -> (yahoo symbol, 缩放系数, 展示名, 单位, 说明)
SERIES: dict[str, tuple[str, float, str, str, str]] = {
    # 2026-10-25 实测: Yahoo 的 ^TNX 现在**直接给百分数**(4.988=4.988%), 不再 ×10。
    # 老资料写的是 ×10(42.5=4.25%) —— 两种格式都在野外存在, 故用自适应归一(见 _norm_yield),
    # 而不是写死系数(写死 0.1 会把 4.988 变成 0.4988, 差 10 倍)。
    "us10y": ("^TNX", 1.0, "美国10年期国债收益率", "%", "Cboe 10Y 收益率;>20 视为 ×10 旧格式自动还原"),
    "us2y": ("^IRX", 1.0, "美国13周国库券收益率", "%", "短期利率代理，非 2Y"),
    "brent": ("BZ=F", 1.0, "布伦特原油", "美元/桶", "ICE 布伦特近月期货"),
    "wti": ("CL=F", 1.0, "WTI 原油", "美元/桶", "NYMEX 近月期货"),
    "dxy": ("DX-Y.NYB", 1.0, "美元指数", "点", "ICE 美元指数"),
    "vix": ("^VIX", 1.0, "VIX 恐慌指数", "点", "Cboe 波动率指数"),
    "spx": ("^GSPC", 1.0, "标普500", "点", "用于对照 A 股外部环境"),
    "gold": ("GC=F", 1.0, "COMEX 黄金", "美元/盎司", "近月期货"),
}

_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_TTL = 300.0  # 5 分钟：宏观日频，没必要每次打网络


def _norm_yield(v: float) -> float:
    """收益率归一：>20 判为 ×10 旧格式(如 49.88→4.988)，否则视为已是百分数。

    为什么不用写死系数：Yahoo 在 2026 年改了 ^TNX 的口径（从 ×10 改成直接给百分数），
    任何写死的系数都会在格式切换时静默差 10 倍 —— 而 10 倍的美债收益率会让归因完全跑偏。
    """
    return round(v / 10.0, 4) if v > 20 else round(v, 4)


def _fetch_raw(symbol: str, range_: str = "6mo", interval: str = "1d") -> dict[str, Any]:
    url = f"{_BASE}{urllib.parse.quote(symbol)}?range={range_}&interval={interval}"
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def get_series(key: str, range_: str = "6mo") -> dict[str, Any]:
    """取单条宏观序列。失败返回 points=[] + reason（绝不编数）。"""
    k = (key or "").strip().lower()
    meta = SERIES.get(k)
    if not meta:
        return {
            "key": k, "points": [], "reason": f"不支持的指标 {key!r}",
            "supported": sorted(SERIES),
        }
    cache_key = f"{k}:{range_}"
    hit = _CACHE.get(cache_key)
    if hit and time.time() - hit[0] < _TTL:
        return hit[1]

    symbol, scale, name, unit, note = meta
    out: dict[str, Any] = {
        "key": k, "name": name, "unit": unit, "symbol": symbol, "note": note,
        "source": "Yahoo Finance chart API（免 key）", "points": [],
    }
    try:
        raw = _fetch_raw(symbol, range_)
        res = (raw.get("chart") or {}).get("result") or []
        if not res:
            out["reason"] = f"Yahoo 无 {symbol} 数据（可能停牌/代码变更）"
            return out
        r = res[0]
        ts = r.get("timestamp") or []
        closes = ((r.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
        pts = []
        for t, c in zip(ts, closes):
            if c is None:
                continue
            day = datetime.fromtimestamp(int(t), tz=timezone.utc).strftime("%Y-%m-%d")
            val = float(c) * scale
            if k == "us10y":
                val = _norm_yield(float(c))
            pts.append({"date": day, "value": round(val, 4)})
        pts.sort(key=lambda p: p["date"])
        out["points"] = pts
        out["as_of"] = pts[-1]["date"] if pts else None
        if not pts:
            out["reason"] = "返回时间序列为空"
        _CACHE[cache_key] = (time.time(), out)
    except Exception as e:  # noqa: BLE001 — 外部源失败如实上报
        logger.warning("宏观序列取数失败 %s(%s): %s", k, symbol, e)
        out["reason"] = f"取数失败：{type(e).__name__}: {str(e)[:80]}"
    return out


def summarize(key: str, range_: str = "6mo") -> dict[str, Any]:
    """序列 → 首末/高低/区间涨跌（供工具直接叙述，不再让模型自己算）。"""
    s = get_series(key, range_)
    pts = s.get("points") or []
    if len(pts) < 2:
        return {**s, "change": None, "change_pct": None, "high": None, "low": None}
    vals = [p["value"] for p in pts]
    first, last = vals[0], vals[-1]
    return {
        **s,
        "first_date": pts[0]["date"], "last_date": pts[-1]["date"],
        "first": first, "last": last,
        "change": round(last - first, 4),
        "change_pct": round((last - first) / first * 100, 2) if first else None,
        "high": max(vals), "low": min(vals),
    }


def snapshot_all(range_: str = "1mo") -> list[dict[str, Any]]:
    """一次取全部指标（并行不值得，8 个串行 <2s，日频够用）。"""
    return [summarize(k, range_) for k in SERIES]
