"""TQ 快照活性门禁：交易时段内累计量不推进 ⇒ 判陈旧并降级。

钉死的三条（2026-10-30 网关只读实测 + 盘中故障复盘）：
1. 首次观测只能是 UNPROVEN：单次快照没有报时字段，不声称新鲜、也不冤枉成陈旧；
2. 只有累计成交量/笔数的**推进**才算证据：价格变化不能证明有新成交；
3. 计数器回退/重置（客户端盘中重启）⇒ 证据作废，不得当成"有推进"。

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
)


@pytest.fixture(autouse=True)
def _fresh_state():
    """进程级观测器是全局状态：每个用例前后都清，否则前一个用例的推进/陈旧会串味。"""
    tqmod._reset_liveness()
    tqmod._reset_quote_date_stats()
    tqmod._reset_daily_circuit()
    yield
    tqmod._reset_liveness()
    tqmod._reset_daily_circuit()


# ---------------------------------------------------------------------------
# 纯观测器
# ---------------------------------------------------------------------------

def test_first_observation_is_unproven_not_fresh():
    gate = TqLivenessGate(max_symbols=4, stale_seconds=60)
    assert gate.observe("600519.SH", "100", "50", now=100, market_open=True) is Liveness.UNPROVEN


def test_volume_advance_proves_fresh():
    gate = TqLivenessGate(max_symbols=4, stale_seconds=60)
    gate.observe("600519.SH", "100", "50", now=100, market_open=True)
    assert gate.observe("600519.SH", "101", "51", now=110, market_open=True) is Liveness.FRESH


def test_no_advance_past_window_during_session_is_stale():
    gate = TqLivenessGate(max_symbols=4, stale_seconds=60)
    gate.observe("600519.SH", "100", "50", now=100, market_open=True)
    # 首次观测后 60s 内仍属"还没到判死的窗口"
    assert gate.observe("600519.SH", "100", "50", now=150, market_open=True) is Liveness.UNPROVEN
    assert gate.observe("600519.SH", "100", "50", now=161, market_open=True) is Liveness.STALE


def test_last_advance_starts_the_stale_clock():
    gate = TqLivenessGate(max_symbols=4, stale_seconds=60)
    gate.observe("600519.SH", "100", "50", now=100, market_open=True)
    assert gate.observe("600519.SH", "200", "90", now=500, market_open=True) is Liveness.FRESH
    assert gate.observe("600519.SH", "200", "90", now=550, market_open=True) is Liveness.FRESH
    assert gate.observe("600519.SH", "200", "90", now=561, market_open=True) is Liveness.STALE


def test_closed_session_never_judges_stale():
    """收盘后不动是正常的：不许因此把报价判死（否则全站整天降级）。"""
    gate = TqLivenessGate(max_symbols=4, stale_seconds=60)
    gate.observe("600519.SH", "100", "50", now=100, market_open=False)
    assert gate.observe("600519.SH", "100", "50", now=9999, market_open=False) is Liveness.NOT_APPLICABLE


def test_price_change_alone_is_not_evidence():
    gate = TqLivenessGate(max_symbols=4, stale_seconds=60)
    gate.observe("600519.SH", "100", "50", now=100, market_open=True)
    assert gate.observe("600519.SH", "100", "50", now=161, market_open=True) is Liveness.STALE


def test_counter_reset_rewinds_to_unproven():
    """客户端盘中重启会把累计量清零：旧证据必须作废，不能拿"变小了"当推进。"""
    gate = TqLivenessGate(max_symbols=4, stale_seconds=60)
    gate.observe("600519.SH", "100", "50", now=100, market_open=True)
    assert gate.observe("600519.SH", "200", "90", now=110, market_open=True) is Liveness.FRESH
    assert gate.observe("600519.SH", "2", "1", now=120, market_open=True) is Liveness.UNPROVEN
    # 重置后仍在窗口内 ⇒ 只能说"证不出来"；过了窗口才判陈旧（重新从重置时刻计时）
    assert gate.observe("600519.SH", "2", "1", now=170, market_open=True) is Liveness.UNPROVEN
    assert gate.observe("600519.SH", "2", "1", now=190, market_open=True) is Liveness.STALE
    assert gate.observe("600519.SH", "2", "1", now=190, market_open=False) is Liveness.NOT_APPLICABLE


def test_clock_rollback_resets_the_window():
    gate = TqLivenessGate(max_symbols=4, stale_seconds=60)
    gate.observe("600519.SH", "100", "50", now=500, market_open=True)
    assert gate.observe("600519.SH", "100", "50", now=400, market_open=True) is Liveness.UNPROVEN


@pytest.mark.parametrize("volume", [None, "", "abc", "nan", "inf", "-1"])
def test_unreadable_cumulative_volume_cannot_be_judged(volume):
    gate = TqLivenessGate(max_symbols=4, stale_seconds=60)
    assert gate.observe("600519.SH", volume, "50", now=100, market_open=True) is Liveness.UNPROVEN
    assert gate.tracked == 0


def test_capacity_exhaustion_fails_closed():
    gate = TqLivenessGate(max_symbols=1, stale_seconds=60)
    gate.observe("600519.SH", "100", "50", now=100, market_open=True)
    assert gate.tracked == 1
    # 表满：新标的既不记录、也不声称新鲜（不因为"没见过"就放行）
    assert gate.observe("000001.SZ", "1", "1", now=101, market_open=True) is Liveness.UNPROVEN
    assert gate.tracked == 1


def test_invalid_bounds_rejected():
    with pytest.raises(ValueError):
        TqLivenessGate(max_symbols=0, stale_seconds=60)
    with pytest.raises(ValueError):
        TqLivenessGate(max_symbols=4, stale_seconds=0)


# ---------------------------------------------------------------------------
# 交易时段判定（只按周末+时间窗，节假日不猜）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "moment,expected",
    [
        ("2026-10-30 09:29:59", False),  # 集合竞价尾段：撮合前累计量本就不动
        ("2026-10-30 09:30:00", True),
        ("2026-10-30 11:29:59", True),
        ("2026-10-30 11:30:00", False),  # 午休
        ("2026-10-30 12:59:59", False),
        ("2026-10-30 13:00:00", True),
        ("2026-10-30 14:59:59", True),
        ("2026-10-30 15:00:00", False),  # 收盘后不动是正常
        ("2026-10-31 10:00:00", False),  # 周六
        ("2026-11-01 10:00:00", False),  # 周日
    ],
)
def test_session_windows(moment, expected):
    assert cn_continuous_session_open(datetime.fromisoformat(moment)) is expected


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


def test_helper_uses_injected_clock_and_session():
    snap = {"Volume": "100", "ItemNum": "50"}
    assert tqmod._snapshot_stale("600519", snap, now=100, market_open=True) is False
    # 生产窗口是 QUOTE_LIVENESS_STALE_S（90s）：超窗才判陈旧
    assert tqmod._snapshot_stale(
        "600519", snap, now=100 + tqmod.QUOTE_LIVENESS_STALE_S + 1, market_open=True
    ) is True
    # 非交易时段同一份数据不得判陈旧
    assert tqmod._snapshot_stale("000001", snap, now=100, market_open=False) is False
    assert tqmod._snapshot_stale("000001", snap, now=99999, market_open=False) is False
