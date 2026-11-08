"""今日必读 AI 策展(Phase C) — 排序/兜底的 handler 行为。

2026-11-08 起 `curate_today` 增加了 `user` 依赖(模型权限/BYOK 归属按调用者判定),
直调 handler 时必须显式传 user; `_get_ai_client` 的桩也要接受真实签名
`(db, model_id=None, user=None)` —— 端点内部就是按这个签名传参的。
授权/fail-closed(deny_all/granted/未知 model_id/BYOK/缺身份)的行为断言走真实路由,
见 `tests/test_curate_auth.py`。
"""
from __future__ import annotations
import asyncio
from typing import cast

from src.web.api import dashboard
from src.web.database import SessionLocal
from src.web.models import User


class _User:
    """普通 inherit 用户: 本文件只测排序/兜底, 不触达 DB 的 AIModel/BYOK 解析。"""

    id = "u-curate-test"
    username = "curate_tester"
    role = "member"
    permissions = {"model_access": {"mode": "inherit", "model_ids": []}}


class _FakeAI:
    def __init__(self, r): self._r = r
    async def chat(self, s, u, temperature=0.3): return self._r


def test_curate_orders_by_importance(monkeypatch):
    """AI 返回的重要度用于降序排序,并带 why。"""
    monkeypatch.setattr(
        dashboard,
        "_get_ai_client",
        lambda db, mid=None, user=None: _FakeAI("0|40|小异动\n1|90|提醒触发"),
    )
    req = dashboard.CurateRequest(candidates=[
        dashboard.CurateCandidate(type="watch", symbol="A", name="甲", signal="x"),
        dashboard.CurateCandidate(type="alert", symbol="B", name="乙", signal="y"),
    ])
    db = SessionLocal()
    try:
        res = asyncio.run(dashboard.curate_today(req, db, user=cast(User, _User())))
    finally:
        db.close()
    assert res["items"][0]["index"] == 1
    assert res["items"][0]["importance"] == 90
    assert res["items"][0]["why"] == "y"  # 模型理由不在候选原文中，回退该候选的真实信号


def test_curate_fallback_on_ai_fail(monkeypatch):
    """AI 失败时按原序兜底,不报错。"""
    class _Boom:
        async def chat(self, *a, **k): raise RuntimeError("boom")
    monkeypatch.setattr(dashboard, "_get_ai_client", lambda db, mid=None, user=None: _Boom())
    req = dashboard.CurateRequest(candidates=[dashboard.CurateCandidate(type="alert", symbol="B", name="乙", signal="y")])
    db = SessionLocal()
    try:
        res = asyncio.run(dashboard.curate_today(req, db, user=cast(User, _User())))
    finally:
        db.close()
    assert len(res["items"]) == 1
    assert res["items"][0]["index"] == 0
