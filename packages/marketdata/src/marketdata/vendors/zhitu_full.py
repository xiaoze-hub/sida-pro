"""智兔数服 Engine vendors(K线/资金流/股东/财务), 走 ZhituApiClient, token 多 key 池化。

注册进 Engine 的 kline/capital_flow/shareholders/fundamentals 类型, 优先级高于东财
(东财在云服务器不稳定, 智兔作优先稳定源; 腾讯仍保留)。
"""

import logging

from marketdata.symbol import Symbol
from marketdata.types import Bar, CapitalFlow, ShareholderItem, Fundamentals
from marketdata.vendors.base import (
    KlineVendor,
    CapitalFlowVendor,
    ShareholdersVendor,
    FundamentalsVendor,
)
from marketdata.vendors.zhitu_api import (
    kline as zhitu_kline,
    capital_flow as zhitu_capital_flow,
    top10_holders as zhitu_top10,
    finance_main as zhitu_finance_main,
)

logger = logging.getLogger("marketdata.zhitu_full")


def _num(r: dict, *keys) -> float | None:
    """按序取第一个非空可转数值的 key; 全缺返回 None(不回退 0, 风险方案1.1)。"""
    for k in keys:
        v = r.get(k)
        if v is None or str(v).strip() == "":
            continue
        try:
            return float(v)
        except (TypeError, ValueError):
            continue
    return None


def _to_zhitu_code(sym: Symbol) -> str:
    """Symbol → 智兔代码格式 000001.SZ / 600519.SH / 00700.HK。"""
    if sym.market.value == "CN":
        suffix = "SH" if sym.code.startswith(("6", "9")) else "SZ"
        return f"{sym.code}.{suffix}"
    if sym.market.value == "HK":
        return f"{sym.code}.HK"
    return sym.code


class ZhituKlineVendor(KlineVendor):
    name = "zhitu"
    supports_markets = {"CN", "HK"}

    def fetch(self, symbols: list[Symbol], config: dict) -> list[Bar]:
        out: list[Bar] = []
        for sym in symbols:
            code = _to_zhitu_code(sym)
            rows = zhitu_kline(code, level="d", latest=120)
            if not rows:
                continue
            for r in rows:
                try:
                    # 2026-09-08 (风险方案1.1): OHLC 缺失的行不是一根 K 线, 直接丢弃,
                    # 绝不产出 0 价格 bar 混进均线; volume 缺失按 Bar 契约落 0(停牌日合法值,
                    # K 线维度治理统一在 1.2/B1 全量重刷处理)。
                    open_ = _num(r, "open", "o")
                    close = _num(r, "close", "c")
                    high = _num(r, "high", "h")
                    low = _num(r, "low", "l")
                    if open_ is None or close is None or high is None or low is None:
                        logger.debug("zhitu K线 %s 缺 OHLC, 丢弃行: %s", code, r)
                        continue
                    out.append(Bar(
                        date=str(r.get("date") or r.get("t") or r.get("time"))[:10],
                        open=open_,
                        high=high,
                        low=low,
                        close=close,
                        volume=_num(r, "volume", "v") or 0.0,
                    ))
                except (TypeError, ValueError):
                    continue
        return out


class ZhituCapitalFlowVendor(CapitalFlowVendor):
    name = "zhitu"
    supports_markets = {"CN"}

    def fetch(self, symbols: list[Symbol], config: dict) -> list[CapitalFlow]:
        out: list[CapitalFlow] = []
        for sym in symbols:
            code = _to_zhitu_code(sym)
            rows = zhitu_capital_flow(code, latest=1)
            if not rows:
                continue
            r = rows[0]
            try:
                # 2026-09-08 (风险方案1.1): 资金流缺失保留 None(CapitalFlow 字段皆 Optional),
                # 净流入 0 与"无数据"是两回事, 不许混。
                out.append(CapitalFlow(
                    symbol=sym.code,
                    name="",
                    main_net_inflow=_num(r, "main_net", "zljlr"),
                    super_net_inflow=_num(r, "特大"),
                    big_net_inflow=_num(r, "大"),
                    mid_net_inflow=_num(r, "中"),
                    small_net_inflow=_num(r, "小"),
                ))
            except (TypeError, ValueError):
                continue
        return out


class ZhituShareholdersVendor(ShareholdersVendor):
    name = "zhitu"
    supports_markets = {"CN"}

    def fetch(self, symbols: list[Symbol], config: dict) -> list[ShareholderItem]:
        out: list[ShareholderItem] = []
        for sym in symbols:
            code = _to_zhitu_code(sym)
            d = zhitu_top10(code)
            if not d:
                continue
            # 智兔 top10_holders 返回 {report_date, holders:[...]}
            rep_date = str(d.get("date") or d.get("report_date") or "")
            holders = d.get("holders") or d.get("data") or []
            for h in holders[:10]:
                try:
                    # 2026-09-08 (风险方案1.1): 缺失保留 None, 不伪造 0 户/0 变化。
                    shares = _num(h, "shares")
                    change = _num(h, "change")
                    ratio = _num(h, "ratio")
                    out.append(ShareholderItem(
                        report_date=rep_date,
                        symbol=sym.code,
                        holder_num=int(shares) if shares is not None else None,
                        change_num=int(change) if change is not None else None,
                        change_ratio=ratio,
                    ))
                except (TypeError, ValueError):
                    continue
        return out


class ZhituFundamentalsVendor(FundamentalsVendor):
    name = "zhitu"
    supports_markets = {"CN"}

    def fetch(self, symbols: list[Symbol], config: dict) -> list[Fundamentals]:
        out: list[Fundamentals] = []
        for sym in symbols:
            code = _to_zhitu_code(sym)
            d = zhitu_finance_main(code)
            if not d:
                continue
            try:
                # 2026-09-08 (风险方案1.1): 估值缺失保留 None, 不伪造 PE=0。
                out.append(Fundamentals(
                    symbol=sym.code,
                    market=sym.market.value,
                    name="",
                    pe_ttm=_num(d, "pe", "syl"),
                    pb=_num(d, "pb", "scl"),
                    total_market_value=_num(d, "total_mv", "zgz"),
                ))
            except (TypeError, ValueError):
                continue
        return out
