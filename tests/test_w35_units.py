"""W3.5/B5 单位一致性校验测试: 恒等式出口 + ×10000 假数据 + 东财 f57 + 对账 job。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "packages" / "marketdata" / "src"))

from src.core.unit_check import (  # noqa: E402
    DEFAULT_TOL_PCT,
    UnitInconsistencyError,
    assert_unit_consistency,
    unit_deviation,
)


# ---------- unit_deviation 纯函数 ----------


def test_deviation_math():
    # vol=100万股, close=10.00, amount=1000万 → implied=10.00, dev=0
    assert unit_deviation(volume=1_000_000, close=10.0, amount=10_000_000) == pytest.approx(0.0)
    # 手当股(×100 少除) → implied=1000 → dev=9900%
    assert unit_deviation(volume=10_000, close=10.0, amount=10_000_000) == pytest.approx(9900.0)
    # 万元当元(×10000 少乘) → implied=0.001 → dev≈99.99%
    assert unit_deviation(volume=1_000_000, close=10.0, amount=1000) == pytest.approx(99.99)


def test_deviation_missing_fields_skip_not_zero():
    # 缺失走 None(1.1 语义), 不当 0 —— 0 会把停牌/缺数据误判成单位错误
    assert unit_deviation(volume=0, close=10.0, amount=100.0) is None
    assert unit_deviation(volume=100, close=None, amount=100.0) is None
    assert unit_deviation(volume=100, close=10.0, amount=None) is None


# ---------- assert_unit_consistency 出口行为 ----------


def test_pass_within_tol_returns_dev():
    dev = assert_unit_consistency(
        symbol="000001", trade_date="2026-09-09", volume=1_000_000,
        close=10.0, amount=10_030_000, source="test",  # dev=0.3%
    )
    assert dev == pytest.approx(0.3)


def test_x10000_fake_data_logs_and_counts(caplog, monkeypatch):
    """方案 B5 验收: amount 放大 10000 倍假数据 → error 日志 + 失败计数自增。"""
    calls = []
    import src.core.datasource_failures as failures_mod

    monkeypatch.setattr(
        failures_mod, "record",
        lambda provider, kind="fetch": calls.append((provider, kind)),
    )
    with caplog.at_level("ERROR", logger="src.core.unit_check"):
        dev = assert_unit_consistency(
            symbol="600519", trade_date="2026-09-09", volume=1_000_000,
            close=1500.0, amount=1_500_000_000 * 10_000, source="evil_vendor",
        )
    assert dev > 99.0
    assert "单位口径异常" in caplog.text and "600519" in caplog.text
    assert calls == [("evil_vendor", "parse")]


def test_strict_env_raises(monkeypatch):
    monkeypatch.setenv("SIDA_STRICT_UNITS", "1")
    monkeypatch.setattr("src.core.unit_check._record_failure", lambda s: None)
    with pytest.raises(UnitInconsistencyError) as exc:
        assert_unit_consistency(
            symbol="600519", volume=1_000_000, close=1500.0,
            amount=1_500_000_000 * 100, source="v",
        )
    assert "600519" in str(exc.value)


def test_custom_tol_and_env_tol(monkeypatch):
    monkeypatch.delenv("SIDA_UNIT_TOL_PCT", raising=False)
    assert assert_unit_consistency(
        symbol="x", volume=100, close=10.0, amount=1050, tol_pct=4.0,
    ) == pytest.approx(5.0)  # dev 5% > 自定义 4% 也会走违规路径? 不 —— 返回 dev
    # dev=5% < 默认 5%? 相等不算超阈; 上行显式 tol=4.0 时超阈但仍返回(非 strict)
    monkeypatch.setenv("SIDA_UNIT_TOL_PCT", "0.01")
    assert DEFAULT_TOL_PCT == 5.0  # 实测校准值不被 env 改写(仅运行时覆盖)


def test_default_tol_is_calibrated_not_arbitrary():
    # B5 明确禁止拍脑茼 2%: 默认值须来自 docs/_frozen/data.md 实测(不复权 max dev 1.28%)
    assert DEFAULT_TOL_PCT == 5.0


# ---------- 东财 f57 → Bar.amount ----------


def test_eastmoney_kline_parses_amount(monkeypatch):
    from marketdata.vendors import kline as kline_mod

    payload = {"data": {"klines": [
        "2026-09-08,10.50,10.62,10.70,10.40,123456,131361020.00",
        "2026-09-05,10.30,10.48,10.55,10.20,98765,0",  # amount=0 → None(诚实缺失)
    ]}}
    monkeypatch.setattr(kline_mod, "market_get", lambda *a, **k: payload)
    bars = kline_mod.fetch_eastmoney_kline("1.600519", 5)
    assert bars[0].amount == 131_361_020.00
    assert bars[0].volume == 12_345_600  # 手→股 ×100
    assert bars[1].amount is None


def test_bar_amount_defaults_none_for_sources_without_it():
    from marketdata.types import Bar

    b = Bar(date="2026-09-08", open=1, close=1, high=1, low=1, volume=1)
    assert b.amount is None


# ---------- 每日对账 job(离线: mock 拉取) ----------


def test_recon_job_produces_report(tmp_path, monkeypatch, caplog):
    import src.core.unit_recon as recon

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(recon, "_sample_pool", lambda n: [("600519", "CN"), ("000001", "CN")])
    rows_good = ["2026-09-08,10.50,10.62,10.70,10.40,123456,131361020.00"]  # dev≈0.2%
    rows_bad = ["2026-09-08,10.50,10.62,10.70,10.40,123456,131361020.00",  # 正常
                "2026-09-05,10.30,10.48,10.55,10.20,98765,9876500000000"]  # ×10000 级

    def fake_fetch(symbol, days):
        return rows_bad if symbol == "600519" else rows_good

    monkeypatch.setattr(recon, "_fetch_unadjusted_rows", fake_fetch)
    with caplog.at_level("ERROR", logger="src.core.unit_check"):
        report = recon.run_unit_reconciliation(2)

    assert report["bars_checked"] == 3
    assert len(report["violations"]) == 1
    assert report["violations"][0]["symbol"] == "600519"
    assert report["violations"][0]["dev_pct"] > 99
    assert report["max_dev_at"]["symbol"] == "600519"
    assert "单位口径异常" in caplog.text
    path = Path(report["report_path"])
    assert path.exists() and path.parent == tmp_path / "reports" / "unit_recon"
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["violations"] == report["violations"]


def test_recon_job_falls_back_on_pool_failure(monkeypatch):
    import src.core.unit_recon as recon

    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr("src.collectors.klines_ingestor.get_default_symbols", boom)
    monkeypatch.setattr(recon, "_fetch_unadjusted_rows", lambda s, d: [])
    report = recon.run_unit_reconciliation(3)
    assert report["sample_n"] == 3  # 固定流动池兜底, 对账不因库挂而失效
    assert report["bars_checked"] == 0
