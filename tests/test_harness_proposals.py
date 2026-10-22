# -*- coding: utf-8 -*-
"""新分析维度提案（P0 采集层，2026-10-22）。

铁律：
- **只记录，不生效** —— 提案绝不改配方、绝不改变任何一次作答；
- **去重**：同一用户同一维度反复提 → 不插新行，只加 `seen_count`（哪条最该补，看次数）；
- **用户隔离**：别人的提案一条都看不到（decide 越权 → None → API 404）；
- 状态流转 accept / reject / snooze 三选一，落 `status` + `decided_at`。
"""
from __future__ import annotations

import asyncio
import json
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.core import harness_proposals as hp
from src.db.models import Base, HarnessProposal
from src.web.database import get_db


# ── 内存库 ────────────────────────────────────────────────────────────────

@pytest.fixture()
def db_session():
    """SQLite 内存库(StaticPool: TestClient 在工作线程跑端点, 必须共享同一连接)。"""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _rec):  # noqa: ANN001
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(bind=engine, tables=[HarnessProposal.__table__])
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    db = Session()
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


@pytest.fixture()
def db(db_session):
    return db_session


@pytest.fixture()
def uid():
    return str(uuid.uuid4())


@pytest.fixture()
def other_uid():
    return str(uuid.uuid4())


# ── propose: 去重 ─────────────────────────────────────────────────────────

def test_propose_creates_row(db, uid):
    row = hp.propose(db, uid, dimension="股东户数变化", reason="想知道筹码是集中还是分散")
    assert row["created"] is True
    assert row["dimension"] == "股东户数变化"
    assert row["status"] == "pending"
    assert row["seen_count"] == 1
    assert row["id"]
    assert db.query(HarnessProposal).count() == 1


def test_propose_dedupes_same_dimension_increments_count(db, uid):
    """同一维度再提 → 不插新行，只加计数，返回既有行(created=False)。"""
    first = hp.propose(db, uid, dimension="股东户数变化", reason="想看筹码集中度")
    second = hp.propose(db, uid, dimension="股东户数变化", reason="还是想看筹码集中度")
    third = hp.propose(db, uid, dimension=" 股东户数变化 ", reason="第三次提")

    assert db.query(HarnessProposal).count() == 1, "重复提案不许插新行"
    assert second["id"] == first["id"]
    assert third["id"] == first["id"]
    assert second["created"] is False and third["created"] is False
    assert second["seen_count"] == 2
    assert third["seen_count"] == 3
    # 原始理由不被后来的措辞覆盖
    assert third["reason"] == first["reason"]


def test_propose_normalizes_whitespace_and_case(db, uid):
    """空白差异/大小写差异必须算同一个维度（否则统计就废了）。"""
    a = hp.propose(db, uid, dimension="龙虎榜 席位风格", reason="看游资偏好")
    b = hp.propose(db, uid, dimension="龙虎榜席位风格", reason="看游资偏好")
    assert a["id"] == b["id"]
    assert db.query(HarnessProposal).count() == 1


def test_different_dimensions_are_separate_rows(db, uid):
    hp.propose(db, uid, dimension="股东户数变化", reason="a")
    hp.propose(db, uid, dimension="龙虎榜席位风格", reason="b")
    assert db.query(HarnessProposal).count() == 2


# ── propose: 用户隔离 ─────────────────────────────────────────────────────

def test_different_users_same_dimension_are_independent(db, uid, other_uid):
    """两个用户提同名维度 → 互不影响，各一行、各自计数。"""
    a = hp.propose(db, uid, dimension="股东户数变化", reason="我要看")
    b = hp.propose(db, other_uid, dimension="股东户数变化", reason="我也要看")

    assert a["id"] != b["id"]
    assert db.query(HarnessProposal).count() == 2

    # 我这边再提 → **只**加我自己的计数
    mine = hp.propose(db, uid, dimension="股东户数变化", reason="再提一次")
    assert mine["seen_count"] == 2
    other_row = db.query(HarnessProposal).filter(
        HarnessProposal.user_id == other_uid).one()
    assert int(other_row.seen_count) == 1, "别人的计数不许被我这边加"

    # 对方自己再提 → 加的是对方的
    theirs = hp.propose(db, other_uid, dimension="股东户数变化", reason="我也再提一次")
    assert theirs["seen_count"] == 2
    assert db.query(HarnessProposal).count() == 2


def test_list_proposals_only_returns_own(db, uid, other_uid):
    hp.propose(db, uid, dimension="股东户数变化", reason="a")
    hp.propose(db, other_uid, dimension="龙虎榜席位风格", reason="b")

    mine = hp.list_proposals(db, uid)
    assert len(mine) == 1
    assert mine[0]["dimension"] == "股东户数变化"
    assert hp.list_proposals(db, other_uid)[0]["dimension"] == "龙虎榜席位风格"


# ── 参数校验 ──────────────────────────────────────────────────────────────

def test_empty_dimension_raises(db, uid):
    with pytest.raises(ValueError):
        hp.propose(db, uid, dimension="", reason="缺维度")
    with pytest.raises(ValueError):
        hp.propose(db, uid, dimension="   ", reason="全空白")
    with pytest.raises(ValueError):
        hp.propose(db, uid, dimension="x", reason="只有一个字")
    assert db.query(HarnessProposal).count() == 0, "非法输入不许落库"


def test_too_long_dimension_raises(db, uid):
    with pytest.raises(ValueError):
        hp.propose(db, uid, dimension="x" * 41, reason="太长")
    # 40 字刚好合法
    row = hp.propose(db, uid, dimension="x" * 40, reason="边界")
    assert row["seen_count"] == 1


def test_missing_user_id_raises(db):
    with pytest.raises(ValueError):
        hp.propose(db, "", dimension="股东户数变化", reason="没有用户")


# ── decide: 状态流转 ──────────────────────────────────────────────────────

def test_decide_accept_sets_status_and_decided_at(db, uid):
    row = hp.propose(db, uid, dimension="股东户数变化", reason="a")
    out = hp.decide(db, uid, row["id"], action="accept")
    assert out["status"] == "accept"
    assert out["decided_at"], "decided_at 必须写"
    assert hp.list_proposals(db, uid, status="pending") == []
    assert len(hp.list_proposals(db, uid, status="accept")) == 1


@pytest.mark.parametrize("action", ["reject", "snooze"])
def test_decide_reject_and_snooze(db, uid, action):
    row = hp.propose(db, uid, dimension="股东户数变化", reason="a")
    out = hp.decide(db, uid, row["id"], action=action)
    assert out["status"] == action
    assert out["decided_at"]


def test_decide_invalid_action_raises(db, uid):
    row = hp.propose(db, uid, dimension="股东户数变化", reason="a")
    with pytest.raises(ValueError):
        hp.decide(db, uid, row["id"], action="delete_everything")
    # 状态没被改
    assert hp.list_proposals(db, uid)[0]["status"] == "pending"


def test_decide_unknown_proposal_returns_none(db, uid):
    assert hp.decide(db, uid, 999999, action="accept") is None


def test_decide_other_users_proposal_returns_none(db, uid, other_uid):
    """越权：别人的提案不能被自己"审核"。"""
    row = hp.propose(db, other_uid, dimension="股东户数变化", reason="b")
    assert hp.decide(db, uid, row["id"], action="accept") is None
    assert hp.list_proposals(db, other_uid)[0]["status"] == "pending", "别人的提案状态不许变"


def test_rejected_dimension_can_be_proposed_again(db, uid):
    """唯一键带 status 的意义：被拒之后再次出现，能重新提一条。"""
    row = hp.propose(db, uid, dimension="股东户数变化", reason="a")
    hp.decide(db, uid, row["id"], action="reject")

    again = hp.propose(db, uid, dimension="股东户数变化", reason="我还是想要")
    assert again["created"] is True
    assert again["status"] == "pending"
    assert again["id"] != row["id"]
    assert len(hp.list_proposals(db, uid)) == 2


def test_deciding_twice_merges_instead_of_violating_unique_key(db, uid):
    """回归：同一维度先 accept、再重新提再 accept → 唯一键 (user,norm,status) 会撞。

    正确行为是**合并**（计数相加、旧行删除），不是抛 IntegrityError。
    """
    first = hp.propose(db, uid, dimension="股东户数变化", reason="a")
    hp.decide(db, uid, first["id"], action="accept")
    second = hp.propose(db, uid, dimension="股东户数变化", reason="b")
    assert second["created"] is True

    out = hp.decide(db, uid, second["id"], action="accept")
    assert out["status"] == "accept"
    assert out["seen_count"] == 2, "合并时计数要相加"
    assert db.query(HarnessProposal).count() == 1, "同一 (user,norm,status) 只留一行"
    assert db.query(HarnessProposal).filter(
        HarnessProposal.status == "accept").count() == 1


# ── stats ────────────────────────────────────────────────────────────────

def test_stats_orders_by_seen_count_desc(db, uid):
    hp.propose(db, uid, dimension="股东户数变化", reason="a")
    hp.propose(db, uid, dimension="龙虎榜席位风格", reason="b")
    hp.propose(db, uid, dimension="龙虎榜席位风格", reason="b2")
    hp.propose(db, uid, dimension="龙虎榜席位风格", reason="b3")

    items = list(hp.stats(db, uid))
    assert len(items) == 2
    assert items[0]["dimension"] == "龙虎榜席位风格"
    assert items[0]["seen_count"] == 3
    assert items[1]["dimension"] == "股东户数变化"
    assert items[1]["seen_count"] == 1


def test_stats_caps_at_ten(db, uid):
    for i in range(12):
        hp.propose(db, uid, dimension=f"维度{i:02d}", reason="r")
    items = list(hp.stats(db, uid))
    assert len(items) == 10
    assert items[0]["dimension"] == "维度00"  # 同分时按最近出现排


def test_stats_only_own_rows(db, uid, other_uid):
    hp.propose(db, uid, dimension="股东户数变化", reason="a")
    hp.propose(db, other_uid, dimension="龙虎榜席位风格", reason="b")
    items = list(hp.stats(db, uid))
    assert [it["dimension"] for it in items] == ["股东户数变化"]


def test_stats_empty_for_unknown_user(db):
    assert list(hp.stats(db, "no-such-user")) == []


# ── 工具层: note_new_dimension ────────────────────────────────────────────

def test_tool_visible_but_excluded_from_fallback():
    """工具进注册表且模型可见, 但**不进无配方兜底**(2026-10-22 改口径, 见 specs RECIPE-006)。

    原先是 schema=None(模型彻底看不见) —— 代价是采集链路永远没有入口、提案表恒空。
    现在改成「可见 + 只挂分析配方 + fallback=False」: 由**路由**挡住闲聊路径, 而不是靠隐藏工具。
    """
    from src.agents.chat.registry import CHAT_TOOL_REGISTRY, no_fallback_tool_names
    from src.web.api import chat as chat_api

    tool = CHAT_TOOL_REGISTRY["note_new_dimension"]
    assert tool.schema is not None, "采集工具必须对模型可见, 否则提案表恒空"
    assert tool.caliber and tool.caliber.strip()
    assert tool.fallback is False, "采集工具不许进闲聊/问价的服务兜底路径"
    assert "note_new_dimension" in no_fallback_tool_names()
    names = [t["function"]["name"] for t in chat_api.CHAT_TOOLS]
    assert "note_new_dimension" in names


def test_tool_mounted_only_in_analysis_recipe():
    """只挂一份分析配方(deep_diagnosis) —— 既不是死管道, 也不到处撒。"""
    from src.harness.recipes import list_recipes

    mounted = [r.id for r in list_recipes() if "note_new_dimension" in r.tools]
    assert mounted == ["deep_diagnosis"], f"采集工具只该挂在 deep_diagnosis, 实际: {mounted}"


def test_tool_not_exposed_as_public_skill():
    """采集层写本人数据 → 不许被 skill 网关对外放。"""
    from src.web.api.skills_gateway import BLOCKED_SKILLS, OPEN_SKILLS

    assert "note_new_dimension" in BLOCKED_SKILLS
    assert "note_new_dimension" not in OPEN_SKILLS


def test_tool_returns_four_fields(db, uid):
    from src.agents.chat.registry import CHAT_TOOL_REGISTRY

    class _U:
        id = uid

    handler = CHAT_TOOL_REGISTRY["note_new_dimension"].handler
    out = asyncio.run(handler(db, {"dimension": "股东户数变化", "reason": "想知道筹码集中度"}, _U()))
    payload = json.loads(out)
    assert payload["created"] is True
    assert payload["dimension"] == "股东户数变化"
    assert payload["seen_count"] == 1
    assert payload["status"] == "pending"


def test_tool_rejects_missing_params(db, uid):
    from src.agents.chat.registry import CHAT_TOOL_REGISTRY

    class _U:
        id = uid

    handler = CHAT_TOOL_REGISTRY["note_new_dimension"].handler
    out = asyncio.run(handler(db, {"dimension": "股东户数变化"}, _U()))
    assert "缺少参数" in out
    assert db.query(HarnessProposal).count() == 0


# ── API ───────────────────────────────────────────────────────────────────

class _FakeUser:
    def __init__(self, uid: str):
        self.id = uid
        self.role = "member"
        self.username = f"user_{uid[:8]}"
        self.is_active = True
        self.token_version = 0


def _make_client(db, user):
    from src.web.api import copilot as copilot_api
    from src.web.api.auth import get_current_user

    app = FastAPI()
    app.include_router(copilot_api.router, prefix="/api")
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app)


def test_api_list_and_decide(db, uid):
    row = hp.propose(db, uid, dimension="股东户数变化", reason="a")
    with _make_client(db, _FakeUser(uid)) as c:
        r = c.get("/api/copilot/proposals", params={"status": "pending"})
        assert r.status_code == 200
        items = r.json()
        assert len(items) == 1 and items[0]["dimension"] == "股东户数变化"

        r2 = c.post(f"/api/copilot/proposals/{row['id']}/decide", json={"action": "accept"})
        assert r2.status_code == 200
        assert r2.json()["proposal"]["status"] == "accept"

        r3 = c.get("/api/copilot/proposals", params={"status": "pending"})
        assert r3.json() == []


def test_api_decide_invalid_action_400(db, uid):
    row = hp.propose(db, uid, dimension="股东户数变化", reason="a")
    with _make_client(db, _FakeUser(uid)) as c:
        r = c.post(f"/api/copilot/proposals/{row['id']}/decide", json={"action": "nope"})
        assert r.status_code == 400


def test_api_decide_other_users_proposal_404(db, uid, other_uid):
    """越权 404：不能审核别人的提案，也不泄露它是否存在。"""
    row = hp.propose(db, other_uid, dimension="股东户数变化", reason="b")
    with _make_client(db, _FakeUser(uid)) as c:
        r = c.post(f"/api/copilot/proposals/{row['id']}/decide", json={"action": "accept"})
        assert r.status_code == 404
    assert hp.list_proposals(db, other_uid)[0]["status"] == "pending"


def test_api_list_does_not_leak_other_users(db, uid, other_uid):
    hp.propose(db, uid, dimension="股东户数变化", reason="a")
    hp.propose(db, other_uid, dimension="龙虎榜席位风格", reason="b")
    with _make_client(db, _FakeUser(uid)) as c:
        items = c.get("/api/copilot/proposals").json()
    assert [it["dimension"] for it in items] == ["股东户数变化"]


def test_api_stats_endpoint(db, uid):
    hp.propose(db, uid, dimension="股东户数变化", reason="a")
    hp.propose(db, uid, dimension="龙虎榜席位风格", reason="b")
    hp.propose(db, uid, dimension="龙虎榜席位风格", reason="b2")
    with _make_client(db, _FakeUser(uid)) as c:
        r = c.get("/api/copilot/proposals/stats")
        assert r.status_code == 200
        data = r.json()
        assert data["items"][0]["dimension"] == "龙虎榜席位风格"
        assert data["items"][0]["seen_count"] == 2
        assert data["summary"]["pending"] == 2


def test_api_requires_login(db):
    """没登录 → 401(端点必须挂 get_current_user)。"""
    from src.web.api import copilot as copilot_api

    app = FastAPI()
    app.include_router(copilot_api.router, prefix="/api")
    with TestClient(app) as c:
        assert c.get("/api/copilot/proposals").status_code == 401


# ── P2: 提案 → 现有工具匹配 ────────────────────────────────────────────────
#
# 「用户提了新维度」不等于「平台没有这个能力」: 注册表 66 个工具、deep_diagnosis 只挂 11 个。
# 匹配只做提示（面板一行 muted 文案），不挂工具、不改配方、不影响任何一次作答。

def test_candidate_tools_finds_registered_tool_for_shareholder_dimension():
    """股东户数变化 → get_fundamentals_detail（注册表里早就有，只是没进配方）。"""
    hits = hp.match_candidate_tools("股东户数变化", "配方只覆盖十大股东，看不到户数变化趋势")
    assert hits, "股东户数变化必须能匹配到已有工具, 否则审核人会把已有能力当新需求排期"
    assert hits[0]["name"] == "get_fundamentals_detail"
    assert hits[0]["caliber"], "候选必须带口径 —— 同一个指标不同口径不可混算"


def test_candidate_tools_returns_name_and_caliber_shape():
    hits = hp.match_candidate_tools("筹码集中度")
    assert hits and set(hits[0]) == {"name", "caliber"}
    assert all(isinstance(h["name"], str) and h["name"] for h in hits)


def test_candidate_tools_respects_limit():
    assert len(hp.match_candidate_tools("筹码", limit=1)) == 1
    assert len(hp.match_candidate_tools("股东户数变化", limit=10)) <= 3


def test_candidate_tools_empty_for_uncovered_dimension():
    """没覆盖的维度 → []；不许为了「有东西显示」硬凑一个工具。"""
    assert hp.match_candidate_tools("限售解禁") == []
    assert hp.match_candidate_tools("机构调研") == []


def test_candidate_tools_empty_dimension_or_no_registry(monkeypatch):
    assert hp.match_candidate_tools("") == []
    assert hp.match_candidate_tools("   ") == []
    # 注册表不可用（import 挂了）→ 空结果，不抛异常
    monkeypatch.setattr(hp, "_TOOL_TOKENS", {})
    monkeypatch.setitem(__import__("sys").modules, "src.agents.chat.registry", None)
    assert hp.match_candidate_tools("股东户数变化") == []


def test_candidate_tools_excludes_collection_and_proposal_tools():
    """采集/记忆/提议类工具不是「可挂载的分析能力」, 不许出现在候选里。"""
    banned = {"note_new_dimension", "remember_preference"}
    probes = ["股东户数变化", "新维度提案", "记住我的偏好", "筹码集中度", "龙虎榜席位风格"]
    for dim in probes:
        names = {h["name"] for h in hp.match_candidate_tools(dim, dim)}
        assert not (names & banned), f"{dim} 候选里混进了非取数工具: {names & banned}"
        assert not any(n.startswith("propose_") for n in names)


def test_row_dict_includes_candidate_tools(db, uid):
    hp.propose(db, uid, dimension="股东户数变化", reason="想看户数变化趋势")
    row = hp.list_proposals(db, uid)[0]
    assert row["candidate_tools"]
    assert row["candidate_tools"][0]["name"] == "get_fundamentals_detail"


def test_row_dict_survives_matcher_failure(db, uid, monkeypatch):
    """匹配挂了也必须能看提案（附注失败不该让整行 500 / 消失）。"""
    hp.propose(db, uid, dimension="股东户数变化", reason="a")

    def _boom(*_a, **_kw):
        raise RuntimeError("registry down")

    monkeypatch.setattr(hp, "match_candidate_tools", _boom)
    rows = hp.list_proposals(db, uid)
    assert len(rows) == 1
    assert rows[0]["candidate_tools"] == []
    assert rows[0]["dimension"] == "股东户数变化"


def test_api_list_exposes_candidate_tools(db, uid):
    hp.propose(db, uid, dimension="股东户数变化", reason="a")
    with _make_client(db, _FakeUser(uid)) as c:
        items = c.get("/api/copilot/proposals").json()
    assert items[0]["candidate_tools"][0]["name"] == "get_fundamentals_detail"
