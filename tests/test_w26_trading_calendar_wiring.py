"""W2.6 (B6, 2026-09-09): 交易日历统一接线测试。

覆盖:
- is_trading_day 未覆盖年份显式抛 TradingCalendarError(红线: 禁止回落 weekday 推测)
- 新 API: is_auction_time / is_trading_session / trading_day_anchor
- market.MarketDef.is_trading_time: CN 走日历(节假日休市), HK/US 维持周末判定
- kline_collector: _is_auction_time 走日历; TTL 竞价档不再是死分支
- dark_flow / darkflow / backfill 的交易日门走日历
"""

from __future__ import annotations

import datetime as _dt
from zoneinfo import ZoneInfo

import pytest

from src.core.trading_calendar import (
    TradingCalendarError,
    add_trading_days,
    is_auction_time,
    is_trading_day,
    is_trading_session,
    trading_day_anchor,
)

SH = ZoneInfo("Asia/Shanghai")

# 已知日期(与日历静态表核对): 2026-09-09 周三(交易日), 2026-09-12 周六,
# 2026-09-11 周五, 2026-09-30 周三; 2026-10-01(周四)~10-07 国庆休市,
# 2026-10-10 周六为国庆补班交易日。
WED = "2026-09-09"
SAT = "2026-09-12"
FRI = "2026-09-11"


class TestFailLoud:
    """红线: 缺失年份显式报错, 不许回落 weekday 推测"""

    def test_uncovered_year_raises(self):
        with pytest.raises(TradingCalendarError):
            is_trading_day("2030-03-05")

    def test_add_trading_days_into_uncovered_year_raises(self):
        # 2027-12-30 起算 3 个交易日会走进 2028 → 必须报错而不是当周末处理
        with pytest.raises(TradingCalendarError):
            add_trading_days("2027-12-30", 3)

    def test_anchor_into_uncovered_year_raises(self):
        # 2025-01-01 是节假日, 回看上一交易日会走进 2024
        with pytest.raises(TradingCalendarError):
            trading_day_anchor("2025-01-01")

    def test_2027_makeup_workday_still_works(self):
        # 2027 表(预估)在覆盖范围内, 不应报错
        assert is_trading_day("2027-10-09") is True  # 周六补班


class TestIntradayHelpers:
    def test_auction_window_on_trading_day(self):
        assert is_auction_time(_dt.datetime(2026, 9, 9, 9, 14)) is False
        assert is_auction_time(_dt.datetime(2026, 9, 9, 9, 15)) is True
        assert is_auction_time(_dt.datetime(2026, 9, 9, 9, 20)) is True
        assert is_auction_time(_dt.datetime(2026, 9, 9, 9, 25)) is True
        assert is_auction_time(_dt.datetime(2026, 9, 9, 9, 26)) is False

    def test_auction_time_holiday_false(self):
        # 验收用例: 国庆(周四) 9:20 —— 旧 weekday 判定会误判 True
        assert is_auction_time(_dt.datetime(2026, 10, 1, 9, 20)) is False

    def test_auction_time_weekend_false(self):
        assert is_auction_time(_dt.datetime(2026, 9, 12, 9, 20)) is False

    def test_auction_time_aware_datetime_normalized(self):
        # UTC 01:20 = 上海 09:20
        assert is_auction_time(_dt.datetime(2026, 9, 9, 1, 20, tzinfo=ZoneInfo("UTC"))) is True

    def test_trading_session_windows(self):
        assert is_trading_session(_dt.datetime(2026, 9, 9, 9, 29)) is False
        assert is_trading_session(_dt.datetime(2026, 9, 9, 9, 30)) is True
        assert is_trading_session(_dt.datetime(2026, 9, 9, 11, 30)) is True
        assert is_trading_session(_dt.datetime(2026, 9, 9, 11, 31)) is False
        assert is_trading_session(_dt.datetime(2026, 9, 9, 13, 0)) is True
        assert is_trading_session(_dt.datetime(2026, 9, 9, 15, 0)) is True
        assert is_trading_session(_dt.datetime(2026, 9, 9, 15, 1)) is False

    def test_trading_session_holiday_and_weekend_false(self):
        assert is_trading_session(_dt.datetime(2026, 10, 1, 10, 0)) is False
        assert is_trading_session(_dt.datetime(2026, 9, 12, 10, 0)) is False

    def test_trading_day_anchor(self):
        assert trading_day_anchor(WED) == _dt.date(2026, 9, 9)    # 交易日 → 自身
        assert trading_day_anchor(SAT) == _dt.date(2026, 9, 11)   # 周六 → 周五
        assert trading_day_anchor("2026-10-01") == _dt.date(2026, 9, 30)  # 国庆 → 9/30
        assert trading_day_anchor("2026-10-03") == _dt.date(2026, 9, 30)  # 国庆周六 → 9/30


class TestMarketDef:
    def test_cn_holiday_not_trading(self):
        from src.models.market import MARKETS, MarketCode

        md = MARKETS[MarketCode.CN]
        assert md.is_trading_time(_dt.datetime(2026, 10, 1, 10, 0, tzinfo=SH)) is False

    def test_cn_trading_day_in_session(self):
        from src.models.market import MARKETS, MarketCode

        md = MARKETS[MarketCode.CN]
        assert md.is_trading_time(_dt.datetime(2026, 9, 9, 10, 0, tzinfo=SH)) is True
        assert md.is_trading_time(_dt.datetime(2026, 9, 9, 12, 0, tzinfo=SH)) is False  # 午休
        assert md.is_trading_time(_dt.datetime(2026, 9, 12, 10, 0, tzinfo=SH)) is False  # 周六

    def test_cn_makeup_saturday_is_trading(self):
        from src.models.market import MARKETS, MarketCode

        md = MARKETS[MarketCode.CN]
        # 2026-10-10 周六为国庆补班交易日, 盘中应判定交易中
        assert md.is_trading_time(_dt.datetime(2026, 10, 10, 10, 0, tzinfo=SH)) is True

    def test_cn_uncovered_year_raises(self):
        from src.models.market import MARKETS, MarketCode

        with pytest.raises(TradingCalendarError):
            MARKETS[MarketCode.CN].is_trading_time(
                _dt.datetime(2030, 3, 5, 10, 0, tzinfo=SH)
            )

    def test_hk_us_keep_weekday_gate(self):
        # 港美暂无日历(登记的限制): 落在各自时段内仍按 weekday 判定
        from src.models.market import MARKETS, MarketCode

        hk = MARKETS[MarketCode.HK]
        assert hk.is_trading_time(
            _dt.datetime(2026, 10, 1, 10, 0, tzinfo=ZoneInfo("Asia/Hong_Kong"))
        ) is True
        us = MARKETS[MarketCode.US]
        assert us.is_trading_time(
            _dt.datetime(2026, 10, 3, 12, 0, tzinfo=ZoneInfo("America/New_York"))
        ) is False  # 周六


class TestKlineCollector:
    def test_is_auction_time_delegates_to_calendar(self):
        from src.collectors.kline_collector import _is_auction_time

        assert _is_auction_time(_dt.datetime(2026, 9, 9, 9, 20)) is True
        assert _is_auction_time(_dt.datetime(2026, 10, 1, 9, 20)) is False

    def test_ttl_auction_tier_reachable(self, monkeypatch):
        # 旧写法竞价档嵌在 is_trading_time(9:30 起)里是死分支; 现在竞价期直接 15s
        import src.collectors.kline_collector as kc
        from src.models.market import MarketCode

        monkeypatch.setattr(kc, "_is_auction_time", lambda now=None: True)
        assert kc._kline_cache_ttl(MarketCode.CN) == kc._KLINE_TTL_AUCTION_S

    def test_ttl_auction_tier_cn_only(self, monkeypatch):
        import src.collectors.kline_collector as kc
        from src.models.market import MarketCode, MarketDef

        monkeypatch.setattr(kc, "_is_auction_time", lambda now=None: True)
        monkeypatch.setattr(MarketDef, "is_trading_time", lambda self, dt=None: False)
        assert kc._kline_cache_ttl(MarketCode.HK) == kc._KLINE_TTL_CLOSED_S

    def test_ttl_closed_tier_on_holiday(self, monkeypatch):
        # 验收用例: 国庆(周四)白天 → TTL 走非交易档
        import src.collectors.kline_collector as kc
        from src.models.market import MarketCode

        monkeypatch.setattr("src.core.trading_calendar.is_trading_day", lambda d: False)
        assert kc._kline_cache_ttl(MarketCode.CN) == kc._KLINE_TTL_CLOSED_S

    def test_ttl_trading_tier(self, monkeypatch):
        import src.collectors.kline_collector as kc
        from src.models.market import MarketCode, MarketDef

        monkeypatch.setattr(kc, "_is_auction_time", lambda now=None: False)
        monkeypatch.setattr(MarketDef, "is_trading_time", lambda self, dt=None: True)
        assert kc._kline_cache_ttl(MarketCode.CN) == kc._KLINE_TTL_TRADING_S


class TestDarkFlow:
    def test_drop_future_ticks_holiday_passes_all(self):
        # 国庆(周四)白天: 上一交易日的 tick 不再被当"未来时刻"误丢
        from src.core.dark_flow import _drop_future_ticks

        ticks = [{"t": "14:30:00", "price": 10.0, "amt": 100.0}]
        out = _drop_future_ticks(ticks, now=_dt.datetime(2026, 10, 1, 10, 0))
        assert out == ticks

    def test_drop_future_ticks_trading_day_still_drops(self):
        from src.core.dark_flow import _drop_future_ticks

        ticks = [{"t": "14:30:00", "price": 10.0, "amt": 100.0}]
        out = _drop_future_ticks(ticks, now=_dt.datetime(2026, 9, 9, 10, 0))
        assert out == []

    def test_in_trading_hours_non_trading_day_false(self, monkeypatch):
        from src.core import dark_flow

        monkeypatch.setattr("src.core.trading_calendar.is_trading_day", lambda d: False)
        assert dark_flow._in_trading_hours() is False


class TestDarkflowStaleness:
    def test_staleness_holiday_not_stale(self):
        # 国庆白天: 无新 tick 也不误报 stale
        from src.web.api.darkflow import _tick_staleness

        info = _tick_staleness(
            last_tick_t="09:30:00",
            trade_date="2026-10-01",
            now=_dt.datetime(2026, 10, 1, 10, 30),
        )
        assert info["stale"] is False

    def test_staleness_trading_day_still_works(self):
        from src.web.api.darkflow import _tick_staleness

        info = _tick_staleness(
            last_tick_t="09:30:00",
            trade_date="2026-09-09",
            now=_dt.datetime(2026, 9, 9, 10, 30),
        )
        assert info["stale"] is True
        assert info["lag_sec"] == 3600


class TestBackfillMarketDay:
    def test_holiday_false(self):
        from src.core.kline_backfill_scheduler import _is_market_day

        assert _is_market_day(_dt.datetime(2026, 10, 1, 18, 0, tzinfo=SH)) is False

    def test_trading_day_true(self):
        from src.core.kline_backfill_scheduler import _is_market_day

        assert _is_market_day(_dt.datetime(2026, 9, 9, 18, 0, tzinfo=SH)) is True

    def test_weekend_false(self):
        from src.core.kline_backfill_scheduler import _is_market_day

        assert _is_market_day(_dt.datetime(2026, 9, 12, 18, 0, tzinfo=SH)) is False
