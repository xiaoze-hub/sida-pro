"""`POST /copilot/runs` 的 recipe：「记录说跑了配方」必须等于「实际按配方执行」。

**要钉的偏差**（2026-10-28 复核，本文件已转正为红→绿，无 xfail）：

    提交体 `SubmitRunBody.recipe` 原先被写进 `copilot_runs.recipe` 落库，
    但执行链 `submit_run → _execute(...) → run_copilot_question(...) → chat_api.send_message`
    **一次都没把这个 id 传下去**。`_execute` 的签名里没有 `recipe`，
    `run_copilot_question` 只构造 `SendMessageBody(content=question)` ——
    没有 mentions，配方注入（`harness.context.apply_mentions` / `inject_recipe`）
    永远走不到。结果：治理页 `by_recipe` 统计的「这份配方跑了 N 次」里，
    N 次没有一次真的按配方执行（没有工具白名单、没有 prompt_fragment、没有预算）。

    这是**记录说谎**，不是渲染问题：同一个 run 行既当证据又当执行凭据。

契约（本文件逐条断言，全部走真实提交链）：

    A. 提交时给的 recipe 必须传到执行体（真实 `submit_run` → 真实 `_execute` 任务，
       **不再手搓 `_execute`**：手搓等于绕过要修的那段调度，丢字段照样测不出来）；
    B. 显式传参时 `_execute` 必须把 recipe 透传给执行函数（拦「改了签名没改调用」）；
    C. 没传参时（`skills_gateway` 的任务入口只写行不传参）必须回落到 run 行上的记录，
       否则两条入口各说各话、又变回「记录跑了配方、实际裸 chat」；
    D. 执行体必须把 recipe 变成 `Mention(type="agent")` 送进 chat（既有通路，不改 chat.py）；
    E. 端到端：API → 任务 → mention → 真实上下文约束（工具白名单 + 预算 + prompt_fragment）；
    F. 未知配方**提交即拒绝**（400，不落库、不排任务），行里记着未知配方时**不许静默降级**；
    G. 缺省（recipe=None / 空白）行为不变：正文一字不改、不塞假 mention。
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.db.models import ChatConversation, CopilotRun, CopilotStep, User
from src.web.api import copilot_jobs
from src.web.api.auth import get_current_user

RECIPE = "chip_analyst"
UNKNOWN = "recipe_that_does_not_exist"


class _User:
    def __init__(self, uid: str = "u1", role: str = "member") -> None:
        self.id = uid
        self.username = "tester"
        self.role = role
        self.permissions: dict = {}


@pytest.fixture()
def Session(monkeypatch):
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    for t in (CopilotRun.__table__, CopilotStep.__table__,
              User.__table__, ChatConversation.__table__):
        try:
            t.create(eng, checkfirst=True)
        except Exception as e:  # PG 专属列在 sqlite 建不出来就跳过
            pytest.skip(f"sqlite 建表失败: {e}")

    session_factory = sessionmaker(bind=eng)
    import src.db.session as session_mod

    monkeypatch.setattr(session_mod, "SessionLocal", session_factory)
    copilot_jobs._TASKS.clear()
    yield session_factory
    _stop_tasks()


@pytest.fixture()
def client(Session):
    app = FastAPI()
    app.include_router(copilot_jobs.router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: _User("u1")
    with TestClient(app) as c:
        yield c


def _row(Session, run_id):
    db = Session()
    try:
        return db.query(CopilotRun).filter(CopilotRun.run_id == run_id).first()
    finally:
        db.close()


def _rows(Session):
    db = Session()
    try:
        return db.query(CopilotRun).all()
    finally:
        db.close()


def _seed_user(Session, uid="u1"):
    db = Session()
    try:
        db.add(User(id=uid, username=f"u-{uid}", password_hash="x"))
        db.commit()
    finally:
        db.close()


def _mk_run(Session, run_id="r1", recipe=None, status="queued", user_id="u1"):
    db = Session()
    try:
        db.add(CopilotRun(run_id=run_id, user_id=user_id, recipe=recipe,
                          question="q", status=status, tool_calls=0))
        db.commit()
    finally:
        db.close()


def _stop_tasks():
    for t in list(copilot_jobs._TASKS.values()):
        t.cancel()
    copilot_jobs._TASKS.clear()


def _submit_and_wait(question: str, recipe: str | None, *, user: _User | None = None) -> dict:
    """走真实提交链：`submit_run` → `create_task(_execute(...))` → 等这条任务跑完。

    直接手搓 `_execute(...)` 是上一版测试的写法，它把「提交层丢字段」这段绕过去了，
    所以永远是假红/假绿 —— 这里必须让真实的调度代码参与。
    """
    user = user or _User("u1")

    async def go():
        out = await copilot_jobs.submit_run(
            copilot_jobs.SubmitRunBody(question=question, recipe=recipe), user=user,
        )
        task = copilot_jobs._TASKS[out["run_id"]]
        await asyncio.wait_for(task, 20)
        return out

    return asyncio.run(go())


# ── A. 提交 → 执行体：recipe 不许在调度层丢掉（真实提交链）────────────────
def test_提交的_recipe_必须交给执行体(Session, monkeypatch):
    seen: dict = {}

    async def spy(**kw):
        seen.update(kw)
        return 1

    monkeypatch.setattr(copilot_jobs, "run_copilot_question", spy)
    _seed_user(Session)

    out = _submit_and_wait("看看筹码", RECIPE)

    row = _row(Session, out["run_id"])
    assert row.recipe == RECIPE, "落库的 recipe 与提交值不一致（记录层已坏）"
    assert seen.get("recipe") == RECIPE, (
        f"提交的 recipe={RECIPE!r} 没传到执行体，kwargs={sorted(seen)}；"
        "记录说跑了配方，实际跑的是裸 chat"
    )
    assert seen.get("run_id") == out["run_id"], "run_id 也不许在调度层丢"


# ── B. 显式传参：`_execute` 必须透传（拦「改了签名没改调用」）─────────────
def test_显式传参时_执行体把_recipe_透传给执行函数(Session, monkeypatch):
    seen: dict = {}

    async def spy(**kw):
        seen.update(kw)
        return 1

    monkeypatch.setattr(copilot_jobs, "run_copilot_question", spy)
    _mk_run(Session, "r-explicit")

    asyncio.run(copilot_jobs._execute(
        "r-explicit", user_id="u1", conversation_id=None, question="q", recipe=RECIPE,
    ))

    assert seen.get("recipe") == RECIPE, f"recipe 没透传，kwargs={sorted(seen)}"


# ── C. 未传参：回落到 run 行记录（`skills_gateway` 任务入口）──────────────
def test_未传参时_按_run_行记录执行(Session, monkeypatch):
    """`skills_gateway` 只把 recipe 写进行里、调 `_execute` 不传参 ——

    若执行体只看形参，那条入口依然「记录跑了配方、实际裸 chat」。
    配方的唯一事实源必须是 run 行：形参缺省时回落到它。
    """
    from src.web.api import chat as chat_api

    seen: dict = {}

    async def fake_send(*, conversation_id, body, user):
        seen["body"] = body
        return None

    monkeypatch.setattr(chat_api, "send_message", fake_send)
    _seed_user(Session)
    _mk_run(Session, "r-gw", recipe=RECIPE)

    asyncio.run(copilot_jobs._execute(
        "r-gw", user_id="u1", conversation_id=None, question="看看筹码",
    ))

    agents = [m.value for m in (getattr(seen["body"], "mentions", None) or [])
              if (getattr(m, "type", "") or "").lower() == "agent"]
    assert agents == [RECIPE], (
        f"行里记着 recipe={RECIPE}，执行时却没带约束（mentions={seen['body'].mentions!r}）"
    )
    assert _row(Session, "r-gw").status == "ok"


# ── D. 执行体 → chat_api.send_message：这一轮必须带配方 ────────────────────
def test_执行体把_recipe_变成_mention_再送进_chat(Session, monkeypatch):
    from src.web.api import chat as chat_api

    _seed_user(Session, "u1")
    seen: dict = {}

    async def fake_send(*, conversation_id, body, user):
        seen["body"] = body
        return None

    monkeypatch.setattr(chat_api, "send_message", fake_send)

    asyncio.run(copilot_jobs.run_copilot_question(
        user_id="u1", conversation_id=None, question="看看筹码", recipe=RECIPE))

    body = seen["body"]
    agents = [m.value for m in (getattr(body, "mentions", None) or [])
              if (getattr(m, "type", "") or "").lower() == "agent"]
    assert agents == [RECIPE], (
        f"发给 chat 的 body.mentions 里没有 agent 配方（mentions={body.mentions!r}）；"
        "配方注入只能在 mention 通路上生效"
    )
    assert body.content == "看看筹码", "配方是约束，不是正文改写"


# ── E. 端到端：API → 任务 → mention → 真实上下文约束 ──────────────────────
def test_端到端_配方形成真实工具白名单与预算(Session, monkeypatch):
    """整条链一次跑通，断言的是 chat.py 真正会读到的那些上下文变量。

    约束来源全部是线上同一处：`harness.context.enter_request_ctx`
    （`apply_mentions` → `inject_recipe` → REQ_TOOLS/REQ_BUDGET/REQ_RECIPE_LABEL），
    以及 `route_with_persona(..., forced=REQ_TOOLS.get())`。
    """
    from src.harness import context as ctx
    from src.harness.recipes import get_recipe
    from src.web.api import chat as chat_api

    rec = get_recipe(RECIPE)
    assert rec is not None, f"配方 {RECIPE} 不在 src/harness/recipes/ 里"
    assert rec.tools and rec.max_tool_calls

    _seed_user(Session)
    seen: dict = {}

    async def fake_send(*, conversation_id, body, user):
        seen["body"] = body
        seen["conversation_id"] = conversation_id
        return None

    monkeypatch.setattr(chat_api, "send_message", fake_send)

    out = _submit_and_wait("看看 600111 的筹码", RECIPE)

    row = _row(Session, out["run_id"])
    assert row.status == "ok"
    assert row.recipe == RECIPE
    assert row.conversation_id == seen["conversation_id"], "执行体建的会话必须回填到 run 行"

    body = seen["body"]
    assert body.content == "看看 600111 的筹码", "用户原话不许被配方改写"

    db = Session()
    try:
        conv = db.query(ChatConversation).filter(
            ChatConversation.id == seen["conversation_id"]).one()
        messages = [{"role": "system", "content": "SYS"}]
        tokens = ctx.enter_request_ctx(db, conv, body, messages)
        try:
            assert ctx.REQ_TOOLS.get() == frozenset(rec.tools), (
                "工具白名单没生效：模型仍能调配方以外的工具"
            )
            assert ctx.REQ_BUDGET.get() == {"max": rec.max_tool_calls, "used": 0}, (
                "预算没生效：max_tool_calls 落空"
            )
            assert ctx.REQ_RECIPE_LABEL.get() == rec.label
            system_text = messages[0]["content"]
            assert rec.prompt_fragment[:24] in system_text, "prompt_fragment 没注入"
            assert "本轮只允许调用这些工具" in system_text

            # 路由侧：forced=白名单 → 本轮工具集必须被收窄到配方内
            route, _note = ctx.route_with_persona(
                db, body.content, "600111", True, chat_api.CHAT_TOOLS, ctx.REQ_TOOLS.get(),
            )
            names = {(s.get("function") or {}).get("name") for s in route.tools}
            assert names and names <= set(rec.tools), (
                f"实际下发给模型的工具超出配方白名单："
                f"{sorted(str(n) for n in names - set(rec.tools))}"
            )
            assert len(names) < len(chat_api.CHAT_TOOLS), "白名单没有产生任何收窄"
        finally:
            ctx.exit_request_ctx(tokens)
    finally:
        db.close()


# ── F. 未知配方：提交即拒绝，不许记账 ─────────────────────────────────────
def test_未知配方提交即拒绝_且不落库(client, Session):
    resp = client.post("/api/copilot/runs", json={"question": "看看筹码", "recipe": UNKNOWN})

    assert resp.status_code == 400, f"未知配方必须拒绝，实际 {resp.status_code}: {resp.text[:200]}"
    assert UNKNOWN in resp.text or "未知配方" in resp.text
    assert _rows(Session) == [], "被拒的提交不许留下 run 行（记录与执行必须一致）"
    assert copilot_jobs._TASKS == {}, "被拒的提交不许排任务"


def test_行里记着未知配方时_不许退化成裸_chat(Session, monkeypatch):
    from src.web.api import chat as chat_api

    called: list = []

    async def fake_send(*, conversation_id, body, user):
        called.append(body)
        return None

    monkeypatch.setattr(chat_api, "send_message", fake_send)
    _seed_user(Session)
    _mk_run(Session, "r-ghost", recipe=UNKNOWN)

    asyncio.run(copilot_jobs._execute(
        "r-ghost", user_id="u1", conversation_id=None, question="看看筹码",
    ))

    row = _row(Session, "r-ghost")
    assert not called, "配方不存在时仍然发了裸 chat —— 记录在说谎"
    assert row.status == "failed"
    assert UNKNOWN in (row.error or "")


def test_入参归一_落库值与执行值同一个(client, Session, monkeypatch):
    """`" chip_analyst "` 与 `chip_analyst` 必须落成同一个 id 且执行同一个 id。"""
    seen: dict = {}

    async def spy(**kw):
        seen.update(kw)
        return 1

    monkeypatch.setattr(copilot_jobs, "run_copilot_question", spy)
    body = client.post("/api/copilot/runs",
                       json={"question": "看看筹码", "recipe": f"  {RECIPE}  "}).json()
    _stop_tasks()

    assert body["status"] == "queued"
    assert _row(Session, body["run_id"]).recipe == RECIPE, "落库的是入参原文（带空白）"


# ── G. 缺省行为不许变（这条必须一直是绿的）─────────────────────────────────
def test_recipe_为_None_时正文与_mentions_都不许被改(Session, monkeypatch):
    from src.web.api import chat as chat_api

    _seed_user(Session, "u1")
    seen: dict = {}

    async def fake_send(*, conversation_id, body, user):
        seen["body"] = body
        return None

    monkeypatch.setattr(chat_api, "send_message", fake_send)

    asyncio.run(copilot_jobs.run_copilot_question(
        user_id="u1", conversation_id=None, question="上证多少点"))

    body = seen["body"]
    assert body.content == "上证多少点", "默认路径不许改写用户原文"
    assert not (getattr(body, "mentions", None) or []), "没有配方时不许塞假 mention"


def test_空白_recipe_视作未指定():
    """`recipe=""` / 纯空白不是「未知配方」，但也不许落成空壳 id。"""
    assert copilot_jobs.resolve_recipe_id("") is None
    assert copilot_jobs.resolve_recipe_id("   ") is None
    assert copilot_jobs.resolve_recipe_id(None) is None
    assert copilot_jobs.resolve_recipe_id(f" {RECIPE} ") == RECIPE


# ── 契约可行性：修法不需要新机制 ──────────────────────────────────────────
def test_配方经_mention_通路确实生效(Session):
    """证明「recipe → Mention(type="agent")」是既有、可用、无需改 chat.py 的通路。

    这条用例把修复方向钉死：执行体只要把 recipe 转成一个 agent mention，
    `harness.context.apply_mentions` 就会 load 配方并 `inject_recipe`，
    同时 `route_for_request(..., forced=recipe)` 拿它锁工具白名单与预算。
    """
    from src.harness.context import Mention, apply_mentions
    from src.harness.recipes import get_recipe

    rec = get_recipe(RECIPE)
    assert rec is not None, f"配方 {RECIPE} 不在 src/harness/recipes/ 里"

    db = Session()
    try:
        conv = ChatConversation(user_id="u1", title="t")
        db.add(conv)
        db.commit()
        body = _Body(content="看看筹码", mentions=[Mention(type="agent", value=RECIPE)])
        got, _note = apply_mentions(db, conv, body)
    finally:
        db.close()

    assert got is not None and got.id == RECIPE, "agent mention 没解析出配方"


class _Body:
    """最小请求体（apply_mentions 只读 content/mentions/stock_symbol 等字段）。"""

    def __init__(self, content="", mentions=None, stock_symbol=None, stock_market=None):
        self.content = content
        self.mentions = mentions or []
        self.stock_symbol = stock_symbol
        self.stock_market = stock_market


# ── H. 归属：别人的 run 行不能变成我的配方凭据（跨账号 recipe 错绑）──────────
def test_后台新会话先绑定本人run再执行(Session, monkeypatch):
    from src.web.api import chat as chat_api
    _seed_user(Session, "u1")
    _mk_run(Session, "r-async", user_id="u1")
    seen = {}

    async def fake_send(*, conversation_id, body, user):
        seen['id'] = conversation_id
        assert _row(Session, 'r-async').conversation_id == conversation_id

    monkeypatch.setattr(chat_api, 'send_message', fake_send)
    cid = asyncio.run(copilot_jobs.run_copilot_question(
        user_id='u1', conversation_id=None, question='q', run_id='r-async',
    ))
    assert seen['id'] == cid


def test_不许借用他人run行上记录的配方(Session, monkeypatch):
    """行上的 `recipe` 不只是一条备注，它是**执行凭据**：工具白名单、max_tool_calls、
    prompt_fragment 全由它决定（见 `harness.context.inject_recipe`）。

    只按 `run_id` 取这条记录，等于任何人拿到别人的 run_id 就能把自己的这一轮套上
    别人的配方：自己的会话被别人的约束锁住，治理页 `by_recipe` 还会把这轮记在
    别人的配方名下。归属不符必须与「这行没有记录」同办（None），不许借用。
    """
    from src.web.api import chat as chat_api

    _seed_user(Session, "u1")
    _seed_user(Session, "u2")
    _mk_run(Session, "r-belongs-to-u2", recipe=RECIPE, user_id="u2")

    seen: dict = {}

    async def fake_send(*, conversation_id, body, user):
        seen["body"] = body
        return None

    monkeypatch.setattr(chat_api, "send_message", fake_send)

    with pytest.raises(RuntimeError, match="归属无法核验"):
        asyncio.run(copilot_jobs.run_copilot_question(
            user_id="u1", conversation_id=None, question="我只是拿到了一个 run_id",
            run_id="r-belongs-to-u2",
        ))
    assert seen == {}, "归属不符不能执行或复用他人的 run"
    assert _row(Session, "r-belongs-to-u2").recipe == RECIPE, "不许改写他人记录"


def test_自己的run行配方照常回落(Session):
    """防过度收紧：自己行的配方必须照旧回落到执行体（`skills_gateway` 入口靠它）。"""
    _seed_user(Session, "u1")
    _mk_run(Session, "r-mine", recipe=RECIPE, user_id="u1")
    assert copilot_jobs._recorded_recipe("r-mine", "u1") == RECIPE


def test_无主run行不回落配方(Session):
    """NULL/空串归属的行没有可验证的归属人 —— 与读口径同一条边界：不借凭据。

    与「NULL 可审计读」的区别：审计读是**看历史**，借配方是**拿它去执行**；
    拿不准归属时执行侧 default deny，宁可按裸 chat 跑。
    """
    _seed_user(Session, "u1")
    _mk_run(Session, "r-legacy", recipe=RECIPE, user_id=None)
    _mk_run(Session, "r-empty", recipe=RECIPE, user_id="")
    assert copilot_jobs._recorded_recipe("r-legacy", "u1") is None
    assert copilot_jobs._recorded_recipe("r-empty", "u1") is None
