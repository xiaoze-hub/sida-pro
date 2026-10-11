"""用户自选池专属信号质量报告(2026-10-11 胜率证据层 ②)的钉子。

钉什么:
  ① **多用户隔离**: 报告只聚合当前 user_id 自选池标的的账本行(别人池子的信号不进);
     user_id 缺失 → available=False(不退化到全局池);
  ② **样本不足不给数字**: n < min_sample → hit_rate=None + insufficient=True;
  ③ **未回填不是 0**: 只统计已回填行(缺失不进分母);
  ④ regime 分桶 + 『该信号只在X期有效』标注;
  ⑤ 空自选池 / 无数据 → 显式 available=False + 原因, 不编造;
  ⑥ 只陈述实测, 报告不含任何建议文案。

禁真网络: 内存 sqlite。
"""
from __future__ import annotations

import sqlalchemy as sa

from src.core import decision_log as dl
from src.core import signal_quality as sq
from src.db.models import Stock
from src.web.migrations import _m175_decision_log, _m184_decision_log_regime


def _engine():
    eng = sa.create_engine("sqlite://")
    with eng.connect() as conn:
        _m175_decision_log(conn)
        _m184_decision_log_regime(conn)
    Stock.__table__.create(eng)
    return eng


def _add_stock(eng, user_id, symbol):
    with eng.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO stocks (user_id, symbol, name, market) "
            "VALUES (:u, :s, :n, 'CN')"
        ), {"u": user_id, "s": symbol, "n": symbol})


def _provider(bars):
    def _p(_engine, symbol, start_day):
        return [b for b in bars.get(symbol, []) if b[0] >= start_day]
    return _p


def test_missing_user_id_not_isolatable():
    eng = _engine()
    r = sq.watchlist_signal_quality(eng, user_id="")
    assert r["available"] is False and "隔离" in r["error"]


def test_empty_watchlist_explicit():
    eng = _engine()
    r = sq.watchlist_signal_quality(eng, user_id="user-a")
    assert r["available"] is False and r["symbols_count"] == 0
    assert "自选池为空" in r["error"]


def test_isolation_between_users():
    eng = _engine()
    _add_stock(eng, "user-a", "AAA")
    _add_stock(eng, "user-b", "CCC")
    # 三个信号: AAA(a 的池), CCC(b 的池), ZZZ(不在任何人池子)
    dl.record_signal(eng, signal_kind="resonance3", symbol="AAA", trade_date="2026-09-16", price=10.0, regime="发酵")
    dl.record_signal(eng, signal_kind="resonance3", symbol="CCC", trade_date="2026-09-16", price=10.0, regime="发酵")
    dl.record_signal(eng, signal_kind="resonance3", symbol="ZZZ", trade_date="2026-09-16", price=10.0, regime="发酵")
    bars = {
        "AAA": [("2026-09-16", 10.0), ("2026-09-17", 11.0)],
        "CCC": [("2026-09-16", 10.0), ("2026-09-17", 9.0)],
        "ZZZ": [("2026-09-16", 10.0), ("2026-09-17", 12.0)],
    }
    dl.backfill_outcomes(eng, series_provider=_provider(bars))

    ra = sq.watchlist_signal_quality(eng, user_id="user-a", min_sample=1)
    assert ra["available"] is True and ra["symbols_count"] == 1
    assert ra["signals"][0]["n_total"] == 1
    assert [s["symbol"] for s in ra["signals"][0]["by_symbol"]] == ["AAA"]

    rb = sq.watchlist_signal_quality(eng, user_id="user-b", min_sample=1)
    assert rb["signals"][0]["by_symbol"][0]["symbol"] == "CCC"
    # ZZZ 不在任何人池子 → 谁的报告都不含它
    assert "ZZZ" not in {s["symbol"] for s in ra["signals"][0]["by_symbol"]}


def test_insufficient_sample_hides_number():
    eng = _engine()
    _add_stock(eng, "user-a", "AAA")
    for i in range(5):  # 5 < 30
        dl.record_signal(eng, signal_kind="resonance3", symbol="AAA",
                         trade_date=f"2026-09-1{i}", price=10.0, regime="发酵")
    bars = {"AAA": [("2026-09-10", 10.0), ("2026-09-17", 11.0)]}
    # 用 provider 回填: 只有首个信号日当根存在才算得出
    dl.backfill_outcomes(eng, series_provider=lambda e, s, d: bars["AAA"] if d == "2026-09-10" else [])

    r = sq.watchlist_signal_quality(eng, user_id="user-a", min_sample=30)
    h = r["signals"][0]["horizons"]["t1"]
    assert h["insufficient"] is True and h["hit_rate"] is None
    assert "样本不足" in h["note"]


def test_regime_bucket_and_note():
    eng = _engine()
    _add_stock(eng, "user-a", "AAA")
    dl.record_signal(eng, signal_kind="resonance3", symbol="AAA", trade_date="2026-09-16", price=10.0, regime="发酵")
    dl.record_signal(eng, signal_kind="resonance3", symbol="AAA", trade_date="2026-09-17", price=10.0, regime="退潮")
    bars = {"AAA": [("2026-09-16", 10.0), ("2026-09-17", 11.0), ("2026-09-18", 12.0)]}
    dl.backfill_outcomes(eng, series_provider=_provider(bars))

    r = sq.watchlist_signal_quality(eng, user_id="user-a", min_sample=1)
    sig = r["signals"][0]
    regs = {b["regime"]: b for b in sig["regime_buckets"]}
    assert regs["ferment"]["horizons"]["t1"]["hit_rate"] == 1.0
    assert regs["ferment"]["label"] == "发酵"
    assert isinstance(sig["regime_note"], str)


def test_no_advice_language():
    """报告只陈述实测: 不含任何建议/方向文案(铁律: AI 备证据不拍方向)。"""
    eng = _engine()
    _add_stock(eng, "user-a", "AAA")
    dl.record_signal(eng, signal_kind="resonance3", symbol="AAA", trade_date="2026-09-16", price=10.0, regime="发酵")
    dl.backfill_outcomes(eng, series_provider=_provider({"AAA": [("2026-09-16", 10.0), ("2026-09-17", 11.0)]}))
    r = sq.watchlist_signal_quality(eng, user_id="user-a", min_sample=1)
    blob = str(r)
    for bad in ("建议买入", "建议卖出", "推荐买", "必然上涨"):
        assert bad not in blob
