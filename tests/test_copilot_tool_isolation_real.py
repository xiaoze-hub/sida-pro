# -*- coding: utf-8 -*-
"""工具统一身份保护: **真实**跨用户隔离测试(本人/他人/空身份/服务身份/外部旁路)。

与 `test_copilot_execute_auth_boundary.py` 的分工:
那边用临时注册的假 handler 验证「守卫先于 handler」这一个点位; 本文件**不替换任何
handler**, 直接走 `_execute_tool` → `CHAT_TOOL_REGISTRY` → 真实的 `_tool_get_watchlist`
/ `_tool_get_notifications`, 用真实 SQLAlchemy 会话查真实表, 断言实际返回内容。

隔离边界(先确认, 再断言):
- 库文件建在 pytest `tmp_path` 下, 与 conftest 的 `SIDA_DB_URL` 临时库、仓库
  `data/sida.db`、生产库**都不是同一个文件**; fixture 里显式断言路径落点。
- 只 `create_all` 本用例需要的 users / stocks / notifications 三张表, 不跑迁移。
- 不 monkeypatch 注册表、不 patch handler、不改 `requires`; 断言注册表里就是真实函数。

契约(改动前已存在, 本文件只做回归):
- `requires == "self"` 的工具在**未登录 / 服务身份**下必须在 handler 之前被拒,
  且不带出任何一行数据(含全局记录)。
- 已登录用户只看到**本人 + user_id IS NULL 的全局记录**, 看不到他人记录。
- 外部 skills gateway 对 self 工具一律不开放(BLOCKED 或不在 OPEN_SKILLS),
  HTTP 实测同样拒绝。
"""

from __future__ import annotations

import asyncio
import inspect
import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from src.agents.chat.registry import CHAT_TOOL_REGISTRY
from src.db.models import Base, Notification, Stock, User
from src.web.api.auth import ServicePrincipal
from src.web.api.chat import _REQ_BUDGET, _REQ_TOOLS, _execute_tool

SELF_TOOLS = ("get_watchlist", "get_notifications")
REJECT_MSG = "该工具需要已登录的用户身份。"


# ─────────────────────── 隔离 SQLite 测试库 ───────────────────────


@pytest.fixture()
def iso_engine(tmp_path):
    """独立 SQLite 文件库: 路径必须是 tmp_path 下的文件, 不是任何生产库。"""
    db_path = tmp_path / "copilot_tool_isolation.db"
    engine = create_engine(f"sqlite:///{db_path}", future=True)

    # 打开 FK 约束, 让「挂到不存在用户」的脏数据在这里就炸, 而不是静默漏过。
    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _rec):  # noqa: ANN001
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(
        engine,
        tables=[User.__table__, Stock.__table__, Notification.__table__],
    )

    resolved = str(engine.url.database)
    assert resolved == str(db_path)
    assert resolved.startswith(str(tmp_path))
    assert "data/sida.db" not in resolved
    yield engine
    engine.dispose()


@pytest.fixture()
def iso_db(iso_engine):
    Session = sessionmaker(bind=iso_engine, expire_on_commit=False, future=True)
    db = Session()
    try:
        yield db
    finally:
        db.rollback()
        db.close()


@pytest.fixture()
def seeded(iso_db):
    """两位真实用户 + 各自私有自选/通知 + 全局(NULL)记录。

    名字里带一次性随机 tag, 断言用 tag 而不是 "Alice"/"Bob" 这类可能撞车的词。
    """
    tag = uuid.uuid4().hex[:8]
    alice = User(id=str(uuid.uuid4()), username=f"alice_{tag}", password_hash="x",
                 role="member", is_active=True, token_version=1)
    bob = User(id=str(uuid.uuid4()), username=f"bob_{tag}", password_hash="x",
               role="member", is_active=True, token_version=1)
    iso_db.add_all([alice, bob])
    iso_db.flush()

    names = {
        "alice_stock": f"ALICE-{tag}",
        "bob_stock": f"BOB-{tag}",
        "global_stock": f"GLOBAL-{tag}",
        "alice_notify": f"ALICE-NOTIFY-{tag}",
        "bob_notify": f"BOB-NOTIFY-{tag}",
        "global_notify": f"GLOBAL-NOTIFY-{tag}",
    }
    now = datetime(2026, 11, 7, 15, 0, 0)
    iso_db.add_all([
        Stock(user_id=alice.id, symbol="300001", name=names["alice_stock"], market="CN", sort_order=1),
        Stock(user_id=bob.id, symbol="300002", name=names["bob_stock"], market="CN", sort_order=2),
        Stock(user_id=None, symbol="600000", name=names["global_stock"], market="CN", sort_order=0),
        # 时间故意错开, 让「按 created_at 倒序」的断言是确定的
        Notification(user_id=alice.id, category="agent_run", level="info",
                     title=names["alice_notify"], body="alice private body",
                     created_at=now),
        Notification(user_id=bob.id, category="agent_run", level="warning",
                     title=names["bob_notify"], body="bob private body",
                     created_at=now + timedelta(minutes=2)),
        Notification(user_id=None, category="system", level="info",
                     title=names["global_notify"], body="global broadcast",
                     created_at=now + timedelta(minutes=1)),
    ])
    iso_db.commit()
    return {"alice": alice, "bob": bob, "tag": tag, "names": names}


@pytest.fixture(autouse=True)
def _clean_request_ctx():
    """清掉本线程残留的请求态白名单/预算, 避免邻居用例的 ContextVar 影响判据。"""
    t_tools = _REQ_TOOLS.set(None)
    t_budget = _REQ_BUDGET.set(None)
    try:
        yield
    finally:
        _REQ_BUDGET.reset(t_budget)
        _REQ_TOOLS.reset(t_tools)


def _run_tool(db, name, user, args=None):
    return asyncio.run(_execute_tool(db, name, args or {}, user))


def _watchlist_names(text: str) -> set[str]:
    """从 "自选股列表：\\n- 名称(CN:代码)" 里取出名称集合(精确比对, 不做子串猜测)。"""
    return {ln[2:].split("(", 1)[0] for ln in text.splitlines() if ln.startswith("- ")}


def _notify_total(text: str) -> int:
    """取 "（共N条）" 里的 N。"""
    marker = text.rsplit("共", 1)[-1].split("条", 1)[0]
    return int(marker)


# ─────────────────────── 0. 调用链真实性 ───────────────────────


def test_self_tools_are_registered_with_real_handlers():
    """不替换 handler 的前提: 注册表里挂的就是 registry 模块里的真实函数。"""
    for name in SELF_TOOLS:
        tool = CHAT_TOOL_REGISTRY[name]
        assert tool.requires == "self"
        assert tool.schema is not None  # 模型可见
        assert inspect.getmodule(tool.handler).__name__ == "src.agents.chat.registry"
        assert tool.handler.__name__ == f"_tool_{name}"
    # 证明两枚工具确实在同一个注册表里被 _execute_tool 分发(不是测试自建入口)
    assert set(SELF_TOOLS) <= set(CHAT_TOOL_REGISTRY)


# ─────────────────────── 1. 自选股: 本人 / 他人 / 全局 ───────────────────────


@pytest.mark.parametrize("who", ["alice", "bob"])
def test_watchlist_owner_sees_own_and_global_not_others(iso_db, seeded, who):
    me, other = (("alice", "bob") if who == "alice" else ("bob", "alice"))
    text = _run_tool(iso_db, "get_watchlist", seeded[me])

    assert _watchlist_names(text) == {
        seeded["names"][f"{me}_stock"],
        seeded["names"]["global_stock"],
    }
    assert seeded["names"][f"{other}_stock"] not in text


def test_watchlist_other_user_rows_exist_so_isolation_is_meaningful(iso_db, seeded):
    """反向自证: 他人/总计行真的在库里, 上面的「看不到」不是空库造成的假绿。"""
    assert iso_db.query(Stock).count() == 3
    assert iso_db.query(Stock).filter(Stock.user_id == seeded["bob"].id).count() == 1


@pytest.mark.parametrize(
    "anonymous",
    [
        None,
        SimpleNamespace(id=None),
        SimpleNamespace(),  # 连 id 属性都没有
        SimpleNamespace(id=""),  # 空串身份
    ],
    ids=["none", "id-none", "no-attr", "id-empty"],
)
def test_watchlist_empty_identity_rejected_without_leak(iso_db, seeded, anonymous):
    text = _run_tool(iso_db, "get_watchlist", anonymous)

    assert text == REJECT_MSG
    for key in ("alice_stock", "bob_stock", "global_stock"):
        assert seeded["names"][key] not in text


def test_watchlist_service_principal_rejected_before_handler_and_budget(iso_db, seeded):
    budget = {"max": 5, "used": 0}
    token = _REQ_BUDGET.set(budget)
    try:
        text = _run_tool(iso_db, "get_watchlist", ServicePrincipal())
    finally:
        _REQ_BUDGET.reset(token)

    assert text == REJECT_MSG
    assert budget["used"] == 0  # 身份拦截先于预算计费
    for key in ("alice_stock", "bob_stock", "global_stock"):
        assert seeded["names"][key] not in text


# ─────────────────────── 2. 通知: 本人 / 他人 / 全局 ───────────────────────


@pytest.mark.parametrize("who", ["alice", "bob"])
def test_notifications_owner_sees_own_and_global_not_others(iso_db, seeded, who):
    me, other = (("alice", "bob") if who == "alice" else ("bob", "alice"))
    text = _run_tool(iso_db, "get_notifications", seeded[me], {"limit": 10})

    assert seeded["names"][f"{me}_notify"] in text
    assert seeded["names"]["global_notify"] in text
    assert seeded["names"][f"{other}_notify"] not in text
    other_body = "bob private body" if who == "alice" else "alice private body"
    assert other_body not in text
    assert _notify_total(text) == 2  # 库里共 3 条, 只应数到本人+全局


def test_notifications_unread_only_stays_in_scope(iso_db, seeded):
    text = _run_tool(iso_db, "get_notifications", seeded["alice"], {"unread_only": True})

    assert _notify_total(text) == 2
    assert seeded["names"]["alice_notify"] in text
    assert seeded["names"]["global_notify"] in text
    assert seeded["names"]["bob_notify"] not in text


@pytest.mark.parametrize(
    "anonymous",
    [None, SimpleNamespace(id=None), SimpleNamespace()],
    ids=["none", "id-none", "no-attr"],
)
def test_notifications_empty_identity_rejected_without_leak(iso_db, seeded, anonymous):
    text = _run_tool(iso_db, "get_notifications", anonymous)

    assert text == REJECT_MSG
    for key in ("alice_notify", "bob_notify", "global_notify"):
        assert seeded["names"][key] not in text
    assert "private body" not in text


def test_notifications_service_principal_rejected_before_handler_and_budget(iso_db, seeded):
    budget = {"max": 5, "used": 0}
    token = _REQ_BUDGET.set(budget)
    try:
        text = _run_tool(iso_db, "get_notifications", ServicePrincipal())
    finally:
        _REQ_BUDGET.reset(token)

    assert text == REJECT_MSG
    assert budget["used"] == 0
    for key in ("alice_notify", "bob_notify", "global_notify"):
        assert seeded["names"][key] not in text


# ─────────────────────── 3. 外部 skills gateway 旁路 ───────────────────────


def test_skills_gateway_allowlist_excludes_every_self_tool():
    from src.web.api import skills_gateway as gw

    self_tools = {n for n, t in CHAT_TOOL_REGISTRY.items() if t.requires == "self"}
    assert {"get_watchlist", "get_notifications"} <= self_tools  # 防空转

    # ① 任何 self 工具都不在对外开放白名单里; ② 两枚个人数据工具显式 BLOCKED。
    assert self_tools & set(gw.OPEN_SKILLS) == set()
    for name in SELF_TOOLS:
        assert name in gw.BLOCKED_SKILLS
    assert not (set(gw.OPEN_SKILLS) & set(gw.BLOCKED_SKILLS))


@pytest.mark.parametrize("name", SELF_TOOLS)
def test_skills_gateway_tier_check_rejects_self_tools(name):
    """档位校验对所有 self 工具都拒绝(将来新增 self 工具也不会被静默放行)。"""
    from fastapi import HTTPException

    from src.web.api import skills_gateway as gw

    ident = SimpleNamespace(channel="guest", tier="free")
    with pytest.raises(HTTPException) as ei:
        gw._check_call_tier(name, ident)
    assert ei.value.status_code in (403, 404)


@pytest.mark.parametrize("name", SELF_TOOLS)
def test_skills_gateway_http_rejects_self_tool_and_returns_no_data(name):
    """真 HTTP 旁路实测: 游客无凭证调 /api/skills/{name}/run 必须被拒。"""
    from fastapi.testclient import TestClient

    from src.web.app import app

    with TestClient(app) as client:
        resp = client.post(
            f"/api/skills/{name}/run",
            json={"args": {}},
            # 过 CSRF 中间件(匿名浏览器同源请求的形态): cookie 与头一致即可,
            # 这里恰是「无登录身份」的外部调用面, 不应被 CSRF 403 掩盖掉档位拒绝。
            cookies={"csrf_token": "iso-test"},
            headers={
                "X-CSRF-Token": "iso-test",
                "X-Forwarded-For": f"203.0.113.{uuid.uuid4().int % 200 + 20}",
            },
        )

    assert resp.status_code == 403
    assert "不对外开放" in resp.text
    assert "自选股列表" not in resp.text
    assert "系统通知" not in resp.text


def test_watchlist_user_id_foreign_key_is_enforced(iso_db):
    """隔离库确实开了 FK: 挂到不存在用户的行写不进去(证明前面的用户是真实行)。"""
    iso_db.add(Stock(user_id="ghost-user", symbol="000001", name="ghost", market="CN"))
    with pytest.raises(IntegrityError):
        iso_db.commit()
    iso_db.rollback()
