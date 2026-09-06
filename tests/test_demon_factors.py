"""批次B 存档化单测: 因子落库/直读 roundtrip(SQLite 测试库)。"""
import pytest

from src.core.demon_factors import load_factor_pool, recompute_factors


def _ev(d: str, sealed=1, one_way=0):
    return {
        "trade_date": d, "symbol": "209901", "market": "CN", "name": "测试妖股",
        "prev_close": 10.0, "limit_price": 11.0, "open_price": 11.0, "high_price": 11.0,
        "low_price": 10.8, "close_price": 11.0 if sealed else 10.9,
        "touched": 1, "is_sealed_close": sealed, "one_way": one_way, "open_count": None,
        "first_time": None, "max_seal_amount": None, "limit_days": None, "circ_mv": None,
        "source": "test", "extra": None,
    }


@pytest.fixture()
def _seed_and_cleanup():
    from sqlalchemy import text

    from src.core.limit_up_backfill import _upsert_rows
    from src.web.database import SessionLocal

    n = _upsert_rows([_ev("20990102"), _ev("20990103"), _ev("20990106", one_way=1)])
    yield n
    db = SessionLocal()
    try:
        db.execute(text("DELETE FROM limit_up_events WHERE symbol = '209901'"))
        db.execute(text("DELETE FROM demon_factors WHERE symbol = '209901'"))
        db.commit()
    finally:
        db.close()


def test_factor_roundtrip(_seed_and_cleanup):
    assert _seed_and_cleanup == 3
    r = recompute_factors(["209901"])
    assert r["updated"] >= 1
    pool = load_factor_pool(topn=50)
    row = next((x for x in pool if x["symbol"] == "209901"), None)
    assert row is not None
    assert row["grade"] in ("普通", "活跃", "妖", "极妖")
    assert row["n_events"] == 3
    assert row["max_streak"] >= 2
    assert "排板可参与" in (row["participation"] or "")
    assert "wencai" in " ".join(row["flags"]) or row["flags"]


def test_recompute_updates_existing(_seed_and_cleanup):
    recompute_factors(["209901"])
    r1 = recompute_factors(["209901"])
    assert r1["updated"] == 1  # upsert 不重复插行
    from sqlalchemy import text

    from src.web.database import SessionLocal
    db = SessionLocal()
    try:
        n = db.execute(text("SELECT COUNT(*) FROM demon_factors WHERE symbol = '209901'")).scalar()
        assert n == 1
    finally:
        db.close()
