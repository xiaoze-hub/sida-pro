"""P2 成熟化回归: 统一 envelope + PG 陈旧快照 failover。

- envelope.pack 打出 seq/ts/topic/user_id/payload, seq 单调递增
- replay_since 只回 seq 更大的帧, user_id 过滤(* 全播帧人人可回)
- _pg_klines: 新鲜 PG 返回 (bars, asof); 最新 bar 超 12 天 → (None,None) 回落联网
"""
from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

from src.models.market import MarketCode
from src.web.realtime import envelope as env


def _fresh_bars(n=40, end=None):
    end = end or date.today()
    return [(end - timedelta(days=i)).isoformat() for i in range(n - 1, -1, -1)]


def test_envelope_seq_monotonic():
    env.reset_for_tests()
    a = env.pack("quote.tick", None, {"type": "quotes"})
    b = env.pack("notif.push", "u1", {"id": 1})
    assert set(a) == {"seq", "ts", "topic", "user_id", "payload"}
    assert b["seq"] > a["seq"]
    assert a["user_id"] == "*"


def test_replay_filters(monkeypatch):
    monkeypatch.setenv("REDIS_DISABLED", "1")  # 强制进程内 seq, 不依赖外部 Redis
    env.reset_for_tests()
    f1 = env.pack("quote.tick", None, {"n": 1})
    f2 = env.pack("notif.push", "u1", {"n": 2})
    f3 = env.pack("notif.push", "u2", {"n": 3})
    assert env.replay_since(0) == []
    assert env.replay_since("bad") == []
    all_frames = env.replay_since(f1["seq"])
    assert [f["seq"] for f in all_frames] == [f2["seq"], f3["seq"]]
    mine = env.replay_since(f1["seq"], user_id="u1")
    assert [f["seq"] for f in mine] == [f2["seq"]]  # 全播帧无(本批全定向); u2 的被滤掉
    assert env.replay_since(f3["seq"]) == []


def _pg_engine(dates):
    eng = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    with eng.begin() as c:
        c.execute(
            text(
                "CREATE TABLE klines (symbol TEXT, market TEXT, period TEXT, source TEXT,"
                " ts TEXT, open REAL, high REAL, low REAL, close REAL, volume REAL)"
            )
        )
        for d in dates:
            c.execute(
                text(
                    "INSERT INTO klines VALUES ('600519','CN','1d','tencent',"
                    f" '{d}', 1500, 1510, 1490, 1505, 100000)"
                )
            )
    return eng


def test_pg_fresh_returns_asof(monkeypatch):
    import src.web.database as db_mod
    from src.web.api.klines import _pg_klines

    eng = _pg_engine(_fresh_bars(40))
    monkeypatch.setattr(db_mod, "engine", eng)
    bars, asof = _pg_klines("600519", MarketCode("CN"), 60)
    assert bars is not None and len(bars) == 40
    assert asof == date.today().isoformat()


def test_pg_stale_falls_through(monkeypatch):
    """最新 bar 30 天前 → 陈旧快照 → (None,None) → 调用方回落联网, 不再静默服务旧数。"""
    import src.web.database as db_mod
    from src.web.api.klines import _pg_klines

    old_end = date.today() - timedelta(days=30)
    eng = _pg_engine(_fresh_bars(40, end=old_end))
    monkeypatch.setattr(db_mod, "engine", eng)
    bars, asof = _pg_klines("600519", MarketCode("CN"), 60)
    assert (bars, asof) == (None, None)


def test_pg_thin_or_empty_falls_through(monkeypatch):
    import src.web.database as db_mod
    from src.web.api.klines import _pg_klines

    eng = _pg_engine(_fresh_bars(10))  # 过薄(<30)
    monkeypatch.setattr(db_mod, "engine", eng)
    assert _pg_klines("600519", MarketCode("CN"), 60) == (None, None)
    eng2 = _pg_engine([])
    monkeypatch.setattr(db_mod, "engine", eng2)
    assert _pg_klines("600519", MarketCode("CN"), 60) == (None, None)
