"""Alpha Vantage 行情 vendor(美股/全球, 免费 25 req/day, 多 key 池可放大额度)。

从 config["api_key"] 读凭证; Engine 的 KeyPool 负责多 key 轮换与限流冷却。
Alpha Vantage GLOBAL_QUOTE 返回 USD 计价的美股/全球股票实时报价。
文档: https://www.alphavantage.co/documentation/#latestprice
"""
from __future__ import annotations

import logging
import json
import urllib.parse
import urllib.request

from marketdata.errors import VendorError
from marketdata.http import record_error
from marketdata.symbol import Market, Symbol
from marketdata.types import Quote
from marketdata.vendors.base import QuoteVendor

logger = logging.getLogger(__name__)

_BASE = "https://www.alphavantage.co/query"


class AlphaVantageQuoteVendor(QuoteVendor):
    name = "alphavantage"
    supports_markets = {"US", "HK", "GLOBAL"}

    def fetch(self, symbols: list[Symbol], config: dict) -> list[Quote]:
        api_key = config.get("api_key") or ""
        if not api_key:
            raise VendorError("alphavantage 需要 api_key(config['api_key'])")
        if not symbols:
            return []

        out: list[Quote] = []
        for s in symbols:
            code = self._to_av_symbol(s)
            url = f"{_BASE}?function=GLOBAL_QUOTE&symbol={urllib.parse.quote(code)}&apikey={urllib.parse.quote(api_key)}"
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "PanWatch/1.0"})
                with urllib.request.urlopen(req, timeout=20) as resp:
                    data = resp.read().decode("utf-8", "replace")
                quote = self._parse(data, s)
                if quote is not None:
                    out.append(quote)
            except VendorError:
                # 凭证/限流错误: 透传给 Engine, 触发 KeyPool 冷却+切换(不吞)
                raise
            except urllib.error.HTTPError as e:
                # 401/429 等 HTTP 错误: 读 body 判断, 凭证类透传给 Engine 切换 key
                body = e.read().decode("utf-8", "replace") if e.fp else ""
                if e.code in (401, 403, 429) or "error" in body or "Note" in body or "Information" in body:
                    raise VendorError(f"alphavantage HTTP {e.code}: {body[:120]}") from e
                logger.debug(f"alphavantage {code} HTTP {e.code}: {body[:80]}")
                record_error(f"alphavantage {code}: HTTP {e.code}")
            except Exception as e:
                logger.debug(f"alphavantage {code} 失败: {e}")
                record_error(f"alphavantage {code}: {type(e).__name__}: {e}")
        return out

    @staticmethod
    def _to_av_symbol(s: Symbol) -> str:
        # Alpha Vantage 用交易所后缀: 美股裸代码, 港股 XXXX.HK
        if s.market == Market.HK:
            return f"{int(s.code):04d}.HK" if s.code.isdigit() else f"{s.code}.HK"
        return s.code

    @staticmethod
    def _parse(raw: str, s: Symbol) -> Quote | None:
        try:
            d = json.loads(raw)
        except json.JSONDecodeError:
            return None
        # 限流/错误: {"Note":"Thank you for using Alpha Vantage! ... API call frequency"}
        if "Note" in d or "Information" in d:
            raise VendorError(d.get("Note") or d.get("Information") or "alphavantage rate limited")
        gq = d.get("Global Quote") or {}
        if not gq:
            return None
        try:
            # 2026-09-08 (风险方案1.1): 缺失字段保留 None + status 标注, 不用 0 冒充。
            def _f(v) -> float | None:
                if v is None or str(v).strip() == "":
                    return None
                try:
                    return float(v)
                except (TypeError, ValueError):
                    return None

            price = _f(gq.get("05. price"))
            prev = _f(gq.get("08. previous close"))
            pct = _f(str(gq.get("10. change percent") or "").rstrip("%"))
            open_ = _f(gq.get("02. open"))
            high = _f(gq.get("03. high"))
            low = _f(gq.get("04. low"))
            vol = _f(gq.get("06. volume"))

            missing = [n for n, v in (
                ("current_price", price), ("prev_close", prev), ("open_price", open_),
                ("high_price", high), ("low_price", low), ("volume", vol),
            ) if v is None]
            if price is None and prev is None:
                status = "missing"
            elif missing:
                status = "partial"
            else:
                status = "ok"

            return Quote(
                symbol=s.code,
                market=s.market.value,
                name=gq.get("01. symbol", s.code),
                current_price=price,
                prev_close=prev,
                open_price=open_,
                high_price=high,
                low_price=low,
                change_amount=_f(gq.get("09. change")),
                change_pct=pct,
                volume=vol,
                status=status,
                missing_fields=missing,
            )
        except (ValueError, TypeError) as e:
            logger.debug(f"alphavantage 解析失败: {e}")
            return None
