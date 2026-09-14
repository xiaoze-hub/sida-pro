# -*- coding: utf-8 -*-
"""KI-028: 2028 交易日静态表覆盖(预估, 须 2027-12 前复核)。"""
import pytest
from datetime import date

from src.core.trading_calendar import (
    TradingCalendarError,
    is_trading_day,
    add_trading_days,
)


def test_2028_year_covered():
    # 不再抛 TradingCalendarError
    assert is_trading_day(date(2028, 3, 15))  # 周三普通日


def test_2028_holiday_and_workday():
    # 元旦后补休 01-03 周一 休市
    assert not is_trading_day(date(2028, 1, 3))
    # 预估春节 01-26~02-01 休市
    assert not is_trading_day(date(2028, 1, 28))
    # 预估春节前补班 01-22 周六 交易
    assert is_trading_day(date(2028, 1, 22))
    # 国庆
    assert not is_trading_day(date(2028, 10, 2))


def test_still_errors_on_uncovered_year():
    with pytest.raises(TradingCalendarError):
        is_trading_day(date(2029, 6, 1))


def test_add_trading_days_crosses_2028_spring_festival():
    # 2028-01-20(周四) +2 → 01-21(周五) → 01-22(周六补班)
    d = add_trading_days(date(2028, 1, 20), 2)
    assert d == date(2028, 1, 22)
