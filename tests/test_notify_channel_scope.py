"""通知渠道解析 & 手动触发归属回归(2026-10-29 三审 R4/R5)。

修前实测:

- R4: `POST /api/stocks/{id}/agents/{name}/trigger` 的异步线程里 `_notify` **不传
  user_id** → `notify_task_done` 兜底推给 owner, member 触发的分析结果进了 owner 的
  站内信/渠道; 线程里还直接用了请求级 ORM 对象(`user.id`)。
- R5: `resolve_notify_channels` 在"按用户收窄"时**无视** stock_agent / agent 上配置的
  渠道选择集合 —— 用户只勾了 1 个渠道, 结果所有启用渠道都收到(静默超发); 交集为空
  与"本来没渠道"也无法区分(静默跳过); 共享桶缺渠道同样没声音。

现在的口径:

1. 配置集合存在 → **只在归属可见的启用渠道里取交集**(只缩不扩), 被排除的要留痕;
2. 交集为空 → 一条都不发, reason=config-not-permitted, warning 可见;
3. 该归属没有任何启用渠道 → reason=*-empty, warning 可见(诚实告警, 不静默);
4. 任何情况都**不回退 owner**;
5. 手动触发线程用**起线程前抓好的标量 uid**, 通知归属触发者。
"""

from __future__ import annotations

import uuid

import pytest

from src.bootstrap.runtime import resolve_notify_channels, resolve_notify_channels_ex
from src.db.models import AgentConfig, NotifyChannel, StockAgent, User
from src.web.database import SessionLocal, init_db

PREFIX = "chn"


@pytest.fixture(scope="module", autouse=True)
def _db_ready():
    init_db()


@pytest.fixture()
def env():
    db = SessionLocal()
    tag = uuid.uuid4().hex[:6]
    created = {"users": [], "channels": [], "agents": [], "stock_agents": []}
    try:
        a = User(id=str(uuid.uuid4()), username=f"{PREFIX}_a_{tag}", password_hash="x",
                 role="member", is_active=True, token_version=1)
        b = User(id=str(uuid.uuid4()), username=f"{PREFIX}_b_{tag}", password_hash="x",
                 role="member", is_active=True, token_version=1)
        db.add_all([a, b])
        db.commit()
        created["users"] = [a.id, b.id]

        def ch(uid, name, enabled=True):
            c = NotifyChannel(name=f"{PREFIX}-{name}-{tag}", type="bark", config={},
                              enabled=enabled, user_id=uid)
            db.add(c)
            db.commit()
            created["channels"].append(c.id)
            return c

        ch_a1 = ch(a.id, "a1")
        ch_a2 = ch(a.id, "a2")
        ch_a_off = ch(a.id, "a-off", enabled=False)
        ch_b = ch(b.id, "b")
        ch_shared = ch(None, "shared")

        agent_name = f"{PREFIX}_agent_{tag}"
        agent = AgentConfig(name=agent_name, display_name=agent_name, kind="workflow", enabled=True)
        db.add(agent)
        db.commit()
        created["agents"].append(agent.id)

        yield {
            "db": db,
            "users": {"a": a, "b": b},
            "channels": {"a1": ch_a1, "a2": ch_a2, "a_off": ch_a_off, "b": ch_b, "shared": ch_shared},
            "agent": agent,
            "agent_name": agent_name,
            "created": created,
        }
    finally:
        db.rollback()
        db.query(StockAgent).filter(StockAgent.id.in_(created["stock_agents"])).delete(
            synchronize_session=False
        )
        db.query(AgentConfig).filter(AgentConfig.id.in_(created["agents"])).delete(
            synchronize_session=False
        )
        db.query(NotifyChannel).filter(NotifyChannel.id.in_(created["channels"])).delete(
            synchronize_session=False
        )
        from tests.conftest import purge_users

        purge_users(db, ids=created["users"])
        db.commit()
        db.close()


def _ids(channels) -> set[int]:
    return {int(c.id) for c in channels}


# ──────────────────────────── R5: 解析口径 ────────────────────────────


def test_user_scope_without_config_takes_own_plus_shared(env):
    a = env["users"]["a"]
    res = resolve_notify_channels_ex(env["agent_name"], user_id=a.id)
    assert res.scope == "user"
    assert res.reason == "no-config"
    ids = _ids(res.channels)
    assert ids == {env["channels"]["a1"].id, env["channels"]["a2"].id, env["channels"]["shared"].id}
    assert env["channels"]["b"].id not in ids
    assert env["channels"]["a_off"].id not in ids, "禁用渠道不得选中"


def test_config_selection_narrows_and_does_not_over_send(env):
    """配置只勾 a1 → 只发 a1, 不再"所有渠道都收到"。"""
    a = env["users"]["a"]
    agent = env["agent"]
    agent.notify_channel_ids = [env["channels"]["a1"].id]
    env["db"].commit()

    res = resolve_notify_channels_ex(env["agent_name"], user_id=a.id)
    assert res.reason == "ok"
    assert _ids(res.channels) == {env["channels"]["a1"].id}
    assert res.configured_ids == (env["channels"]["a1"].id,)


def test_config_pointing_at_foreign_channel_is_dropped_not_leaked(env):
    """配置里只勾了 B 的私有渠道: 这条配置对 A 不适用 → 绝不发给 B, 退回 A 自己的渠道并告警。"""
    a = env["users"]["a"]
    agent = env["agent"]
    agent.notify_channel_ids = [env["channels"]["b"].id]
    env["db"].commit()

    res = resolve_notify_channels_ex(env["agent_name"], user_id=a.id)
    ids = _ids(res.channels)
    assert env["channels"]["b"].id not in ids, "绝不能发给配置里他人的私有渠道"
    assert ids == {env["channels"]["a1"].id, env["channels"]["a2"].id, env["channels"]["shared"].id}
    assert res.reason == "config-not-applicable"
    assert res.dropped_ids == (env["channels"]["b"].id,)


def test_config_foreign_and_no_own_channels_sends_nothing(env):
    """配置不相交 + 该归属一条渠道都没有 → 一条都不发(不借 owner/他人)。"""
    db = env["db"]
    b = env["users"]["b"]
    db.query(NotifyChannel).filter(NotifyChannel.user_id == b.id).update(
        {"enabled": False}, synchronize_session=False
    )
    db.query(NotifyChannel).filter(NotifyChannel.user_id.is_(None)).update(
        {"enabled": False}, synchronize_session=False
    )
    env["agent"].notify_channel_ids = [env["channels"]["a1"].id]
    db.commit()

    res = resolve_notify_channels_ex(env["agent_name"], user_id=b.id)
    assert res.channels == []
    assert res.reason == "config-not-permitted"


def test_config_mixed_keeps_only_permitted(env):
    a = env["users"]["a"]
    agent = env["agent"]
    agent.notify_channel_ids = [env["channels"]["b"].id, env["channels"]["a2"].id]
    env["db"].commit()

    res = resolve_notify_channels_ex(env["agent_name"], user_id=a.id)
    assert _ids(res.channels) == {env["channels"]["a2"].id}
    assert res.dropped_ids == (env["channels"]["b"].id,)


def test_shared_bucket_empty_is_reported_honestly(env):
    """共享桶没有任何渠道 → 空列表 + 明确 reason(不静默、不改发 owner)。"""
    env["db"].query(NotifyChannel).filter(NotifyChannel.user_id.is_(None)).update(
        {"enabled": False}, synchronize_session=False
    )
    env["db"].commit()
    res = resolve_notify_channels_ex(env["agent_name"], user_id=None)
    assert res.channels == []
    assert res.reason == "shared-bucket-empty"


def test_shared_bucket_never_falls_back_to_owner_channels(env):
    """owner 有私有渠道时, 共享桶(uid=None)也不许借来发。"""
    db = env["db"]
    owner = User(id=str(uuid.uuid4()), username=f"{PREFIX}_o_{uuid.uuid4().hex[:6]}",
                 password_hash="x", role="owner", is_active=True, token_version=1)
    db.add(owner)
    db.commit()
    ch = NotifyChannel(name=f"{PREFIX}-owner-{uuid.uuid4().hex[:6]}", type="bark",
                       config={}, enabled=True, user_id=owner.id)
    db.add(ch)
    db.commit()
    try:
        res = resolve_notify_channels_ex(env["agent_name"], user_id=None)
        assert ch.id not in _ids(res.channels)
    finally:
        db.query(NotifyChannel).filter(NotifyChannel.id == ch.id).delete(synchronize_session=False)
        from tests.conftest import purge_users

        purge_users(db, ids=[owner.id])
        db.commit()


def test_public_api_is_channels_view(env):
    a = env["users"]["a"]
    chans = resolve_notify_channels(env["agent_name"], user_id=a.id)
    assert isinstance(chans, list)
    assert _ids(chans) == _ids(resolve_notify_channels_ex(env["agent_name"], user_id=a.id).channels)


# ──────────────────────────── R4: 手动触发归属 ────────────────────────────


def test_async_trigger_notifies_the_triggering_user(monkeypatch):
    """异步线程必须把通知归给**触发者**, 不允许落回 owner 兜底。

    真 HTTP + 真线程: 端点 `wait=false` 起后台线程 → 线程里调 `notify_task_done`。
    这里把 `server.trigger_agent_for_stock`(agent 本体, 会连外网)换成立即抛错,
    于是线程走到失败分支 `_notify(False, ...)` —— 正是修前把结果推给 owner 的那条路。
    """
    import time

    import server as server_mod
    from fastapi.testclient import TestClient

    from src.db.models import Notification, Stock, StockAgent, User
    from src.web.database import SessionLocal

    db = SessionLocal()
    tag = uuid.uuid4().hex[:6]
    created = {"users": [], "stocks": [], "stock_agents": []}
    captured: dict = {}
    try:
        u = User(id=str(uuid.uuid4()), username=f"{PREFIX}_r4_{tag}", password_hash="x",
                 role="member", is_active=True, token_version=1)
        db.add(u)
        db.commit()
        created["users"] = [u.id]

        st = Stock(symbol=f"TR{tag}", name=f"TR{tag}", market="CN", user_id=u.id)
        db.add(st)
        db.commit()
        created["stocks"] = [st.id]

        sa = StockAgent(stock_id=st.id, agent_name="daily_report")
        db.add(sa)
        db.commit()
        created["stock_agents"] = [sa.id]

        from src.web.api.auth import create_token

        token, _ = create_token(u)

        def boom(*_a, **_k):
            raise RuntimeError("agent down")

        monkeypatch.setattr(server_mod, "trigger_agent_for_stock", boom, raising=False)

        def fake_notify(label, **kw):
            captured.update(kw)
            captured["label"] = label
            return 1

        monkeypatch.setattr("src.core.notify_center.notify_task_done", fake_notify)

        from src.web.app import app

        client = TestClient(app)
        r = client.post(
            f"/api/stocks/{st.id}/agents/daily_report/trigger",
            params={"wait": "false", "bypass_throttle": "true", "bypass_market_hours": "true"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 200, r.text

        for _ in range(100):
            if captured:
                break
            time.sleep(0.05)
        assert captured, "后台线程没有发出通知(可能线程未跑完或 _notify 未调用)"
        assert captured.get("user_id") == u.id, (
            f"通知归属必须是触发者 {u.id}, 实际 {captured.get('user_id')}(修前为 None → 落到 owner)"
        )
    finally:
        db.rollback()
        db.query(StockAgent).filter(StockAgent.id.in_(created["stock_agents"])).delete(
            synchronize_session=False
        )
        db.query(Notification).filter(Notification.user_id.in_(created["users"])).delete(
            synchronize_session=False
        )
        db.query(Stock).filter(Stock.id.in_(created["stocks"])).delete(synchronize_session=False)
        from tests.conftest import purge_users

        purge_users(db, ids=created["users"])
        db.commit()
        db.close()
