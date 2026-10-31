"""验证码挑战的服务端存储：一次性、会过期、答错也烧掉。"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from src.core import captcha as cap
from src.db.models import CaptchaChallenge
from src.web.api import captcha_store
from src.web.database import Base


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setenv("AUTH_CAPTCHA_SECRET", "unit-test-secret-0123456789")


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _issue(db) -> cap.Captcha:
    item = cap.new_captcha()
    captcha_store.store_challenge(db, item)
    return item


def test_correct_answer_passes_once_then_is_burned(db):
    item = _issue(db)
    assert captcha_store.consume_challenge(db, item.nonce, item.answer.lower()) == (True, "ok")
    assert captcha_store.consume_challenge(db, item.nonce, item.answer) == (False, "unknown")


def test_wrong_answer_also_burns_the_challenge(db):
    """答错就烧：否则同一张图可以被反复猜（OCR/脚本的口子）。"""
    item = _issue(db)
    wrong = "ZZZZZ" if item.answer != "ZZZZZ" else "YYYYY"
    assert captcha_store.consume_challenge(db, item.nonce, wrong) == (False, "mismatch")
    assert captcha_store.consume_challenge(db, item.nonce, item.answer) == (False, "unknown")


def test_unknown_nonce_is_rejected(db):
    assert captcha_store.consume_challenge(db, "never-issued", "ABCDE") == (False, "unknown")


def test_expired_challenge_is_rejected_and_cleaned(db):
    item = _issue(db)
    later = datetime.utcnow() + timedelta(seconds=cap.TTL_SECONDS + 5)
    assert captcha_store.consume_challenge(db, item.nonce, item.answer, now=later) == (False, "expired")
    assert captcha_store.consume_challenge(db, item.nonce, item.answer) == (False, "unknown")


def test_empty_answer_is_rejected(db):
    item = _issue(db)
    assert captcha_store.consume_challenge(db, item.nonce, "") == (False, "missing")


def test_answer_is_stored_only_as_digest(db):
    item = _issue(db)
    rows = db.execute(text("select answer_digest from captcha_challenges")).fetchall()
    assert rows and all(item.answer not in (r[0] or "") for r in rows)


def test_prune_drops_only_long_expired_rows(db):
    old = cap.new_captcha(now=0)  # expires_at ≈ 180
    captcha_store.store_challenge(db, old, now=datetime(1970, 1, 1))
    fresh = _issue(db)
    # 再存一张会触发清理：1970 年那张早该没了
    _issue(db)
    remaining = {r[0] for r in db.execute(text("select nonce from captcha_challenges")).fetchall()}
    assert old.nonce not in remaining
    assert fresh.nonce in remaining
