"""全球隔夜市场指数采集(yahoo finance 免费 HTTP, 无 key)。

盘前分析用: 美股三大指数收盘 + 亚太市场(日经/KOSPI/台湾加权)+ 美股股指期货
(盘中实时,反映隔夜情绪延续)。恒生指数已有腾讯源,这里也带上做完整性。

- fetch_global_indices() -> dict[str, dict]: {name: {price, change_pct, ts}}
- 任一指数失败不影响其他;全部失败返回 {}(调用方如实降级, 不编造)。
"""
from __future__ import annotations

import logging
import urllib.parse
import urllib.request
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# name -> yahoo symbol
_YAHOO_SYMBOLS: dict[str, str] = {
    "道琼斯": "^DJI",
    "纳斯达克": "^IXIC",
    "标普500": "^GSPC",
    "纳指100期货": "NQ=F",
    "道指期货": "YM=F",
    "标普期货": "ES=F",
    "日经225": "^N225",
    "韩国KOSPI": "^KS11",
    "台湾加权": "^TWII",
    "恒生指数": "^HSI",
}

_UA = {"User-Agent": "Mozilla/5.0 (PanWatch/1.0)"}
_CACHE_TTL = 300.0  # 5 分钟进程内缓存
_cache: tuple[float, dict] | None = None


def _fetch_one(symbol: str) -> dict | None:
    """拉单只 yahoo 指数。返回 {price, change_pct} 或 None。"""
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        f"{urllib.parse.quote(symbol)}?range=5d&interval=1d"
    )
    req = urllib.request.Request(url, headers=_UA)
    try:
        import json

        raw = urllib.request.urlopen(req, timeout=10).read()
        data = json.loads(raw)
        result = (data.get("chart") or {}).get("result") or []
        if not result:
            return None
        meta = result[0].get("meta") or {}
        closes = [
            c
            for c in (((result[0].get("indicators") or {}).get("quote") or [{}])[0].get("close") or [])
            if c is not None
        ]
        price = meta.get("regularMarketPrice")
        if price is None:
            price = closes[-1] if closes else None
        if price is None:
            return None
        change_pct = None
        if len(closes) >= 2 and closes[-2]:
            # regularMarketPrice 可能是盘中实时价, 用前一根收盘算涨跌幅
            change_pct = round((float(price) / float(closes[-2]) - 1.0) * 100.0, 2)
        return {"price": float(price), "change_pct": change_pct}
    except Exception as e:  # noqa: BLE001 - 单只失败不影响整体
        logger.debug("[global_indices] %s 拉取失败: %r", symbol, e)
        return None


def fetch_global_indices(force_refresh: bool = False) -> dict:
    """拉全球关键指数。返回 {name: {price, change_pct}}(5min 缓存)。"""
    global _cache
    now = datetime.now(timezone.utc).timestamp()
    if not force_refresh and _cache and now - _cache[0] < _CACHE_TTL:
        return _cache[1]

    out: dict = {}
    for name, sym in _YAHOO_SYMBOLS.items():
        got = _fetch_one(sym)
        if got:
            out[name] = got
    _cache = (now, out)
    logger.info("[global_indices] 采集完成: %d/%d", len(out), len(_YAHOO_SYMBOLS))
    return out


def clear_cache() -> None:
    global _cache
    _cache = None
