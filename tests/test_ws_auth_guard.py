"""WS 鉴权与广播隔离回归(2026-09-08 T8 审计修复)。

- 原 WS 握手只 decode_token 验签, 不校验 is_active/token_version:
  禁用账号/改密踢人后, 旧 JWT 在剩余有效期(默认 12h)内仍可连 WS 收通知/行情。
- 原行情聚合器收集所有用户持仓并向全体广播: 任一账号可通过推送的 symbol
  集合推断他人的持仓/自选(隐私泄露)。

覆盖:
  - verify_ws_token_payload: 禁用/版本不符/畸形 ver 全拒
  - quote_stream 广播按用户 symbol 集过滤(A 看不到仅属于 B 的标的)
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.web.api.auth import verify_ws_token_payload
from src.web.api import quote_stream as qs
from src.web.models import Base, User

U_ACTIVE = "aaaa1111-0000-0000-0000-000000000001"
U_DISABLED = "aaaa1111-0000-0000-0000-000000000002"
U_OLDVER = "aaaa1111-0000-0000-0000-000000000003"


@pytest.fixture()
def db_session(monkeypatch):
    eng = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(eng, tables=[User.__table__])
    TestingSession = sessionmaker(bind=eng, autoflush=False, expire_on_commit=False)
    import src.web.database as db_mod

    monkeypatch.setattr(db_mod, "SessionLocal", TestingSession)
    db = TestingSession()
    db.add_all(
        [
            User(id=U_ACTIVE, username="active", password_hash="x", role="member",
                 is_active=True, token_version=2),
            User(id=U_DISABLED, username="disabled", password_hash="x", role="member",
                 is_active=False, token_version=1),
            User(id=U_OLDVER, username="oldver", password_hash="x", role="member",
                 is_active=True, token_version=5),
        ]
    )
    db.commit()
    yield db, TestingSession
    db.close()


def test_verify_ws_ok(db_session):
    db, _ = db_session
    assert verify_ws_token_payload(db, {"sub": U_ACTIVE, "ver": 2}) == U_ACTIVE


def test_verify_ws_rejects_disabled(db_session):
    db, _ = db_session
    assert verify_ws_token_payload(db, {"sub": U_DISABLED, "ver": 1}) is None


def test_verify_ws_rejects_stale_version(db_session):
    """改密后 token_version 已递增, 旧 ver 的 JWT 被拒。"""
    db, _ = db_session
    assert verify_ws_token_payload(db, {"sub": U_OLDVER, "ver": 4}) is None


def test_verify_ws_rejects_malformed_ver(db_session):
    db, _ = db_session
    assert verify_ws_token_payload(db, {"sub": U_ACTIVE, "ver": "abc"}) is None
    assert verify_ws_token_payload(db, None) is None
    assert verify_ws_token_payload(db, {"ver": 2}) is None


def test_broadcast_filtered_per_user(monkeypatch):
    """A 的订阅收不到仅属于 B 的标的行情。"""
    qs._last_snapshot.clear()
    with qs._subscribers_lock:
        qs._subscribers.clear()
    # per-user 关注缓存: A 只关注 600519, B 只关注 000001
    with qs._symbols_lock:
        qs._user_symbols_cache.clear()
        qs._user_symbols_cache.update(
            {"userA": {"600519"}, "userB": {"000001"}, "": set(), None: set()}
        )

    sid_a, q_a = qs.subscribe("userA")
    sid_b, q_b = qs.subscribe("userB")
    try:
        qs._broadcast(
            {"type": "quotes", "ts": 0.0, "data": {"600519": {"price": 1500}, "000001": {"price": 10}}}
        )
        # put_nowait 塞入, 同步取即可(不依赖事件循环, 全量 suite 里更稳)
        frame_a = q_a.get_nowait()
        frame_b = q_b.get_nowait()
        syms_a = set((frame_a["payload"].get("data") or {}).keys())
        syms_b = set((frame_b["payload"].get("data") or {}).keys())
        assert syms_a == {"600519"}   # A 收不到 B 的 000001
        assert syms_b == {"000001"}
        # 定向帧的 envelope.user_id 是本人(replay 过滤口径一致)
        assert frame_a["user_id"] == "userA"
        assert frame_b["user_id"] == "userB"
    finally:
        qs.unsubscribe(sid_a)
        qs.unsubscribe(sid_b)
        with qs._symbols_lock:
            qs._user_symbols_cache.clear()


def test_broadcast_skips_user_with_no_watchlist(monkeypatch):
    """无关注标的的用户不收任何行情帧(不泄露他人集合)。"""
    monkeypatch.setattr(qs, "_collect_watchlist_symbols", lambda: {})
    qs._last_snapshot.clear()
    with qs._subscribers_lock:
        qs._subscribers.clear()
    with qs._symbols_lock:
        qs._user_symbols_cache.clear()
        qs._user_symbols_cache.update({"userA": set(), None: set()})

    sid_a, q_a = qs.subscribe("userA")
    try:
        qs._broadcast({"type": "quotes", "ts": 0.0, "data": {"600519": {"price": 1500}}})
        assert q_a.empty()
    finally:
        qs.unsubscribe(sid_a)
        with qs._symbols_lock:
            qs._user_symbols_cache.clear()
