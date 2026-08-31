"""P4 主力意图物理守卫复现测试(2026-08-23)。

主力成交额(买+卖)超过全日总成交额 130% 物理上不可能 → data_status='suspect',
不给吸筹/派发结论(2026-08 两次逐笔重复计数致净额翻倍事故的盘中实时拦截)。
"""
from __future__ import annotations

from types import SimpleNamespace

from marketdata.symbol import Symbol
from src.core import dark_flow as df


def _fake_ticks(main_buy_count: int = 40, main_sell_count: int = 20) -> list[dict]:
    """每笔 500 万(≥20万 → 全是主力单)。"""
    ticks = []
    for _ in range(main_buy_count):
        ticks.append({"d": "B", "amt": 500e4, "vol": 1000, "t": "10:00:00", "price": 10.0})
    for _ in range(main_sell_count):
        ticks.append({"d": "S", "amt": 500e4, "vol": 1000, "t": "10:30:00", "price": 10.0})
    return ticks


def _run(monkeypatch, ticks, turnover: float) -> dict | None:
    monkeypatch.setattr(df, "_fetch_all_ticks", lambda code: ticks)
    fake_q = SimpleNamespace(
        current_price=10.0, high_price=10.5, low_price=9.5, change_pct=1.0,
        volume_ratio=1.2, volume_outer=None, volume_inner=None, turnover=turnover,
        prev_close=9.9,
    )
    import marketdata.vendors.tencent as tencent_mod
    import marketdata.vendors.tencent_panel as tencent_panel_mod
    monkeypatch.setattr(
        tencent_mod.TencentQuoteVendor, "fetch", lambda self, syms, opts: [fake_q]
    )
    monkeypatch.setattr(tencent_panel_mod, "fetch_price_distribution", lambda symbol, limit=70: [])
    return df.compute_dark_flow(Symbol.parse("600519", "CN"))


class TestPhysicalGuard:
    def test_main_exceeding_turnover_marks_suspect(self, monkeypatch):
        # 主力买 2亿 + 卖 1亿 = 3亿 主力成交, 但总成交额只有 1亿 → 物理不可能
        r = _run(monkeypatch, _fake_ticks(40, 20), turnover=1e8)
        assert r is not None
        assert r["data_status"] == "suspect"
        assert "数据异常" in r["signal"]
        assert "吸筹" not in r["signal"], "P4: 异常数据不得输出吸筹/派发结论"

    def test_normal_turnover_stays_ok(self, monkeypatch):
        # 主力成交 3亿 < 总成交额 5亿 → 正常
        r = _run(monkeypatch, _fake_ticks(40, 20), turnover=5e8)
        assert r is not None
        assert r["data_status"] == "ok"
        assert "数据异常" not in r["signal"]

    def test_missing_turnover_no_false_positive(self, monkeypatch):
        # 行情拿不到成交额(None/0) → 守卫不触发, 不误伤
        r = _run(monkeypatch, _fake_ticks(40, 20), turnover=0.0)
        assert r is not None
        assert r["data_status"] == "ok"
