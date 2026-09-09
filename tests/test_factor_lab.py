"""B6.1: 因子工厂 —— 注册表 + 横截面分层回测。"""

from __future__ import annotations

import pytest

from src.core.factor_lab import (
    FactorSpec,
    cross_section_quantiles,
    factor_fn,
    factor_registry,
    factor_summary,
    register_factor,
)


def test_register_and_lookup():
    @register_factor(FactorSpec(code="demo_alpha", name="示例因子", category="test"))
    def _compute(bars):  # pragma: no cover - 仅登记
        return 0.0

    assert "demo_alpha" in factor_registry()
    assert factor_fn("demo_alpha") is _compute
    with pytest.raises(ValueError):
        register_factor(FactorSpec(code="demo_alpha", name="重复"))


def test_cross_section_quantiles_monotonic():
    # 因子值越大 → 收益越高(完美单调)
    values = {f"S{i}": float(i) for i in range(10)}
    returns = {f"S{i}": float(i) * 0.01 for i in range(10)}
    res = cross_section_quantiles(values, returns, n_quantiles=5)
    assert res["n"] == 10
    assert len(res["quantiles"]) == 5
    assert res["monotonic"] is True
    assert res["long_short"] == pytest.approx(0.08, abs=1e-9)


def test_cross_section_quantiles_reverse_not_monotonic():
    values = {f"S{i}": float(i) for i in range(10)}
    returns = {f"S{i}": -float(i) * 0.01 for i in range(10)}
    res = cross_section_quantiles(values, returns, n_quantiles=5)
    assert res["monotonic"] is False
    assert res["long_short"] == pytest.approx(-0.08, abs=1e-9)


def test_cross_section_ignores_unpaired_and_short():
    assert cross_section_quantiles({"A": 1.0}, {"B": 0.1})["quantiles"] == []
    res = cross_section_quantiles({"A": 1.0, "B": 2.0}, {"A": 0.1, "B": 0.2}, n_quantiles=5)
    assert res["n"] == 2 and len(res["quantiles"]) == 2  # 样本不足按实际分


def test_factor_summary_multi_day():
    daily_values = {d: {f"S{i}": float(i) for i in range(10)} for d in ("2026-09-01", "2026-09-02")}
    daily_returns = {d: {f"S{i}": float(i) * 0.01 for i in range(10)} for d in daily_values}
    out = factor_summary(daily_values, daily_returns, n_quantiles=5)
    assert out["days"] == 2
    assert out["monotonic_ratio"] == 1.0
    assert out["avg_long_short"] == pytest.approx(0.08, abs=1e-9)
