"""同花顺板块/大盘资金 vendor(2026-08-09 香港节点实测可用)。

端点(均免登录,海外可达):
- https://data.10jqka.com.cn/funds/hyzjl/   行业资金(流入/流出/净额,单位亿)
- https://data.10jqka.com.cn/funds/gnzjl/   概念资金(同构表)
- https://data.10jqka.com.cn/funds/ddzz/    大单追踪(备用)

⚠️ 关键坑(2026-08-09 海外节点 43.128.140.167 实测):
  1. 页面 gbk 编码,必须 decode('gbk');Referer 必须是 data.10jqka.com.cn 否则 401
  2. 数据是 HTML 表格,板块名在 <a> 里,数值在 <td> 里;字段顺序:
     序号/名称/指数/涨跌幅/流入(亿)/流出(亿)/净额(亿)/家数/领涨股/领涨涨幅/领涨现价
  3. 大盘资金 = 全部行业净额求和(实测 50 行业,流入/流出/净额可加总)
  4. 东财 push2 在香港被 502 挡,新浪个股资金可达但无板块维度 —— 同花顺是板块/大盘资金
     的唯一免登录免费源
"""

from __future__ import annotations

import logging
import re

from marketdata.http import market_get
from marketdata.symbol import Symbol
from marketdata.types import BoardCapitalFlow, MarketCapitalFlow
from marketdata.vendors.base import Vendor

logger = logging.getLogger(__name__)

_FLOW_URL = "https://data.10jqka.com.cn/funds/{kind}/"
_FLOW_HOST = "data.10jqka.com.cn"
_FLOW_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/122.0.0.0 Safari/537.36",
    "Referer": "https://data.10jqka.com.cn/",
}

# 行字段顺序(对应同花顺表格列)
_COL_KIND_MAP = {
    "hyzjl": "industry",
    "gnzjl": "concept",
}


def _safe_float(value) -> float | None:
    if value is None or value == "" or value == "-" or value == "--":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_board_rows(html: str, board_type: str) -> list[BoardCapitalFlow]:
    """解析同花顺资金表格 HTML → BoardCapitalFlow 列表。"""
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S)
    boards: list[BoardCapitalFlow] = []
    rank = 0
    for row in rows:
        a = re.search(r'<a[^>]*>([^<]{2,12})</a>', row)
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
        if not a or len(cells) < 6:
            continue
        clean = [re.sub(r"<[^>]+>", "", c).replace("\n", "").replace("\t", "").strip() for c in cells]
        if not clean or not clean[0].isdigit():
            continue
        rank += 1
        try:
            boards.append(BoardCapitalFlow(
                board_name=clean[1] if len(clean) > 1 else a.group(1).strip(),
                board_type=board_type,
                index_value=_safe_float(clean[2]) if len(clean) > 2 else None,
                change_pct=_safe_float(clean[3].rstrip("%")) if len(clean) > 3 and clean[3].endswith("%") else (_safe_float(clean[3]) if len(clean) > 3 else None),
                inflow=_safe_float(clean[4]) if len(clean) > 4 else None,
                outflow=_safe_float(clean[5]) if len(clean) > 5 else None,
                net_inflow=_safe_float(clean[6]) if len(clean) > 6 else None,
                stock_count=int(float(clean[7])) if len(clean) > 7 and clean[7].isdigit() else None,
                leader_name=clean[8] if len(clean) > 8 else "",
                leader_change_pct=_safe_float(clean[9].rstrip("%")) if len(clean) > 9 and clean[9].endswith("%") else (_safe_float(clean[9]) if len(clean) > 9 else None),
                leader_price=_safe_float(clean[10]) if len(clean) > 10 else None,
                rank=rank,
            ))
        except (ValueError, IndexError) as e:
            logger.warning(f"[ths_flow] 行解析跳过: {clean[:6]} ({e})")
            continue
    return boards


class ThsBoardFlowVendor(Vendor):
    """同花顺板块资金(行业 hyzjl + 概念 gnzjl)。"""

    name = "ths_flow"
    supports_markets = {"CN"}

    def fetch(self, symbols: list[Symbol], config: dict) -> list[BoardCapitalFlow]:
        # symbols 为空 = 全市场板块;symbol 里可带 board_type=concept 选择概念
        kind = "gnzjl" if config.get("board_type") == "concept" else "hyzjl"
        html = market_get(
            _FLOW_URL.format(kind=kind),
            host_key=_FLOW_HOST,
            headers=_FLOW_HEADERS,
            parse="text",
            encoding="gbk",
            retries=2,
            timeout=10,
            symbol="__board__",
            log_label=f"同花顺板块资金({kind})",
        )
        if not html:
            return []
        return _parse_board_rows(html, _COL_KIND_MAP[kind])


class ThsMarketFlowVendor(Vendor):
    """大盘资金汇总(全市场行业净额求和)。"""

    name = "ths_market_flow"
    supports_markets = {"CN"}

    def fetch(self, symbols: list[Symbol], config: dict) -> list[MarketCapitalFlow]:
        html = market_get(
            _FLOW_URL.format(kind="hyzjl"),
            host_key=_FLOW_HOST,
            headers=_FLOW_HEADERS,
            parse="text",
            encoding="gbk",
            retries=2,
            timeout=10,
            symbol="__market__",
            log_label="同花顺大盘资金",
        )
        if not html:
            return []
        boards = _parse_board_rows(html, "industry")
        if not boards:
            return []
        total_in = sum(b.inflow or 0.0 for b in boards)
        total_out = sum(b.outflow or 0.0 for b in boards)
        net = sum(b.net_inflow or 0.0 for b in boards)
        return [MarketCapitalFlow(
            total_inflow=round(total_in, 1),
            total_outflow=round(total_out, 1),
            net_inflow=round(net, 1),
            board_count=len(boards),
            source="ths_hyzjl",
        )]
