"""批次D 信号复盘单测(纯函数部分)。"""
from src.core.signal_review import (
    OFFICIAL_BENCHMARK,
    _future_close,
    hit_rate,
    record_signal,
)


def test_future_close_trading_day_advance():
    closes = {f"2026090{i}" for i in range(1, 6)} | {"20260913"}
    closes = {d: 10.0 + i for i, d in enumerate(sorted(f"2026090{i}" for i in range(1, 6)))}
    closes["20260913"] = 16.0
    # T+1: 09-01 后第 1 个交易日 = 09-02
    c, key = _future_close(closes, "20260901", 1)
    assert c == 11.0 and key == "20260902"
    # T+4: 09-05 后不足 4 个交易日 → None(显式无数据)
    c4, _ = _future_close(closes, "20260905", 4)
    assert c4 is None
    # 事件日不在序列(停牌/新股)
    assert _future_close(closes, "20260830", 1) == (None, None)


def test_record_signal_dedupe_and_type_guard():
    try:
        ok1 = record_signal("ambush_candidate", "600000", direction="long", strength=7.0,
                            payload={"catalyst": "测试"}, emit_date="20990101")
        ok2 = record_signal("ambush_candidate", "600000", direction="long", strength=7.0,
                            payload={}, emit_date="20990101")
        assert ok1 is True and ok2 is False  # 首条口径幂等
        assert record_signal("bogus_type", "600000", emit_date="20990102") is False
    finally:
        from sqlalchemy import text
        from src.web.database import SessionLocal
        db = SessionLocal()
        try:
            db.execute(text("DELETE FROM signal_snapshots WHERE emit_date = '20990101' OR emit_date = '20990102'"))
            db.commit()
        finally:
            db.close()


def test_hit_rate_direction_and_counts():
    try:
        # 3 条 long: T+1 = +5, -1, +2 → 胜率 66.7%, 均值 2.0
        for i, o in enumerate((5.0, -1.0, 2.0)):
            record_signal("gs_signal", f"60000{i}", direction="long", emit_date="20990110")
        from sqlalchemy import text
        from src.web.database import SessionLocal
        db = SessionLocal()
        try:
            for i, o in enumerate((5.0, -1.0, 2.0)):
                db.execute(
                    text("UPDATE signal_snapshots SET outcome_t1 = :o, checked_t1 = 1"
                         " WHERE signal_type = 'gs_signal' AND symbol = :s AND emit_date = '20990110'"),
                    {"o": o, "s": f"60000{i}"},
                )
            db.commit()
        finally:
            db.close()
        r = hit_rate(signal_type="gs_signal", days=3650)
        t1 = r["by_type"]["gs_signal"]["t1"]
        assert t1["n"] == 3
        assert abs(t1["win_rate"] - 66.67) < 0.01
        assert abs(t1["avg_pct"] - 2.0) < 0.01
        assert "official_benchmark" not in r["by_type"]["gs_signal"]  # gs 无官方基准
    finally:
        from sqlalchemy import text
        from src.web.database import SessionLocal
        db = SessionLocal()
        try:
            db.execute(text("DELETE FROM signal_snapshots WHERE emit_date = '20990110'"))
            db.commit()
        finally:
            db.close()


def test_benchmark_only_for_resonance():
    assert "resonance" in OFFICIAL_BENCHMARK
    assert OFFICIAL_BENCHMARK["resonance"]["win_rate"] == 75.42
