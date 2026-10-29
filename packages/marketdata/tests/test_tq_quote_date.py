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


@pytest.fixture(autouse=True)
def _fresh_tq_date_state():
    """进程级计数/熔断是全局状态：每个用例前后都清，否则前一个用例的熔断会串到下一个。"""
    tqmod._reset_quote_date_stats()
    tqmod._reset_daily_circuit()
    yield
    tqmod._reset_daily_circuit()


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
    assert tqmod.prove_quote_date(bars, 1354.00, today, snapshot_volume="42992") == (
        _iso(today), tqmod.DATE_BASIS_DAILY_BAR
    )


def test_halted_today_bar_points_at_last_traded_day():
    """停牌：今日 bar 量为 0，现价 = 最后有成交那天的收盘价 ⇒ 日期是**最后成交日**。"""
    today = _today()
    bars = [
        _bar("20261019", "3.62", "27342900"),
        _bar("20261020", "3.57", "22044772"),
        _bar(today, "3.57", "0"),
    ]
    # 快照当日无量（手）与今日 bar 无量一致 ⇒ 保留严格的历史日期，且证据来源仍是日线口径。
    assert tqmod.prove_quote_date(bars, 3.57, today, snapshot_volume="0") == (
        "2026-10-20", tqmod.DATE_BASIS_DAILY_BAR
    )


def test_halted_without_matching_history_is_unknown():
    today = _today()
    bars = [_bar("20261019", "3.62", "27342900"), _bar(today, "3.57", "0")]
    assert tqmod.prove_quote_date(bars, 3.57, today, snapshot_volume="0") == (None, "")


def test_newest_bar_not_today_is_unknown():
    """最新 bar 不是今天：分不清「今日 bar 还没落」与「今天没开盘」⇒ 不猜。"""
    today = _today()
    bars = [_bar("20261028", "1330.75", "3880493")]
    assert tqmod.prove_quote_date(bars, 1330.75, today, snapshot_volume="0") == (None, "")


def test_price_mismatch_is_unknown():
    today = _today()
    bars = [_bar("20261028", "1330.75", "3880493"), _bar(today, "1354.00", "4299231")]
    assert tqmod.prove_quote_date(bars, 1349.99, today, snapshot_volume="42992") == (None, "")


def test_garbage_bars_do_not_raise():
    today = _today()
    bars = [{"Date": None, "Close": "x", "Volume": None}, _bar(today, "1354.00", "4299231")]
    assert tqmod.prove_quote_date(bars, 1354.00, today, snapshot_volume="42992") == (
        _iso(today), tqmod.DATE_BASIS_DAILY_BAR
    )
    assert tqmod.prove_quote_date([], 1354.00, today, snapshot_volume="42992") == (None, "")
    assert tqmod.prove_quote_date(bars, None, today, snapshot_volume="42992") == (None, "")


# ---------------------------------------------------------------------------
# 2026-10-29 独立复审：精度 / 复权 / 冷缓存 / 批量缺失 四类口径修正
# ---------------------------------------------------------------------------

def test_one_tick_apart_is_not_the_same_price():
    """一分钱就是另一个价格：差一档绝不许算配对（旧容差 0.011 会把它们说成同价）。

    证据是"同源同日的两个字段相等"；把 1354.01 配给收 1354.00 的 bar 等于凭空造日期。
    """
    today = _today()
    assert tqmod.prove_quote_date(
        [_bar(today, "1354.00", "4299231")], 1354.01, today, snapshot_volume="42992"
    ) == (None, "")
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
    assert tqmod.prove_quote_date(bars, 3.57, today, snapshot_volume="0") == (None, "")


def test_nan_rows_are_not_a_trade_or_a_match():
    """NaN（源端未填充/无值）不是"有成交"，更不是"同价"；今日无值只能落到最后成交日。"""
    today = _today()
    bars = [_bar("20261028", "1330.75", "3880493"), _bar(today, "nan", "nan")]
    assert tqmod.prove_quote_date(bars, 1330.75, today, snapshot_volume="0") == (
        "2026-10-28", tqmod.DATE_BASIS_DAILY_BAR
    )
    # 窗口里全是 NaN ⇒ 没有任何可证明的日期（不许把 NaN 当数值参与比较）
    assert tqmod.prove_quote_date(
        [_bar(today, "nan", "nan")], 1330.75, today, snapshot_volume="0"
    ) == (None, "")
    assert tqmod.prove_quote_date(
        [_bar(today, "1330.75", "nan")], 1330.75, today, snapshot_volume="0"
    ) == (None, "")


def test_daily_rpc_asks_for_unadjusted_and_unfilled_bars(monkeypatch):
    """日线必须显式**不复权 + 不填充**：前复权/填充值会与快照现价虚假配对。

    契约依据（`docs/research/tdx_local_contract/` + 客户端 `tqcenter.get_market_data`）：
      - `dividend_type` 默认 None → 'none'（**不复权**），'front'=前复权 / 'back'=后复权；
      - `fill_data` 默认 True（"补齐缺失交易日"语义；仓库契约只登记默认值、**实现未实测**），
        批量取数时同批别的票有今天，停牌票就可能多出一根"今天有量"的假 bar；
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
    tqmod.prove_quote_date([_bar(today, "1354.00", "4299231")], 1354.00, today, snapshot_volume="42992")
    tqmod.prove_quote_date([_bar(today, "1354.00", "4299231")], 1349.99, today, snapshot_volume="42992")
    tqmod.prove_quote_date(
        [_bar("20261028", "1330.75", "3880493")], 1330.75, today, snapshot_volume="0"
    )
    stats = tqmod.quote_date_stats()
    assert stats.get(tqmod.DATE_REASON_TODAY) == 1
    assert stats.get(tqmod.DATE_REASON_PRICE_MISMATCH) == 1
    assert stats.get(tqmod.DATE_REASON_NO_TODAY_BAR) == 1


def test_short_window_degrades_and_is_counted():
    """停牌但窗口里没有可成交 bar（冷缓存只给 1 根 / 停牌超过窗口）⇒ None + 计数。"""
    tqmod._reset_quote_date_stats()
    today = _today()
    # 窗口内只有今天这根无量 bar：没有"最后一个有成交日"可认。
    assert tqmod.prove_quote_date(
        [_bar(today, "3.57", "0")], 3.57, today, snapshot_volume="0"
    ) == (None, "")
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
    tqmod._reset_daily_circuit()

    def _rpc(method, params, timeout=None, **kw):
        if method == "get_market_snapshot":
            # 停牌日快照无量（手）：与今日 bar 的无量一致，才轮得到"窗口里没有成交 bar"。
            return dict(_SNAP, Volume="0")
        if method == "get_market_data":
            return {"600519.SH": {"Date": [_today()], "Close": ["1354.00"], "Volume": ["0"]}}
        return {}

    monkeypatch.setattr(tqmod, "_rpc", _rpc)
    q = tqmod.TqQuoteVendor().fetch([Symbol.parse("600519", "CN")], {})[0]
    assert q.quote_date is None
    stats = tqmod.quote_date_stats()
    assert stats.get(tqmod.DEGRADE_SHORT_WINDOW) == 1
    assert stats.get(tqmod.DATE_REASON_WINDOW_EMPTY) == 1


# ---------------------------------------------------------------------------
# 2026-10-29 独立复审（第二处漏洞）：成交量交叉校验
#
# 两个具体失效场景，单靠"收盘价逐分相等"挡不住：
#   ① `fill_data=False` 只是**请求参数**（网关是否真按它执行没有实测，不能排除被静默
#      忽略）：停牌票若被补齐出一根"今天有量"的假 bar（Volume/Close 抄上一交易日），
#      而快照成交量是 0；
#   ② 快照今日有量（真的在成交），但今日日线还没落盘 / 是 NaN —— 窗口里"最后一个有成交
#      且同价"的 bar 是**昨天**，只看收盘价就会把昨天的收盘价说成现价的日期。
# 因此把**同一标的、同一市场**（TQ 只有 CN，故同市场由 vendor 保证）的快照成交量当交叉
# 校验：单位不同（快照=手，日线=股）**只比零/正**，不比数值；两边对"今天有没有成交"说法
# 不一致 ⇒ 证据冲突 ⇒ 未知。没有可靠有效的成交量 ⇒ 不推断日期。
# ---------------------------------------------------------------------------

def test_filled_today_bar_with_volume_conflicts_with_zero_snapshot_volume():
    """① 停牌票被前向填充出"今日有量"假 bar（Close/Volume 都抄上一日）⇒ 冲突，不是今天。

    即使 `fill_data=False` 被网关静默忽略（未实测，无法排除），这根假 bar 的收盘价也
    恰好等于停牌前收盘价、等于快照 Now（停牌股快照保持最后成交价）—— 只比价格会 100%
    判成"今天"。快照成交量 0 是唯一能戳破它的证据。
    """
    today = _today()
    tqmod._reset_quote_date_stats()
    bars = [
        _bar("20261028", "3.57", "22044772"),
        _bar(today, "3.57", "22044772"),  # 假"今日有量"（实际是填充值）
    ]
    assert tqmod.prove_quote_date(bars, 3.57, today, snapshot_volume="0") == (None, "")
    assert tqmod.quote_date_stats().get(tqmod.DATE_REASON_VOLUME_CONFLICT) == 1
    # 正例：同一组 bar 在真成交日（快照有量）才成立 —— 冲突不是"一律否决"。
    assert tqmod.prove_quote_date(bars, 3.57, today, snapshot_volume="220447") == (
        _iso(today), tqmod.DATE_BASIS_DAILY_BAR
    )


def test_snapshot_traded_but_no_today_bar_is_conflict_not_yesterday():
    """② 快照今日有量、日线里没有今天这根（未落盘）⇒ 冲突；不许退到昨天的同价 bar。"""
    today = _today()
    tqmod._reset_quote_date_stats()
    bars = [_bar("20261028", "1354.00", "3880493")]  # 只有昨天，且同价
    assert tqmod.prove_quote_date(bars, 1354.00, today, snapshot_volume="42992") == (None, "")
    assert tqmod.quote_date_stats().get(tqmod.DATE_REASON_VOLUME_CONFLICT) == 1
    # 对照：快照同样无量时才走既有的 `no_today_bar`（如实降级，仍为 None）。
    tqmod._reset_quote_date_stats()
    assert tqmod.prove_quote_date(bars, 1354.00, today, snapshot_volume="0") == (None, "")
    assert tqmod.quote_date_stats().get(tqmod.DATE_REASON_NO_TODAY_BAR) == 1
    assert tqmod.quote_date_stats().get(tqmod.DATE_REASON_VOLUME_CONFLICT) is None


@pytest.mark.parametrize("today_vol", ["0", "nan", ""])
def test_today_bar_without_volume_but_snapshot_traded_is_conflict(today_vol):
    """今日 bar 无量/NaN，但快照有量 ⇒ bar 没跟上（未落盘/填充异常），不许落昨日。

    这是"历史同价可误判昨日"的确切反例：昨天收 3.57，今天也 3.57，回退逻辑会把现价
    说成昨天的。真实情况是今天在成交（快照有量），只是日线没更新 ⇒ 未知。
    """
    today = _today()
    tqmod._reset_quote_date_stats()
    bars = [_bar("20261028", "3.57", "22044772"), _bar(today, "3.57", today_vol)]
    assert tqmod.prove_quote_date(bars, 3.57, today, snapshot_volume="3500") == (None, "")
    assert tqmod.quote_date_stats().get(tqmod.DATE_REASON_VOLUME_CONFLICT) == 1


@pytest.mark.parametrize("snap_vol", [None, "nan", "inf", "-1", ""])
def test_unreliable_snapshot_volume_never_proves_a_date(snap_vol):
    """快照成交量缺失/NaN/Inf/负 ⇒ 没有可靠有效的成交量，**不推断日期**（连"今天"也不行）。

    价格相等 + bar 有量只是一半证据；没有独立的那一半（快照成交量）就无法交叉校验，
    宁可 unknown，也不把"看起来对"的日期发出去。
    """
    today = _today()
    tqmod._reset_quote_date_stats()
    bars = [_bar("20261028", "1330.75", "3880493"), _bar(today, "1354.00", "4299231")]
    assert tqmod.prove_quote_date(bars, 1354.00, today, snapshot_volume=snap_vol) == (None, "")
    assert tqmod.quote_date_stats().get(tqmod.DEGRADE_VOLUME_UNRELIABLE) == 1
    # 默认（不传）= 没有成交量证据 ⇒ 同样不推断。
    assert tqmod.prove_quote_date(bars, 1354.00, today) == (None, "")


def test_volume_units_are_only_compared_as_zero_or_positive():
    """单位不同（快照=手、日线=股）⇒ 只比"零/正"，绝不比数值。

    快照 1 手 = 100 股；日线 Volume=100（股）。若按数值比较会判成"差两个数量级"冲突，
    等于用单位差否决正确证据。零/正口径下两边都"有量"⇒ 证据一致。
    """
    today = _today()
    bars = [_bar(today, "1354.00", "100")]
    assert tqmod.prove_quote_date(bars, 1354.00, today, snapshot_volume="1") == (
        _iso(today), tqmod.DATE_BASIS_DAILY_BAR
    )


def test_zero_price_never_proves_a_date():
    """0 不是价格（types.py 红线）：纯函数层也不许拿 0 去和历史收盘价配对。"""
    today = _today()
    tqmod._reset_quote_date_stats()
    bars = [_bar(today, "0", "0")]
    assert tqmod.prove_quote_date(bars, 0.0, today, snapshot_volume="0") == (None, "")
    assert tqmod.quote_date_stats().get(tqmod.DATE_REASON_PRICE_INVALID) == 1


def test_daily_volume_from_another_symbol_is_not_used(monkeypatch):
    """混标的响应：请求 600519、日线只回了 000001 的数组 ⇒ 不跨标的取日期。"""
    tqmod._reset_quote_date_stats()

    def _rpc(method, params, timeout=None, **kw):
        if method == "get_market_snapshot":
            return dict(_SNAP)
        if method == "get_market_data":
            return {"000001.SZ": {"Date": [_today()], "Close": ["1354.00"], "Volume": ["4299231"]}}
        return {}

    monkeypatch.setattr(tqmod, "_rpc", _rpc)
    q = tqmod.TqQuoteVendor().fetch([Symbol.parse("600519", "CN")], {})[0]
    assert q.quote_date is None and q.quote_date_basis == ""
    assert tqmod.quote_date_stats().get(tqmod.DEGRADE_MISSING_SYMBOL) == 1


def test_daily_transport_failure_never_skips_snapshot(monkeypatch):
    """日线传输层失败 ≠ 网关这轮不通：快照是**主数据**，必须先取、且不许被日线失败带走。

    旧实现先打日线、日线 `httpx.TransportError` 就 `return []`，把「一次日线超时」当成
    「网关全链不可用」⇒ 引擎切腾讯，并把 TQ 记成 empty 失败。实测 1.2s 的日线小超时在
    隧道抖动/客户端冷缓存下会单次触发，而同一 host:port 的快照仍然可用。
    现在顺序反过来：快照先取（主数据），日线只为**旁证日期**；日线失败只丢日期。
    """
    import httpx

    seen: list[dict] = []

    def _rpc(method, params, timeout=None, **kw):
        seen.append({"method": method, "timeout": timeout})
        if method == "get_market_data":
            raise httpx.ConnectTimeout("tunnel down")
        if method == "get_market_snapshot":
            return dict(_SNAP)
        return {}

    monkeypatch.setattr(tqmod, "_rpc", _rpc)
    quotes = tqmod.TqQuoteVendor().fetch([Symbol.parse("600519", "CN")], {})
    assert len(quotes) == 1, "日线失败不许把同一源的主行情一起丢掉"
    assert quotes[0].current_price == 1354.00
    assert quotes[0].quote_date is None and quotes[0].quote_date_basis == ""
    assert "get_market_snapshot" in [c["method"] for c in seen]
    daily = [c for c in seen if c["method"] == "get_market_data"]
    assert daily and daily[0]["timeout"] == tqmod.QUOTE_DATE_RPC_TIMEOUT_S
    assert tqmod.QUOTE_DATE_RPC_TIMEOUT_S < tqmod._TIMEOUT_S
    assert tqmod.quote_date_stats().get(tqmod.DEGRADE_RPC_FAILED) == 1
    assert tqmod._daily_circuit_open() is True


def test_daily_app_error_keeps_quote_and_never_trips_global_circuit(monkeypatch):
    """日线**应用层**报错（如 ErrorId/协议错误）不许开全局熔断：代价只有一次廉价 RPC。

    全局熔断的唯一理由是"每次都要白等一个超时"（传输层失败）。ErrorId 可能只是批量里
    某个标的的问题，甚至下一轮就消失；拿它停掉 30s 的日期证据会扩大到所有标的/所有用户
    （用户报告的「任何某标的 ErrorId 都触发全局熔断，然后 TQ 回来又没日期」的抖动）。
    """
    seen: list[dict] = []

    def _rpc(method, params, timeout=None, **kw):
        seen.append({"method": method, "timeout": timeout})
        if method == "get_market_data":
            raise RuntimeError("TQ get_market_data ErrorId=5: period error")
        if method == "get_market_snapshot":
            return dict(_SNAP)
        return {}

    tqmod._reset_quote_date_stats()
    tqmod._reset_daily_circuit()
    monkeypatch.setattr(tqmod, "_rpc", _rpc)
    quotes = tqmod.TqQuoteVendor().fetch([Symbol.parse("600519", "CN")], {})
    assert len(quotes) == 1 and quotes[0].quote_date is None
    assert quotes[0].quote_date_basis == ""
    assert "get_market_snapshot" in [c["method"] for c in seen]
    assert tqmod.quote_date_stats().get(tqmod.DEGRADE_RPC_FAILED) == 1
    assert tqmod._daily_circuit_open() is False, "应用层报错不许开全局熔断"
    assert tqmod.quote_date_stats().get(tqmod.DATE_REASON_NO_BARS) is None

    # 下一轮仍会重新尝试日线（没有 30s 空白窗）：报价照出，日期照旧 unknown。
    seen.clear()
    quotes = tqmod.TqQuoteVendor().fetch([Symbol.parse("600519", "CN")], {})
    assert len(quotes) == 1 and quotes[0].current_price == 1354.00
    assert "get_market_data" in [c["method"] for c in seen]


def test_daily_circuit_window_expires():
    """熔断窗口是 QUOTE_DATE_CIRCUIT_OPEN_S 秒，不是永久（网关恢复后立刻重新取证）。"""
    tqmod._trip_daily_circuit(now=100.0)
    assert tqmod._daily_circuit_open(now=100.0) is True
    assert tqmod._daily_circuit_open(now=100.0 + tqmod.QUOTE_DATE_CIRCUIT_OPEN_S - 0.01) is True
    assert tqmod._daily_circuit_open(now=100.0 + tqmod.QUOTE_DATE_CIRCUIT_OPEN_S) is False
    tqmod._reset_daily_circuit()
    assert tqmod._daily_circuit_open(now=100.0) is False


# ---------------------------------------------------------------------------
# 2026-10-29 独立复审（第三处）：日线只是**旁证**，不许让它把主行情带走
#
# 复现（用户报告）：单次日线 ReadTimeout(1.2s) ⇒ `_daily_bars` 返回 None ⇒ fetch
# 直接返回空 ⇒ 引擎切腾讯，并把 TQ 记成 empty 失败；30s 模块级熔断到期后 TQ 又
# 回来了（只是 date unknown）⇒ 源在腾讯/TQ 之间反复抖动。
#
# 新口径：
#   ① 快照是主数据，**先取**；日线只为日期旁证，失败/超时/熔断只丢日期（明确 unknown）；
#   ② 只有快照这一路**整批**都拿不到（传输层失败 / 全部应用错误）才交引擎切源；
#   ③ 单标的 ErrorId 只丢该标的日期，不许开全局熔断，也不许扩大成"全部行情不可用"；
#   ④ 熔断只由**请求级**失败（传输层 / 无部分数据的 ErrorId）触发，有界退避；
#   ⑤ 观测区分「当前熔断状态」与「累计计数」，熔断跳过不许记成 no_bars。
# ---------------------------------------------------------------------------

def test_daily_read_timeout_keeps_snapshot_and_numeric_fields(monkeypatch):
    """单次日线 ReadTimeout：报价照出、数值字段不受影响、日期明确 unknown。"""
    import httpx

    seen: list[dict] = []

    def _rpc(method, params, timeout=None, **kw):
        seen.append({"method": method, "timeout": timeout})
        if method == "get_market_data":
            raise httpx.ReadTimeout("daily cold cache slow")
        if method == "get_market_snapshot":
            return dict(_SNAP)
        return {}

    tqmod._reset_quote_date_stats()
    monkeypatch.setattr(tqmod, "_rpc", _rpc)
    quotes = tqmod.TqQuoteVendor().fetch([Symbol.parse("600519", "CN")], {})
    assert len(quotes) == 1
    q = quotes[0]
    assert q.current_price == 1354.00
    assert q.prev_close == 1330.75
    assert q.change_pct == pytest.approx(round((1354.00 - 1330.75) / 1330.75 * 100, 4))
    assert q.volume == 42992, "快照成交量是手，不许被日期这条路的失败改写"
    assert q.quote_date is None and q.quote_date_basis == ""
    assert "get_market_snapshot" in [c["method"] for c in seen]
    stats = tqmod.quote_date_stats()
    assert stats.get(tqmod.DEGRADE_RPC_FAILED) == 1
    assert stats.get(tqmod.DATE_REASON_NO_BARS) is None, "取数失败不是「这只票没有 bar」"


def test_circuit_open_skips_daily_but_keeps_quote_and_never_counts_no_bars(monkeypatch):
    """熔断期内：日线一次都不打，快照照打、报价照出；跳过只记 `daily_circuit_open`。"""
    seen: list[str] = []

    def _rpc(method, params, timeout=None, **kw):
        seen.append(method)
        if method == "get_market_snapshot":
            return dict(_SNAP)
        return {}

    tqmod._reset_quote_date_stats()
    tqmod._trip_daily_circuit()          # 真实 monotonic 基准，fetch 内也是 None
    monkeypatch.setattr(tqmod, "_rpc", _rpc)
    quotes = tqmod.TqQuoteVendor().fetch([Symbol.parse("600519", "CN")], {})
    assert len(quotes) == 1 and quotes[0].current_price == 1354.00
    assert quotes[0].quote_date is None
    assert "get_market_data" not in seen, "熔断期内不许再打日线"
    stats = tqmod.quote_date_stats()
    assert stats.get(tqmod.DEGRADE_CIRCUIT_OPEN) == 1
    assert stats.get(tqmod.DATE_REASON_NO_BARS) is None, "熔断跳过 ≠ 没有 bar"


def test_one_symbol_daily_error_does_not_trip_global_circuit(monkeypatch):
    """某标的日线 ErrorId 只丢它自己的日期：别的标的照证、熔断不打开、下个用户不受影响。"""
    today = _today()
    good = {"Date": ["20261028", today], "Close": ["1330.75", "1354.00"],
            "Volume": ["3880493", "4299231"]}

    def _rpc(method, params, timeout=None, **kw):
        if method == "get_market_snapshot":
            return dict(_SNAP)
        if method == "get_market_data":
            assert params["stock_list"] == ["600519.SH", "000001.SZ"]
            return {
                "600519.SH": good,
                "000001.SZ": {"ErrorId": "5", "Error": "bad code"},
            }
        return {}

    tqmod._reset_quote_date_stats()
    tqmod._reset_daily_circuit()
    monkeypatch.setattr(tqmod, "_rpc", _rpc)
    quotes = tqmod.TqQuoteVendor().fetch(
        [Symbol.parse("600519", "CN"), Symbol.parse("000001", "CN")], {}
    )
    by_symbol = {q.symbol: q for q in quotes}
    assert by_symbol["600519"].quote_date == _iso(today)
    assert by_symbol["000001"].quote_date is None
    assert tqmod._daily_circuit_open() is False, "单标的错误不许开全局熔断"
    stats = tqmod.quote_date_stats()
    assert stats.get(tqmod.DEGRADE_SYMBOL_ERROR) == 1
    assert stats.get(tqmod.DATE_REASON_NO_BARS) is None, "该标的日线报错不是「没有 bar」"
    assert stats.get(tqmod.DATE_REASON_TODAY) == 1

    # 另一个用户（下一批）仍能正常取证 —— 上一批的坏标的没有污染共享状态。
    monkeypatch.setattr(tqmod, "_rpc", lambda method, params, timeout=None, **kw: (
        dict(_SNAP) if method == "get_market_snapshot"
        else {"600519.SH": good} if method == "get_market_data" else {}
    ))
    q2 = tqmod.TqQuoteVendor().fetch([Symbol.parse("600519", "CN")], {})[0]
    assert q2.quote_date == _iso(today)


def test_request_error_with_partial_value_keeps_good_symbols_and_no_circuit(monkeypatch):
    """响应级 ErrorId 但带部分 Value：能用几只算几只，不开熔断（有证据就不算整轮失败）。"""
    today = _today()
    good = {"Date": [today], "Close": ["1354.00"], "Volume": ["4299231"]}

    def _rpc(method, params, timeout=None, **kw):
        if method == "get_market_snapshot":
            return dict(_SNAP)
        if method == "get_market_data":
            return {"ErrorId": "5", "Error": "one symbol failed", "Value": {"600519.SH": good}}
        return {}

    tqmod._reset_quote_date_stats()
    tqmod._reset_daily_circuit()
    monkeypatch.setattr(tqmod, "_rpc", _rpc)
    q = tqmod.TqQuoteVendor().fetch([Symbol.parse("600519", "CN")], {})[0]
    assert q.quote_date == _iso(today)
    assert tqmod._daily_circuit_open() is False
    stats = tqmod.quote_date_stats()
    assert stats.get(tqmod.DEGRADE_PARTIAL_ERROR) == 1
    assert stats.get(tqmod.DEGRADE_RPC_FAILED) is None


def test_request_error_without_value_keeps_quote_and_does_not_trip_circuit(monkeypatch):
    """响应级 ErrorId 且没有可用 Value ⇒ 本轮日期 unknown；报价照出、**不跳闸**。

    网关可能因为批量里一个坏标的把整批标成 ErrorId：这不能证明"日线这一路不可用"，
    更不该让下一个用户的日期证据也停 30s。可见性靠 `daily_rpc_failed` 累计计数。
    """
    def _rpc(method, params, timeout=None, **kw):
        if method == "get_market_snapshot":
            return dict(_SNAP)
        if method == "get_market_data":
            return {"ErrorId": "5", "Error": "period error"}
        return {}

    tqmod._reset_quote_date_stats()
    tqmod._reset_daily_circuit()
    monkeypatch.setattr(tqmod, "_rpc", _rpc)
    quotes = tqmod.TqQuoteVendor().fetch([Symbol.parse("600519", "CN")], {})
    assert len(quotes) == 1 and quotes[0].quote_date is None
    assert tqmod._daily_circuit_open() is False
    stats = tqmod.quote_date_stats()
    assert stats.get(tqmod.DEGRADE_RPC_FAILED) == 1
    assert stats.get(tqmod.DATE_REASON_NO_BARS) is None


def test_daily_circuit_backoff_doubles_and_is_bounded():
    """连续失败退避有界（30→60→120→240→300 封顶）：既不每次都白等，也不无限放大。"""
    tqmod._reset_daily_circuit()
    windows: list[int] = []
    now = 100.0
    for _ in range(8):
        tqmod._trip_daily_circuit(now=now)
        state = tqmod._daily_circuit_state(now=now)
        windows.append(int(state["remaining_s"]))
        now += state["remaining_s"]
    assert windows[:4] == [30, 60, 120, 240]
    assert max(windows) == int(tqmod.QUOTE_DATE_CIRCUIT_MAX_S) == 300
    assert tqmod._daily_circuit_state(now=now)["open"] is False


def test_trip_while_already_open_does_not_escalate():
    """熔断已开时并发失败不重复升级窗口（多用户同时打，也不会把退避瞬间推到上限）。"""
    tqmod._reset_daily_circuit()
    tqmod._trip_daily_circuit(now=100.0)
    assert tqmod._daily_circuit_state(now=100.0)["consecutive_failures"] == 1
    remaining = tqmod._trip_daily_circuit(now=110.0)
    assert remaining == pytest.approx(20.0)
    state = tqmod._daily_circuit_state(now=110.0)
    assert state["consecutive_failures"] == 1
    assert state["remaining_s"] == pytest.approx(20.0)


def test_daily_success_closes_circuit_and_resets_backoff():
    """日线恢复成功 ⇒ 当前熔断立即关闭、退避归零（不残留 30s 空白窗）。"""
    tqmod._reset_daily_circuit()
    tqmod._trip_daily_circuit()
    assert tqmod._daily_circuit_state()["open"] is True
    tqmod._note_daily_success()
    state = tqmod._daily_circuit_state()
    assert state["open"] is False and state["consecutive_failures"] == 0


def test_diagnostics_separate_current_circuit_from_cumulative_counters():
    """当前熔断是**状态**（会关），计数是**历史**（不清零）—— 面板不许把两者混成一个数。"""
    today = _today()
    tqmod._reset_quote_date_stats()
    tqmod._reset_daily_circuit()
    tqmod.prove_quote_date([_bar("20261028", "10.00", "100")], 10.0, today, snapshot_volume="0")
    tqmod._trip_daily_circuit()
    diag = tqmod.quote_date_diagnostics()
    assert diag["counters"].get(tqmod.DATE_REASON_NO_TODAY_BAR) == 1
    assert diag["circuit"]["open"] is True
    assert diag["circuit"]["remaining_s"] > 0
    assert diag["circuit"]["consecutive_failures"] == 1
    tqmod._reset_daily_circuit()
    diag2 = tqmod.quote_date_diagnostics()
    assert diag2["circuit"]["open"] is False
    assert diag2["circuit"]["remaining_s"] == 0
    assert diag2["counters"].get(tqmod.DATE_REASON_NO_TODAY_BAR) == 1, "历史计数不因熔断结束而消失"


def test_tq_never_quotes_non_cn_symbol_with_same_code(monkeypatch):
    """市场同号：CN 000001 与 US 000001 只允许 CN 那只走 TQ，代码相同也不许串。"""
    seen_codes: list[str] = []

    def _rpc(method, params, timeout=None, **kw):
        if method == "get_market_snapshot":
            seen_codes.append(params["stock_code"])
            return dict(_SNAP)
        return {}

    tqmod._reset_quote_date_stats()
    tqmod._reset_daily_circuit()
    monkeypatch.setattr(tqmod, "_rpc", _rpc)
    quotes = tqmod.TqQuoteVendor().fetch(
        [Symbol.parse("000001", "CN"), Symbol.parse("000001", "US")], {}
    )
    assert [q.symbol for q in quotes] == ["000001"]
    assert {q.market for q in quotes} == {"CN"}
    assert seen_codes == ["000001.SZ"], "非 CN 同号标的连快照都不许打"

    seen_codes.clear()
    assert tqmod.TqQuoteVendor().fetch([Symbol.parse("000001", "US")], {}) == []
    assert seen_codes == []


# ---------------------------------------------------------------------------
# 源优先级 / 超时 / 熔断（引擎级）：TQ 先说，只有网关**确证**不可用才轮到腾讯
# ---------------------------------------------------------------------------

class _FakeVendor:
    name = "tencent"
    supports_markets = {"CN"}

    def __init__(self) -> None:
        self.calls = 0

    def fetch(self, symbols, config):
        self.calls += 1
        return [{"symbol": s.code, "vendor": self.name} for s in symbols]


def _engine_for_priority(monkeypatch, rpc):
    from marketdata.cache import TTLCache
    from marketdata.defaults import InMemoryMetricsSink, StaticConfigProvider
    from marketdata.engine import Engine
    from marketdata.ports import SourceConfig

    monkeypatch.setattr(tqmod, "_rpc", rpc)
    tencent = _FakeVendor()
    metrics = InMemoryMetricsSink()
    engine = Engine(
        datatype="quote",
        vendors={"tq": tqmod.TqQuoteVendor(), "tencent": tencent},
        config=StaticConfigProvider({"quote": [
            SourceConfig(vendor="tencent", priority=2),   # 故意乱序，验证按 priority 排
            SourceConfig(vendor="tq", priority=1),
        ]}),
        metrics=metrics,
        cache=TTLCache(5.0),
        default_ttl=5.0,
    )
    return engine, tencent, metrics


def test_engine_keeps_tq_priority_when_only_daily_times_out(monkeypatch):
    """日线超时但快照可用 ⇒ 引擎必须仍用 TQ（有价、无日期），不许切腾讯、不许记失败。"""
    import httpx
    from marketdata.types import Request

    def _rpc(method, params, timeout=None, **kw):
        if method == "get_market_data":
            raise httpx.ReadTimeout("daily slow")
        if method == "get_market_snapshot":
            return dict(_SNAP)
        return {}

    tqmod._reset_quote_date_stats()
    tqmod._reset_daily_circuit()
    engine, tencent, metrics = _engine_for_priority(monkeypatch, _rpc)
    r = engine.fetch(Request(symbols=("600519",), market="CN"))
    assert r.ok and r.vendor == "tq", "TQ 有快照就必须优先用 TQ"
    assert len(r.data) == 1
    assert tencent.calls == 0, "单次日线慢不能当成 TQ 不可用"
    snap = metrics.snapshot()
    assert snap["tq"]["success_rate"] == 1.0, "TQ 这一路取数成功，不许记成失败（污染指标）"
    assert tencent.name not in snap


def test_engine_fails_over_only_when_snapshot_chain_is_down(monkeypatch):
    """快照传输层失败 = 网关确证不可用 ⇒ 才切腾讯，并如实记 TQ 一次失败。"""
    import httpx
    from marketdata.types import Request

    def _rpc(method, params, timeout=None, **kw):
        if method == "get_market_snapshot":
            raise httpx.ConnectTimeout("tunnel down")
        return {}

    tqmod._reset_quote_date_stats()
    tqmod._reset_daily_circuit()
    engine, tencent, metrics = _engine_for_priority(monkeypatch, _rpc)
    r = engine.fetch(Request(symbols=("600519",), market="CN"))
    assert r.ok and r.vendor == "tencent"
    assert tencent.calls == 1
    assert metrics.snapshot()["tq"]["success_rate"] == 0.0
