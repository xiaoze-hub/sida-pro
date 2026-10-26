"""交易阶段 / 数据日期工具单测(2026-10-18, A-03 配套)。

`src/core/trade_phase.py` 是全站"数据日 + 交易阶段"的唯一事实源, 竞价池 / 机会池 /
转债雷达都读它。这里把边界钉死, 防止以后有人改成"星期近似"或拿自然日冒充交易日。
"""
from __future__ import annotations

from datetime import date, datetime

import pytest

from src.core import trade_phase as tp


@pytest.mark.parametrize(
    "stamp,phase,label",
    [
        ("2026-10-18 15:00", tp.PHASE_NON_TRADING_DAY, "非交易日"),
        ("2026-10-17 10:00", tp.PHASE_NON_TRADING_DAY, "非交易日"),  # 周六
        ("2026-10-19 08:00", tp.PHASE_PRE_OPEN, "盘前"),
        ("2026-10-16 09:15", tp.PHASE_AUCTION, "集合竞价 09:15-09:25"),
        ("2026-10-16 09:25", tp.PHASE_AUCTION, "集合竞价 09:15-09:25"),
        ("2026-10-16 09:26", tp.PHASE_AUCTION_MATCH, "竞价撮合 09:25-09:30"),
        ("2026-10-16 09:30", tp.PHASE_CONTINUOUS, "连续竞价"),
        ("2026-10-16 11:30", tp.PHASE_CONTINUOUS, "连续竞价"),
        ("2026-10-16 12:30", tp.PHASE_NOON_BREAK, "午间休市"),
        ("2026-10-16 13:00", tp.PHASE_CONTINUOUS, "连续竞价"),
        ("2026-10-16 15:00", tp.PHASE_CONTINUOUS, "连续竞价"),
        ("2026-10-16 15:01", tp.PHASE_POST_CLOSE, "收盘后"),
    ],
)
def test_phase_boundaries(stamp, phase, label):
    now = datetime.fromisoformat(stamp)
    assert tp.trade_phase(now) == phase
    assert tp.phase_label(phase) == label


def test_phase_meta_shape():
    m = tp.phase_meta(datetime.fromisoformat("2026-10-16 10:00:00"))
    assert m["trade_phase"] == tp.PHASE_CONTINUOUS
    assert m["is_trading_day"] is True
    assert m["is_live_session"] is True
    assert m["now"].startswith("2026-10-16T10:00:00")


def test_naive_is_treated_as_shanghai_wall_clock():
    naive = datetime(2026, 10, 16, 10, 0)
    assert tp.cn_now(naive).tzinfo is not None
    assert tp.trade_phase(naive) == tp.PHASE_CONTINUOUS


def test_parse_date_accepts_all_client_shapes():
    assert tp.parse_date("2026-10-16") == date(2026, 10, 16)
    assert tp.parse_date("20261016") == date(2026, 10, 16)
    assert tp.parse_date("2026-10-16 09:25:03") == date(2026, 10, 16)
    assert tp.parse_date(date(2026, 10, 16)) == date(2026, 10, 16)
    assert tp.parse_date(None) is None
    assert tp.parse_date("") is None
    assert tp.parse_date("garbage") is None


def test_resolve_data_date_on_non_trading_day_returns_previous_trading_day():
    assert tp.resolve_data_date(now=datetime.fromisoformat("2026-10-18 15:00")) == date(2026, 10, 16)


def test_resolve_data_date_prefers_older_last_sync_when_today_not_synced():
    """周一盘前还没有今日同步, 但库里最后一条是上周五 → 如实给上周五(不冒充今天)。"""
    got = tp.resolve_data_date(
        now=datetime.fromisoformat("2026-10-19 08:30"),
        last_sync=datetime(2026, 10, 16, 9, 25, 3),
    )
    assert got == date(2026, 10, 16)


def test_resolve_data_date_in_live_session_is_today():
    assert tp.resolve_data_date(now=datetime.fromisoformat("2026-10-16 09:20")) == date(2026, 10, 16)


def test_is_live_now_requires_today_and_live_phase():
    assert tp.is_live_now(data_date=date(2026, 10, 16), now=datetime.fromisoformat("2026-10-16 09:20")) is True
    assert tp.is_live_now(data_date=date(2026, 10, 16), now=datetime.fromisoformat("2026-10-16 15:30")) is False
    assert tp.is_live_now(data_date=date(2026, 10, 16), now=datetime.fromisoformat("2026-10-18 09:20")) is False
    assert tp.is_live_now(data_date=None, now=datetime.fromisoformat("2026-10-16 09:20")) is False


def test_as_of_and_data_date_iso():
    assert tp.as_of_iso(datetime.fromisoformat("2026-10-16 09:20:05")).startswith("2026-10-16T09:20:05")
    assert tp.data_date_iso(date(2026, 10, 16)) == "2026-10-16"
    assert tp.data_date_iso(None) is None
