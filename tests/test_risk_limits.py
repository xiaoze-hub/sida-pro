"""W3/B3.1-B3.2: 组合级风控闸门(回撤熔断/持仓只数/单票/总敞口)。"""

from __future__ import annotations

from src.core.risk_limits import RiskLimits, check_entry, load_risk_limits


def _limits(**kw) -> RiskLimits:
    base = dict(halt_drawdown=0.20, max_single=0.20, max_exposure=0.90, max_positions=20)
    base.update(kw)
    return RiskLimits(**base)


def test_check_entry_allows_normal_case():
    assert check_entry(
        limits=_limits(), account_drawdown_pct=0.05, equity=100_000.0,
        position_value=30_000.0, new_position_value=10_000.0, position_count=3,
    ) is None


def test_check_entry_halts_on_drawdown():
    assert check_entry(
        limits=_limits(), account_drawdown_pct=0.25, equity=80_000.0,
        position_value=0.0, new_position_value=1_000.0, position_count=0,
    ) == "drawdown_halt"


def test_check_entry_blocks_single_name_and_exposure():
    # 单票 25% > 20% 上限
    assert check_entry(
        limits=_limits(), account_drawdown_pct=0.0, equity=100_000.0,
        position_value=10_000.0, new_position_value=25_000.0, position_count=1,
    ) == "max_single"
    # 总敞口 85% + 10% > 90%
    assert check_entry(
        limits=_limits(), account_drawdown_pct=0.0, equity=100_000.0,
        position_value=85_000.0, new_position_value=10_000.0, position_count=5,
    ) == "max_exposure"


def test_check_entry_blocks_position_count_and_zero_equity():
    assert check_entry(
        limits=_limits(max_positions=2), account_drawdown_pct=0.0, equity=100_000.0,
        position_value=10_000.0, new_position_value=1_000.0, position_count=2,
    ) == "max_positions"
    assert check_entry(
        limits=_limits(), account_drawdown_pct=0.0, equity=0.0,
        position_value=0.0, new_position_value=1_000.0, position_count=0,
    ) == "no_equity"


def test_load_risk_limits_reads_env(monkeypatch):
    monkeypatch.setenv("SIDA_RISK_HALT_DRAWDOWN", "0.15")
    monkeypatch.setenv("SIDA_RISK_MAX_SINGLE", "0.10")
    monkeypatch.setenv("SIDA_RISK_MAX_EXPOSURE", "0.80")
    monkeypatch.setenv("SIDA_RISK_MAX_POSITIONS", "8")
    lim = load_risk_limits()
    assert (lim.halt_drawdown, lim.max_single, lim.max_exposure, lim.max_positions) == (
        0.15, 0.10, 0.80, 8,
    )


def test_alert_thresholds_from_env(monkeypatch):
    """W3/B3.4: 盘中监测的止损/止盈预警阈值可配置。"""
    monkeypatch.setenv("SIDA_ALERT_STOP_LOSS_PCT", "-6.5")
    monkeypatch.setenv("SIDA_ALERT_TAKE_PROFIT_PCT", "12.5")
    from src.agents.intraday_monitor import IntradayMonitorAgent

    agent = IntradayMonitorAgent()
    assert agent.stop_loss_warning == -6.5
    assert agent.take_profit_warning == 12.5
    # 显式传参优先于环境变量
    agent2 = IntradayMonitorAgent(stop_loss_warning=-3.0, take_profit_warning=5.0)
    assert agent2.stop_loss_warning == -3.0 and agent2.take_profit_warning == 5.0
