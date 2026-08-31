"""Shadow Account 测试: 交割单解析 / FIFO 配对 / 行为画像 / 规则提取 / 归因。"""

from __future__ import annotations

import pandas as pd
import pytest

from src.core.shadow_account import (
    compute_behavior,
    compute_profile,
    extract_shadow_profile,
    pair_trades_fifo,
    parse_file,
    records_to_dataframe,
)
from src.core.shadow_account.backtester import run_shadow_attribution
from src.core.shadow_account.extractor import MIN_PROFITABLE_ROUNDTRIPS
from src.core.shadow_account.parsers import TradeRecord


# ---------------- Fixtures ----------------

def _make_journal_csv(path, rows: list[tuple]) -> str:
    """构造同花顺格式交割单 CSV;返回格式名。"""
    import csv

    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["成交时间", "证券代码", "证券名称", "操作", "成交数量", "成交价格", "成交金额", "手续费", "印花税", "过户费"])
        for r in rows:
            w.writerow(r)
    return "tonghuashun"


def _roundtrip_csv(path, n: int = 10, *, wins: int | None = None) -> str:
    """构造 n 个完整买卖回合(每股一次),部分盈利。"""
    wins = wins if wins is not None else max(3, n // 2)
    rows = []
    for i in range(n):
        code = f"600{500 + i:03d}"
        price = 10.0 + i
        side_sell = "卖出" if i < wins else "买入"  # 占位,下面覆盖
        rows.append(
            ("2026-07-01 09:30:00", code, "测试股份", "买入", 100, price, price * 100, 5, 1, 0.5)
        )
        rows.append(
            ("2026-07-03 14:30:00", code, "测试股份", "卖出", 100, price + (2.0 if i < wins else -1.0), 0, 5, 1, 0.5)
        )
    return _make_journal_csv(path, rows)


# ---------------- 解析器 ----------------

def test_parse_tonghuashun(tmp_path):
    p = tmp_path / "ths.csv"
    _make_journal_csv(p, [
        ("2026-07-01 09:30:00", "600519", "贵州茅台", "买入", 100, 1500.0, 150000.0, 5, 1, 0.5),
        ("2026-07-03 14:30:00", "600519", "贵州茅台", "卖出", 100, 1600.0, 160000.0, 5, 1, 0.5),
    ])
    fmt, records = parse_file(p)
    assert fmt == "tonghuashun"
    assert len(records) == 2
    r = records[0]
    assert r.symbol == "600519.SH"
    assert r.side == "buy"
    assert r.market == "china_a"
    assert r.quantity == 100.0


def test_parse_eastmoney(tmp_path):
    import csv

    p = tmp_path / "em.csv"
    with open(p, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["成交日期", "成交时间", "股票代码", "股票名称", "买卖标志", "成交数量", "成交均价", "成交金额", "佣金", "印花税"])
        w.writerow(["20260701", "09:35:00", "000001", "平安银行", "B", 200, 11.0, 2200.0, 1.0, 0.5])
        w.writerow(["20260703", "14:00:00", "000001", "平安银行", "S", 200, 12.0, 2400.0, 1.0, 0.5])
    fmt, records = parse_file(p)
    assert fmt == "eastmoney"
    assert records[0].symbol == "000001.SZ"
    assert records[0].side == "buy"
    assert records[1].side == "sell"


def test_records_to_dataframe_sorts_by_datetime():
    records = [
        TradeRecord("2026-07-03 10:00:00", "600519.SH", "x", "sell", 1, 1.0, 1.0, 0.0, "china_a"),
        TradeRecord("2026-07-01 10:00:00", "600519.SH", "x", "buy", 1, 1.0, 1.0, 0.0, "china_a"),
    ]
    df = records_to_dataframe(records)
    assert df.iloc[0]["side"] == "buy"


# ---------------- FIFO 配对 ----------------

def test_pair_trades_fifo_basic():
    df = records_to_dataframe([
        TradeRecord("2026-07-01 09:30:00", "600519.SH", "x", "buy", 100, 10.0, 1000.0, 1.0, "china_a"),
        TradeRecord("2026-07-02 09:30:00", "600519.SH", "x", "buy", 50, 11.0, 550.0, 1.0, "china_a"),
        TradeRecord("2026-07-03 09:30:00", "600519.SH", "x", "sell", 120, 12.0, 1440.0, 1.0, "china_a"),
    ])
    rts = pair_trades_fifo(df)
    # 卖出 120 股 = 100 股 @10(第一批)+ 20 股 @11(第二批),剩 30 股 @11 未平
    assert len(rts) == 2
    rt0 = rts[0]
    assert rt0["qty"] == 100
    assert rt0["buy_price"] == 10.0
    assert rt0["pnl"] > 0
    rt1 = rts[1]
    assert rt1["qty"] == 20
    assert rt1["buy_price"] == 11.0
    assert rt1["pnl"] > 0


# ---------------- 画像 + 行为 ----------------

def test_compute_profile_and_behavior(tmp_path):
    p = tmp_path / "j.csv"
    _roundtrip_csv(p, n=8, wins=5)
    _, records = parse_file(p)
    df = records_to_dataframe(records)
    profile = compute_profile(df)
    assert profile["total_trades"] == 16
    assert profile["total_roundtrips"] == 8
    assert profile["win_rate"] == pytest.approx(5 / 8, abs=0.01)

    behavior = compute_behavior(df)
    assert "disposition_effect" in behavior
    assert "overtrading" in behavior
    assert "chasing_momentum" in behavior
    assert "anchoring" in behavior


# ---------------- 规则提取 ----------------

def test_extract_shadow_profile(tmp_path):
    p = tmp_path / "j.csv"
    # 10 笔盈利 + 4 笔亏损,确保盈利 >= 5
    _roundtrip_csv(p, n=14, wins=10)
    profile = extract_shadow_profile(p)
    assert profile.shadow_id.startswith("shadow_")
    assert profile.profitable_roundtrips >= MIN_PROFITABLE_ROUNDTRIPS
    assert len(profile.rules) >= 1
    rule = profile.rules[0]
    assert rule.human_text  # 非空
    assert rule.holding_days_range[1] >= rule.holding_days_range[0]
    assert rule.support_count >= 1


def test_extract_insufficient_profitable(tmp_path):
    p = tmp_path / "j.csv"
    _roundtrip_csv(p, n=4, wins=1)  # 仅 1 笔盈利 < 5
    with pytest.raises(ValueError, match="Insufficient profitable roundtrips"):
        extract_shadow_profile(p)


# ---------------- 归因 ----------------

def test_run_shadow_attribution(tmp_path):
    p = tmp_path / "j.csv"
    _roundtrip_csv(p, n=10, wins=6)
    profile = extract_shadow_profile(p)
    attribution, shadow_pnl, real_pnl = run_shadow_attribution(profile, p)
    assert attribution.missed_signals_pnl + attribution.noise_trades_pnl + attribution.early_exit_pnl + attribution.late_exit_pnl + attribution.overtrading_pnl == pytest.approx(
        shadow_pnl - real_pnl, abs=0.5
    )
    assert isinstance(shadow_pnl, float)
    assert isinstance(real_pnl, float)


def test_attribution_counterfactual_shape(tmp_path):
    p = tmp_path / "j.csv"
    _roundtrip_csv(p, n=12, wins=7)
    profile = extract_shadow_profile(p)
    attribution, _, _ = run_shadow_attribution(profile, p)
    for t in attribution.counterfactual_trades:
        assert {"symbol", "buy_dt", "sell_dt", "hold_days", "pnl", "impact", "reason"} <= set(t.keys())


# ---------------- 汇总结果 ----------------

def test_summarize_result_shape(tmp_path):
    from src.core.shadow_account.backtester import summarize_result

    p = tmp_path / "j.csv"
    _roundtrip_csv(p, n=8, wins=5)
    profile = extract_shadow_profile(p)
    attribution, shadow_pnl, real_pnl = run_shadow_attribution(profile, p)
    result = summarize_result(profile, attribution, shadow_pnl, real_pnl)
    d = result.to_dict()
    assert d["delta_pnl"] == pytest.approx(shadow_pnl - real_pnl, abs=0.01)
    assert "china_a" in d["per_market"]
