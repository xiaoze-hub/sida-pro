"""腾讯证券资讯/股东/基本面 vendor(2026-08-11 深挖接入)。

统一封装腾讯网页端(gu.qq.com)同源接口, 供盘中监测/盘前盘后/chat 助手复用:

- notice_list  : news/noticeList/search  → 个股公告列表(异动/停复牌/财报)
- news_list    : news/info/search       → 个股新闻列表
- top_holders  : hs/ltgd/get?type=ltgd  → 十大股东/流通股东(筹码结构)
- org_rating   : hs/jggd/get            → 机构股东评级统计
- industry_rank: hs/hypm/get            → 行业排名(PE/市值/每股收益 vs 板块均值)
- stock_brief  : app/stockinfo/jiankuang → 个股简况(每股收益/净利润/营收/净资产)
- plate_list   : app/stockinfo/plateNew  → 所属板块(区域/概念)
- investment   : hs/tzld/get            → 投资亮点(定增/收购等文本)
- hot_rank     : app/HotStock/getHotRankIndex → 热门股票榜(5分钟/1日热度)

所有函数返回 dict/list 或 None, 失败不抛异常(调用方优雅降级)。
"""
from __future__ import annotations

import json
import logging
import urllib.request

from marketdata.symbol import Symbol

logger = logging.getLogger(__name__)

_BASE = "https://proxy.finance.qq.com/ifzqgtimg/appstock"
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://gu.qq.com/",
}


def _tencent_code(symbol: Symbol) -> str | None:
    code = (symbol.code or "").strip()
    if not code.isdigit() or len(code) != 6:
        return None
    if code[0] in ("6", "9") or code.startswith("688"):
        return f"sh{code}"
    if code[0] in ("0", "2", "3"):
        return f"sz{code}"
    return None


def _get(url: str) -> dict | None:
    try:
        req = urllib.request.Request(url, headers=_HEADERS)
        with urllib.request.urlopen(req, timeout=12) as resp:
            body = resp.read().decode("utf-8", "replace")
        j = json.loads(body)
        if j.get("code") != 0:
            logger.debug(f"[tencent_info] {url[:80]} code={j.get('code')}: {j.get('msg')}")
            return None
        return j.get("data") or {}
    except Exception as e:
        logger.warning(f"[tencent_info] {url[:80]} 失败: {type(e).__name__}: {e}")
        return None


def fetch_notice_list(symbol: Symbol, limit: int = 5) -> list[dict] | None:
    """个股公告列表(异动/停复牌/财报)。"""
    code = _tencent_code(symbol)
    if not code:
        return None
    d = _get(f"{_BASE}/news/noticeList/search?symbol={code}&page=1&num={limit}")
    if not d:
        return None
    inner = d.get("data")
    items = inner.get("data") if isinstance(inner, dict) else (inner or [])
    out = []
    for it in (items or [])[:limit]:
        if isinstance(it, dict):
            out.append({
                "title": it.get("title") or "",
                "time": it.get("time") or "",
                "type": it.get("type") or "",
                "url": it.get("url") or "",
            })
    return out or None


def fetch_news_list(symbol: Symbol, limit: int = 5) -> list[dict] | None:
    """个股新闻列表。"""
    code = _tencent_code(symbol)
    if not code:
        return None
    d = _get(f"{_BASE}/news/info/search?symbol={code}&page=1&n={limit}&type=3")
    if not d:
        return None
    items = d.get("data") or []
    out = []
    for it in items[:limit]:
        if isinstance(it, dict):
            out.append({
                "title": it.get("title") or "",
                "time": it.get("time") or "",
            })
    return out or None


def fetch_top_holders(symbol: Symbol, limit: int = 10) -> list[dict] | None:
    """十大股东(筹码结构: 国有/自然人/机构持股占比)。"""
    code = _tencent_code(symbol)
    if not code:
        return None
    d = _get(f"{_BASE}/hs/ltgd/get?code={code}&type=ltgd")
    if not d:
        return None
    rows = (d.get("data") or [{}])[0].get("rows") or []
    out = []
    for r in rows[:limit]:
        if isinstance(r, dict):
            out.append({
                "name": r.get("gdmc") or "",
                "shares": _num(r.get("cgsl")),       # 股
                "ratio": _num(r.get("ltbl")),         # 流通占比 %
                "type": r.get("gfxz") or "",           # 国有股/自然人股/机构
                "changed": r.get("bdms"),              # 变动标志
            })
    return out or None


def fetch_org_rating(symbol: Symbol) -> dict | None:
    """机构股东评级统计。"""
    code = _tencent_code(symbol)
    if not code:
        return None
    return _get(f"{_BASE}/hs/jggd/get?code={code}")


def fetch_industry_rank(symbol: Symbol) -> dict | None:
    """行业排名(PE/市值/每股收益 + 板块均值)。"""
    code = _tencent_code(symbol)
    if not code:
        return None
    return _get(f"{_BASE}/hs/hypm/get?code={code}")


def fetch_stock_brief(symbol: Symbol) -> dict | None:
    """个股简况(每股收益/净利润/营收/净资产/行业)。"""
    code = _tencent_code(symbol)
    if not code:
        return None
    return _get(f"{_BASE}/app/stockinfo/jiankuang?code={code}")


def fetch_plate_list(symbol: Symbol) -> dict | None:
    """所属板块(区域/概念)。"""
    code = _tencent_code(symbol)
    if not code:
        return None
    return _get(f"{_BASE}/app/stockinfo/plateNew?code={code}")


def fetch_investment(symbol: Symbol) -> list[str] | None:
    """投资亮点(定增/收购等文本要点)。

    注意: tzld 接口整个返回就是 list(非 {code,data} 包裹), 单独处理。
    """
    code = _tencent_code(symbol)
    if not code:
        return None
    try:
        req = urllib.request.Request(f"{_BASE}/hs/tzld/get?code={code}", headers=_HEADERS)
        with urllib.request.urlopen(req, timeout=12) as resp:
            body = resp.read().decode("utf-8", "replace")
        j = json.loads(body)
        items = j if isinstance(j, list) else (j.get("data") or [])
        return [str(x) for x in items if x] or None
    except Exception as e:
        logger.warning(f"[tencent_info] tzld 失败: {type(e).__name__}: {e}")
        return None


def fetch_hot_rank(limit: int = 10) -> list[dict] | None:
    """热门股票榜(5分钟热度: 市场情绪/题材热度)。"""
    d = _get("https://proxy.finance.qq.com/ifzqgtimg/appstock/app/HotStock/getHotRankIndex?app=web&type=1&day=1")
    if not d:
        return None
    out = []
    for bucket in ("5minutes", "1day"):
        rows = ((d.get(bucket) or {}).get("rankResult") or [])[:limit]
        if rows:
            out.extend({
                "symbol": r.get("symbol") or "",
                "name": r.get("name") or "",
                "rank": r.get("rank") or "",
                "zdf": r.get("zdf") or "",
                "price": r.get("zxj") or "",
                "bucket": bucket,
            } for r in rows if isinstance(r, dict))
            break
    return out or None


def _num(v) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
