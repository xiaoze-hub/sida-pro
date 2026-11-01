"""`is_trading_day(d)` 必须带日期（2026-11-01）。

线上：`tq_market_flow.run_scheduled` 零参调用 → 每 5 分钟抛
`TypeError: is_trading_day() missing 1 required positional argument: 'd'`，
TQ 全市场成交额采集静默停摆（日志里 01:49、01:50 连续两次）。
"""

from __future__ import annotations

import datetime as dt

import pytest

from src.collectors import tq_market_flow
from src.core import trading_calendar


@pytest.fixture()
def _strict_calendar(monkeypatch):
    """把 is_trading_day 换成真签名（必须带 d）——零参调用会像线上一样当场炸。"""
    seen: list[object] = []

    def strict(d):
        seen.append(d)
        return False  # 非交易日：稳定地走 return None 分支

    monkeypatch.setattr(trading_calendar, "is_trading_day", strict)
    monkeypatch.setattr(trading_calendar, "is_trading_session", lambda: False)
    return seen


def test_tq_market_flow_passes_date(_strict_calendar):
    out = tq_market_flow.run_scheduled()
    assert out is None
    assert _strict_calendar, "必须把日期传给 is_trading_day"
    assert isinstance(_strict_calendar[0], dt.date)


def test_lhb_backfill_passes_date(monkeypatch, _strict_calendar):
    from src.collectors import lhb_tq_backfill

    monkeypatch.setattr(lhb_tq_backfill, "backfill", lambda: {"ok": True})
    out = lhb_tq_backfill.run_scheduled()
    assert out == {"skipped": "非交易日"}
    assert _strict_calendar and isinstance(_strict_calendar[0], dt.date)
