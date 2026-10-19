"""东方财富个股研报数据采集器 (Eastmoney Report API)

官方研报中心直连接口: https://reportapi.eastmoney.com/report/list
支持按 A 股股票代码查询最新券商与机构深度研报、评级、作者、发布日期及 PDF/原文直达。
"""
from __future__ import annotations

import datetime
import json
import logging
import urllib.parse
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

_REPORT_API_URL = "https://reportapi.eastmoney.com/report/list"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Referer": "https://data.eastmoney.com/report/stock.jshtml",
}


def _clean_symbol(symbol: str) -> str:
    s = (symbol or "").strip()
    return s[:6] if len(s) >= 6 else s


def fetch_eastmoney_research_reports(symbol: str, page: int = 1, page_size: int = 20) -> dict[str, Any]:
    """获取指定个股的东方财富研报中心券商研报列表。"""
    sym = _clean_symbol(symbol)
    if not sym:
        return {"items": [], "total": 0, "page": page, "page_size": page_size}

    today_str = datetime.date.today().strftime("%Y-%m-%d")
    # 默认回溯 3 年内的机构研报
    params = {
        "industryCode": "*",
        "pageSize": str(page_size),
        "industry": "*",
        "rating": "*",
        "ratingChange": "*",
        "beginTime": "2023-01-01",
        "endTime": today_str,
        "pageNo": str(page),
        "fields": "",
        "qType": "0",
        "orgCode": "",
        "code": sym,
        "rcode": "",
    }

    url = f"{_REPORT_API_URL}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers=_HEADERS)

    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            total = data.get("hits", 0)
            raw_items = data.get("data") or []

            items = []
            for r in raw_items:
                # 机构评级
                rating = r.get("emRatingName") or r.get("ratingName") or "研报跟踪"
                # 研报代码/ID
                info_code = r.get("infoCode") or ""
                # PDF 链接 (若有 attachCode 或 infoCode)
                attach_code = r.get("attachCode") or info_code
                pdf_url = f"https://pdf.dfcfw.com/pdf/H3_{attach_code}_1.pdf" if attach_code else None
                # 东财研报原文链接
                detail_url = f"https://data.eastmoney.com/report/zw_stock.jshtml?encodeUrl={info_code}" if info_code else None

                # 分析师/作者
                author = r.get("researcher") or r.get("author") or ""

                items.append({
                    "info_code": info_code,
                    "title": r.get("title") or "",
                    "org_name": r.get("orgSName") or r.get("orgName") or "",
                    "author": author,
                    "publish_date": (r.get("publishDate") or "")[:10],
                    "rating": rating,
                    "target_price": r.get("predictNextTwoYearEps"),  # 备用
                    "industry_name": r.get("industryName") or "",
                    "pdf_url": pdf_url,
                    "detail_url": detail_url,
                })

            return {
                "symbol": sym,
                "items": items,
                "total": total,
                "page": page,
                "page_size": page_size,
            }
    except Exception as e:
        logger.warning("获取东方财富研报中心失败(%s): %s", sym, e)
        return {"symbol": sym, "items": [], "total": 0, "page": page, "page_size": page_size, "error": str(e)}
