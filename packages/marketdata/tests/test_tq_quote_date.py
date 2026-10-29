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
# 2026-10-29 独立复审：精度 / 复权 / 冷缓存 / 批量缺失 四类口径修正
# ---------------------------------------------------------------------------

def test_one_tick_apart_is_not_the_same_price():
    """一分钱就是另一个价格：差一档绝不许算配对（旧容差 0.011 会把它们说成同价）。

    证据是"同源同日的两个字段相等"；把 1354.01 配给收 1354.00 的 bar 等于凭空造日期。
    """
    today = _today()
    assert tqmod.prove_quote_date([_bar(today, "1354.00", "4299231")], 1354.01, today) == (None, "")
    assert not tqmod._price_matches(1354.01, 1354.00)
    assert not tqmod._price_matches(3.58, 3.57)
    # 只有浮点/尾零级别的差才算相等（同一字段的两种写法）
    assert tqmod._price_matches(1354.0, 1354.00)
    assert tqmod._price_matches(float("1354.00"), 1354.00)


def test_older_matching_day_is_not_used_when_latest_traded_close_differs():
    """停牌时只认**最后一个有成交的 bar**：更早的同价日只是巧合。

    旧实现在 rows[:-1] 里捞**任意**同价日（这里会返回 2026-10-15，把四天前的收盘价
    说成现价的日期）。最后有成交的那天收 3.62 ≠ 现价 3.57 ⇒ 必须 None。
    """
    today = _today()
    bars = [
        _bar("20261015", "3.57", "18400000"),
        _bar("20261019", "3.62", "27342900"),
        _bar(today, "3.57", "0"),
    ]
    assert tqmod.prove_quote_date(bars, 3.57, today) == (None, "")


def test_nan_rows_are_not_a_trade_or_a_match():
    """NaN（源端未填充/无值）不是"有成交"，更不是"同价"；今日无值只能落到最后成交日。"""
    today = _today()
    bars = [_bar("20261028", "1330.75", "3880493"), _bar(today, "nan", "nan")]
    assert tqmod.prove_quote_date(bars, 1330.75, today) == ("2026-10-28", tqmod.DATE_BASIS_DAILY_BAR)
    # 窗口里全是 NaN ⇒ 没有任何可证明的日期（不许把 NaN 当数值参与比较）
    assert tqmod.prove_quote_date([_bar(today, "nan", "nan")], 1330.75, today) == (None, "")
    assert tqmod.prove_quote_date([_bar(today, "1330.75", "nan")], 1330.75, today) == (None, "")


def test_daily_rpc_asks_for_unadjusted_and_unfilled_bars(monkeypatch):
    """日线必须显式**不复权 + 不填充**：前复权/填充值会与快照现价虚假配对。

    契约依据（`docs/research/tdx_local_contract/` + 客户端 `tqcenter.get_market_data`）：
      - `dividend_type` 默认 None → 'none'（**不复权**），'front'=前复权 / 'back'=后复权；
      - `fill_data=True`（默认）会把该股票缺数据的交易日**前向填充**成上一交易日的
        Close/Volume —— 批量取数时同批别的票有今天，停牌票就会多出一根"今天有量"的假 bar；
      - `count` 是"最近 N 根"，冷缓存下客户端只回最新 1 根 ⇒ 窗口不足必须如实降级。
    所以这里既要断言参数，也要断言窗口常量。
    """
    seen: list[dict] = []

    def _rpc(method, params, timeout=None, **kw):
        if method == "get_market_snapshot":
            return dict(_SNAP)
        if method == "get_market_data":
            seen.append(dict(params))
            return {}
        return {}

    monkeypatch.setattr(tqmod, "_rpc", _rpc)
    tqmod.TqQuoteVendor().fetch([Symbol.parse("600519", "CN")], {})
    assert seen, "必须真的打了日线接口"
    params = seen[0]
    assert params.get("period") == "1d"
    assert params.get("dividend_type") == "none", "日线必须不复权，前复权历史价会与现价虚假配对"
    assert params.get("fill_data") is False, "不许让源端前向填充，假 bar 会冒充今日成交"
    assert params.get("count") == tqmod.QUOTE_DATE_WINDOW
    assert tqmod.QUOTE_DATE_WINDOW >= 5, "3 根窗口连一周停牌都覆盖不了"


def test_quote_date_reasons_are_counted():
    """每条判定都要可观测：证明成功与每种降级各计数，运维才知道"日期为什么没了"。"""
    tqmod._reset_quote_date_stats()
    today = _today()
    tqmod.prove_quote_date([_bar(today, "1354.00", "4299231")], 1354.00, today)
    tqmod.prove_quote_date([_bar(today, "1354.00", "4299231")], 1349.99, today)
    tqmod.prove_quote_date([_bar("20261028", "1330.75", "3880493")], 1330.75, today)
    stats = tqmod.quote_date_stats()
    assert stats.get(tqmod.DATE_REASON_TODAY) == 1
    assert stats.get(tqmod.DATE_REASON_PRICE_MISMATCH) == 1
    assert stats.get(tqmod.DATE_REASON_NO_TODAY_BAR) == 1


def test_short_window_degrades_and_is_counted():
    """停牌但窗口里没有可成交 bar（冷缓存只给 1 根 / 停牌超过窗口）⇒ None + 计数。"""
    tqmod._reset_quote_date_stats()
    today = _today()
    # 窗口内只有今天这根无量 bar：没有"最后一个有成交日"可认。
    assert tqmod.prove_quote_date([_bar(today, "3.57", "0")], 3.57, today) == (None, "")
    assert tqmod.quote_date_stats().get(tqmod.DATE_REASON_WINDOW_EMPTY) == 1


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

    types.py 红线：缺字段一律 None，禁止用 0 冒充。
    ⚠️ 跳过≠回补：引擎是**整批**取数（按数量比较候选源），**没有**逐票补数逻辑；
    默认 min_count=1 时这份"少一只"的结果就会被当成成功返回 ⇒ 该票在本次取数里
    就是**没有行情**（下游按"无行情"显式降级）。见 test_partial_batch_keeps_only_priced_symbol
    与 test_engine.py::test_partial_batch_is_not_backfilled_per_symbol。
    """
    zero_snap = dict(_SNAP, Now="0.00", LastClose="51.80")

    def _rpc(method, params, timeout=None, **kw):
        if method == "get_market_snapshot":
            return dict(zero_snap)
        return {}

    monkeypatch.setattr(tqmod, "_rpc", _rpc)
    assert tqmod.TqQuoteVendor().fetch([Symbol.parse("002827", "CN")], {}) == []


def test_partial_batch_keeps_only_priced_symbol(monkeypatch):
    """一只 0 价 + 一只正常 ⇒ 返回**只有**正常那只，且不伪造缺失票的任何字段。

    这是"部分缺失"的确切契约：TQ 这一路不会替缺失票编报价，也不会在批量里悄悄
    换成别的票；缺失票由下游按"无行情"处理。
    """
    zero = dict(_SNAP, Now="0.00", LastClose="51.80")

    def _rpc(method, params, timeout=None, **kw):
        if method == "get_market_snapshot":
            return dict(zero if params.get("stock_code", "").startswith("002827") else _SNAP)
        if method == "get_market_data":
            return {}
        return {}

    monkeypatch.setattr(tqmod, "_rpc", _rpc)
    quotes = tqmod.TqQuoteVendor().fetch(
        [Symbol.parse("002827", "CN"), Symbol.parse("600519", "CN")], {}
    )
    assert [q.symbol for q in quotes] == ["600519"]
    assert quotes[0].current_price == 1354.00


def test_fetch_without_daily_evidence_still_emits_quote(monkeypatch):
    """日线里这只票没有任何 bar（冷缓存/该票无历史）⇒ 报价照出，日期 None 且计入降级计数。"""
    tqmod._reset_quote_date_stats()

    def _rpc(method, params, timeout=None, **kw):
        if method == "get_market_snapshot":
            return dict(_SNAP)
        if method == "get_market_data":
            return {"600519.SH": {"Date": [], "Close": [], "Volume": []}}
        return {}

    monkeypatch.setattr(tqmod, "_rpc", _rpc)
    q = tqmod.TqQuoteVendor().fetch([Symbol.parse("600519", "CN")], {})[0]
    assert q.quote_date is None and q.quote_date_basis == ""
    stats = tqmod.quote_date_stats()
    assert stats.get(tqmod.DEGRADE_MISSING_SYMBOL) == 1
    assert stats.get(tqmod.DATE_REASON_NO_BARS) == 1


def test_cold_cache_short_window_is_counted(monkeypatch):
    """冷缓存只回 1 根 ⇒ 记 `daily_short_window`（可观测），且绝不因窗口短就放宽判定。

    TQ 客户端冷缓存只返回最新 1 根（见 K 线 vendor 的实测注释）。这时停牌股的历史那半边
    根本没有 → 只能给 None；但"为什么没有日期"必须能在计数里看见（source_health 面板）。
    """
    tqmod._reset_quote_date_stats()

    def _rpc(method, params, timeout=None, **kw):
        if method == "get_market_snapshot":
            return dict(_SNAP)
        if method == "get_market_data":
            return {"600519.SH": {"Date": [_today()], "Close": ["1354.00"], "Volume": ["0"]}}
        return {}

    monkeypatch.setattr(tqmod, "_rpc", _rpc)
    q = tqmod.TqQuoteVendor().fetch([Symbol.parse("600519", "CN")], {})[0]
    assert q.quote_date is None
    stats = tqmod.quote_date_stats()
    assert stats.get(tqmod.DEGRADE_SHORT_WINDOW) == 1
    assert stats.get(tqmod.DATE_REASON_WINDOW_EMPTY) == 1
