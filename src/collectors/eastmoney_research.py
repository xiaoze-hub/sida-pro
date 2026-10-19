"""东方财富机构研报数据采集器 (Eastmoney DataCenter Report API)

数据源：reportName=RPT_DEVELOP_RES_DATA
URL: https://datacenter-web.eastmoney.com/api/data/v1/get
支持按 A 股 6 位股票代码查询机构研报列表、评级、目标价、摘要、券商机构与 PDF 链接。
"""
from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

_DATACENTER_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Referer": "https://data.eastmoney.com/report/",
}


def _clean_symbol(symbol: str) -> str:
    s = (symbol or "").strip()
    return s[:6] if len(s) >= 6 else s


def fetch_eastmoney_research_reports(symbol: str, page: int = 1, page_size: int = 20) -> dict[str, Any]:
    """获取指定个股的东方财富研报中心列表。"""
    sym = _clean_symbol(symbol)
    if not sym:
        return {"items": [], "total": 0, "page": page, "page_size": page_size}

    # 构造请求参数
    filter_expr = f'(SECURITY_CODE="{sym}")'
    params = {
        "sortColumns": "PUBLISH_DATE",
        "sortTypes": "-1",
        "pageSize": str(page_size),
        "pageNumber": str(page),
        "reportName": "RPT_DEVELOP_RES_DATA",
        "columns": "ALL",
        "filter": filter_expr,
    }

    url = f"{_DATACENTER_URL}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers=_HEADERS)

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if not data.get("success") or not data.get("result"):
                return {"items": [], "total": 0, "page": page, "page_size": page_size}

            res = data["result"]
            total = res.get("count", 0)
            raw_data = res.get("data") or []

            items = []
            for r in raw_data:
                # 机构评级
                rating = r.get("EMRATING_NAME") or r.get("RATING_NAME") or "未评级"
                # 研报代码/ID
                info_code = r.get("INFO_CODE") or ""
                # PDF 链接
                pdf_url = f"https://pdf.dfcfw.com/pdf/H3_{info_code}_1.pdf" if info_code else None
                # 东财研报原文链接
                detail_url = f"https://data.eastmoney.com/report/zw_stock.jshtml?encodeUrl={info_code}" if info_code else None

                items.append({
                    "info_code": info_code,
                    "title": r.get("TITLE") or "",
                    "org_name": r.get("ORG_NAME") or r.get("ORG_SNAME") or "",
                    "author": r.get("AUTHOR") or "",
                    "publish_date": (r.get("PUBLISH_DATE") or "")[:10],
                    "rating": rating,
                    "target_price": r.get("TARGET_PRICE"),
                    "predict_this_year_eps": r.get("PREDICT_NEXT_YEAR_EPS"),
                    "predict_this_year_pe": r.get("PREDICT_NEXT_YEAR_PE"),
                    "industry_name": r.get("INDUSTRY_NAME") or "",
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
        logger.warning("获取东方财富研报失败(%s): %s", sym, e)
        return {"symbol": sym, "items": [], "total": 0, "page": page, "page_size": page_size, "error": str(e)}
