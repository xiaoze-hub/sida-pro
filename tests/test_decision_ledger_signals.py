"""决策账本扩信号覆盖(P1-1, 2026-10-10)的钉子。

钉什么(对应审计 P1-1「只入账 resonance3 一类信号」):
  ① GS 买卖点(market_scan → signal_kind=gs_cross)、妖股因子入池(demon_factors →
     demon_pool)、竞价池候选(entry_candidates → auction_pool) 各走真实 emit 路径后,
     账本里多出**对应 signal_kind** 的行;
  ② 幂等: 同一 (symbol, trade_date, signal_kind) 重跑不双计(唯一键 UPSERT);
  ③ 无价不写: 信号时点价拿不到 → **不入账**(不硬填 0);
  ④ 主流程不因账本失败中断: record_many_safe 取引擎/写入失败只 warn, 返回 0, 不抛。

禁真网络: GS 用例 monkeypatch fetch_bars; 其余走真实 sqlite 测试库。
"""
from __future__ import annotations

import datetime

import pytest
import sqlalchemy as sa

from src.core import market_scan
from src.core.decision_log import record_many_safe
from src.web.migrations import _m175_decision_log


def _ensure_ledger_table() -> None:
    from src.db.session import get_write_engine

    with get_write_engine().begin() as conn:
        _m175_decision_log(conn)


@pytest.fixture()
def _ledger():
    _ensure_ledger_table()


def _conn():
    from src.db.session import get_write_engine

    return get_write_engine().begin()


def _count_kind(kind: str, symbol: str = "") -> int:
    from src.db.session import get_write_engine

    sql = "SELECT COUNT(*) FROM decision_log WHERE signal_kind = :k"
    params = {"k": kind}
    if symbol:
        sql += " AND symbol = :s"
        params["s"] = symbol
    with get_write_engine().connect() as conn:
        return int(conn.execute(sa.text(sql), params).scalar() or 0)


def _row(kind: str, symbol: str):
    from src.db.session import get_write_engine

    with get_write_engine().connect() as conn:
        return conn.execute(
            sa.text(
                "SELECT trade_date, price_at_signal FROM decision_log "
                "WHERE signal_kind = :k AND symbol = :s"
            ),
            {"k": kind, "s": symbol},
        ).fetchall()


# ── ④ 旁路失败不阻断 ────────────────────────────────────────────────────────
def test_record_many_safe_swallows_engine_failure(monkeypatch):
    import src.db.session as session_mod

    def _boom():
        raise RuntimeError("引擎不可用")

    monkeypatch.setattr(session_mod, "get_write_engine", _boom)
    # 不抛, 返回 0(信号生成优先, 账本只是旁路)
    assert record_many_safe(
        [{"signal_kind": "gs_cross", "symbol": "X", "trade_date": "20260901", "price": 1.0}]
    ) == 0


# ── ① GS 买卖点(真实 scan emit 路径) ────────────────────────────────────────
def _gs_bars_ending_in_cross() -> list[dict]:
    base = datetime.date(2026, 9, 1)
    bars = []
    for i in range(28):
        bars.append(
            {
                "date": (base + datetime.timedelta(days=i)).strftime("%Y-%m-%d"),
                "open": 10, "close": 10, "high": 10.1, "low": 9.9, "volume": 1000,
            }
        )
    bars.append(
        {
            "date": (base + datetime.timedelta(days=28)).strftime("%Y-%m-%d"),
            "open": 10, "close": 15, "high": 15.1, "low": 9.9, "volume": 5000,
        }
    )
    return bars


@pytest.fixture()
def _cleanup_gs():
    _ensure_ledger_table()
    yield
    with _conn() as conn:
        conn.execute(sa.text("DELETE FROM decision_log WHERE symbol = '209777'"))


def test_gs_scan_emits_ledger_row_and_is_idempotent(monkeypatch, _cleanup_gs):
    import src.core.decision_pioneer as pioneer

    monkeypatch.setattr(pioneer, "fetch_bars", lambda sym, market, days=60: _gs_bars_ending_in_cross())
    out1 = market_scan.scan(symbols=["209777"], with_zljc=False)
    assert out1["logged_signals"] == 1
    rows = _row("gs_cross", "209777")
    assert len(rows) == 1
    assert abs(float(rows[0][1]) - 15.0) < 1e-9  # 信号时点价 = 末根收盘
    assert rows[0][0] == "20260929"

    # 幂等重跑: 行列数不变(同 kind+symbol+trade_date UPSERT)
    out2 = market_scan.scan(symbols=["209777"], with_zljc=False)
    assert out2["logged_signals"] == 1
    assert len(_row("gs_cross", "209777")) == 1


def test_gs_no_price_is_not_written(_cleanup_gs):
    # 有交叉信号但取不到价 / 价为 0 → 不入账(不硬填 0)
    metrics = [
        {"symbol": "209777", "gs_cross_side": "G", "close": None, "close_date": "20260929"},
        {"symbol": "209777", "gs_cross_side": "S", "close": 0, "close_date": "20260929"},
    ]
    assert market_scan._log_gs_signals(metrics, trade_date="20260929") == 0
    assert _count_kind("gs_cross", "209777") == 0


# ── ② 妖股因子入池(真实 recompute_factors emit 路径) ───────────────────────
def _demon_event(d: str) -> dict:
    return {
        "trade_date": d, "symbol": "209778", "market": "CN", "name": "测试妖股",
        "prev_close": 10.0, "limit_price": 11.0, "open_price": 11.0, "high_price": 11.0,
        "low_price": 10.8, "close_price": 11.0, "touched": 1, "is_sealed_close": 1,
        "one_way": 0, "open_count": None, "first_time": None, "max_seal_amount": None,
        "limit_days": None, "circ_mv": None, "source": "test", "extra": None,
    }


@pytest.fixture()
def _seed_demon():
    _ensure_ledger_table()
    from src.core.limit_up_backfill import _upsert_rows

    # 6 只涨停事件 → grade=活跃(非普通), 满足"入池"
    _upsert_rows([_demon_event(f"209901{1 + i:02d}") for i in range(6)])
    yield
    with _conn() as conn:
        conn.execute(sa.text("DELETE FROM limit_up_events WHERE symbol = '209778'"))
        conn.execute(sa.text("DELETE FROM demon_factors WHERE symbol = '209778'"))
        conn.execute(sa.text("DELETE FROM decision_log WHERE symbol = '209778'"))


def test_demon_pool_emits_ledger_row(_seed_demon):
    from src.core.demon_factors import recompute_factors

    r = recompute_factors(["209778"])
    assert r["logged"] == 1
    rows = _row("demon_pool", "209778")
    assert len(rows) == 1
    assert abs(float(rows[0][1]) - 11.0) < 1e-9  # 最新涨停事件收盘价
    assert rows[0][0] == "20990106"

    # 幂等重跑不双计
    recompute_factors(["209778"])
    assert len(_row("demon_pool", "209778")) == 1


def test_demon_no_price_is_not_written():
    _ensure_ledger_table()
    from src.core.demon_factors import _log_demon_signals

    assert _log_demon_signals(
        [{"symbol": "209778", "trade_date": "20260901", "price": None, "grade": "妖"}]
    ) == 0
    assert _count_kind("demon_pool", "209778") == 0


# ── ③ 竞价池候选(真实 emit 路径) ───────────────────────────────────────────
def _auction_scored_item(symbol: str, price):
    return {
        "market": "CN", "symbol": symbol, "candidate_source": "auction",
        "quote": {"current_price": price}, "kline": {}, "inp": {"meta": {}},
        "is_holding": False, "action": "watch", "action_label": "关注",
        "signal": "竞价异动", "reason": "高开", "strategy_tags": ["auction_anomaly"],
        "score": 60, "evidence": [], "plan": {}, "quality": 0, "confidence": 0.6,
        "status": "active",
    }


def test_auction_persist_emits_ledger_row_and_is_idempotent(_ledger):
    from src.core.entry_candidates import _persist_candidates

    items = [
        _auction_scored_item("209779", 12.3),   # 竞价源有价 → 入账
        _auction_scored_item("209781", None),   # 竞价源无价 → 不写
    ]
    try:
        _persist_candidates(snapshot="2026-09-01", scored_items=items)
        rows = _row("auction_pool", "209779")
        assert len(rows) == 1
        assert abs(float(rows[0][1]) - 12.3) < 1e-9
        assert rows[0][0] == "20260901"
        assert _count_kind("auction_pool", "209781") == 0

        # 幂等重跑不双计
        _persist_candidates(snapshot="2026-09-01", scored_items=items)
        assert len(_row("auction_pool", "209779")) == 1
    finally:
        with _conn() as conn:
            conn.execute(sa.text("DELETE FROM decision_log WHERE symbol IN ('209779','209781')"))
            conn.execute(sa.text("DELETE FROM entry_candidates WHERE stock_symbol IN ('209779','209781')"))
