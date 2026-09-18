"""决策日志(B6)的钉子 —— 重点钉"不编造"的那几处。

钉什么:
  ① 迁移 v175 幂等 + 唯一键;
  ② record_signal 幂等(同 kind+symbol+day 走 UPDATE, 不堆行), 且**二次写入没给价时保留原价**;
  ③ backfill **只在未来那根 K 线真的存在时才填**: 不够 T+n 就只填得出来的档, 一档都填不出就原样留着;
     信号日当天没 K 线(停牌/非交易日) → 一档都不填; price 为 None → 不填(不拿今天的价冒充当时);
  ④ 命中定义: 收益 > 0 记 1, **平盘(0)记 0**;
  ⑤ stats 在 n < min_sample 时**不给命中率**(返回 insufficient), 页面显示"样本不足"而不是拿 3 个样本算 67%。
"""
from __future__ import annotations

import sqlalchemy as sa

from src.core import decision_log as dl
from src.web.migrations import _m175_decision_log


def _engine():
    eng = sa.create_engine("sqlite://")
    with eng.connect() as conn:
        _m175_decision_log(conn)
    return eng


def _provider(bars: dict[str, list[tuple[str, float]]]):
    """假 K 线序列提供者: 按 (symbol, start_day) 给序列。"""

    def _p(_engine, symbol: str, start_day: str):
        return [b for b in bars.get(symbol, []) if b[0] >= start_day]

    return _p


def test_migration_idempotent():
    eng = _engine()
    with eng.connect() as conn:
        _m175_decision_log(conn)  # 再调一次不能报错
        cols = {r[1] for r in conn.execute(sa.text("PRAGMA table_info(decision_log)"))}
    assert {"signal_kind", "symbol", "trade_date", "price_at_signal", "ret_t1", "hit_t5", "filled_at"} <= cols


def test_record_is_idempotent_and_keeps_price():
    eng = _engine()
    dl.record_signal(eng, signal_kind="resonance3", symbol="002361", trade_date="2026-09-17", price=10.5)
    # 二次只更新上下文、没给价 → 原价必须保留(不能把已有的价抹成 NULL)
    dl.record_signal(eng, signal_kind="resonance3", symbol="002361", trade_date="2026-09-17", context={"hits": 3})
    with eng.connect() as conn:
        rows = conn.execute(sa.text("SELECT price_at_signal, context_json FROM decision_log")).fetchall()
    assert len(rows) == 1, "同 kind+symbol+day 必须 UPDATE, 不能堆行"
    assert float(rows[0][0]) == 10.5
    assert "hits" in rows[0][1]


def test_backfill_only_fills_when_future_bar_exists():
    eng = _engine()
    dl.record_signal(eng, signal_kind="resonance3", symbol="AAA", trade_date="2026-09-10", price=10.0)
    bars = {
        "AAA": [
            ("2026-09-10", 10.0),
            ("2026-09-11", 11.0),  # T+1 → +10% 命中
            ("2026-09-12", 10.0),
            ("2026-09-15", 9.0),  # T+3 → -10% 未命中
            ("2026-09-16", 10.0),
            ("2026-09-17", 12.0),  # T+5 → +20% 命中
        ]
    }
    out = dl.backfill_outcomes(eng, series_provider=_provider(bars))
    assert out["filled"] == 1
    with eng.connect() as conn:
        r = conn.execute(sa.text("SELECT ret_t1, hit_t1, ret_t3, hit_t3, ret_t5, hit_t5 FROM decision_log")).fetchone()
    assert abs(float(r[0]) - 0.1) < 1e-6 and int(r[1]) == 1
    assert abs(float(r[2]) + 0.1) < 1e-6 and int(r[3]) == 0
    assert abs(float(r[4]) - 0.2) < 1e-6 and int(r[5]) == 1


def test_backfill_leaves_unavailable_horizons_null():
    eng = _engine()
    dl.record_signal(eng, signal_kind="resonance3", symbol="BBB", trade_date="2026-09-16", price=10.0)
    bars = {"BBB": [("2026-09-16", 10.0), ("2026-09-17", 10.5)]}  # 只有 T+1
    dl.backfill_outcomes(eng, series_provider=_provider(bars))
    with eng.connect() as conn:
        r = conn.execute(sa.text("SELECT ret_t1, ret_t3, ret_t5, hit_t5 FROM decision_log")).fetchone()
    assert r[0] is not None and r[1] is None and r[2] is None and r[3] is None, "没到的档必须留 NULL, 不许推算"


def test_backfill_flat_is_not_a_win():
    """平盘(收益 0)记未命中 —— 不把"没动"算成赚。"""
    eng = _engine()
    dl.record_signal(eng, signal_kind="resonance3", symbol="CCC", trade_date="2026-09-16", price=10.0)
    bars = {"CCC": [("2026-09-16", 10.0), ("2026-09-17", 10.0)]}
    dl.backfill_outcomes(eng, series_provider=_provider(bars))
    with eng.connect() as conn:
        r = conn.execute(sa.text("SELECT ret_t1, hit_t1 FROM decision_log")).fetchone()
    assert float(r[0]) == 0.0 and int(r[1]) == 0


def test_backfill_skips_when_signal_day_has_no_bar_or_no_price():
    eng = _engine()
    # a) 信号日当天没有 K 线(停牌/非交易日) → 不填
    dl.record_signal(eng, signal_kind="resonance3", symbol="DDD", trade_date="2026-09-16", price=10.0)
    # b) 当时没取到价 → 不填(不拿今天的价冒充当时)
    dl.record_signal(eng, signal_kind="resonance3", symbol="EEE", trade_date="2026-09-16", price=None)
    bars = {
        "DDD": [("2026-09-17", 11.0), ("2026-09-18", 12.0)],  # 缺信号日那根
        "EEE": [("2026-09-16", 10.0), ("2026-09-17", 11.0)],
    }
    out = dl.backfill_outcomes(eng, series_provider=_provider(bars))
    assert out["filled"] == 0
    with eng.connect() as conn:
        n_filled = conn.execute(sa.text("SELECT COUNT(*) FROM decision_log WHERE ret_t1 IS NOT NULL")).scalar()
    assert int(n_filled) == 0


def test_stats_refuses_to_report_on_small_sample():
    eng = _engine()
    for i, sym in enumerate(["S1", "S2", "S3"]):
        dl.record_signal(eng, signal_kind="resonance3", symbol=sym, trade_date="2026-09-16", price=10.0)
    bars = {s: [("2026-09-16", 10.0), ("2026-09-17", 10.0 + i)] for i, s in enumerate(["S1", "S2", "S3"])}
    dl.backfill_outcomes(eng, series_provider=_provider({**bars, "S1": [("2026-09-16", 10.0), ("2026-09-17", 11.0)]}))
    st = dl.stats(eng, min_sample=30)
    row = st["rows"][0]
    assert row["signal_kind"] == "resonance3"
    assert row["horizons"]["t1"]["insufficient"] is True
    assert row["horizons"]["t1"]["hit_rate"] is None, "样本不足不许给命中率"
    assert "样本不足" in row["horizons"]["t1"]["note"]

    st2 = dl.stats(eng, min_sample=2)
    assert st2["rows"][0]["horizons"]["t1"]["insufficient"] is False
    assert st2["rows"][0]["horizons"]["t1"]["hit_rate"] is not None


def test_stats_counts_only_backfilled_in_denominator():
    eng = _engine()
    dl.record_signal(eng, signal_kind="gs_buy", symbol="X1", trade_date="2026-09-16", price=10.0)
    dl.record_signal(eng, signal_kind="gs_buy", symbol="X2", trade_date="2026-09-16", price=10.0)
    bars = {"X1": [("2026-09-16", 10.0), ("2026-09-17", 11.0)]}  # X2 没有后续 K 线
    dl.backfill_outcomes(eng, series_provider=_provider(bars))
    st = dl.stats(eng, min_sample=1)
    row = st["rows"][0]
    assert row["n_total"] == 2
    assert row["horizons"]["t1"]["n"] == 1, "未回填的不许进分母"
    assert row["horizons"]["t1"]["hit_rate"] == 1.0


def test_stats_empty_db_says_no_samples():
    eng = _engine()
    st = dl.stats(eng)
    assert st["rows"] == []
    assert "不用推算值填充" in st["note"]
