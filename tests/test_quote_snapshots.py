"""快照行情 1 分钟桶落库(批次2 2/2, 2026-09-10): 幂等写入 + 时段守卫 + 采集管线。

覆盖: (trade_date, market, symbol, ts) 幂等(重拉不新增) → 交易时段守卫(午休/
盘前/收盘后/周末跳过; 日历挂掉退回 weekday) → 分钟桶 floor + 字段映射 → 指数
走腾讯原始符号(market='IDX', 避免与同号个股撞键) → collect_once 全链路
(monkeypatch 行情源, 不触网) + 永不抛语义。
"""
from __future__ import annotations

from datetime import datetime

import src.db.session as dbs
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

from src.core import quote_snapshots as qs
from src.web.migrations import _m157_quote_snapshots_table


def _mk_engine():
    eng = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    with eng.begin() as conn:
        _m157_quote_snapshots_table(conn)
    return eng


def _patch(monkeypatch):
    eng = _mk_engine()
    monkeypatch.setattr(dbs, "engine", eng)
    return eng


def _row(symbol="600000", ts="2026-09-10 10:00:00", trade_date="20260910", market="CN"):
    return {
        "trade_date": trade_date, "ts": ts, "symbol": symbol, "market": market,
        "name": "测试", "last": 10.0, "open": 9.9, "high": 10.1, "low": 9.8,
        "prev_close": 9.9, "change_pct": 1.0, "volume": 1000.0, "turnover": 10000.0,
        "turnover_rate": 1.0, "volume_ratio": 1.5, "circ_mv": 5e9, "source": "eastmoney",
    }


def test_persist_idempotent(monkeypatch):
    eng = _patch(monkeypatch)
    rows = [_row(), _row(symbol="000001")]
    assert qs.persist_quote_snapshots(rows) == 2
    # 同桶重拉 → 不新增
    assert qs.persist_quote_snapshots(rows) == 0
    # 不同 trade_date 是新键
    assert qs.persist_quote_snapshots([_row(trade_date="20260911")]) == 1
    with eng.connect() as conn:
        n = conn.execute(text("SELECT COUNT(*) FROM quote_snapshots")).scalar()
        assert n == 3


def test_persist_never_raises(monkeypatch):
    class _Boom:
        def begin(self):
            raise RuntimeError("engine down")

    monkeypatch.setattr(qs, "_engine", lambda: _Boom())
    assert qs.persist_quote_snapshots([_row()]) == 0
    assert qs.persist_quote_snapshots([]) == 0


def test_in_trading_window_guard(monkeypatch):
    monkeypatch.setattr("src.core.trading_calendar.is_trading_day", lambda d: True)
    assert qs.in_trading_window(datetime(2026, 9, 10, 9, 0)) is False   # 盘前
    assert qs.in_trading_window(datetime(2026, 9, 10, 10, 0)) is True   # 盘中
    assert qs.in_trading_window(datetime(2026, 9, 10, 12, 0)) is False  # 午休
    assert qs.in_trading_window(datetime(2026, 9, 10, 15, 30)) is False  # 收盘后
    assert qs.in_trading_window(datetime(2026, 9, 10, 15, 4, 59)) is True  # 收盘余量内


def test_in_trading_window_calendar_fallback(monkeypatch):
    def _boom(d):
        raise RuntimeError("calendar down")

    monkeypatch.setattr("src.core.trading_calendar.is_trading_day", _boom)
    # 周四盘中 → 退回 weekday 判定 True
    assert qs.in_trading_window(datetime(2026, 9, 10, 10, 0)) is True
    # 周六盘中 → False
    assert qs.in_trading_window(datetime(2026, 9, 12, 10, 0)) is False


def test_stock_rows_minute_floor_and_fields(monkeypatch):
    import src.core.marketdata_client as mdc

    captured = {}

    def _fake_md(symbols, market):
        captured["symbols"] = symbols
        captured["market"] = market
        return [
            {"symbol": "600000", "name": "浦发", "current_price": 10.0,
             "open_price": 9.9, "high_price": 10.1, "low_price": 9.8, "prev_close": 9.9,
             "change_pct": 1.0, "volume": 1000.0, "turnover": 10000.0,
             "turnover_rate": 1.0, "volume_ratio": 1.5,
             "circulating_market_value": 5e9, "source": "eastmoney"},
            {"symbol": "", "name": "坏行"},  # 空符号丢弃
        ]

    monkeypatch.setattr(mdc, "md_quote_rows", _fake_md)
    rows = qs._stock_rows({"CN": ["600000"]}, datetime(2026, 9, 10, 10, 30, 17, 123))
    assert captured == {"symbols": ["600000"], "market": "CN"}
    assert len(rows) == 1
    r = rows[0]
    assert r["ts"] == "2026-09-10 10:30:00"  # 分钟 floor(秒/微秒归零)
    assert r["trade_date"] == "20260910"
    assert r["symbol"] == "600000"
    assert r["last"] == 10.0 and r["circ_mv"] == 5e9 and r["source"] == "eastmoney"


def test_index_rows_uses_tencent_symbol(monkeypatch):
    import src.core.marketdata_client as mdc

    class _MD:
        def index_quotes(self, syms):
            assert "sh000001" in syms
            return [
                {"symbol": "000001", "name": "上证指数", "current_price": 3000.0,
                 "prev_close": 2990.0, "change_pct": 0.33, "volume": 1e8, "turnover": 2e8},
                {"symbol": "399006", "name": "创业板指", "current_price": 2000.0,
                 "prev_close": 1990.0, "change_pct": 0.5, "volume": 1e8, "turnover": 2e8},
                {"symbol": "000300", "name": "沪深300", "current_price": 4000.0,
                 "prev_close": 3990.0, "change_pct": 0.25, "volume": 1e8, "turnover": 2e8},
                {"symbol": "399005", "name": "不在清单的丢", "current_price": 5000.0},
            ]

    monkeypatch.setattr(mdc, "get_market_data", lambda: _MD())
    rows = qs._index_rows(datetime(2026, 9, 10, 10, 0))
    # 裸码 → 腾讯原始符号对位; 不在 INDEX_SYMBOLS 的 399005 丢弃
    assert [r["symbol"] for r in rows] == ["sh000001", "sz399006", "sh000300"]
    assert all(r["market"] == "IDX" for r in rows)
    assert rows[0]["source"] == "tencent"
    assert rows[0]["ts"] == "2026-09-10 10:00:00"


def test_collect_once_skips_non_trading_time(monkeypatch):
    _patch(monkeypatch)
    monkeypatch.setattr("src.core.trading_calendar.is_trading_day", lambda d: d.weekday() < 5)
    # 午休
    assert qs.collect_once(datetime(2026, 9, 10, 12, 0)) == {"skipped": "non_trading_time"}
    # 周六盘中
    assert qs.collect_once(datetime(2026, 9, 12, 10, 0)) == {"skipped": "non_trading_time"}
    # 收盘后
    assert qs.collect_once(datetime(2026, 9, 10, 16, 0)) == {"skipped": "non_trading_time"}


def test_collect_once_happy_path(monkeypatch):
    eng = _patch(monkeypatch)
    monkeypatch.setattr("src.core.trading_calendar.is_trading_day", lambda d: True)
    monkeypatch.setattr(qs, "collect_market_symbols", lambda: {"CN": ["600000"]})

    import src.core.marketdata_client as mdc

    monkeypatch.setattr(
        mdc, "md_quote_rows",
        lambda s, m: [{"symbol": "600000", "name": "浦发", "current_price": 10.0, "source": "eastmoney"}],
    )

    class _MD:
        def index_quotes(self, syms):
            return [{"symbol": "000001", "name": "上证指数", "current_price": 3000.0,
                     "prev_close": 2990.0, "change_pct": 0.33, "volume": 1e8, "turnover": 2e8}]

    monkeypatch.setattr(mdc, "get_market_data", lambda: _MD())

    out = qs.collect_once(datetime(2026, 9, 10, 10, 0, 30))
    assert out == {"trade_date": "20260910", "rows_fetched": 2, "rows_saved": 2}  # 1 个股 + 1 指数
    # 同一分钟桶内重跑(秒不同) → 幂等 0 新增
    out2 = qs.collect_once(datetime(2026, 9, 10, 10, 0, 59))
    assert out2 == {"trade_date": "20260910", "rows_fetched": 2, "rows_saved": 0}
    with eng.connect() as conn:
        n = conn.execute(text("SELECT COUNT(*) FROM quote_snapshots")).scalar()
        assert n == 2
        r = conn.execute(
            text("SELECT market, symbol, ts FROM quote_snapshots WHERE market = 'IDX'")
        ).fetchone()
        assert (r[0], r[1], r[2]) == ("IDX", "sh000001", "2026-09-10 10:00:00")


def test_collect_once_never_raises(monkeypatch):
    _patch(monkeypatch)
    monkeypatch.setattr("src.core.trading_calendar.is_trading_day", lambda d: True)

    def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(qs, "collect_market_symbols", _boom)
    out = qs.collect_once(datetime(2026, 9, 10, 10, 0))
    assert "error" in out
