"""妖股因子 LHB 回填(2026-09-10): 龙虎榜落库幂等 + lhb 维激活 + 回填管线。

覆盖: 行归一化(非 A 股丢弃) → 幂等 upsert((trade_date,symbol,reason) 唯一)
→ 上榜统计分组(同日多原因计 1 天, circ_mv 取最新) → demon_score lhb 维
(有数据=真实 0 分不打缺数据旗) → 历史回填管线(假抓取, 不触网)。
"""
from __future__ import annotations

import src.db.session as dbs
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

from src.core import lhb_backfill as lhb
from src.core.demon_score import demon_score_from_events
from src.web.migrations import _m156_dragon_tiger_events_table


def _mk_engine():
    eng = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    with eng.begin() as conn:
        _m156_dragon_tiger_events_table(conn)
    return eng


def _patch(monkeypatch):
    eng = _mk_engine()
    monkeypatch.setattr(dbs, "engine", eng)
    return eng


class _FakeItem:
    def __init__(self, trade_date, symbol, name="测试", reason="日涨幅偏离", free_market_cap=None):
        self.trade_date = trade_date
        self.symbol = symbol
        self.name = name
        self.reason = reason
        self.close = 10.0
        self.change_pct = 10.0
        self.net_buy = 1_000_000.0
        self.buy_amt = 5_000_000.0
        self.sell_amt = 4_000_000.0
        self.deal_amt = 9_000_000.0
        self.turnover_pct = 15.0
        self.free_market_cap = free_market_cap


def test_norm_items_filters_non_stock_and_normalizes():
    rows = lhb._norm_items([
        _FakeItem("2026-09-09 00:00:00", "603186", free_market_cap=3.3e10),
        _FakeItem("2026-09-09", "513100", reason="ETF"),      # 6 位保留(完整榜单, join 过滤)
        _FakeItem("2026-09-09", "60318", reason="5位"),        # 5位 → 丢
        _FakeItem("20260909", "  002361 ", reason=None),        # 符号带空白+无原因 → 归一/落空串
    ])
    assert [r["symbol"] for r in rows] == ["603186", "513100", "002361"]
    assert rows[0]["trade_date"] == "20260909"
    assert rows[0]["free_market_cap"] == 3.3e10
    assert rows[0]["source"] == "eastmoney"
    assert rows[2]["reason"] == ""  # 空原因不编造, 落空串


def test_upsert_day_idempotent(monkeypatch):
    eng = _patch(monkeypatch)
    rows = lhb._norm_items([
        _FakeItem("20260909", "603186", reason="偏离值"),
        _FakeItem("20260909", "603186", reason="换手率达20%"),  # 同股同日第二原因
    ])
    assert lhb._upsert_day(rows) == 2
    # 同键重拉 → 不新增
    assert lhb._upsert_day(rows) == 0
    with eng.connect() as conn:
        n = conn.execute(text("SELECT COUNT(*) FROM dragon_tiger_events")).scalar()
        assert n == 2


def test_lhb_stats_groups_by_day_and_latest_cap(monkeypatch):
    eng = _patch(monkeypatch)
    rows = lhb._norm_items([
        _FakeItem("20260908", "603186", reason="偏离值", free_market_cap=3.0e10),
        _FakeItem("20260908", "603186", reason="换手率", free_market_cap=3.0e10),
        _FakeItem("20260909", "603186", reason="偏离值", free_market_cap=3.3e10),
        _FakeItem("20260909", "002361", reason="偏离值", free_market_cap=2.0e10),
    ])
    lhb._upsert_day(rows)
    stats = lhb.lhb_stats()
    assert stats["603186"]["n_lhb"] == 2  # 同日多原因计 1 天
    assert stats["603186"]["circ_mv"] == 3.3e10  # 最新一日
    assert stats["002361"]["n_lhb"] == 1
    only = lhb.lhb_stats(symbols=["002361"])
    assert set(only.keys()) == {"002361"}


def test_demon_score_lhb_dim_activation():
    evs = [{"trade_date": "20260901", "is_sealed_close": 1, "touched": 1, "one_way": 0}]
    # 缺数据口径: flag 打标, 计 0
    r = demon_score_from_events(evs)
    assert any("龙虎榜频次缺数据" in f for f in r["flags"])
    # 有数据: n_lhb=12 → 80 分, 无 flag
    r2 = demon_score_from_events(evs, n_lhb=12)
    assert r2["dims"]["lhb"]["score"] == 80
    assert not any("龙虎榜频次缺数据" in f for f in r2["flags"])
    # 未上榜=真实 0: 计 0 分且不打 flag
    r3 = demon_score_from_events(evs, n_lhb=0)
    assert r3["dims"]["lhb"]["score"] == 0
    assert not any("龙虎榜频次缺数据" in f for f in r3["flags"])
    # 市值加分走通
    r4 = demon_score_from_events(evs, n_lhb=0, circ_mv=30e8)
    assert any("小盘 20-50 亿" in f for f in r4["flags"])


def test_backfill_history_pipeline_no_network(monkeypatch):
    eng = _patch(monkeypatch)
    monkeypatch.setattr(lhb, "_trading_dates", lambda cutoff, limit=0: ["20260908", "20260909"])
    monkeypatch.setattr(
        lhb, "_fetch_day",
        lambda d: [_FakeItem(d, "603186", free_market_cap=3.3e10)],
    )
    monkeypatch.setattr(lhb.time, "sleep", lambda s: None)
    stats = lhb.backfill_history(days=365)
    assert stats["rows_saved"] == 2
    assert stats["days"] == 2
    # 重跑幂等
    stats2 = lhb.backfill_history(days=365)
    assert stats2["rows_saved"] == 0
    with eng.connect() as conn:
        n = conn.execute(text("SELECT COUNT(*) FROM dragon_tiger_events")).scalar()
        assert n == 2
    # daily_recent 走同一管线
    d = lhb.daily_recent(days=3)
    assert d["rows_saved"] == 0  # 已在库
    assert "603186" not in (d["touched"] or [])


def test_daily_job_triggers_recompute_for_touched(monkeypatch):
    _patch(monkeypatch)
    calls = {}
    monkeypatch.setattr(lhb, "_trading_dates", lambda cutoff, limit=0: ["20260909"])
    monkeypatch.setattr(
        lhb, "_fetch_day",
        lambda d: [_FakeItem(d, "603186", free_market_cap=3.3e10)],
    )
    monkeypatch.setattr(lhb.time, "sleep", lambda s: None)

    import src.core.demon_factors as df

    def _fake_recompute(symbols=None):
        calls["symbols"] = symbols
        return {"updated": len(symbols or [])}

    monkeypatch.setattr(df, "recompute_factors", _fake_recompute)
    out = lhb.daily_job(days=3, recompute=True)
    assert calls.get("symbols") == ["603186"]
    assert out["factors"] == {"updated": 1}
