"""TQ 快照活性门禁：交易时段内累计量不推进 ⇒ 判陈旧并降级。

钉死的五条（2026-10-30 网关只读实测 + 盘中故障复盘 + 审查意见）：
1. 首次观测只能是 UNPROVEN：单次快照没有报时字段，不声称新鲜、也不冤枉成陈旧；
2. 只有累计成交量**或累计笔数**的推进才算证据：价格变化不能证明有新成交；
3. 计数器回退/重置（客户端盘中重启）⇒ 证据作废，不得当成"有推进"；
4. 时段标识由**墙钟**给出：取数是按需触发的，午休可能一次观测都没有，靠"上次观测到的是
   休市"来判断新时段会漏 —— 第一笔午后观测会带着上午的基线判整批陈旧；
5. 非交易时段不下结论。

另钉死两条与网关能力相关的事实（不是假设）：
- 订阅类方法在网关上不存在（`-32601 MCP不支持该tqcenter方法名`），所以**不存在**
  "先订阅再取快照"这条实现路径 —— 只能靠观测证伪；
- `get_market_snapshot` 无 time/date 字段，日期只能由同标的日线旁证（见 test_tq_quote_date）。
"""
from __future__ import annotations

from datetime import datetime

import pytest

pytest.importorskip("marketdata")

from marketdata.symbol import Symbol  # noqa: E402
from marketdata.vendors import tq as tqmod  # noqa: E402
from marketdata.vendors.tq_liveness import (  # noqa: E402
    Liveness,
    TqLivenessGate,
    cn_continuous_session_open,
    cn_session_state,
)

AM, PM = "20261030:am", "20261030:pm"


@pytest.fixture(autouse=True)
def _fresh_state():
    """进程级观测器是全局状态：每个用例前后都清，否则前一个用例的推进/陈旧会串味。"""
    tqmod._reset_liveness()
    tqmod._reset_quote_date_stats()
    tqmod._reset_daily_circuit()
    yield
    tqmod._reset_liveness()
    tqmod._reset_daily_circuit()


def _gate(stale: float = 60) -> TqLivenessGate:
    return TqLivenessGate(max_symbols=8, stale_seconds=stale)


# ---------------------------------------------------------------------------
# 纯观测器
# ---------------------------------------------------------------------------

def test_first_observation_is_unproven_not_fresh():
    gate = _gate()
    assert gate.observe("600519.SH", "100", "50", now=100, session=AM,
                        market_open=True) is Liveness.UNPROVEN


def test_volume_advance_proves_fresh():
    gate = _gate()
    gate.observe("600519.SH", "100", "50", now=100, session=AM, market_open=True)
    assert gate.observe("600519.SH", "101", "51", now=110, session=AM,
                        market_open=True) is Liveness.FRESH


def test_tick_count_advance_also_proves_fresh():
    """成交量不变但逐笔笔数推进 ⇒ 有新的逐笔事件，报价已不是上一份（撤单/挂单）。"""
    gate = _gate()
    gate.observe("600519.SH", "100", "50", now=100, session=AM, market_open=True)
    assert gate.observe("600519.SH", "100", "51", now=110, session=AM,
                        market_open=True) is Liveness.FRESH
    assert gate.observe("600519.SH", "100", "51", now=180, session=AM,
                        market_open=True) is Liveness.STALE


def test_no_advance_past_window_during_session_is_stale():
    gate = _gate()
    gate.observe("600519.SH", "100", "50", now=100, session=AM, market_open=True)
    # 首次观测后 60s 内仍属"还没到判死的窗口"
    assert gate.observe("600519.SH", "100", "50", now=150, session=AM,
                        market_open=True) is Liveness.UNPROVEN
    assert gate.observe("600519.SH", "100", "50", now=161, session=AM,
                        market_open=True) is Liveness.STALE


def test_last_advance_starts_the_stale_clock():
    gate = _gate()
    gate.observe("600519.SH", "100", "50", now=100, session=AM, market_open=True)
    assert gate.observe("600519.SH", "200", "90", now=500, session=AM,
                        market_open=True) is Liveness.FRESH
    assert gate.observe("600519.SH", "200", "90", now=550, session=AM,
                        market_open=True) is Liveness.FRESH
    assert gate.observe("600519.SH", "200", "90", now=561, session=AM,
                        market_open=True) is Liveness.STALE


def test_closed_session_never_judges_stale():
    """收盘后不动是正常的：不许因此把报价判死（否则全站整天降级）。"""
    gate = _gate()
    assert gate.observe("600519.SH", "100", "50", now=100, session="20261030:closed",
                        market_open=False) is Liveness.NOT_APPLICABLE
    assert gate.observe("600519.SH", "100", "50", now=9999, session="20261030:closed",
                        market_open=False) is Liveness.NOT_APPLICABLE


def test_price_change_alone_is_not_evidence():
    gate = _gate()
    gate.observe("600519.SH", "100", "50", now=100, session=AM, market_open=True)
    assert gate.observe("600519.SH", "100", "50", now=161, session=AM,
                        market_open=True) is Liveness.STALE


def test_counter_reset_rewinds_to_unproven():
    """客户端盘中重启会把累计量清零：旧证据必须作废，不能拿"变小了"当推进。"""
    gate = _gate()
    gate.observe("600519.SH", "100", "50", now=100, session=AM, market_open=True)
    assert gate.observe("600519.SH", "200", "90", now=110, session=AM,
                        market_open=True) is Liveness.FRESH
    assert gate.observe("600519.SH", "2", "1", now=120, session=AM,
                        market_open=True) is Liveness.UNPROVEN
    # 重置后仍在窗口内 ⇒ 只能说"证不出来"；过了窗口才判陈旧（重新从重置时刻计时）
    assert gate.observe("600519.SH", "2", "1", now=170, session=AM,
                        market_open=True) is Liveness.UNPROVEN
    assert gate.observe("600519.SH", "2", "1", now=190, session=AM,
                        market_open=True) is Liveness.STALE


def test_clock_rollback_resets_the_window():
    gate = _gate()
    gate.observe("600519.SH", "100", "50", now=500, session=AM, market_open=True)
    assert gate.observe("600519.SH", "100", "50", now=400, session=AM,
                        market_open=True) is Liveness.UNPROVEN


@pytest.mark.parametrize("volume", [None, "", "abc", "nan", "inf", "-1"])
def test_unreadable_cumulative_volume_cannot_be_judged(volume):
    gate = _gate()
    assert gate.observe("600519.SH", volume, "50", now=100, session=AM,
                        market_open=True) is Liveness.UNPROVEN
    assert gate.tracked == 0


def test_capacity_evicts_least_recently_seen():
    """全市场扫描会超上限：必须淘汰最久未见的，而不是让门禁对新标的静默失效。"""
    gate = TqLivenessGate(max_symbols=1, stale_seconds=60)
    assert gate.observe("600519.SH", "100", "50", now=100, session=AM,
                        market_open=True) is Liveness.UNPROVEN
    assert gate.observe("600519.SH", "200", "90", now=110, session=AM,
                        market_open=True) is Liveness.FRESH
    # 新标的进来：淘汰最久未见的 600519，新标的照常开始观测（不是被拒之门外）
    assert gate.observe("000001.SZ", "100", "50", now=120, session=AM,
                        market_open=True) is Liveness.UNPROVEN
    assert gate.tracked == 1
    # 被淘汰的标的历史已丢 ⇒ 重新观测只能从头攒证据，不沿用旧推进/旧起点
    assert gate.observe("600519.SH", "200", "90", now=200, session=AM,
                        market_open=True) is Liveness.UNPROVEN


def test_invalid_bounds_rejected():
    with pytest.raises(ValueError):
        TqLivenessGate(max_symbols=0, stale_seconds=60)
    with pytest.raises(ValueError):
        TqLivenessGate(max_symbols=4, stale_seconds=0)


# ---------------------------------------------------------------------------
# 时段
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "moment,expected_open,expected_session",
    [
        ("2026-10-30 09:29:59", False, "20261030:closed"),  # 集合竞价：累计量本就不动
        ("2026-10-30 09:30:00", True, "20261030:am"),
        ("2026-10-30 11:29:59", True, "20261030:am"),
        ("2026-10-30 11:30:00", False, "20261030:closed"),  # 午休
        ("2026-10-30 12:59:59", False, "20261030:closed"),
        ("2026-10-30 13:00:00", True, "20261030:pm"),
        ("2026-10-30 14:59:59", True, "20261030:pm"),
        ("2026-10-30 15:00:00", False, "20261030:closed"),  # 收盘后不动是正常
        ("2026-10-31 10:00:00", False, "20261031:closed"),  # 周六
    ],
)
def test_session_state(moment, expected_open, expected_session):
    session, is_open = cn_session_state(datetime.fromisoformat(moment))
    assert (is_open, session) == (expected_open, expected_session)
    assert cn_continuous_session_open(datetime.fromisoformat(moment)) is expected_open


def test_new_session_restarts_even_without_any_observation_during_the_break():
    """午休期间可能**一次观测都没有**（取数按需触发）：时段标识变化必须自己触发重计时。

    真实时序：11:29:50 推进 → 午休零请求 → 13:00:01 第一笔午后请求（累计量未变）。
    若靠"上次观测到的是休市"来判断，这里 prev.session 还是 am ⇒ 拿上午基线算
    5411s > 90 ⇒ 整批误判陈旧（这正是审查指出的漏判）。
    """
    gate = TqLivenessGate(max_symbols=8, stale_seconds=90)
    assert gate.observe("600519.SH", "100", "50", now=100, session=AM,
                        market_open=True) is Liveness.UNPROVEN
    assert gate.observe("600519.SH", "200", "90", now=150, session=AM,
                        market_open=True) is Liveness.FRESH
    # 午休零观测，直接跳到午后第一笔
    assert gate.observe("600519.SH", "200", "90", now=5251, session=PM,
                        market_open=True) is Liveness.UNPROVEN
    assert gate.observe("600519.SH", "200", "90", now=5330, session=PM,
                        market_open=True) is Liveness.UNPROVEN
    assert gate.observe("600519.SH", "200", "90", now=5342, session=PM,
                        market_open=True) is Liveness.STALE


def test_auction_then_open_restarts():
    """集合竞价（非连续竞价）观测到开盘：同样重新计时，不许拿竞价时段当基线。"""
    gate = TqLivenessGate(max_symbols=8, stale_seconds=90)
    assert gate.observe("600519.SH", "0", "0", now=100, session="20261030:closed",
                        market_open=False) is Liveness.NOT_APPLICABLE
    assert gate.observe("600519.SH", "0", "0", now=1600, session=AM,
                        market_open=True) is Liveness.UNPROVEN


# ---------------------------------------------------------------------------
# vendor 接线：陈旧票必须从整批剔除（引擎据此换源）
# ---------------------------------------------------------------------------

def _snapshot(volume: str, items: str, price: str = "10.00") -> dict:
    return {
        "Now": price,
        "LastClose": "9.00",
        "Open": "9.50",
        "Max": "10.20",
        "Min": "9.40",
        "Volume": volume,
        "Amount": "1000.00",
        "ItemNum": items,
        "Inside": "10",
        "Outside": "20",
        "ErrorId": "0",
    }


def test_stale_snapshot_is_dropped_from_batch(monkeypatch):
    """判陈旧 ⇒ 该票从本批剔除 ⇒ 引擎看到 partial 后整批换源。"""

    def _rpc(method, params, timeout=None, **kw):
        if method == "get_market_snapshot":
            return dict(_snapshot("1666566", "4838"))
        return {"ErrorId": "0", "Value": {}}  # 日线/扩展指标：本用例不关心

    monkeypatch.setattr(tqmod, "_rpc", _rpc)
    monkeypatch.setattr(tqmod, "_snapshot_stale", lambda code, snap: True)
    assert tqmod.TqQuoteVendor().fetch([Symbol.parse("002361", "CN")], {}) == []
    assert tqmod.quote_date_stats().get(tqmod.DEGRADE_SNAPSHOT_STALE, 0) == 1


def test_unproven_snapshot_is_kept(monkeypatch):
    """证据不足（首次观测）不等于旧价：不许因此丢报价。"""

    def _rpc(method, params, timeout=None, **kw):
        if method == "get_market_snapshot":
            return dict(_snapshot("1666566", "4838"))
        return {"ErrorId": "0", "Value": {}}

    monkeypatch.setattr(tqmod, "_rpc", _rpc)
    monkeypatch.setattr(tqmod, "_snapshot_stale", lambda code, snap: False)
    quotes = tqmod.TqQuoteVendor().fetch([Symbol.parse("002361", "CN")], {})
    assert [q.symbol for q in quotes] == ["002361"]
    assert tqmod.quote_date_stats().get(tqmod.DEGRADE_SNAPSHOT_STALE, 0) == 0


def test_helper_uses_injected_clock_session_and_market_state():
    snap = {"Volume": "100", "ItemNum": "50"}
    assert tqmod._snapshot_stale("600519", snap, now=100, session=AM,
                                market_open=True) is False
    # 生产窗口是 QUOTE_LIVENESS_STALE_S（90s）：超窗才判陈旧
    assert tqmod._snapshot_stale(
        "600519", snap, now=100 + tqmod.QUOTE_LIVENESS_STALE_S + 1, session=AM,
        market_open=True,
    ) is True
    # 非交易时段同一份数据不得判陈旧
    other = {"Volume": "100", "ItemNum": "50"}
    assert tqmod._snapshot_stale("000001", other, now=100, session="20261030:closed",
                                 market_open=False) is False
    assert tqmod._snapshot_stale("000001", other, now=99999, session="20261030:closed",
                                 market_open=False) is False
