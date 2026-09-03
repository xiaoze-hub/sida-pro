"""history_store 落库回查测试(09-03): DP快照 + L2逐笔, SQLite 实跑。"""
from __future__ import annotations


SYM = "TEST hist900001"


def _mk_dp(symbol=SYM):
    return {
        "symbol": symbol,
        "market": "CN",
        "institution_activity": {"activity": 4.25, "level": "强势"},
        "gs": {"side": "G"},
        "l2": {"main_net": 123456.0},
        "main_intent": {"main_net": 999.0},
    }


def _mk_ticks(n=5):
    return [
        {"d": "B", "amt": 100000.0 + i, "vol": 10 + i, "price": 10.5, "t": f"09:30:{i:02d}"}
        for i in range(n)
    ]


def test_migrations_idempotent():
    from sqlalchemy import text

    from src.web.database import engine
    from src.web.migrations import _m127_dp_history_table, _m128_l2_ticks_table

    with engine.begin() as conn:
        _m127_dp_history_table(conn)
        _m128_l2_ticks_table(conn)
        _m127_dp_history_table(conn)  # 重跑不报错
        _m128_l2_ticks_table(conn)
        tables = {r[0] for r in conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))}
    assert "decision_pioneer_history" in tables
    assert "l2_ticks" in tables


def test_dp_record_query_roundtrip():
    from sqlalchemy import text

    from src.core.history_store import query_dp_history, record_dp_snapshot
    from src.web.database import engine

    try:
        record_dp_snapshot(SYM, "CN", _mk_dp())
        rows = query_dp_history(SYM, "CN", days=1)
        assert len(rows) >= 1
        last = rows[-1]
        assert last["activity"] == 4.25
        assert last["level"] == "强势"
        assert last["gs_side"] == "G"
    finally:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM decision_pioneer_history WHERE symbol = :s"), {"s": SYM})


def test_l2_persist_dedupe():
    from sqlalchemy import text

    from src.core.history_store import persist_l2_ticks, query_l2_ticks
    from src.web.database import engine

    try:
        w1 = persist_l2_ticks(SYM, "CN", "thsdk_big_order", _mk_ticks(5))
        assert w1 == 5
        w2 = persist_l2_ticks(SYM, "CN", "thsdk_big_order", _mk_ticks(5))
        assert w2 == 0, "重复拉取必须幂等去重"
        rows = query_l2_ticks(SYM, "CN", days=1)
        assert len(rows) == 5
        assert rows[0]["direction"] == "B"
    finally:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM l2_ticks WHERE symbol = :s"), {"s": SYM})
