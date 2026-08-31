"""日内异动池 vendor:东财 dycalchis.eastmoney.com(海外可达)。

移植自 a-stock-data(SKILL.md V3.6.0)§8.5 em_price_anomaly() + em_stock_monitor() 函数。

⚠️ 关键坑(2026-08-09 海外节点 43.128.140.167 实测):
  1. **必须带齐 HQ_PARAMS 5 个字段**(team/product/client/version/name/user),
     缺一个就被拒:result=1001 "unknow team" 或 "unknow version"
  2. 缺 pageSize/pageNo → result=1001
  3. **result != 0 是接口拒绝**(不是"今日无异动"),绝不能静默吞——必须 raise
  4. 数据可能是昨日(周日无新异动,返回最近交易日)
  5. URL = dycalchis.eastmoney.com / price-anomaly/list(不是 push2his!)

字段映射(单字母 code,见原函数 _anomaly_market 逻辑):
  e=异常规则码(1-8/40-43),s=板块码(1=主板/4=创业板/5=科创板/6=科创板?8=北交所),
  s==6 且 e∈{4,5,6,7} 时 key = e*10(更严阈值那档),
  x=累计偏离值%,d=统计窗口天数,a=当日涨跌幅%,o=2=非当日。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from marketdata.http import market_get
from marketdata.symbol import Symbol
from marketdata.vendors.base import Vendor

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AnomalyItem:
    """交易所"严重异常波动"明细/统计。
    实测(2026-08-09 海外 43.128.140.167): 当日/前一日异动条目,
    含规则码(rule_code)+ 文字解释(rule)+ 累计偏离值(deviation%)。
    """
    symbol: str
    name: str
    market: str                            # SH/SZ/BJ
    change_pct: float | None = None        # 当日涨跌幅%
    deviation: float | None = None         # 累计偏离值%
    days: int | None = None                # 统计窗口天数
    rule_code: int = 0                     # 异动规则码(40/41/42/43 是阈值加严版)
    rule: str = ""                         # 异动规则文字说明
    is_today: bool = False                 # o=2 时为非当日
    trade_date: str = ""                   # 接口返回的统计日期(YYYYMMDD)
    timestamp: datetime = field(default_factory=datetime.now)
    extra: dict = field(default_factory=dict)  # 异动统计(price-anomaly/count)用: times/price

_BASE = "https://dycalchis.eastmoney.com/price-anomaly"
_HOST_KEY = "dycalchis.eastmoney.com"

# SKILL HQ_PARAMS(东财 H5 固定公共参数)
HQ_PARAMS = {
    "team": "h5",
    "product": "EastMoney",
    "client": "WAP",
    "version": "9001",
    "name": "WAP",
    "user": "123",
}

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)
_HEADERS = {"User-Agent": _UA, "Referer": "https://vipmoney.eastmoney.com/"}

# 异常规则码 → 文字说明
ANOMALY_RULES = {
    1:  "主板连续10个交易日内4次出现同向异常波动",
    2:  "创业板连续10个交易日内3次出现同向异常波动",
    3:  "科创板连续10个交易日内3次出现同向异常波动",
    4:  "连续十个交易日内日收盘价涨跌幅偏离值累计达到+100%",
    5:  "连续十个交易日内日收盘价涨跌幅偏离值累计达到-50%",
    6:  "连续三十个交易日内日收盘价涨跌幅偏离值累计达到+200%",
    7:  "连续三十个交易日内日收盘价涨跌幅偏离值累计达到-70%",
    8:  "北交所连续10个交易日内3次出现同向异常波动",
    40: "连续十个交易日内日收盘价涨跌幅偏离值累计达到+150%",
    41: "连续十个交易日内日收盘价涨跌幅偏离值累计达到-75%",
    42: "连续三十个交易日内日收盘价涨跌幅偏离值累计达到+250%",
    43: "连续三十个交易日内日收盘价涨跌幅偏离值累计达到-85%",
}


def _market_of(code: str, m: int | None, board: int | None) -> str:
    """m=0 深市/1 沪市/2 北交所;board (s 字段) 是板块码 1/4/5/6/8。"""
    if m == 1 or m == "1":
        return "SH"
    if m == 0 or m == "0":
        return "SZ"
    if m == 2 or m == "2":
        return "BJ"
    # fallback: 看代码前缀
    if str(code).startswith(("60", "68", "90")):
        return "SH"
    if str(code).startswith(("00", "30", "20")):
        return "SZ"
    return "BJ"


def _anomaly_get(path: str, page_size: int, page_no: int, **extra: Any) -> dict:
    """内部 helper:走 market_get,失败/result!=0 都 raise。"""
    params = {**HQ_PARAMS, "pageSize": str(page_size), "pageNo": str(page_no), **extra}
    r = market_get(
        f"{_BASE}/{path}",
        host_key=_HOST_KEY,
        headers=_HEADERS,
        params=params,
        parse="json",
        retries=2,
        timeout=20,
        log_label=f"东财异动-{path}",
    )
    if not r:
        raise RuntimeError(f"东财异动 {path} 接口不可用(网络/限流);海外节点需要走代理")
    if r.get("result") != 0:
        # 正向识别:接口用 result!=0 表达拒绝,不能当成"今日无异动"静默吞掉
        raise RuntimeError(f"东财异动接口拒绝: result={r.get('result')} msg={r.get('msg')!r}")
    return r


# 异动 vendor 借用 base.Vendor 基类(无对应 AnomalyVendor 类型), 已在模块顶部导入


class EmAnomalyVendor(Vendor):
    """东财异动池(交易所"严重异常波动"口径),海外可达。"""

    name = "eastmoney_anomaly"
    supports_markets = {"CN"}

    def fetch(self, symbols: list[Symbol], config: dict) -> list[AnomalyItem]:
        """取异动明细(price-anomaly/list)。

        Args:
            symbols: 忽略(异动池是市场级)
            config: {"page_size": 200, "page_no": 1, "limit": 100}
        """
        page_size = int(config.get("page_size", 200))
        page_no = int(config.get("page_no", 1))
        limit = int(config.get("limit", page_size))

        d = _anomaly_get("list", page_size, page_no)
        items: list[AnomalyItem] = []
        for x in d.get("data") or []:
            e = x.get("e")
            s = x.get("s")
            # s==6 且 e∈{4,5,6,7} 时按 e*10 取更严阈值那档
            key = e * 10 if (s == 6 and e in (4, 5, 6, 7)) else e
            items.append(AnomalyItem(
                symbol=str(x.get("c", "")),
                name=str(x.get("n", "")),
                market=_market_of(str(x.get("c", "")), x.get("m"), s),
                change_pct=x.get("a"),
                deviation=x.get("x"),
                days=x.get("d"),
                rule_code=key,
                rule=ANOMALY_RULES.get(key, f"未知规则码 {key}"),
                is_today=x.get("o") != 2,
                trade_date=str(d.get("date", "")),
                timestamp=datetime.now(),
            ))
        return items[:limit]


class EmAnomalyCountVendor(Vendor):
    """东财异动统计(price-anomaly/count):按标的聚合的异动次数 + 现价。"""

    name = "eastmoney_anomaly_count"
    supports_markets = {"CN"}

    def fetch(self, symbols: list[Symbol], config: dict) -> list[AnomalyItem]:
        page_size = int(config.get("page_size", 50))
        page_no = int(config.get("page_no", 1))
        sort_key = str(config.get("sort_key", ""))
        sort_dir = str(config.get("sort_dir", ""))
        limit = int(config.get("limit", page_size))

        extra = {}
        if sort_key:
            extra["sortKey"] = sort_key
        if sort_dir:
            extra["sortDir"] = sort_dir
        d = _anomaly_get("count", page_size, page_no, **extra)
        items: list[AnomalyItem] = []
        for x in d.get("data") or []:
            items.append(AnomalyItem(
                symbol=str(x.get("c", "")),
                name=str(x.get("n", "")),
                market=_market_of(str(x.get("c", "")), x.get("m"), x.get("s")),
                change_pct=x.get("a"),
                deviation=x.get("x"),
                days=x.get("d"),
                rule_code=0,
                rule="异动统计",
                is_today=x.get("o") != 2,
                extra={"times": x.get("t"), "price": x.get("p")},
                trade_date=str(d.get("date", "")),
                timestamp=datetime.now(),
            ))
        return items[:limit]
