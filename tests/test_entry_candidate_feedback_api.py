"""入场候选反馈 API 测试(2026-08-13)。

覆盖: POST /entry-candidates/feedback 提交、GET 查询、最新一条优先去重、清理。
"""

import pytest
from fastapi.testclient import TestClient

from src.web.database import SessionLocal
from src.web.models import EntryCandidateFeedback

TEST_SYMBOL = "TSTFB0001"
SNAP = "2099-01-01"  # 不可能的真实快照日期, 避免污染业务数据


@pytest.fixture()
def client():
    from src.web.app import app

    return TestClient(app)


@pytest.fixture()
def token(client):
    r = client.post("/api/auth/login", json={"username": "admin", "password": "xz.170530"})
    assert r.status_code == 200, r.text
    return r.json()["data"]["token"]


@pytest.fixture(autouse=True)
def clean_test_rows():
    """每个测试后清理测试符号的反馈行。"""
    yield
    db = SessionLocal()
    try:
        db.query(EntryCandidateFeedback).filter(
            EntryCandidateFeedback.stock_symbol == TEST_SYMBOL,
            EntryCandidateFeedback.snapshot_date == SNAP,
        ).delete()
        db.commit()
    finally:
        db.close()


def _submit(client, token, useful):
    r = client.post(
        "/api/recommendations/entry-candidates/feedback",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "snapshot_date": SNAP,
            "stock_symbol": TEST_SYMBOL,
            "stock_market": "CN",
            "useful": useful,
            "candidate_source": "watchlist",
            "strategy_tags": ["trend_follow"],
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["ok"] is True
    return r


def test_submit_candidate_feedback(client, token):
    """提交反馈返回 ok。"""
    _submit(client, token, True)


def test_query_candidate_feedback_latest_wins(client, token):
    """同一标的多次反馈, GET 只返回最新一条(最新优先)。"""
    _submit(client, token, True)
    _submit(client, token, False)
    r = client.get(
        f"/api/recommendations/entry-candidates/feedback?snapshot_date={SNAP}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    items = [i for i in r.json()["data"]["items"] if i["stock_symbol"] == TEST_SYMBOL]
    assert len(items) == 1
    assert items[0]["useful"] is False
    assert items[0]["snapshot_date"] == SNAP
    assert items[0]["stock_market"] == "CN"


def test_query_candidate_feedback_requires_auth(client):
    """未登录访问反馈接口应 401。"""
    r = client.get("/api/recommendations/entry-candidates/feedback")
    assert r.status_code == 401
