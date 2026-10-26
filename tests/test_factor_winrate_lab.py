"""测试：
1) `market_scan._default_cn_universe` 与 `stock_list._fetch_from_tq` 修复 `universe=0` 事故；
2) `backtest_lab` 五大因子族多维回测、一字涨停剔除、无条件基准超额、幂等落库 `factor_winrate_daily` 与 `get_factor_winrate` 工具。
"""
import asyncio

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.core import backtest_lab, market_scan
from src.db.models import Base, FactorWinrateDaily


@pytest.fixture()
def db():
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng, tables=[FactorWinrateDaily.__table__])
    Session = sessionmaker(bind=eng)
    s = Session()
    try:
        yield s
    finally:
        s.close()


def _synthetic_bars(n: int = 120, base: float = 10.0) -> list[dict]:
    """构造含涨跌波动、突破、弹簧与涨停的合成日线。"""
    import math

    out = []
    c = base
    for i in range(n):
        wave = math.sin(i / 4.0) * 0.35 + (0.02 if i % 9 != 0 else -0.04)
        prev = c
        c = max(3.0, round(prev * (1.0 + wave * 0.08), 2))
        o = round(prev * (1.0 + (0.005 if i % 2 == 0 else -0.005)), 2)
        h = round(max(o, c) * (1.02 if i % 7 != 0 else 1.05), 2)
        l = round(min(o, c) * (0.98 if i % 11 != 0 else 0.94), 2)
        v = 10000.0 * (2.2 if i % 6 == 0 else (0.45 if i % 5 in (1, 2) else 1.0))
        # 每隔 25 根造一根涨停板
        if i in (50, 51, 80):
            c = round(prev * 1.10, 2)
            h = c
            o = round(prev * 1.02, 2)
            l = o
            v = 25000.0
        out.append(
            {
                "date": f"2026{(i // 28) + 1:02d}{(i % 28) + 1:02d}",
                "open": o,
                "high": h,
                "low": l,
                "close": c,
                "volume": v,
            }
        )
    return out


# ──────────────────── 1. universe=0 修复测试 ────────────────────


def test_default_cn_universe_reads_symbol_key_and_filters_non_cn(monkeypatch):
    """get_stock_list 返回的是 {'symbol': '000001', 'market': 'CN'}，不能只认 'code'。"""
    from src.collectors import stock_list as sl

    monkeypatch.setattr(
        sl,
        "get_stock_list",
        lambda: [
            {"symbol": "000001", "name": "平安银行", "market": "CN"},
            {"symbol": "600519", "name": "贵州茅台", "market": "CN"},
            {"symbol": "00700", "name": "腾讯控股", "market": "HK"},
            {"code": "002361.SZ", "name": "神剑股份", "market": "CN"},
        ],
    )
    syms = market_scan._default_cn_universe()
    assert syms == ["000001", "600519", "002361"]


def test_default_cn_universe_falls_back_to_tq_when_stock_list_empty(monkeypatch):
    from marketdata.vendors import tq as tqmod
    from src.collectors import stock_list as sl

    monkeypatch.setattr(sl, "get_stock_list", lambda: [])
    monkeypatch.setattr(
        tqmod,
        "tq_rpc",
        lambda method, params, timeout=15.0: [
            {"Code": "600111.SH", "Name": "北方稀土"},
            {"Code": "300750.SZ", "Name": "宁德时代"},
        ],
    )
    syms = market_scan._default_cn_universe()
    assert syms == ["600111", "300750"]


def test_fetch_from_tq_normalizes_codes(monkeypatch):
    from marketdata.vendors import tq as tqmod
    from src.collectors import stock_list as sl

    monkeypatch.setattr(
        tqmod,
        "tq_rpc",
        lambda method, params, timeout=15.0: [
            {"Code": "002361.SZ", "Name": "神剑股份"},
            {"Code": "600519.SH", "Name": "贵州茅台"},
            {"Code": "002361.SZ", "Name": "重复应去重"},
        ],
    )
    rows = sl._fetch_from_tq()
    assert rows == [
        {"symbol": "002361", "name": "神剑股份", "market": "CN"},
        {"symbol": "600519", "name": "贵州茅台", "market": "CN"},
    ]


# ──────────────────── 2. backtest_lab 与落库测试 ────────────────────


def test_evaluate_bars_universe_computes_baseline_and_excess():
    universe = {f"00{i:04d}": _synthetic_bars(120, base=10.0 + i) for i in range(1, 7)}
    rows = backtest_lab.evaluate_bars_universe(
        universe, horizons=(1, 5), as_of_date="2026-10-23", min_sample=5
    )
    assert rows, "应产出基准与各因子族分桶行"
    families = {r["factor_family"] for r in rows}
    assert "baseline" in families
    assert "gs_state" in families
    assert "ai_activity" in families
    assert "gs_x_activity" in families

    base_t1 = next(
        r for r in rows if r["factor_family"] == "baseline" and r["horizon"] == 1
    )
    assert base_t1["win_rate_pct"] is not None
    assert base_t1["excess_win_rate_pp"] == 0.0

    # 检查 G区 分桶包含相对基准的超额胜率且恒等式成立
    g_zone_t1 = next(
        r
        for r in rows
        if r["factor_family"] == "gs_state"
        and r["bucket_key"] == "g_zone"
        and r["horizon"] == 1
    )
    assert g_zone_t1["win_rate_pct"] is not None
    assert (
        abs(
            (g_zone_t1["win_rate_pct"] - base_t1["win_rate_pct"])
            - g_zone_t1["excess_win_rate_pp"]
        )
        < 0.05
    )


def test_untradable_one_word_limit_up_is_skipped():
    """T+1 为一字涨停（open==high==low 且涨幅>=4.8%）时必须计入 untradable_skipped，不进分母。"""
    bars = _synthetic_bars(60, base=10.0)
    # 将第 45 根的下一根(46)改成一字涨停
    c45 = bars[45]["close"]
    limit_px = round(c45 * 1.10, 2)
    bars[46] = {
        "date": "20260301",
        "open": limit_px,
        "high": limit_px,
        "low": limit_px,
        "close": limit_px,
        "volume": 5000.0,
    }
    assert backtest_lab._is_untradable_next_bar(c45, bars[46]) is True
    rows = backtest_lab.evaluate_bars_universe(
        {"000001": bars}, horizons=(1,), as_of_date="20261023", min_sample=1
    )
    base_row = next(r for r in rows if r["factor_family"] == "baseline")
    assert base_row["untradable_skipped"] >= 1


def test_small_sample_returns_none_winrate_not_fake():
    """样本数 < min_sample 时 win_rate_pct 必须为 None（诚实口径）。"""
    bars = _synthetic_bars(50, base=10.0)
    rows = backtest_lab.evaluate_bars_universe(
        {"000001": bars}, horizons=(1,), as_of_date="20261023", min_sample=500
    )
    for r in rows:
        assert r["win_rate_pct"] is None
        assert r["excess_win_rate_pp"] is None


def test_upsert_and_load_and_tool_render(db):
    from src.agents.chat import registry

    universe = {f"00{i:04d}": _synthetic_bars(110, base=10.0 + i) for i in range(1, 6)}
    rows = backtest_lab.evaluate_bars_universe(
        universe, horizons=(1, 5), as_of_date="20261023", min_sample=5
    )
    n1 = backtest_lab.upsert_winrate_rows(db, rows)
    assert n1 == len(rows)
    # 重复写入幂等更新，总行数不变
    n2 = backtest_lab.upsert_winrate_rows(db, rows)
    assert n2 == len(rows)
    assert db.query(FactorWinrateDaily).count() == len(rows)

    loaded = backtest_lab.load_winrates(db, factor_family="gs_state", horizon=5)
    assert loaded["available"] is True
    text = backtest_lab.render(loaded)
    assert "factor_winrate_daily" in text
    assert "同池无条件基准" in text
    assert "G区间(趋势多头)" in text

    tool_out = asyncio.run(
        registry._tool_get_factor_winrate(
            db, {"factor_family": "gs_state", "horizon": 5}, None
        )
    )
    assert "数智决策·GS趋势状态与信号" in tool_out
    assert "超额胜率" in tool_out
