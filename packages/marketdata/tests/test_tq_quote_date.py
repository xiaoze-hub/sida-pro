"""TQ 报价日期证据（2026-10-29 F4）：快照没有报时字段，只能用**同标的日线收盘价配对**
证明"这份价属于哪一天"。

本文件把三条实测结论钉死（真实网关只读探测，2026-10-29 收盘后）：

1. `get_market_snapshot` 的 26 个键里没有任何 time/date 字段 ⇒ `quote_time` 只能是 None；
2. `get_more_info.HqDate` **不能**当报价日期：601326/002827/002717 三只停牌股今日成交量 0，
   HqDate 照样是当天 20261029（那是源端刷新日，不是成交日）；
3. 日线是当日的、带 Date/Volume/Close ⇒ 可以证明到**日**：
   - 今日 bar 有量且收盘价==现价 → 今天；
   - 今日 bar 无量（停牌/盘前）→ 现价来自更早的、同价的有成交日；
   - 对不上 / 最新 bar 不是今天 → None（不猜）。
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

pytest.importorskip("marketdata")

from marketdata.symbol import Symbol  # noqa: E402
from marketdata.vendors import tq as tqmod  # noqa: E402

CN = ZoneInfo("Asia/Shanghai")


def _today() -> str:
    return datetime.now(CN).strftime("%Y%m%d")


def _iso(day: str) -> str:
    return f"{day[:4]}-{day[4:6]}-{day[6:8]}"


def _bar(day: str, close: str, vol: str) -> dict:
    return {"Date": day, "Close": close, "Volume": vol}


# ---------------------------------------------------------------------------
# prove_quote_date —— 纯函数
# ---------------------------------------------------------------------------

def test_today_traded_bar_proves_today():
    today = _today()
    bars = [_bar("20261028", "1330.75", "3880493"), _bar(today, "1354.00", "4299231")]
    assert tqmod.prove_quote_date(bars, 1354.00, today) == (_iso(today), tqmod.DATE_BASIS_DAILY_BAR)


def test_halted_today_bar_points_at_last_traded_day():
    """停牌：今日 bar 量为 0，现价 = 最后有成交那天的收盘价 ⇒ 日期是**最后成交日**。"""
    today = _today()
    bars = [
        _bar("20261019", "3.62", "27342900"),
        _bar("20261020", "3.57", "22044772"),
        _bar(today, "3.57", "0"),
    ]
    assert tqmod.prove_quote_date(bars, 3.57, today) == ("2026-10-20", tqmod.DATE_BASIS_DAILY_BAR)


def test_halted_without_matching_history_is_unknown():
    today = _today()
    bars = [_bar("20261019", "3.62", "27342900"), _bar(today, "3.57", "0")]
    assert tqmod.prove_quote_date(bars, 3.57, today) == (None, "")


def test_newest_bar_not_today_is_unknown():
    """最新 bar 不是今天：分不清「今日 bar 还没落」与「今天没开盘」⇒ 不猜。"""
    today = _today()
    bars = [_bar("20261028", "1330.75", "3880493")]
    assert tqmod.prove_quote_date(bars, 1330.75, today) == (None, "")


def test_price_mismatch_is_unknown():
    today = _today()
    bars = [_bar("20261028", "1330.75", "3880493"), _bar(today, "1354.00", "4299231")]
    assert tqmod.prove_quote_date(bars, 1349.99, today) == (None, "")


def test_garbage_bars_do_not_raise():
    today = _today()
    bars = [{"Date": None, "Close": "x", "Volume": None}, _bar(today, "1354.00", "4299231")]
    assert tqmod.prove_quote_date(bars, 1354.00, today) == (_iso(today), tqmod.DATE_BASIS_DAILY_BAR)
    assert tqmod.prove_quote_date([], 1354.00, today) == (None, "")
    assert tqmod.prove_quote_date(bars, None, today) == (None, "")


# ---------------------------------------------------------------------------
# TqQuoteVendor.fetch —— 组装：不伪造时刻、只在证据成立时给日期
# ---------------------------------------------------------------------------

_SNAP = {
    "Now": "1354.00", "LastClose": "1330.75", "Open": "1349.99", "Max": "1356.00", "Min": "1330.01",
    "Volume": "42992", "Amount": "579798.00", "Average": "1348.61",
    "Inside": "17036", "Outside": "25956", "ErrorId": "0",
}


def _fake_rpc_factory(daily_payload=None, daily_raises=False, calls=None):
    def _rpc(method, params, timeout=None, **kw):
        if calls is not None:
            calls.append(method)
        if method == "get_market_snapshot":
            return dict(_SNAP)
        if method == "get_more_info":
            return {"ErrorId": "0"}
        if method == "get_market_data":
            if daily_raises:
                raise RuntimeError("gateway down")
            return daily_payload or {}
        return {}

    return _rpc


def test_fetch_proves_date_but_never_fakes_time(monkeypatch):
    today = _today()
    payload = {"600519.SH": {
        "Date": ["20261028", today], "Close": ["1330.75", "1354.00"],
        "Volume": ["3880493", "4299231"],
    }}
    monkeypatch.setattr(tqmod, "_rpc", _fake_rpc_factory(payload))
    q = tqmod.TqQuoteVendor().fetch([Symbol.parse("600519", "CN")], {})[0]
    assert q.quote_time is None, "TQ 快照无报时字段，服务端时间不许冒充"
    assert q.quote_time_kind == ""
    assert q.quote_date == _iso(today)
    assert q.quote_date_basis == tqmod.DATE_BASIS_DAILY_BAR


def test_fetch_daily_failure_keeps_quote_without_date(monkeypatch):
    """日线这一路挂了 ⇒ 报价照出，只是没有日期证据（不许因此丢报价）。"""
    monkeypatch.setattr(tqmod, "_rpc", _fake_rpc_factory(daily_raises=True))
    quotes = tqmod.TqQuoteVendor().fetch([Symbol.parse("600519", "CN")], {})
    assert len(quotes) == 1
    assert quotes[0].quote_date is None
    assert quotes[0].quote_date_basis == ""


def test_fetch_uses_one_batched_daily_call_for_all_symbols(monkeypatch):
    """日线必须**一次批量**取（≤5 只），不是每票一次 —— 否则 TQ 延迟翻倍。"""
    calls: list[str] = []
    monkeypatch.setattr(tqmod, "_rpc", _fake_rpc_factory({}, calls=calls))
    tqmod.TqQuoteVendor().fetch(
        [Symbol.parse("600519", "CN"), Symbol.parse("000001", "CN")], {}
    )
    assert calls.count("get_market_data") == 1
    assert calls.count("get_market_snapshot") == 2


def test_fetch_skips_daily_for_oversized_batches(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(tqmod, "_rpc", _fake_rpc_factory({}, calls=calls))
    syms = [Symbol.parse(c, "CN") for c in ("600519", "000001", "601318", "000651", "002361", "600036")]
    assert tqmod.TqQuoteVendor().fetch(syms, {}) == []
    assert calls == [], "超过批量上限必须让位给批量源，不许先打一轮 RPC"


def test_fetch_skips_zero_price_instead_of_faking_minus_100pct(monkeypatch):
    """TQ 快照 Now=0（实测停牌股 002827）不是价格：跳过，不许算成 -100% 假暴跌。

    types.py 红线：缺字段一律 None，禁止用 0 冒充。跳过该票后引擎会按优先级用下一源补齐。
    """
    zero_snap = dict(_SNAP, Now="0.00", LastClose="51.80")

    def _rpc(method, params, timeout=None, **kw):
        if method == "get_market_snapshot":
            return dict(zero_snap)
        return {}

    monkeypatch.setattr(tqmod, "_rpc", _rpc)
    assert tqmod.TqQuoteVendor().fetch([Symbol.parse("002827", "CN")], {}) == []
