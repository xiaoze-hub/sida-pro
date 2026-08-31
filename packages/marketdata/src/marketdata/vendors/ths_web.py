"""同花顺 Web 数据源 vendor(2026-08-09 香港节点实测可用,免登录)。

能力(全部实测):
- QuoteVendor   : fuyao 统一行情聚合接口(quota-h.10jqka.com.cn),实时快照 ✅
- KlineVendor   : d.10jqka.com.cn/v6/line 日K线 ✅
- FlashNewsVendor: news.10jqka.com.cn 快讯 ✅
- FundamentalsVendor: basic.10jqka.com.cn F10(财务) ✅

关键要点:
1. fuyao 接口 market 用字符串编码: 沪=17 深=32 北=144
   POST body: {"code_list":[{"codes":["600519"],"market":"17"}],"trade_class":"intraday","data_fields":[...],"lang":"zh_hans","gpid":1}
2. fuyao 字段用 ID 编码: value 数组按 data_fields 顺序
   7=最新价 8=昨收 9=开盘 10=最高 11=最低 13=成交量 19=成交额 24=涨跌幅 30=换手率 6=名称
3. 大字段(名称/涨跌幅)可能为 null,需容错
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime

from marketdata.symbol import Symbol
from marketdata.types import Bar, FlashNews, Fundamentals, Quote
from marketdata.vendors.base import FlashNewsVendor, FundamentalsVendor, KlineVendor, QuoteVendor

logger = logging.getLogger(__name__)

# fuyao 统一行情
FUYAO = "https://quota-h.10jqka.com.cn/fuyao/common_hq_aggr/quote/v1"
# K线
KLINE_URL = "https://d.10jqka.com.cn/v6/line/{symbol}/01/last.js"
# 快讯
NEWS_URL = "https://news.10jqka.com.cn/tapp/news/push/stock/?page=1&tag=&track=website&pagesize={n}"
# F10
F10_URL = "https://basic.10jqka.com.cn/{code}/finance.html"

# 市场编码: A股沪/深/北(fuyao 用字符串)
_MARKET_MAP = {"SH": "17", "SZ": "32", "BJ": "144"}

# fuyao data_fields 字段 ID(实测校准 2026-08-09):
# 7=最新价 8=昨收 13=成交量(手) 19=成交额(元) 9=开盘 10=最高 11=最低
# ⚠️ 6/24/30 语义未校准(6 疑似价格而非名称),不用它们,避免错位
_F_LAST, _F_PRE, _F_OPEN, _F_HIGH, _F_LOW = "7", "8", "9", "10", "11"
_F_VOL, _F_AMT = "13", "19"
_FIELDS = [_F_LAST, _F_PRE, _F_OPEN, _F_HIGH, _F_LOW, _F_VOL, _F_AMT]


def _market_code(symbol: Symbol) -> str | None:
    """Symbol → fuyao market 编码。CN 市场按代码前缀推断交易所。"""
    m = (symbol.market or "").upper()
    if m in _MARKET_MAP:
        return _MARKET_MAP[m]
    if m == "CN":
        code = symbol.code
        if code.startswith("6") or code.startswith("9") or code.startswith("5"):
            return "17"   # 沪
        if code.startswith(("0", "3")):
            return "33"   # 深(实测: 32 返回空,33 正常)
        if code.startswith(("8", "4", "92")):
            return "144"  # 北
    return None


class _ThsBlockedError(RuntimeError):
    """同花顺对当前出口 IP 的访问被拒绝(403)。raise 让 Engine 跳过本 vendor。

    Engine 对 vendor.fetch 抛出的 403 会识别为 rate-limit/credential 类错误,
    直接跳到下一个源;不再 per-symbol 逐个 403(每只股票一次无用请求)。
    """


def _fuyao_post(path: str, payload: dict) -> str:
    """fuyao 接口 POST(fuyao 需 POST + JSON body,market_get 只支持 GET)。"""
    import urllib.error
    import urllib.request

    req = urllib.request.Request(
        f"{FUYAO}/{path}",
        data=json.dumps(payload).encode(),
        headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/122.0",
            "Referer": "https://stockpage.10jqka.com.cn/",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=12) as r:
            return r.read().decode("utf-8", errors="ignore")
    except urllib.error.HTTPError as e:
        if e.code == 403:
            raise _ThsBlockedError(f"同花顺拒绝访问(HTTP 403): {path}") from e
        raise


def _ths_get(url: str) -> str:
    """直接 GET(不走 market_get,避免系统代理导致 403)。"""
    import urllib.error
    import urllib.request

    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/122.0",
        "Referer": "https://www.10jqka.com.cn/",
    })
    try:
        with urllib.request.urlopen(req, timeout=12) as r:
            return r.read().decode("utf-8", errors="ignore")
    except urllib.error.HTTPError as e:
        if e.code == 403:
            raise _ThsBlockedError(f"同花顺拒绝访问(HTTP 403): {url}") from e
        raise


def _f(v) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


class ThsQuoteVendor(QuoteVendor):
    """同花顺 fuyao 实时行情。"""

    name = "ths"

    def fetch(self, symbols: list[Symbol], config: dict) -> list[Quote]:
        quotes: list[Quote] = []
        for symbol in symbols:
            market = _market_code(symbol)
            if not market or not symbol.code.isdigit():
                continue
            payload = {
                "code_list": [{"codes": [symbol.code], "market": market}],
                "trade_class": "intraday",
                "data_fields": _FIELDS,
                "lang": "zh_hans",
                "gpid": 1,
            }
            try:
                body = _fuyao_post("multi_last_snapshot", payload)
                j = json.loads(body)
                qd = j.get("data", {}).get("quote_data") or []
                if not qd:
                    continue
                item = qd[0]
                rows = item.get("value") or []
                if not rows or not rows[0]:
                    continue
                # 服务端会重排 data_fields,以响应顺序为准
                resp_fields = item.get("data_fields") or _FIELDS
                vals = dict(zip(resp_fields, rows[0]))
                last = _f(vals.get(_F_LAST))
                if last is None:
                    continue  # 无最新价则跳过(不造数)
                pre = _f(vals.get(_F_PRE))
                pct = None
                if last is not None and pre:
                    pct = (last - pre) / pre * 100
                quotes.append(Quote(
                    symbol=symbol.code,
                    market=symbol.market,
                    current_price=last,
                    prev_close=pre,
                    open_price=_f(vals.get(_F_OPEN)),
                    high_price=_f(vals.get(_F_HIGH)),
                    low_price=_f(vals.get(_F_LOW)),
                    change_pct=pct,
                    volume=_f(vals.get(_F_VOL)),
                    turnover=_f(vals.get(_F_AMT)),
                ))
            except _ThsBlockedError:
                # 同花顺 403: 整个源不可用, 立即上抛让 Engine 跳过本 vendor
                # (不再 per-symbol 逐个 403 浪费 14 次无用请求)
                raise
            except Exception as e:
                logger.warning(f"[ths_quote] {symbol.code} 失败: {e}")
        return quotes


class ThsKlineVendor(KlineVendor):
    """同花顺 d.10jqka.com.cn 日K线。"""

    name = "ths"

    def fetch(self, symbols: list[Symbol], config: dict) -> list[Bar]:
        bars: list[Bar] = []
        for symbol in symbols:
            if not symbol.code.isdigit():
                continue
            prefix = "hs" if (symbol.market or "").upper() in ("SH", "SZ") else "bj"
            url = KLINE_URL.format(symbol=f"{prefix}_{symbol.code}")
            try:
                body = _ths_get(KLINE_URL.format(symbol=f"{prefix}_{symbol.code}"))
                m = re.search(r"\(\s*(\{.*\})\s*\)", body, re.S)
                if not m:
                    continue
                j = json.loads(m.group(1))
                data = j.get("data") or j.get("result") or ""
                rows = data.strip().split(";") if data else []
                for row in rows:
                    parts = row.split(",")
                    if len(parts) < 7:
                        continue
                    try:
                        bars.append(Bar(
                            date=parts[0],
                            open=float(parts[1]),
                            high=float(parts[2]),
                            low=float(parts[3]),
                            close=float(parts[4]),
                            volume=float(parts[5]),
                        ))
                    except (TypeError, ValueError):
                        continue
            except Exception as e:
                logger.warning(f"[ths_kline] {symbol.code} 失败: {e}")
        return bars


class ThsFlashNewsVendor(FlashNewsVendor):
    """同花顺快讯。"""

    name = "ths"

    def fetch(self, symbols: list[Symbol], config: dict) -> list[FlashNews]:
        n = int(config.get("limit", 20))
        try:
            body = _ths_get(NEWS_URL.format(n=n))
            j = json.loads(body)
            items = j.get("data", {}).get("list") or j.get("list") or []
            result = []
            for i, it in enumerate(items[:n]):
                pub = datetime.now()
                ct = it.get("ctime") or it.get("showtime") or ""
                if ct and str(ct).isdigit():
                    try:
                        pub = datetime.fromtimestamp(int(ct))
                    except (ValueError, OSError):
                        pass
                result.append(FlashNews(
                    source="ths",
                    external_id=str(it.get("id") or i),
                    title=it.get("title") or it.get("digest") or "",
                    content=it.get("digest") or it.get("summary") or "",
                    publish_time=pub,
                    url=it.get("url") or it.get("link") or "",
                ))
            return result
        except Exception as e:
            logger.warning(f"[ths_news] 快讯失败: {e}")
            return []


class ThsFundamentalsVendor(FundamentalsVendor):
    """同花顺 F10 基本面(财务页)。"""

    name = "ths_f10"

    def fetch(self, symbols: list[Symbol], config: dict) -> list[Fundamentals]:
        result = []
        for symbol in symbols:
            if not symbol.code.isdigit():
                continue
            try:
                body = _ths_get(F10_URL.format(code=symbol.code))
                text = body
                result.append(Fundamentals(
                    symbol=symbol.code,
                    market=symbol.market,
                    eps=_find_f10(text, ["eps", "基本每股收益"]),
                    revenue=_find_f10(text, ["revenue", "营业收入"]),
                    net_profit=_find_f10(text, ["netProfit", "归母净利润"]),
                    roe=_find_f10(text, ["roe", "净资产收益率"]),
                ))
            except Exception as e:
                logger.warning(f"[ths_f10] {symbol.code} 失败: {e}")
        return result


def _find_f10(text: str, keys: list[str]) -> float | None:
    """从 F10 HTML/JSON 找数值(宽松匹配多个键名)。"""
    for key in keys:
        for pat in (rf'"{key}"\s*:\s*"?([\-\d.]+)"?',
                    rf'{key}["：:\s]+\s*([\-\d.]+)',
                    rf'{key}[^<]*?<[^>]*>([\-\d.]+)'):
            m = re.search(pat, text)
            if m:
                try:
                    v = float(m.group(1))
                    if abs(v) < 1e12:  # 排除明显异常值
                        return v
                except ValueError:
                    continue
    return None
