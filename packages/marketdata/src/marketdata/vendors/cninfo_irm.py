"""互动易问答 vendor:巨潮 cninfo(海外可达,公司对传闻/利好的官方回应信源)。

移植自 a-stock-data(SKILL.md V3.6.0)§10.1 cninfo_irm() 函数。

⚠️ 关键坑(2026-08-09 海外节点 43.128.140.167 实测):
  1. 必须两步调用:第一步拿 orgId,第二步才能拿问答列表
  2. 第二步**参数必须放 query string**(POST 但 body 空),否则 HTTP 400
  3. orgId 取自第一步的 secid(即便前缀是 gshk,靠 stockcode 过滤照样拿 A 股问答)
  4. 最新提问常未回复(answer=None),回复率因公司而异
  5. 时间是毫秒时间戳
  6. 第一步慢(5.5s)、第二步快(0.9s);高频调用必须加缓存

接口范式:按 symbol(单只股票),非市场级——继承 NewsVendor。
"""
from __future__ import annotations

import json
import logging
import urllib.request
from datetime import datetime

from marketdata.symbol import Symbol
from marketdata.types import NewsArticle
from marketdata.vendors.base import NewsVendor

logger = logging.getLogger(__name__)

_STEP1_API = "https://irm.cninfo.com.cn/newircs/index/queryKeyboardInfo"
_STEP2_API = "https://irm.cninfo.com.cn/newircs/company/question"

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)
_HEADERS = {"User-Agent": _UA}


def _post_form(api: str, body: str, *, log_label: str) -> dict | None:
    """cninfo 第一步专用:POST x-www-form-urlencoded(urllib 直发,market_get 不支持 form body)。"""
    req = urllib.request.Request(
        api, data=body.encode("utf-8"), method="POST",
        headers={**_HEADERS, "Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception as e:
        logger.warning(f"{log_label}: {type(e).__name__}: {e!r}")
        return None


def _post_querystr(api: str, query_string: str, *, log_label: str) -> dict | None:
    """cninfo 第二步专用:POST 但参数放 query string(放 body 返 HTTP 400),body 必须为空。"""
    req = urllib.request.Request(
        f"{api}?{query_string}", data=b"", method="POST", headers=_HEADERS,
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception as e:
        logger.warning(f"{log_label}: {type(e).__name__}: {e!r}")
        return None


class CninfoIrmVendor(NewsVendor):
    """巨潮 cninfo 互动易问答(投资者提问 + 公司官方回复)。

    海外可达(43.128.140.167 实测 200),适合"事件驱动短线博弈"——
    公司对某传闻/利好的官方回应是 AI 问答的独家信源,别处拿不到。
    """

    name = "cninfo_irm"
    supports_markets = {"CN"}

    def fetch(self, symbols: list[Symbol], config: dict) -> list[NewsArticle]:
        """取所有 symbols 的互动易问答(每只最近 page_size 条)。"""
        page_size = int(config.get("page_size", 30))
        page_num = int(config.get("page_num", 1))
        results: list[NewsArticle] = []

        for sym in symbols:
            code = sym.code
            try:
                org_id = self._get_org_id(code)
                if not org_id:
                    continue
                items = self._get_questions(code, org_id, page_size, page_num)
            except Exception as e:
                logger.warning(f"互动易获取失败 [{code}]: {type(e).__name__}: {e!r}")
                continue

            for it in items:
                results.append(self._to_news_article(code, it))
        return results

    @staticmethod
    def _get_org_id(code: str) -> str | None:
        """第一步: keyWord=code 查 orgId。响应 data[0].secid 形如 '9900010768'。"""
        d = _post_form(_STEP1_API, f"keyWord={code}", log_label=f"互动易-orgId[{code}]")
        rows = (d or {}).get("data") or []
        if not rows:
            return None
        return str(rows[0].get("secid", "") or "")

    @staticmethod
    def _get_questions(code: str, org_id: str, page_size: int, page_num: int) -> list[dict]:
        """第二步: 拿问答列表。**参数必须放 query string**(POST body 空),否则 HTTP 400。"""
        qs = (
            f"_t=1&stockcode={code}&orgId={org_id}"
            f"&pageSize={page_size}&pageNum={page_num}"
            f"&keyWord=&startDay=&endDay="
        )
        d = _post_querystr(_STEP2_API, qs, log_label=f"互动易-问答[{code}]")
        return (d or {}).get("rows") or []

    @staticmethod
    def _to_news_article(code: str, it: dict) -> NewsArticle:
        """互动易 raw row → 标准 NewsArticle。
        内容塞 content(question 全文)+ title(question 前 60 字符);
        公司官方回复塞 url 字段(临时,因 NewsArticle 无 summary)。
        importance 启发:有回复=2,未回复=1(便于排序)。
        """
        pub_ms = it.get("pubDate")
        publish_time = (
            datetime.fromtimestamp(pub_ms / 1000) if pub_ms else datetime.now()
        )
        question = it.get("mainContent") or ""
        answer = it.get("attachedContent") or ""  # None = 未回复
        answerer = it.get("attachedAuthor") or ""

        # answer 字段 NewsArticle 没地方放,挤进 url(不影响,前端不用)
        # importance: 已回复 = 2(AI 高优先级),未回复 = 1
        importance = 2 if answer else 1

        return NewsArticle(
            source="cninfo_irm",
            external_id=str(it.get("indexId", "")),
            title=question[:60],
            content=question,
            publish_time=publish_time,
            symbols=[code],
            importance=importance,
            url=answer,  # 临时借用,见下注释
        )


# 注意:NewsArticle 没有 answer 字段,这里把公司回复挤进 url(临时方案)。
# 想要正式字段需扩展 NewsArticle 加 optional answer/answerer/company。
# 当前用法(前端的 ChatWidget 拿 NewsArticle)不显示 url 字段,所以不影响。
