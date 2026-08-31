"""同花顺热点 vendor:zx.10jqka.com.cn(海外可达,板块归因 reason + 热榜 AI 分析)。

移植自 a-stock-data(SKILL.md V3.6.0)§3.1 ths_hot_reason() + §10.2 ths_hot_list()。

⚠️ 关键坑(2026-08-09 海外节点 43.128.140.167 实测):
  1. **ths_hot_reason 盘后才更新**(15:30 后才有当日数据),周日/节假日无新数据
     → 周日调用返回最近交易日(2026-08-07)的数据。调用方应检测 trade_date < today
     提示用户"数据可能滞后"
  2. **ths_hot_list(AI 热榜)**:返回的 analyse 字段含 AI 生成的"行业原因"+"公司原因"两段,
     信息密度比 reason tag 高,适合做"题材归因 LLM 推理的权威基线"
  3. 两条接口均无鉴权,但 ths_hot_list (dq.10jqka.com.cn) 不需要任何 header
  4. ths_hot_reason URL 是 http://(不是 https),且路径里有 GBK 编码

输出 HotStock / HotBoard 数据类型复用现有 types(对齐 discovery.py)。
"""
from __future__ import annotations

import logging
from datetime import datetime

from marketdata.http import market_get
from marketdata.symbol import Symbol
from marketdata.types import HotStock
from marketdata.vendors.base import Vendor

logger = logging.getLogger(__name__)

# ths_hot_reason URL(GBK 编码路径,注意是 http 不是 https)
_REASON_URL = "http://zx.10jqka.com.cn/event/api/getharden/date/{date}/orderby/date/orderway/desc/charset/GBK/"
_HOST_REASON = "zx.10jqka.com.cn"

# ths_hot_list URL(AI 热榜,含 analyse 字段)
_LIST_URL = "https://dq.10jqka.com.cn/fuyao/hot_list_data/out/hot_list/v1/stock"
_HOST_LIST = "dq.10jqka.com.cn"

_UA_WIN = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/117.0.0.0 Safari/537.36"
_UA_MAC = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)


class ThsHotReasonVendor(Vendor):
    """同花顺当日强势股归因(reason = 编辑部人工标注的题材标签)。

    盘后更新,周日/节假日无新数据(返回最近交易日)。
    """

    name = "ths_hot_reason"
    supports_markets = {"CN"}

    def fetch(self, symbols: list[Symbol], config: dict) -> list[HotStock]:
        """取当日强势股 + reason 题材归因(symbols 忽略,市场级)。

        Args:
            symbols: 忽略
            config: {"date": "YYYY-MM-DD", "limit": 100} (date 不传=今天)

        Returns:
            list[HotStock]: 含 change_pct + turnover_pct + 题材归因(reason)
        """
        date = config.get("date") or datetime.now().strftime("%Y-%m-%d")
        limit = int(config.get("limit", 100))

        url = _REASON_URL.format(date=date)
        r = market_get(
            url,
            host_key=_HOST_REASON,
            headers={"User-Agent": _UA_WIN},
            parse="json",
            retries=2,
            timeout=15,
            log_label=f"同花顺热点[{date}]",
        )
        if not r:
            raise RuntimeError(f"同花顺热点 {date} 接口不可用;海外节点需要排查代理")
        if r.get("errocode", 0) != 0:
            raise RuntimeError(f"同花顺热点错误: {r.get('errormsg', '')}")

        rows = r.get("data") or []
        out: list[HotStock] = []
        for it in rows[:limit]:
            market_int = it.get("market", 0)
            market = "SH" if market_int in (1, 17) else ("SZ" if market_int in (33, 0) else "BJ")
            out.append(HotStock(
                symbol=str(it.get("code", "")),
                name=str(it.get("name", "")),
                market=market,
                price=it.get("close", 0),
                change_pct=it.get("zhangfu", 0),
                turnover=it.get("huanshou", 0),
                volume=it.get("chengjiaoliang", 0),
                rank=0,  # 同花顺按日期排序,无 rank 概念
                reason=str(it.get("reason", "")),  # 题材归因(核心字段)
                change_amount=it.get("zhangdie", 0),
                source="ths_hot_reason",
            ))
        return out


class ThsHotListVendor(Vendor):
    """同花顺热榜(AI 归因:analyse 字段含"行业原因"+"公司原因"两段)。

    period: hour(小时榜) / day(日榜)
    """

    name = "ths_hot_list"
    supports_markets = {"CN"}

    def fetch(self, symbols: list[Symbol], config: dict) -> list[HotStock]:
        """取同花顺热榜(symbols 忽略)。

        Args:
            symbols: 忽略
            config: {"period": "hour" / "day", "limit": 50}

        Returns:
            list[HotStock]: rank/heat/概念标签/concepts/analyse(AI 归因)
        """
        period = str(config.get("period", "hour"))
        if period not in ("hour", "day"):
            raise ValueError(f"period 须为 hour/day, 收到 {period}")
        limit = int(config.get("limit", 50))

        params = {"stock_type": "a", "type": period, "list_type": "normal"}
        r = market_get(
            _LIST_URL,
            host_key=_HOST_LIST,
            headers={"User-Agent": _UA_MAC},
            params=params,
            parse="json",
            retries=2,
            timeout=15,
            log_label=f"同花顺热榜[{period}]",
        )
        if not r:
            raise RuntimeError("同花顺热榜接口不可用")
        data = (r.get("data") or {})
        lst = data.get("stock_list") or []
        if r.get("status_code") not in (0, "0", None):
            logger.warning(f"同花顺热榜 status_code={r.get('status_code')}")

        out: list[HotStock] = []
        for it in lst[:limit]:
            market_int = it.get("market", 17)
            market = "SH" if market_int in (1, 17) else ("SZ" if market_int == 33 else "BJ")
            tag = it.get("tag") or {}
            out.append(HotStock(
                symbol=str(it.get("code", "")),
                name=str(it.get("name", "")),
                market=market,
                price=it.get("rise_and_fall", 0),
                change_pct=it.get("rise_and_fall", 0),
                turnover=None,
                volume=None,
                rank=int(it.get("order", 0) or 0),
                heat=it.get("rate"),
                rank_chg=it.get("hot_rank_chg", 0),
                concepts=tuple(tag.get("concept_tag") or []),
                reason=str(it.get("analyse", ""))[:500],
                change_amount=0,
                source="ths_hot_list",
            ))
        return out
