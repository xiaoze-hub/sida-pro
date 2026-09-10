"""新浪(Sina)US/HK 行情 vendor(HTTP,GBK)。免 key 免代理,作腾讯之后的 US/HK 备源。

端点:
- US: GET https://hq.sinajs.cn/list=gb_{code.lower()},多个逗号拼接。
- HK: GET https://hq.sinajs.cn/list=rt_hk{code},多个逗号拼接。
- 指数(2026-09-10 C4 补链): CN 指数全格式(sh000001)/港指 hkHSI/美股 gb_$ixic,
  供 MarketData.index_quotes 在腾讯缺项时兜底,见 fetch_index_quotes()。
Header 必带 Referer + UA,响应 GBK 编码,多行 `var hq_str_XXX_yyy="...";`。
"""

from __future__ import annotations

import logging
import re

from marketdata.http import market_get
from marketdata.symbol import Market, Symbol
from marketdata.types import Quote
from marketdata.vendors.base import QuoteVendor

logger = logging.getLogger(__name__)

_URL = "https://hq.sinajs.cn/list="
_HOST = "hq.sinajs.cn"
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
_HEADERS = {"Referer": "https://finance.sina.com.cn/", "User-Agent": _UA}

_LINE_RE = re.compile(r'hq_str_(gb_|rt_hk)(\S+?)="(.*?)"')


def _to_float(value: str | None) -> float | None:
    if value is None:
        return None
    v = str(value).strip()
    if not v:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _parse_us_line(code: str, content: str) -> Quote | None:
    parts = content.split(",")
    if len(parts) < 30:
        return None
    price = _to_float(parts[1])
    if price is None or price <= 0:
        return None
    return Quote(
        symbol=code.upper(),
        market="US",
        name=parts[0],
        current_price=price,
        prev_close=_to_float(parts[26]),
        open_price=_to_float(parts[5]),
        high_price=_to_float(parts[6]),
        low_price=_to_float(parts[7]),
        volume=_to_float(parts[10]),
        change_pct=_to_float(parts[2]),
        pe_ratio=_to_float(parts[14]),
    )


def _parse_hk_line(code: str, content: str) -> Quote | None:
    parts = content.split(",")
    if len(parts) < 15:
        return None
    price = _to_float(parts[6])
    if price is None or price <= 0:
        return None
    return Quote(
        symbol=code,
        market="HK",
        name=parts[1],
        current_price=price,
        open_price=_to_float(parts[2]),
        prev_close=_to_float(parts[3]),
        high_price=_to_float(parts[4]),
        low_price=_to_float(parts[5]),
        change_amount=_to_float(parts[7]),
        change_pct=_to_float(parts[8]),
        turnover=_to_float(parts[11]),
        volume=_to_float(parts[12]),
    )


_INDEX_CN_RE = re.compile(r"^(sh|sz)(\d{6})$")
_INDEX_HK_RE = re.compile(r"^hk([A-Za-z0-9]+)$")
_INDEX_US_RE = re.compile(r"^us([A-Za-z]+)$")
_INDEX_LINE_RE = re.compile(r'hq_str_([^=]+)="([^"]*)"')


def index_output_code(tencent_symbol: str) -> str | None:
    """腾讯风格指数符号 → 行情输出裸码(与腾讯 parts[2] 口径一致)。

    sh000001→000001 / hkHSI→HSI / usIXIC→.IXIC(点前缀)。非指数符号返回 None。
    """
    s = str(tencent_symbol or "").strip()
    m = _INDEX_CN_RE.match(s)
    if m:
        return m.group(2)
    m = _INDEX_HK_RE.match(s)
    if m:
        return m.group(1)
    m = _INDEX_US_RE.match(s)
    if m:
        return f".{m.group(1).upper()}"
    return None


def _parse_cn_index(content: str) -> dict | None:
    """CN 指数全格式: [0]名 [1]今开 [2]昨收 [3]现价 [4]高 [5]低 [8]量(股) [9]额(元)。"""
    parts = content.split(",")
    if len(parts) < 6:
        return None
    name = parts[0].strip()
    prev = _to_float(parts[2])
    price = _to_float(parts[3])
    if not name or price is None or price <= 0:
        return None
    change_amount = change_pct = None
    if prev is not None and prev > 0:
        change_amount = round(price - prev, 4)
        change_pct = round((price - prev) / prev * 100, 4)
    return {"name": name, "current_price": price, "prev_close": prev,
            "change_amount": change_amount, "change_pct": change_pct}


def _parse_hk_index(content: str) -> dict | None:
    """港指格式(hkHSI): [0]代码 [1]名 [2]今开 [3]昨收 [4]高 [5]低 [6]现价 [7]涨跌额 [8]涨跌幅%。"""
    parts = content.split(",")
    if len(parts) < 9:
        return None
    name = parts[1].strip() or parts[0].strip()
    price = _to_float(parts[6])
    if not name or price is None or price <= 0:
        return None
    return {"name": name, "current_price": price, "prev_close": _to_float(parts[3]),
            "change_amount": _to_float(parts[7]), "change_pct": _to_float(parts[8])}


def _parse_gb_index(content: str) -> dict | None:
    """美股指数 gb_$ 格式: [0]名 [1]现价 [2]涨跌幅% [4]涨跌额 [26]昨收(与个股 _parse_us_line 同布局)。"""
    parts = content.split(",")
    if len(parts) < 27:
        return None
    name = parts[0].strip()
    price = _to_float(parts[1])
    if not name or price is None or price <= 0:
        return None
    return {"name": name, "current_price": price, "prev_close": _to_float(parts[26]),
            "change_amount": _to_float(parts[4]), "change_pct": _to_float(parts[2])}


def fetch_index_quotes(tencent_symbols: list[str]) -> list[dict]:
    """指数实时行情兜底(新浪), 腾讯风格符号进/出(2026-09-10 C4 单源审计补链)。

    仅由 MarketData.index_quotes 在腾讯缺项时调用; 输出 dict 与 tencent.fetch_raw 同构
    (symbol 为裸码/点前缀, 与腾讯 parts[2] 口径一致), 下游消费无感。
    volume/turnover 不填(None): 新浪单位(股/元)与腾讯约定(手)未对齐, 缺失优于错误。
    """
    wanted: list[tuple[str, str]] = []  # (新浪请求名, 输出码)
    for sym in tencent_symbols or []:
        s = str(sym or "").strip()
        m = _INDEX_CN_RE.match(s)
        if m:
            wanted.append((s, m.group(2)))
            continue
        m = _INDEX_HK_RE.match(s)
        if m:
            wanted.append((s, m.group(1)))
            continue
        m = _INDEX_US_RE.match(s)
        if m:
            wanted.append((f"gb_${m.group(1).lower()}", f".{m.group(1).upper()}"))
    if not wanted:
        return []

    text = market_get(
        _URL + ",".join(name for name, _ in wanted),
        host_key=_HOST,
        headers=_HEADERS,
        parse="text",
        encoding="gbk",
        retries=1,
        timeout=8,
        min_interval_s=0.0,
        log_label="新浪指数行情",
    )
    if not text:
        return []

    by_name = {name: code for name, code in wanted}
    out: list[dict] = []
    for line in text.strip().splitlines():
        m = _INDEX_LINE_RE.search(line)
        if not m:
            continue
        sname, content = m.group(1).strip(), m.group(2)
        code = by_name.get(sname)
        if not code or not content.strip():
            continue  # 未请求的行/空响应(如无该指数)= 无数据
        try:
            if sname.startswith("gb_$"):
                rec = _parse_gb_index(content)
            elif sname.startswith("hk"):
                rec = _parse_hk_index(content)
            else:
                rec = _parse_cn_index(content)
        except (ValueError, IndexError) as e:
            logger.debug(f"解析新浪指数行情失败 {sname}: {e}")
            continue
        if not rec:
            continue
        rec.update({"symbol": code, "volume": None, "turnover": None})
        out.append(rec)
    return out


class SinaQuoteVendor(QuoteVendor):
    name = "sina"
    supports_markets = {"US", "HK"}

    def fetch(self, symbols: list[Symbol], config: dict) -> list[Quote]:
        if not symbols:
            return []
        market = symbols[0].market
        if market == Market.US:
            codes = [s.code for s in symbols]
            list_param = ",".join(f"gb_{c.lower()}" for c in codes)
        elif market == Market.HK:
            codes = [s.code for s in symbols]
            list_param = ",".join(f"rt_hk{c}" for c in codes)
        else:
            return []

        text = market_get(
            _URL + list_param,
            host_key=_HOST,
            headers=_HEADERS,
            parse="text",
            encoding="gbk",
            retries=2,
            timeout=8,
            min_interval_s=0.0,
            log_label="新浪报价",
        )
        if not text:
            return []

        out: list[Quote] = []
        for line in text.strip().splitlines():
            m = _LINE_RE.search(line)
            if not m:
                continue
            prefix, code, content = m.group(1), m.group(2), m.group(3)
            try:
                if prefix == "gb_":
                    q = _parse_us_line(code, content)
                else:
                    q = _parse_hk_line(code, content)
            except (ValueError, IndexError) as e:
                logger.debug(f"解析新浪行情失败: {e}")
                continue
            if q:
                out.append(q)
        return out
