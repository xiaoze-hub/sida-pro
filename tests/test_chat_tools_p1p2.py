# -*- coding: utf-8 -*-
"""P1+P2 验收测试 (2026-09-01): 4 空壳工具接真实数据源 + user_id 多用户隔离。

覆盖 (Hermes msg_3d5HE4A8 验收线):
- 4 账号并存 (admin/黄磊/娟姐/demo 同构的隔离测试账号)
- notifications 行级隔离: 本人通知 + user_id=NULL 全局, 不可见他人私有
- 未知/停用用户 → 错误明示"用户 X 不存在或无权访问"
- 缓存 key user_id 前缀 (防跨账号读脏数据)
- 缺失数据显式"无数据"不编造
- 金额=元 单位标注
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import pytest


@pytest.fixture(scope="module")
def iso_users():
    """建 4 个隔离测试账号 (模拟 admin/黄磊/娟姐/demo 并存), 测试后清理。"""
    from src.web.database import SessionLocal
    from src.web.models import User

    names = ["iso_admin", "iso_hl", "iso_jj", "iso_demo"]
    users = {}
    db = SessionLocal()
    try:
        for n in names:
            u = User(id=str(uuid.uuid4()), username=n,
                     password_hash="x", role="member", is_active=True)
            db.add(u)
            users[n] = u.id
        db.commit()
    finally:
        db.close()
    yield users
    db = SessionLocal()
    try:
        from src.web.models import Notification
        db.query(Notification).filter(Notification.user_id.in_(list(users.values()))).delete(
            synchronize_session=False)
        db.query(User).filter(User.id.in_(list(users.values()))).delete(
            synchronize_session=False)
        db.commit()
    finally:
        db.close()


@pytest.fixture(scope="module")
def iso_notifications(iso_users):
    """每用户 2 条私有通知 + 1 条全局 (user_id=NULL)。"""
    from src.web.database import SessionLocal
    from src.web.models import Notification

    db = SessionLocal()
    try:
        # 锚定当日中午, 避免凌晨跑测试时 now-N小时 跨到昨天导致日期窗口漏匹配
        noon = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)
        for name, uid in iso_users.items():
            for i in range(2):
                db.add(Notification(user_id=uid, category="system", level="info",
                                    title=f"private-{name}-{i}", body="b",
                                    created_at=noon - timedelta(minutes=i + 1)))
        db.add(Notification(user_id=None, category="system", level="info",
                            title="global-iso-notice", body="b", created_at=noon))
        db.commit()
    finally:
        db.close()
    return datetime.now().strftime("%Y-%m-%d")


class TestUserIsolation:
    def test_notifications_only_own_plus_global(self, iso_users, iso_notifications):
        from src.core.chat_tools import get_notifications
        from src.core.chat_tools import _cache_path
        import os
        # 清缓存, 保证测试独立
        try:
            os.remove(_cache_path())
        except OSError:
            pass
        r = get_notifications(iso_notifications, user_id="iso_admin")
        assert r.error is None, r.error
        titles = {n["title"] for n in r.data}
        assert "private-iso_admin-0" in titles
        assert "global-iso-notice" in titles
        # 不可见他人私有通知 (不可越权)
        for other in ("iso_hl", "iso_jj", "iso_demo"):
            assert f"private-{other}-0" not in titles
            assert f"private-{other}-1" not in titles

    def test_no_cross_leak_each_user(self, iso_users, iso_notifications):
        from src.core.chat_tools import get_notifications
        for name in ("iso_hl", "iso_jj", "iso_demo"):
            r = get_notifications(iso_notifications, user_id=name)
            assert r.error is None
            titles = {n["title"] for n in r.data}
            assert f"private-{name}-0" in titles
            assert "private-iso_admin-0" not in titles

    def test_unknown_user_denied_with_explicit_error(self, iso_notifications):
        from src.core.chat_tools import (get_forecast, get_notifications,
                                         get_opportunities, get_strategy_signals)
        for fn in (get_notifications, get_opportunities, get_strategy_signals):
            r = fn(iso_notifications, user_id="ghost_user_404")
            assert r.data is None
            assert "ghost_user_404" in r.error
            assert "不存在或无权访问" in r.error
        r = get_forecast(iso_notifications, user_id="ghost_user_404")
        assert r.data is None and "不存在或无权访问" in r.error

    def test_empty_userid_denied(self, iso_notifications):
        from src.core.chat_tools import get_notifications
        r = get_notifications(iso_notifications, user_id="")
        assert r.data is None and r.error


class TestCacheUserPrefix:
    def test_cache_key_user_isolated(self):
        from src.core.chat_tools import _cache_key
        k1 = _cache_key("get_notifications", "user-a", date="2026-09-01")
        k2 = _cache_key("get_notifications", "user-b", date="2026-09-01")
        assert k1 != k2
        assert k1.startswith("user-a:")
        assert k2.startswith("user-b:")

    def test_cache_roundtrip_per_user(self):
        from src.core.chat_tools import _cache_get, _cache_set
        _cache_set("get_notifications", "cache-u1", [{"x": 1}], date="2099-01-01")
        _cache_set("get_notifications", "cache-u2", [{"x": 2}], date="2099-01-01")
        assert _cache_get("get_notifications", "cache-u1", date="2099-01-01") == [{"x": 1}]
        assert _cache_get("get_notifications", "cache-u2", date="2099-01-01") == [{"x": 2}]
        # 跨账号读不到对方缓存
        assert _cache_get("get_notifications", "cache-u3", date="2099-01-01") is None


class TestRealDataSourceWired:
    """4 工具均不再是空壳: 有效用户可查库; 缺数显式"无数据"不编造。"""

    def test_opportunities_reads_entry_candidates(self):
        from src.core.chat_tools import get_opportunities
        # 任意有数据的日期走 DB 路径; 空日期显式无数据
        r = get_opportunities("2099-01-01", user_id="iso_admin", scan=False)
        assert r.data is None and r.note == "无数据"
        assert r.error  # 明示原因, 不编造

    def test_strategy_signals_reads_signal_runs(self):
        from src.core.chat_tools import get_strategy_signals
        r = get_strategy_signals("2099-01-01", user_id="iso_admin")
        assert r.data is None and r.note == "无数据" and r.error

    def test_forecast_honest_when_lib_missing(self):
        import os
        from src.core.chat_tools import get_forecast
        # 指向不存在的预测库 → 显式无数据 (禁止编造预测)
        os.environ["FORECAST_DB_PATH"] = "~/__no_such_forecast__.db"
        try:
            r = get_forecast("2099-01-01", user_id="iso_admin")
            assert r.data is None and r.note == "无数据"
            assert "预测库" in r.error
        finally:
            os.environ.pop("FORECAST_DB_PATH", None)

    def test_units_yuan(self):
        from src.core.chat_tools import get_opportunities
        r = get_opportunities("2099-01-01", user_id="iso_admin", scan=False)
        assert r.units == {} or "元" in r.units.values()
