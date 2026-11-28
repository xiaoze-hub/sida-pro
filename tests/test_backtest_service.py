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

    monkeypatch.setattr(service, "load_price_history", lambda code, mkt, days=800: _bars())
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

    def load(_symbol, _market, days=250):
        seen['days'] = days
        return bars

    monkeypatch.setattr(service, 'load_price_history', load)
    result = service.run_backtest(symbols=['600000'], start_date=start)
    assert seen['days'] >= 1200
    assert result['params']['history_days'] == seen['days']
    assert result['data_coverage']['600000']['status'] == 'usable'
    assert result['data_coverage']['600000']['window_truncated'] is False


def test_end_only_old_window_still_loads(monkeypatch):
    from datetime import date, timedelta
    from src.core.backtest import service
    from src.core.backtest.data_adapter import PriceBar

    seen = {}
    end = (date.today() - timedelta(days=1200)).isoformat()
    bars = [PriceBar(date=end, open=10, high=10, low=10, close=10, volume=1000)] * 40

    def load(_symbol, _market, days=250):
        seen['days'] = days
        return bars

    monkeypatch.setattr(service, 'load_price_history', load)
    result = service.run_backtest(symbols=['600000'], end_date=end)
    assert seen['days'] == 2060
    assert result['data_coverage']['600000']['status'] == 'usable'
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

    monkeypatch.setattr(service, "load_price_history", lambda code, mkt, days=800: [])
    res = service.run_backtest(symbols=["600000"])
    assert res["trades"] == [] and res["skipped_symbols"] == ["600000"]


def test_backtest_router_importable():
    from src.web.api.backtest import router

    assert any(r.path == "/run" for r in router.routes)


def test_ancient_start_is_capped(monkeypatch):
    from src.core.backtest import service
    from src.core.backtest.data_adapter import PriceBar

    seen = {}

    def load(_symbol, _market, days=250):
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
    from datetime import date, timedelta
    from src.collectors import kline_collector
    from src.core.backtest import service
    from src.db import klines_repo

    calls = {}

    def read(_symbol, _market, days):
        calls['pg_days'] = days
        return []

    def collect(_self, _symbol, days, adjust='qfq'):
        calls['collector_bars'] = days
        assert adjust == 'qfq'
        return []

    monkeypatch.setattr(klines_repo, 'load_qfq_bars', read)
    monkeypatch.setattr(kline_collector.KlineCollector, 'get_klines', collect)
    end = (date.today() - timedelta(days=2400)).isoformat()
    result = service.run_backtest(symbols=['600000'], end_date=end, days=800)

    assert calls == {'pg_days': 3260, 'collector_bars': 800}
    assert result['data_coverage']['600000']['status'] == 'history_unavailable'
