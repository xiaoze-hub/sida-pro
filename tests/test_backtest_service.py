"""B2.4: 回测服务(金叉策略) 与端点冒烟。"""

from __future__ import annotations

from src.core.backtest.data_adapter import PriceBar


def _bars() -> list[PriceBar]:
    """先横盘后上涨 → 触发 MA5 上穿 MA20。"""
    out = []
    price = 10.0
    for i in range(60):
        if i < 40:
            price = 10.0 + (0.01 if i % 2 else -0.01)   # 横盘
        else:
            price *= 1.02                               # 连续上涨
        out.append(PriceBar(date=f"2026-06-{i + 1:02d}", open=price, high=price * 1.01,
                            low=price * 0.99, close=price, volume=1_000_000))
    return out


def test_run_backtest_produces_trades_and_metrics(monkeypatch):
    from src.core.backtest import service

    monkeypatch.setattr(service, "load_price_history", lambda *_a, **_kw: _bars())
    res = service.run_backtest(symbols=["600000"], start_date="2026-06-01",
                               end_date="2026-08-30", holding_days=5)

    assert res["signals"] >= 1
    assert res["trades"], "应至少成交一笔"
    assert res["metrics"]["trades"] == len(res["trades"])
    assert {"sharpe", "sortino", "kalmar", "max_drawdown"} <= set(res["metrics"])
    # 净值曲线逐日(B0.1 契约)
    assert len(res["equity_curve"]) == len(res["equity_dates"]) >= 2
    assert res["params"]["strategy"] == "ma5_cross_ma20"


def test_old_start_extends_lookback_and_reports_truncation(monkeypatch):
    from datetime import date, timedelta
    from src.core.backtest import service
    from src.core.backtest.data_adapter import PriceBar

    seen = {}
    start = (date.today() - timedelta(days=1200)).isoformat()
    bars = [PriceBar(date=start, open=10, high=10, low=10, close=10, volume=1000)] * 40

    def load(_symbol, _market, days=250, **_kw):
        seen['days'] = days
        return bars

    monkeypatch.setattr(service, 'load_price_history', load)
    result = service.run_backtest(symbols=['600000'], start_date=start)
    assert seen['days'] >= 1200
    assert result['params']['history_days'] == seen['days']
    assert result['data_coverage']['600000']['status'] == 'usable'
    assert result['data_coverage']['600000']['window_truncated'] is False


def test_end_only_old_window_still_loads(monkeypatch):
    from datetime import date, datetime, timezone
    from src.core.backtest import service
    from src.core.backtest.data_adapter import PriceBar

    seen = {}
    end = '2024-08-20'  # The calendar fixture does not cover the effective start.
    bars = [PriceBar(date=end, open=10, high=10, low=10, close=10, volume=1000)] * 40

    def load(_symbol, _market, days=250, **_kw):
        seen['days'] = days
        return bars

    monkeypatch.setattr(service, 'load_price_history', load)
    result = service.run_backtest(symbols=['600000'], end_date=end)
    expected_days = min(service.MAX_HISTORY_DAYS, (datetime.now(timezone.utc).date() - date.fromisoformat(end)).days + 860)
    assert seen['days'] == expected_days
    assert result['data_coverage']['600000']['status'] == 'usable'
    assert result['data_coverage']['600000']['window_truncated'] is None  # calendar not verified at the historical left edge


def test_weekend_left_edge_with_first_trading_day_is_not_truncated(monkeypatch):
    from datetime import date, timedelta
    from src.core.backtest import service

    monday = date(2026, 6, 8)
    bars = [
        PriceBar(date=(monday + timedelta(days=offset)).isoformat(), open=10, high=10,
                 low=10, close=10, volume=1000)
        for offset in range(45) if (monday + timedelta(days=offset)).weekday() < 5
    ]
    monkeypatch.setattr(service, 'load_price_history', lambda *_a, **_kw: bars)

    result = service.run_backtest(symbols=['600000'], start_date='2026-06-06')
    assert result['data_coverage']['600000']['window_truncated'] is False
    result = service.run_backtest(symbols=['600000'], start_date='2026-06-05')
    assert result['data_coverage']['600000']['window_truncated'] is True


def test_end_only_window_excludes_warmup_from_reported_coverage(monkeypatch):
    from datetime import date, timedelta
    from src.core.backtest import service

    end = date(2026, 6, 30)
    loaded = [
        PriceBar(date=(end - timedelta(days=offset)).isoformat(), open=10, high=10,
                 low=10, close=10, volume=1000)
        for offset in range(60, -1, -1)
    ]
    monkeypatch.setattr(service, 'load_price_history', lambda *_a, **_kw: loaded)
    result = service.run_backtest(symbols=['600000'], end_date=end.isoformat(), days=30)

    coverage = result['data_coverage']['600000']
    assert coverage['bars_through_end'] == 61
    assert coverage['bars_in_window'] == 31
    assert coverage['window_truncated'] is False


def test_run_backtest_skips_short_history(monkeypatch):
    from src.core.backtest import service

    monkeypatch.setattr(service, "load_price_history", lambda *_a, **_kw: [])
    res = service.run_backtest(symbols=["600000"])
    assert res["trades"] == [] and res["skipped_symbols"] == ["600000"]


def test_backtest_router_importable():
    from src.web.api.backtest import router

    assert any(r.path == "/run" for r in router.routes)


def test_invalid_dates_use_the_same_schema_error():
    import pytest
    from pydantic import ValidationError
    from src.web.api.backtest import BacktestIn

    items = []
    for invalid in ('2026-02-30', '2026-02-30T00:00:00', '2026-11-27T25:00:00', 'not-a-date'):
        with pytest.raises(ValidationError) as caught:
            BacktestIn.model_validate({'end_date': invalid})
        error, = caught.value.errors()
        items.append((error['loc'], error['type'], error['msg']))
    assert len(set(items)) == 1 and 'YYYY-MM-DD' in items[0][-1]


def test_window_before_lookback_cap_is_not_a_source_gap(monkeypatch):
    import pytest
    from src.core.backtest import service

    monkeypatch.setattr(service, 'load_price_history', lambda *_a, **_kw: pytest.fail('window is outside the query budget'))
    coverage = service.run_backtest(symbols=['600000'], end_date='2005-01-01')['data_coverage']['600000']
    assert coverage['status'] == 'window_before_history_cutoff'
    assert coverage['window_truncated'] is True
    assert coverage['left_edge_capped'] is True


def test_history_cutoff_uses_bar_persistence_timezone_for_every_market():
    from datetime import date, datetime, timezone
    from src.core.backtest.data_adapter import history_cutoff_day
    from src.models.market import MarketCode

    # Daily bars are persisted at Shanghai midnight for all markets, so the label
    # boundary follows Shanghai midnight rather than the exchange's own session.
    utc = datetime(2026, 11, 27, 18, tzinfo=timezone.utc)  # 02:00 Shanghai on the 28th.
    assert history_cutoff_day(1, utc) == date(2026, 11, 27)


def test_ancient_start_is_capped(monkeypatch):
    from src.core.backtest import service
    from src.core.backtest.data_adapter import PriceBar

    seen = {}

    def load(_symbol, _market, days=250, **_kw):
        seen['days'] = days
        return [PriceBar(date='1990-01-02', open=10, high=10, low=10, close=10, volume=1000)] * 40

    monkeypatch.setattr(service, 'load_price_history', load)
    result = service.run_backtest(symbols=['600000'], start_date='1990-01-01')
    assert seen['days'] == service.MAX_HISTORY_DAYS
    assert result['params']['history_capped'] is True


def test_end_coverage_uses_last_bar_inside_requested_window(monkeypatch):
    from dataclasses import replace
    from src.core.backtest import service

    sample = _bars()[:30]
    later = replace(sample[-1], date='2026-07-06')
    monkeypatch.setattr(service, 'load_price_history', lambda *_a, **_kw: [*sample, later])
    result = service.run_backtest(symbols=['600000'], end_date='2026-07-03')

    coverage = result['data_coverage']['600000']
    assert coverage['status'] == 'usable'
    assert coverage['loaded_last'] == '2026-07-06'
    assert coverage['bars_in_window'] == len(sample)
    assert coverage['end_after_last_bar'] is True


def test_old_window_widens_pg_cutoff_not_collector_bar_budget(monkeypatch):
    from datetime import datetime, timedelta, timezone
    from src.collectors import kline_collector
    from src.core.backtest import service
    from src.db import klines_repo

    calls = {}

    def read(_symbol, _market, days, **_kw):
        calls['pg_days'] = days
        return []

    def collect(_self, _symbol, days, adjust='qfq'):
        calls['collector_bars'] = days
        assert adjust == 'qfq'
        return []

    monkeypatch.setattr(klines_repo, 'load_qfq_bars', read)
    monkeypatch.setattr(kline_collector.KlineCollector, 'get_klines', collect)
    end = (datetime.now(timezone.utc).date() - timedelta(days=2400)).isoformat()
    result = service.run_backtest(symbols=['600000'], end_date=end, days=800)

    assert calls == {'pg_days': 3260, 'collector_bars': 800}
    assert result['data_coverage']['600000']['status'] == 'history_unavailable'


def test_volume_without_positive_evidence_is_disclosed_in_window(monkeypatch):
    from dataclasses import replace
    from typing import cast
    from src.core.backtest import service

    prefix = _bars()[:30]
    bars = [*prefix, replace(prefix[0], date='2026-07-01', volume=0.0),
            replace(prefix[0], date='2026-07-02', volume=cast(float, None)),
            replace(prefix[0], date='2026-07-03', volume=0.0)]
    monkeypatch.setattr(service, 'load_price_history', lambda *_a, **_kw: bars)
    coverage = service.run_backtest(symbols=['600000'], start_date='2026-07-01', end_date='2026-07-02')['data_coverage']['600000']
    assert coverage['bars_in_window'] == 2
    assert coverage['nonpositive_or_unknown_volume_bars'] == 2


def test_invalid_prices_and_market_adjusted_end_are_disclosed(monkeypatch):
    from dataclasses import replace
    from src.core.backtest import service

    prefix = _bars()[:30]
    bars = [*prefix, replace(prefix[0], date='2026-11-20', close=float('nan'))]
    monkeypatch.setattr(service, 'load_price_history', lambda *_a, **_kw: bars)
    coverage = service.run_backtest(symbols=['600000'], start_date='2026-11-20', end_date='2026-11-21')['data_coverage']['600000']
    assert coverage['invalid_ohlc_bars'] == 1
    assert coverage['expected_last_trading_date'] == '2026-11-20'
    assert coverage['end_after_last_bar'] is False


def test_invalid_warmup_close_cannot_create_a_golden_cross(monkeypatch):
    from datetime import date, timedelta
    from src.core.backtest import service

    first = date(2026, 10, 5)
    bars = [PriceBar((first + timedelta(days=i)).isoformat(), 10, 11, 9, 0 if i == 21 else 10, 1000)
            for i in range(50)]
    monkeypatch.setattr(service, 'load_price_history', lambda *_a, **_kw: bars)
    result = service.run_backtest(symbols=['600000'], start_date=bars[22].date)
    assert result['signals'] == 0
    assert result['data_coverage']['600000']['invalid_warmup_ohlc_bars'] == 1


def test_implicit_window_does_not_claim_full_coverage_for_short_history(monkeypatch):
    from datetime import datetime, timezone
    from src.core.backtest import service

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 11, 27, 9, tzinfo=timezone.utc).astimezone(tz)

    monkeypatch.setattr(service, 'datetime', Clock)
    monkeypatch.setattr(service, 'load_price_history', lambda *_a, **_kw: _bars()[:30])
    recent = service.run_backtest(symbols=['600000'], days=450)['data_coverage']['600000']
    deep = service.run_backtest(symbols=['600000'], days=2000)['data_coverage']['600000']
    assert recent['window_truncated'] is True
    assert deep['window_truncated'] is None  # Static calendar does not cover the cutoff.


def test_inside_window_gap_is_separate_from_covered_edges(monkeypatch):
    from datetime import date, timedelta
    from src.core.backtest import service
    from src.core.trading_calendar import is_trading_day

    first, last = date(2026, 10, 12), date(2026, 11, 26)
    dates = [first + timedelta(days=i) for i in range((last - first).days + 1)]
    missing = date(2026, 11, 10)
    bars = [PriceBar(d.isoformat(), 10, 11, 9, 10, 1000) for d in dates if is_trading_day(d) and d != missing]
    monkeypatch.setattr(service, 'load_price_history', lambda *_a, **_kw: [*bars[:-1], bars[-2], bars[-1]])
    result = service.run_backtest(symbols=['600000'], start_date=first.isoformat(), end_date=last.isoformat())
    coverage = result['data_coverage']['600000']
    assert coverage['window_truncated'] is False and coverage['end_after_last_bar'] is False
    assert coverage['missing_sessions_between_bars'] == 1


def test_collector_trim_reports_observed_outside_window_instead_of_no_history(monkeypatch):
    from datetime import datetime, timezone
    from src.collectors import kline_collector
    from src.core.backtest import data_adapter, service
    from src.db import klines_repo

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 11, 23, 8, tzinfo=timezone.utc).astimezone(tz)

    monkeypatch.setattr(data_adapter, 'datetime', Clock)
    monkeypatch.setattr(service, 'datetime', Clock)
    monkeypatch.setattr(klines_repo, 'load_qfq_bars', lambda *_a, **_kw: [])
    monkeypatch.setattr(kline_collector.KlineCollector, 'get_klines', lambda *_a, **_k: [
        PriceBar('2025-01-02', 10, 11, 9, 10, 1000),
    ])
    result = service.run_backtest(symbols=['600000'], days=30)
    coverage = result['data_coverage']['600000']
    assert coverage['status'] == 'history_before_lookback'
    assert coverage['latest_fallback_bar_before_cutoff'] == '2025-01-02'
    assert coverage['bars_in_window'] == 0 and result['trades'] == []


def test_gap_count_keeps_verified_segment_when_early_calendar_is_unknown(monkeypatch):
    from src.core.backtest import service

    bars = [PriceBar(d, 10, 11, 9, 10, 1000) for d in ('2024-12-30', '2025-01-02', '2025-01-06')]
    monkeypatch.setattr(service, 'load_price_history', lambda *_a, **_kw: bars)
    coverage = service.run_backtest(symbols=['600000'], start_date='2024-12-30', end_date='2025-01-06')['data_coverage']['600000']
    assert coverage['missing_sessions_between_bars'] == 1  # Jan 3; Jan 1 is a holiday.
    assert coverage['calendar_unverified_days_between_bars'] == 2


def test_backtest_passes_same_calendar_cutoff_to_database_and_fallback(monkeypatch):
    from datetime import date, datetime, timezone
    from src.collectors import kline_collector
    from src.core.backtest import data_adapter
    from src.db import klines_repo

    def no_rows(_symbol, _market, days, *, date_cutoff=None, date_ceiling=None):
        assert days == 1 and date_cutoff == date(2026, 11, 27)
        assert date_ceiling == date(2026, 11, 27)  # 18:00Z is 02:00 Shanghai, so the 27th is the last final bar.
        return []

    last = PriceBar('2026-11-27', 10, 11, 9, 10, 1000.0)
    monkeypatch.setattr(klines_repo, 'load_qfq_bars', no_rows)
    monkeypatch.setattr(kline_collector.KlineCollector, 'get_klines', lambda *_a, **_k: [last])
    rows = data_adapter.load_price_history('600000', 'CN', days=1,
                                           as_of=datetime(2026, 11, 27, 18, tzinfo=timezone.utc))
    assert rows == [last]


def test_repeated_symbol_is_deduplicated_before_sizing(monkeypatch):
    from src.core.backtest import service

    monkeypatch.setattr(service, 'load_price_history', lambda *_a, **_kw: _bars())
    once = service.run_backtest(symbols=['600000'])
    twice = service.run_backtest(symbols=['600000', '600000'])
    assert twice['signals'] == once['signals'] and twice['trades'] == once['trades']
    assert list(twice['data_coverage']) == ['600000']
    overflow = service.run_backtest(symbols=[f'6000{i:02d}' for i in range(23)])
    assert len(overflow['skipped_symbols']) == 3


def test_naive_as_of_is_utc_not_host_local():
    from datetime import date, datetime
    from src.core.backtest.data_adapter import history_cutoff_day, history_window

    naive = datetime(2026, 11, 27, 18)
    assert history_cutoff_day(1, naive) == date(2026, 11, 27)
    # 02:00 Shanghai on the 28th: the first label is the 28th but the last final session is the 27th.
    assert history_window('CN', 0, naive) == (date(2026, 11, 27), date(2026, 11, 27))


def test_history_window_caps_bars_newer_than_as_of(monkeypatch):
    from datetime import datetime, timezone
    from src.collectors import kline_collector
    from src.core.backtest import data_adapter
    from src.db import klines_repo

    seen = {}

    def repo(_symbol, _market, days, *, date_cutoff=None, date_ceiling=None):
        seen['ceiling'] = date_ceiling
        return []

    bars = [PriceBar(d, 10, 11, 9, 10, 1000) for d in ('2026-11-26', '2026-11-27', '2026-11-30')]
    monkeypatch.setattr(klines_repo, 'load_qfq_bars', repo)
    monkeypatch.setattr(kline_collector.KlineCollector, 'get_klines', lambda *_a, **_k: bars)
    rows = data_adapter.load_price_history('600000', 'CN', days=30, as_of=datetime(2026, 11, 27, 9, tzinfo=timezone.utc))
    assert seen['ceiling'].isoformat() == '2026-11-27'
    assert [bar.date for bar in rows] == ['2026-11-26', '2026-11-27']


def test_short_lookback_still_clears_the_bar_floor(monkeypatch):
    from src.core.backtest import service

    requested = {}
    monkeypatch.setattr(service, 'load_price_history',
                        lambda _s, _m, days=0, **_kw: requested.setdefault('days', days) and _bars())
    service.run_backtest(symbols=['600000'], days=1)
    assert requested['days'] >= service.MIN_HISTORY_DAYS >= service.MIN_BARS


def test_legacy_history_reader_keeps_collector_reply_without_raising(monkeypatch):
    from src.collectors import kline_collector
    from src.core.backtest import data_adapter
    from src.db import klines_repo

    old = PriceBar('2024-12-30', 10, 11, 9, 10, 1000)
    monkeypatch.setattr(klines_repo, 'load_qfq_bars', lambda *_a, **_kw: [])
    monkeypatch.setattr(kline_collector.KlineCollector, 'get_klines', lambda *_a, **_kw: [old])
    assert data_adapter.load_price_history('600000', 'CN', days=1) == [old]


def test_last_closed_session_excludes_the_intraday_provisional_bar():
    from datetime import date, datetime, timezone
    from src.core.backtest.data_adapter import last_closed_session

    # 13:00Z is 21:00 Shanghai: the session bar is final.
    assert last_closed_session('CN', datetime(2026, 11, 27, 13, tzinfo=timezone.utc)) == date(2026, 11, 27)
    # 02:00Z is 10:00 Shanghai: today's bar is still being rewritten.
    assert last_closed_session('CN', datetime(2026, 11, 27, 2, tzinfo=timezone.utc)) == date(2026, 11, 26)
    # Saturday has no session of its own.
    assert last_closed_session('CN', datetime(2026, 11, 21, 4, tzinfo=timezone.utc)) == date(2026, 11, 20)


def test_final_bar_uses_market_clock_and_fails_conservatively_without_calendar():
    from datetime import date, datetime, timezone
    from src.core.backtest.data_adapter import last_closed_session

    assert last_closed_session('HK', datetime(2026, 11, 27, 7, 30, tzinfo=timezone.utc)) == date(2026, 11, 26)
    # Shanghai is already Saturday, but the Friday session in New York is still open.
    assert last_closed_session('US', datetime(2026, 11, 27, 18, tzinfo=timezone.utc)) == date(2026, 11, 26)
    assert last_closed_session('CN', datetime(2024, 11, 27, 2, tzinfo=timezone.utc)) == date(2024, 11, 26)


def test_newer_than_ceiling_bars_are_not_reported_as_old_history(monkeypatch):
    from datetime import datetime, timezone
    import pytest
    from src.collectors import kline_collector
    from src.core.backtest import data_adapter, service
    from src.db import klines_repo

    recent = [PriceBar('2026-12-10', 10, 11, 9, 10, 1000)]
    monkeypatch.setattr(klines_repo, 'load_qfq_bars', lambda *_a, **_kw: [])
    monkeypatch.setattr(kline_collector.KlineCollector, 'get_klines', lambda *_a, **_k: recent)
    clock = datetime(2026, 11, 27, 9, tzinfo=timezone.utc)
    with pytest.raises(data_adapter.HistoryAfterWindow) as error:
        data_adapter.load_price_history('600000', 'CN', days=30, as_of=clock)
    assert error.value.first_date == '2026-12-10'
    monkeypatch.setattr(service, 'load_price_history',
                        lambda *a, **kw: data_adapter.load_price_history(*a, days=kw['days'], as_of=clock))
    report = service.run_backtest(symbols=['600000'])
    coverage = report['data_coverage']['600000']
    assert coverage['status'] == 'history_starts_after_window'
    assert coverage['first_fallback_bar_after_ceiling'] == '2026-12-10'
    assert report['price_adjustment_basis'] == 'qfq_visible_at_query_time_not_point_in_time'


def test_metrics_basis_is_declared(monkeypatch):
    from src.core.backtest import service

    monkeypatch.setattr(service, 'load_price_history', lambda *_a, **_kw: _bars())
    basis = service.run_backtest(symbols=['600000'])['metrics_basis']
    assert basis['trades_win_rate_profit_factor_total_pnl'] == 'realized_closed_only'
    assert basis['total_return_max_drawdown_sharpe'] == 'mark_to_market_equity'


def test_capped_fallback_outside_ceiling_reports_budget_not_gap(monkeypatch):
    from datetime import datetime, timezone
    from src.collectors import kline_collector
    from src.core.backtest import data_adapter, service
    from src.db import klines_repo

    monkeypatch.setattr(data_adapter, 'MAX_FALLBACK_BARS', 1)
    monkeypatch.setattr(klines_repo, 'load_qfq_bars', lambda *_a, **_kw: [])
    monkeypatch.setattr(kline_collector.KlineCollector, 'get_klines',
                        lambda *_a, **_kw: [PriceBar('2026-12-10', 10, 11, 9, 10, 1000)])

    class AfterFetch(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 12, 11, 9, tzinfo=timezone.utc)

    monkeypatch.setattr(service, 'datetime', AfterFetch)
    row = service.run_backtest(symbols=['600000'], end_date='2026-11-27')['data_coverage']['600000']
    assert row['status'] == 'fallback_budget_exhausted_before_window'
    assert row['first_fallback_bar_after_ceiling'] == '2026-12-10'


def test_unclosed_requested_end_is_not_missing_vendor_history(monkeypatch):
    from datetime import datetime, timezone
    from src.core.backtest import service

    class Intraday(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 11, 27, 2, tzinfo=timezone.utc)

    bars = _bars() + [PriceBar('2026-11-26', 10, 11, 9, 10, 1000)]
    monkeypatch.setattr(service, 'datetime', Intraday)
    monkeypatch.setattr(service, 'load_price_history', lambda *_a, **_kw: bars)
    result = service.run_backtest(symbols=['600000'], end_date='2026-11-27')
    row = result['data_coverage']['600000']
    assert row['expected_last_trading_date'] == '2026-11-27'
    assert row['observable_end_date'] == '2026-11-26'
    assert row['end_after_last_bar'] is False
    assert result['sample_window']['requested_end'] == '2026-11-27'
    assert result['sample_window']['effective_end'] == '2026-11-26'


def test_valid_close_can_drive_signal_when_warmup_open_is_invalid(monkeypatch):
    from dataclasses import replace
    from src.core.backtest import service

    bars = _bars()
    monkeypatch.setattr(service, 'load_price_history', lambda *_a, **_kw: bars)
    expected = service.run_backtest(symbols=['600000'])['signals']
    assert expected > 0
    bars[30] = replace(bars[30], open=float('nan'))
    actual = service.run_backtest(symbols=['600000'])
    assert actual['signals'] == expected and actual['data_coverage']['600000']['invalid_ohlc_bars'] == 1
