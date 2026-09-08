"""腾讯行情 vendor(HTTP,GBK)。移植自 akshare_collector._parse_tencent_line/_fetch_tencent_quotes。

2026-09-08 (风险方案1.1) 字段缺失语义:
- 解析层对缺失/空字段一律保留 None, 绝不回退 0(0 价参与涨跌幅算术会伪造 -100% 假暴跌)。
- Quote.status: ok=核心字段齐 / partial=部分缺失 / missing=无有效价格字段。
- Quote.missing_fields: 记录缺失字段名, 供下游显式标注「无数据」。

turnover 单位实测(2026-09-08, qt.gtimg.cn 真实报文, 收盘后):
  sh600519 贵州茅台: parts[35]="1309.30/17534/2302823753", 恒等式
    amt / (price × vol(手) × 100) = 2302823753 / (1309.30×17534×100) = 1.0031 ≈ 1
  sh601318 中国平安: 3930272360 / (55.70×700768×100) = 1.0069 ≈ 1
  (偏差 <0.7% 即 VWAP≠收盘价的正常范围) → parts[35] 第三段成交额单位 = 元。
  交叉印证: parts[37] = amt/10000 (230282 vs 2302823753, 比值 10000.02) = 万元口径。
  恒等式约定见 docs/_frozen/data.md。
"""

from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from marketdata.http import market_get
from marketdata.symbol import Symbol
from marketdata.types import Quote
from marketdata.vendors.base import QuoteVendor

logger = logging.getLogger(__name__)

_URL = "http://qt.gtimg.cn/q="
_HOST = "qt.gtimg.cn"
_MIN_INTERVAL_S = 0.15
_MARKET_TIMEZONES = {
    "CN": "Asia/Shanghai",
    "HK": "Asia/Hong_Kong",
    "US": "America/New_York",
}

# 核心字段的 (parts 下标, 字段名)。缺失时进 missing_fields 并降级 status。
_CORE_FIELDS = (
    (3, "current_price"),
    (4, "prev_close"),
    (5, "open_price"),
    (6, "volume"),
    (7, "volume_outer"),
    (8, "volume_inner"),
    (31, "change_amount"),
    (32, "change_pct"),
    (33, "high_price"),
    (34, "low_price"),
)


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


def _parse_quote_time(value: str | None, market: str) -> datetime | None:
    text = str(value or "").strip()
    if len(text) < 14:
        return None
    try:
        parsed = datetime.strptime(text[:14], "%Y%m%d%H%M%S")
        timezone = _MARKET_TIMEZONES.get(market)
        return parsed.replace(tzinfo=ZoneInfo(timezone)) if timezone else parsed
    except (TypeError, ValueError):
        return None


def _parse_line(line: str, market: str) -> Quote | None:
    if '=""' in line or not line.strip():
        return None
    try:
        _, value = line.split('="', 1)
        parts = value.rstrip('";').split("~")
        if len(parts) < 35:
            return None

        missing: list[str] = []
        values: dict[str, float | None] = {}
        for idx, field_name in _CORE_FIELDS:
            v = _to_float(parts[idx])
            values[field_name] = v
            if v is None:
                missing.append(field_name)

        # parts[35] = "最新价/成交量(手)/成交额(元)" 复合字段; 成交额单位实测=元(见模块 docstring)。
        turnover = None
        if "/" in str(parts[35]):
            tp = parts[35].split("/")
            if len(tp) >= 3:
                turnover = _to_float(tp[2])
                if turnover is None:
                    missing.append("turnover")

        symbol = parts[2]
        if "." in symbol and not symbol.startswith("."):
            symbol = symbol.split(".")[0]

        turnover_rate = _to_float(parts[38]) if len(parts) > 39 else None
        pe_ratio = _to_float(parts[39]) if len(parts) > 39 else None
        pb_ratio = _to_float(parts[46]) if len(parts) > 46 else None
        circulating = _to_float(parts[44]) if len(parts) > 45 else None
        total = _to_float(parts[45]) if len(parts) > 45 else None
        volume_ratio = _to_float(parts[49]) if len(parts) > 49 else None

        price_fields = ("current_price", "prev_close", "open_price", "high_price", "low_price")
        if all(values.get(f) is None for f in price_fields):
            status = "missing"
        elif missing:
            status = "partial"
        else:
            status = "ok"

        return Quote(
            symbol=symbol,
            market=market,
            name=parts[1],
            current_price=values["current_price"],
            prev_close=values["prev_close"],
            open_price=values["open_price"],
            volume=values["volume"],
            volume_outer=values["volume_outer"],   # 外盘(主动买)
            volume_inner=values["volume_inner"],   # 内盘(主动卖)
            change_amount=values["change_amount"],
            change_pct=values["change_pct"],
            high_price=values["high_price"],
            low_price=values["low_price"],
            turnover=turnover,
            turnover_rate=turnover_rate,
            volume_ratio=volume_ratio,
            pe_ratio=pe_ratio,
            pb_ratio=pb_ratio,
            circulating_market_value=circulating,
            total_market_value=total,
            quote_time=_parse_quote_time(parts[30], market),
            status=status,
            missing_fields=missing,
        )
    except (ValueError, IndexError) as e:
        logger.debug(f"解析腾讯行情失败: {e}")
        return None


def _fetch_lines(tencent_symbols: list[str]) -> list[str]:
    """按原始腾讯符号批量拉取响应,GBK 解码后按 ';' 切分为行。tencent quote / index 共用取数核。"""
    if not tencent_symbols:
        return []
    codes = ",".join(tencent_symbols)
    content = market_get(
        _URL + codes,
        host_key=_HOST,
        min_interval_s=_MIN_INTERVAL_S,
        timeout=10,
        retries=2,
        parse="content",
        log_label="腾讯报价",
    )
    if not content:
        return []
    text = content.decode("gbk", errors="ignore") if isinstance(content, (bytes, bytearray)) else str(content)
    return text.strip().split(";")


def fetch_raw(tencent_symbols: list[str]) -> list[dict]:
    """按原始腾讯符号(sh000001/hkHSI/usDJI…)取行情,不经 Symbol.parse。

    供指数等显式符号场景复用(指数代码与个股代码可能撞号,如 000001 既是平安银行又是上证指数)。
    返回 dict 列表:symbol/name/current_price/change_pct/change_amount/prev_close/volume/turnover。
    """
    out: list[dict] = []
    for line in _fetch_lines(tencent_symbols):
        q = _parse_line(line, "")
        # 无价 Quote 缺 price 无法构成报价, 由调用方按「无数据」处理; 绝不回退 0 参与下游算术。
        if q and q.current_price is not None and q.current_price > 0:
            out.append({
                "symbol": q.symbol,
                "name": q.name,
                "current_price": q.current_price,
                "change_pct": q.change_pct,
                "change_amount": q.change_amount,
                "prev_close": q.prev_close,
                "volume": q.volume,
                "turnover": q.turnover,
            })
    return out


class TencentQuoteVendor(QuoteVendor):
    name = "tencent"
    supports_markets = {"CN", "HK", "US"}

    def fetch(self, symbols: list[Symbol], config: dict) -> list[Quote]:
        if not symbols:
            return []
        market = symbols[0].market.value
        codes = [s.to_tencent() for s in symbols]
        out: list[Quote] = []
        for line in _fetch_lines(codes):
            q = _parse_line(line, market)
            # partial(价格在、个别字段缺)照常透传, status/missing_fields 随 Quote 下游标注;
            # 缺价(missing)不出现在结果里 = 调用方视角的「无数据」。
            if q and q.current_price is not None and q.current_price > 0:
                out.append(q)
        return out
