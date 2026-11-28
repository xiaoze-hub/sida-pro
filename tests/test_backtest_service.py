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
    assert seen['days'] >= 1200
    assert result['data_coverage']['600000']['status'] == 'usable'
    assert result['data_coverage']['600000']['window_truncated'] is False


def test_run_backtest_skips_short_history(monkeypatch):
    from src.core.backtest import service

    monkeypatch.setattr(service, "load_price_history", lambda code, mkt, days=800: [])
    res = service.run_backtest(symbols=["600000"])
    assert res["trades"] == [] and res["skipped_symbols"] == ["600000"]


def test_backtest_router_importable():
    from src.web.api.backtest import router

    assert any(r.path == "/run" for r in router.routes)
