"""盘前分析 Agent 后端接线测试 (B 支线, 2026-10-23)。

覆盖:
- GET  /api/premarket/today: 无记录 empty / 当日记录结构化解析 / 历史 is_stale / 跨用户隔离 / 全局共享
- POST /api/premarket/run: 异步排队 + 5 分钟 429 限流 + reset_rate_limit
- GET  /api/premarket/evidence: missing/suspect 块原样透传 + date 参数
- inject_premarket_brief: 当日注入 / 历史显式标注「非今日」/ 无记录不注入 / 并入既有 system
- enter_request_ctx: premarket_strategist 配方自动注入
- get_premarket_brief 聊天工具: 契约(self/fallback/caliber) + 当日/历史/无用户三态
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.core.timezone import beijing_now
from src.db.models import AnalysisHistory

TODAY = beijing_now().strftime("%Y-%m-%d")
YESTERDAY = (beijing_now() - timedelta(days=1)).strftime("%Y-%m-%d")

USER_A = "user-a"
USER_B = "user-b"


class _FakeUser:
    def __init__(self, uid: str):
        self.id = uid


@pytest.fixture()
def db():
    """独立内存 SQLite(建全表) —— 绝不碰 conftest 的共享文件库。"""
    from src.db.session import Base

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


@pytest.fixture(autouse=True)
def _clean_rate_limit():
    """限流是进程内状态, 用例间必须清零(与 conftest _reset_login_ratelimit 同思路)。"""
    from src.web.api import premarket

    premarket.reset_rate_limit()
    yield
    premarket.reset_rate_limit()


def _client_for(db, uid: str) -> TestClient:
    from src.web.api import premarket
    from src.web.api.auth import get_current_user
    from src.web.database import get_db

    app = FastAPI()
    app.include_router(premarket.router, prefix="/api/premarket", tags=["premarket"])
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: _FakeUser(uid)
    return TestClient(app)


def _add_record(
    db,
    uid: str | None,
    date: str,
    *,
    content: str = "底稿正文",
    raw_data: dict | None = None,
    title: str = "",
    status: str = "success",
):
    db.add(
        AnalysisHistory(
            user_id=uid,
            agent_name="premarket_outlook",
            stock_symbol="*",
            analysis_date=date,
            title=title,
            content=content,
            raw_data=raw_data or {},
            status=status,
        )
    )
    db.commit()


# ──────────────── GET /today ────────────────


def test_today_no_record_empty(db):
    client = _client_for(db, USER_A)
    r = client.get("/api/premarket/today")
    assert r.status_code == 200
    body = r.json()
    assert body["empty"] is True
    assert body["date"] == TODAY
    assert body["last"] is None


def test_today_record_structured_from_raw_data(db):
    _add_record(
        db,
        USER_A,
        TODAY,
        content="今日盘前展望正文" * 60,  # 360 字, 摘要应截 280
        title="盘前展望",
        raw_data={
            "gate": {"score": 62, "position_cap": 0.4},
            "mainlines": [{"name": "算力", "strength": 0.8}],
            "plans": [
                {"symbol": "000977", "name": "浪潮信息", "action": "观察", "trigger": "59.5", "stop": "57.2"}
            ],
            "key_levels": [{"symbol": "000977", "kind": "支撑", "price": 57.2}],
        },
    )
    body = _client_for(db, USER_A).get("/api/premarket/today").json()
    assert body["empty"] is False
    assert body["is_stale"] is False
    assert body["date"] == TODAY
    assert body["last"] == {"date": TODAY}
    assert body["title"] == "盘前展望"
    assert len(body["summary"]) <= 280
    assert body["as_of"]
    assert body["quality"] == "ok"
    assert body["structured"]["gate"] == {"score": 62, "position_cap": 0.4}
    assert body["structured"]["mainlines"] == [{"name": "算力", "strength": 0.8}]
    assert body["structured"]["plans"][0]["symbol"] == "000977"
    assert body["key_levels"] == [{"symbol": "000977", "kind": "支撑", "price": 57.2}]


def test_today_record_structured_fallback_content(db):
    """raw_data 空 → 从正文的 fenced JSON 兜底解析(不猜, 解析不出就空结构)。"""
    content = (
        "盘前展望。\n"
        "```json\n"
        '{"gate": {"score": 55, "position_cap": 0.5}, "mainlines": [{"name": "机器人", "strength": 0.6}]}\n'
        "```\n"
    )
    _add_record(db, USER_A, TODAY, content=content, raw_data={})
    body = _client_for(db, USER_A).get("/api/premarket/today").json()
    assert body["empty"] is False
    assert body["structured"]["gate"] == {"score": 55, "position_cap": 0.5}
    assert body["structured"]["mainlines"][0]["name"] == "机器人"


def test_stale_record_is_stale(db):
    """历史记录(非今日) → is_stale=True, 且 date 是记录日期而非今日。"""
    _add_record(db, USER_A, YESTERDAY, content="昨日底稿")
    body = _client_for(db, USER_A).get("/api/premarket/today").json()
    assert body["empty"] is False
    assert body["is_stale"] is True
    assert body["date"] == YESTERDAY


def test_cross_user_isolation_private(db):
    """A 的私有记录对 B 不可见 —— 跨用户隔离。"""
    _add_record(db, USER_A, TODAY, content="A 的私有底稿")
    body = _client_for(db, USER_B).get("/api/premarket/today").json()
    assert body["empty"] is True
    # A 自己看得到
    assert _client_for(db, USER_A).get("/api/premarket/today").json()["empty"] is False


def test_shared_record_visible_to_all(db):
    """user_id=NULL 的全局共享记录, 任何登录用户都可见。"""
    _add_record(db, None, TODAY, content="全局共享底稿")
    body = _client_for(db, USER_B).get("/api/premarket/today").json()
    assert body["empty"] is False
    assert "全局共享底稿" in body["content"]


def test_today_ignores_failed_status(db):
    """status=failed 的记录不算底稿(契约: 只认 success)。"""
    _add_record(db, USER_A, TODAY, content="失败记录", status="failed")
    body = _client_for(db, USER_A).get("/api/premarket/today").json()
    assert body["empty"] is True


# ──────────────── POST /run ────────────────


def test_run_queued(db, monkeypatch):
    from src.web.api import premarket

    calls = []
    monkeypatch.setattr(premarket, "_trigger_run", lambda agent, uid: calls.append((agent, uid)))
    client = _client_for(db, USER_A)
    r = client.post("/api/premarket/run")
    assert r.status_code == 200
    assert r.json() == {"queued": True, "message": "已提交后台执行"}
    assert calls == [("premarket_outlook", USER_A)]


def test_run_rate_limit_429(db, monkeypatch):
    from src.web.api import premarket

    calls = []
    monkeypatch.setattr(premarket, "_trigger_run", lambda agent, uid: calls.append((agent, uid)))
    client = _client_for(db, USER_A)
    assert client.post("/api/premarket/run").status_code == 200
    r = client.post("/api/premarket/run")
    assert r.status_code == 429
    assert "盘前分析触发过于频繁" in r.json()["detail"]
    assert len(calls) == 1  # 被限流的请求没有触发执行
    # reset_rate_limit() 是测试/运维的清理口 —— 清掉后应立即放行
    premarket.reset_rate_limit()
    assert client.post("/api/premarket/run").status_code == 200
    assert len(calls) == 2


def test_run_rate_limit_is_per_user(db, monkeypatch):
    from src.web.api import premarket

    monkeypatch.setattr(premarket, "_trigger_run", lambda agent, uid: None)
    assert _client_for(db, USER_A).post("/api/premarket/run").status_code == 200
    # B 不受 A 的限流影响
    assert _client_for(db, USER_B).post("/api/premarket/run").status_code == 200


# ──────────────── GET /evidence ────────────────

_FAKE_EV = {
    "overnight": {"items": [{"symbol": "usDJI", "price": 40000.0, "change_pct": -0.5}], "quality": "ok", "reason": "", "source": "tencent", "as_of": "2026-10-23T08:00:00"},
    "ladder": {"items": [], "quality": "suspect", "reason": "涨停家数超过全市场股票数上限", "source": "none", "as_of": "2026-10-23T08:00:00"},
    "news": {"items": [], "quality": "missing", "reason": "快讯获取失败: timeout", "source": "none", "as_of": "2026-10-23T08:00:00"},
    "sentiment": {"items": [{"mood": "发酵"}], "quality": "ok", "reason": "", "source": "market_phase", "as_of": "2026-10-23T08:00:00"},
    "watchlist": {"items": [], "quality": "missing", "reason": "数据库会话未提供", "source": "entry_candidates", "as_of": "2026-10-23T08:00:00"},
    "portfolio": {"items": [{"symbol": "000977", "name": "浪潮信息"}], "quality": "ok", "reason": "", "source": "positions", "as_of": "2026-10-23T08:00:00"},
}


def _fake_collect(calls: list):
    def collect(date=None, db=None, user_id=None):
        calls.append({"date": date, "user_id": user_id})
        ev = {k: dict(v) for k, v in _FAKE_EV.items()}
        ev.update({"date": date or TODAY, "as_of": "2026-10-23T08:00:00", "user_id": user_id})
        return ev

    return collect


def test_evidence_blocks_pass_through(db, monkeypatch):
    """missing/suspect 块必须原样透传 —— 取数层已判失真, API 层不屏蔽不猜测。"""
    from src.core import premarket_evidence

    calls = []
    monkeypatch.setattr(premarket_evidence, "collect", _fake_collect(calls))
    body = _client_for(db, USER_A).get("/api/premarket/evidence").json()
    assert body["available"] is True
    assert body["date"] == TODAY
    blocks = body["blocks"]
    assert [b["name"] for b in blocks] == ["overnight", "ladder", "news", "sentiment", "watchlist", "portfolio"]
    assert [b["quality"] for b in blocks] == ["ok", "suspect", "missing", "ok", "missing", "ok"]
    ladder = next(b for b in blocks if b["name"] == "ladder")
    assert ladder["reason"] == "涨停家数超过全市场股票数上限"
    assert calls == [{"date": None, "user_id": USER_A}]


def test_evidence_date_param(db, monkeypatch):
    from src.core import premarket_evidence

    calls = []
    monkeypatch.setattr(premarket_evidence, "collect", _fake_collect(calls))
    r = _client_for(db, USER_A).get("/api/premarket/evidence", params={"date": "20261022"})
    assert r.status_code == 200
    assert calls == [{"date": "20261022", "user_id": USER_A}]
    assert r.json()["date"] == "20261022"


# ──────────────── inject_premarket_brief (harness 注入) ────────────────


def test_inject_premarket_brief_today(db):
    from src.harness.context import inject_premarket_brief

    _add_record(db, USER_A, TODAY, content="今日底稿正文")
    messages = [{"role": "user", "content": "今天怎么看?"}]
    assert inject_premarket_brief(messages, db, _FakeUser(USER_A)) is True
    sys_msgs = [m for m in messages if m.get("role") == "system"]
    assert len(sys_msgs) == 1
    assert f"【今日盘前分析底稿（{TODAY}）】" in sys_msgs[0]["content"]
    assert "今日底稿正文" in sys_msgs[0]["content"]


def test_inject_premarket_brief_history_labeled_not_today(db):
    """历史记录注入时必须显式标注「非今日」, 绝不许把昨日底稿冒充今日。"""
    from src.harness.context import inject_premarket_brief

    _add_record(db, USER_A, YESTERDAY, content="昨日底稿正文")
    messages = [{"role": "system", "content": "你是参谋。"}, {"role": "user", "content": "今天怎么看?"}]
    assert inject_premarket_brief(messages, db, _FakeUser(USER_A)) is True
    sys_content = messages[0]["content"]
    assert f"【历史盘前分析底稿（基准日 {YESTERDAY}，非今日，仅供对照）】" in sys_content
    assert "非今日" in sys_content
    # 既有 system 内容保留(是并入, 不是覆盖)
    assert "你是参谋。" in sys_content


def test_inject_premarket_brief_no_record_no_inject(db):
    from src.harness.context import inject_premarket_brief

    messages = [{"role": "user", "content": "今天怎么看?"}]
    assert inject_premarket_brief(messages, db, _FakeUser(USER_B)) is False
    assert messages == [{"role": "user", "content": "今天怎么看?"}]  # 原样不动


def test_inject_premarket_brief_cross_user_isolation(db):
    """B 注入的是 B 的(或全局)底稿, 绝不注入 A 的私有底稿。"""
    from src.harness.context import inject_premarket_brief

    _add_record(db, USER_A, TODAY, content="A 的私有底稿")
    messages = [{"role": "user", "content": "hi"}]
    assert inject_premarket_brief(messages, db, _FakeUser(USER_B)) is False
    assert "A 的私有底稿" not in str(messages)


def test_enter_request_ctx_auto_inject_premarket_recipe(db, monkeypatch):
    """premarket_strategist 配方命中 → enter_request_ctx 自动注入该用户盘前底稿。"""
    from src.harness import context as hctx

    fake_user = _FakeUser(USER_A)
    monkeypatch.setattr(hctx, "_load_user_of_conv", lambda d, c: fake_user)
    _add_record(db, USER_A, TODAY, content="自动注入底稿")

    conv = SimpleNamespace(stock_symbol=None, stock_market=None, user_id=USER_A)
    body = SimpleNamespace(content="今天大盘怎么看", mentions=[
        SimpleNamespace(type="agent", value="premarket_strategist"),
    ])
    messages = [{"role": "system", "content": "SYSTEM"}, {"role": "user", "content": "今天大盘怎么看"}]
    tokens = hctx.enter_request_ctx(db, conv, body, messages)
    try:
        sys_content = next(m["content"] for m in messages if m.get("role") == "system")
        assert f"【今日盘前分析底稿（{TODAY}）】" in sys_content
        assert "自动注入底稿" in sys_content
    finally:
        hctx.exit_request_ctx(tokens)


def test_enter_request_ctx_no_premarket_recipe_no_inject(db, monkeypatch):
    """非盘前配方(或不带 mention) → 不注入盘前底稿。"""
    from src.harness import context as hctx

    monkeypatch.setattr(hctx, "_load_user_of_conv", lambda d, c: None)
    conv = SimpleNamespace(stock_symbol=None, stock_market=None, user_id=USER_A)
    body = SimpleNamespace(content="600519 怎么样", mentions=[])
    messages = [{"role": "system", "content": "SYSTEM"}, {"role": "user", "content": "600519 怎么样"}]
    tokens = hctx.enter_request_ctx(db, conv, body, messages)
    try:
        assert "盘前分析底稿" not in str(messages)
    finally:
        hctx.exit_request_ctx(tokens)


# ──────────────── get_premarket_brief 聊天工具 ────────────────


def test_get_premarket_brief_tool_contract():
    from src.agents.chat.registry import CHAT_TOOL_REGISTRY

    tool = CHAT_TOOL_REGISTRY["get_premarket_brief"]
    assert tool.requires == "self"
    assert tool.fallback is False
    assert tool.caliber == "盘前分析·当日底稿"
    assert tool.schema is not None
    assert tool.schema["function"]["name"] == "get_premarket_brief"
    assert "date" in tool.schema["function"]["parameters"]["properties"]


def test_get_premarket_brief_tool_today(db):
    from src.agents.chat.registry import CHAT_TOOL_REGISTRY

    _add_record(db, USER_A, TODAY, content="工具读到的底稿")
    out = asyncio.run(CHAT_TOOL_REGISTRY["get_premarket_brief"].handler(db, {}, _FakeUser(USER_A)))
    assert f"【今日盘前分析底稿（{TODAY}）】" in out
    assert "工具读到的底稿" in out


def test_get_premarket_brief_tool_history_date(db):
    from src.agents.chat.registry import CHAT_TOOL_REGISTRY

    _add_record(db, USER_A, YESTERDAY, content="历史底稿")
    out = asyncio.run(
        CHAT_TOOL_REGISTRY["get_premarket_brief"].handler(db, {"date": YESTERDAY}, _FakeUser(USER_A))
    )
    assert f"【历史盘前分析底稿（基准日 {YESTERDAY}，非今日，仅供对照）】" in out


def test_get_premarket_brief_tool_no_user(db):
    from src.agents.chat.registry import CHAT_TOOL_REGISTRY

    out = asyncio.run(CHAT_TOOL_REGISTRY["get_premarket_brief"].handler(db, {}, None))
    assert "需要登录用户" in out
