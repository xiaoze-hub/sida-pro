# -*- coding: utf-8 -*-
"""2026-11-17 二轮审计缺口回归（真实路由 / 工具 / SQLite / 轨迹）。

覆盖三条已在独立审计里被复现的缺口：

  ① **跨标的快照**：会话无绑定 + 正文只写名字（「北方稀土 缠论结构怎么样」）时，
     旧实现让名字解析只发生在 `enter_request_ctx`，快照归属守卫拿不到目标，
     于是 REQ_BOUND=600111 与 REQ_CHAN_SNAPSHOT=000002 并存并输出他票结构。
     现在：名字解析（含模糊/失败 fail-closed）参与**唯一**请求目标判定，进模型前丢弃。
  ② **摘要落库**：`copilot_steps.args/result_digest` 与工具日志曾原样保存 64hex 摘要，
     与"只进本轮上下文、不入库、不跨轮复用"的声明冲突。
     现在：只有 `get_chan_structure` 的 args/result 收缩成前 8 位，其它工具的哈希不动。
  ③ **旧摘要复用**：第二轮不带客户端快照时，模型仍可把上一轮 digest 塞进参数取到结构。
     现在：工具**只**接受本轮复核过的快照；模型自带 digest 一律拒绝。

所有用例都走真实 HTTP 入口（登录/鉴权/配额/落库），只替换数据源边界与模型。
"""
from __future__ import annotations

import asyncio
import json
import re

import pytest

from tests.test_chan_ai_turn_context import (  # noqa: F401 - fixtures re-exported
    FINAL_TEXT,
    PASSWORDS,
    _body,
    _conv,
    _conv_row,
    _digest,
    _headers,
    _k,
    _login,
    _msgs,
    _snap,
    accounts,
    client,
    feed,
    holder,
)

DEMO_ID = "c0ffee00-3333-4333-8444-chatdm000002"
DEMO_PW = "chan-audit-demo-pw"
_DIGEST64 = re.compile(r"(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])")


def _short(digest: str) -> str:
    return digest[:8] + "…"


@pytest.fixture()
def no_network(monkeypatch):
    """把名字解析/纠错/问价快答钉死, 保证用例零外网、且可控"解析成/解析不出"。"""
    import src.core.symbol_guard as sg
    import src.harness.quick_answer as qa

    state = {"code": "600111", "info": None, "is_q": True}

    monkeypatch.setattr(sg, "is_stock_question", lambda t: state["is_q"])
    monkeypatch.setattr(sg, "first_code_from_names", lambda t: state["code"])
    monkeypatch.setattr(sg, "resolve_mentions", lambda t: state["info"] or {
        "resolved": [], "suggestions": [], "unknown": []})
    monkeypatch.setattr("src.web.api.chat.correction_note", lambda t: None)

    async def _none(*a, **k):
        return None

    monkeypatch.setattr(qa, "answer", _none)
    return state


def _resolved(*pairs):
    return {"resolved": list(pairs), "suggestions": [], "unknown": []}


@pytest.fixture()
def local_names(monkeypatch):
    """本地可信**全名**索引（2026-11-18 起同图归属唯一的名字通道）。

    旧实现用 ``resolve_mentions``（后缀词 + TQ 检索）判"正文点了哪只票"；六轮审计把它
    换成了封闭语法 + 已验证全名，所以这里钉的是**真实通道**：本地全名名单。
    """
    import src.core.symbol_guard as sg

    pairs = [
        ("600519", "贵州茅台"), ("600000", "浦发银行"), ("600111", "北方稀土"),
        ("002050", "三花智控"), ("000002", "万科A"), ("601318", "中国平安"),
    ]
    monkeypatch.setattr(sg, "_local_name_pairs", lambda: list(pairs))
    monkeypatch.setattr(sg, "_tq_lookup", lambda word: [])
    monkeypatch.setattr(sg, "_match_cached", lambda word: [])
    monkeypatch.setattr("src.core.chan_followup._NAME_VIEW",
                        {"raw": None, "view": None})
    sg.reset_local_index()
    yield sg
    sg.reset_local_index()
    monkeypatch.setattr("src.core.chan_followup._NAME_VIEW",
                        {"raw": None, "view": None})


def _response(client, token, conv_id, payload):
    return client.post(f"/api/chat/conversations/{conv_id}/messages",
                       headers=_headers(token), json=payload)


# ── ① 名字解析参与唯一目标判定 ──────────────────────────────────────────────

def test_name_only_question_drops_other_symbol_snapshot(
        client, accounts, feed, holder, no_network):
    """无绑定 + 正文只写名字: 快照 000002 必须在进模型前被丢弃, 不许算出他票结构。"""
    digest_other = _digest("000002")
    token = _login(client, "a")
    conv_id = _conv(client, token, None, None)          # 会话没有任何绑定
    no_network["code"] = "600111"                        # 名字 → 北方稀土

    r = _response(client, token, conv_id,
                  _body("北方稀土 缠论结构怎么样", _snap(digest_other, symbol="000002")))
    assert r.status_code == 200, r.text

    out = holder.outputs[-1]
    assert digest_other not in out and _short(digest_other) not in out
    assert "000002" not in out, "他票结构不许出现在本轮工具结果里"
    assert "缺少图表快照摘要" in out

    system = holder.systems[-1]
    assert "600111" in system and "不一致" in system
    assert digest_other not in system
    # 会话不因这条被拒的快照而改成 000002
    assert (_conv_row(conv_id).stock_symbol or "") != "000002"


def test_unresolved_name_fails_closed(client, accounts, feed, holder, no_network,
                                     local_names):
    """名字核不出来（错别字/后缀词但不在名单里）→ fail-closed, 快照整份丢弃。"""
    digest = _digest("000002")
    token = _login(client, "a")
    conv_id = _conv(client, token, None, None)
    no_network["code"] = None

    r = _response(client, token, conv_id,
                  _body("北方稀士 缠论结构怎么样", _snap(digest, symbol="000002")))
    assert r.status_code == 200, r.text
    system = holder.systems[-1]
    assert "无法唯一确定" in system and "不可读同图" in system
    assert _short(digest) not in holder.outputs[-1]
    assert "缺少图表快照摘要" in holder.outputs[-1]


def test_ambiguous_names_fail_closed(client, accounts, feed, holder, no_network,
                                     local_names):
    """一句话里两只不同标的 → 判不出唯一目标, 不许拿快照硬凑。"""
    digest = _digest("600111")
    token = _login(client, "a")
    conv_id = _conv(client, token, None, None)

    r = _response(client, token, conv_id,
                  _body("北方稀土和三花智控 缠论结构怎么样",
                        _snap(digest, symbol="600111")))
    assert r.status_code == 200, r.text
    assert "多只不同标的" in holder.systems[-1]
    assert "缺少图表快照摘要" in holder.outputs[-1]


def test_no_target_at_all_drops_snapshot(client, accounts, feed, holder, no_network):
    """既无显式标的、无绑定、正文也没点名 → 无目标, fail-closed。"""
    digest = _digest("000002")
    token = _login(client, "a")
    conv_id = _conv(client, token, None, None)
    no_network["is_q"] = False
    no_network["code"] = None

    r = _response(client, token, conv_id, _body("缠论结构怎么样", _snap(digest, symbol="000002")))
    assert r.status_code == 200, r.text
    assert "无法唯一确定" in holder.systems[-1]
    assert "缺少图表快照摘要" in holder.outputs[-1]


def test_explicit_symbol_still_wins_over_name(client, accounts, feed, holder, no_network):
    """保留既有优先级: 正文里的 6 位代码优先于名字/会话, 且与快照一致时照常取数。"""
    digest = _digest("600519")
    token = _login(client, "a")
    conv_id = _conv(client, token, "600000", "SH")
    no_network["code"] = "600111"     # 名字解析结果故意不同: 显式代码必须赢

    r = _response(client, token, conv_id,
                  _body("600519 缠论结构怎么样", _snap(digest, symbol="600519")))
    assert r.status_code == 200, r.text
    assert "【缠论结构·只读观测】600519" in holder.outputs[-1]
    assert _short(digest) in holder.outputs[-1]


def test_bound_conversation_with_other_stock_name_fails_closed(
        client, accounts, feed, holder, no_network, local_names):
    """会话绑 600000, 正文却写「北方稀土」(=600111) → 目标自相矛盾, 快照丢掉。

    这条不走"无绑定"路径: 目标来自会话绑定, 名字仍须参与核对 —— 否则页面停在 600000、
    用户问的是北方稀土时, 600000 的快照会被当成本轮结构答出去。
    """
    digest = _digest("600000")
    token = _login(client, "a")
    conv_id = _conv(client, token, "600000", "SH")
    no_network["code"] = "600111"

    r = _response(client, token, conv_id,
                  _body("北方稀土 缠论结构怎么样", _snap(digest, symbol="600000")))
    assert r.status_code == 200, r.text
    system = holder.systems[-1]
    assert "600111" in system and "自相矛盾" in system
    assert "缺少图表快照摘要" in holder.outputs[-1]
    assert _short(digest) not in holder.outputs[-1]


def test_bound_conversation_with_same_stock_name_still_serves(
        client, accounts, feed, holder, no_network, local_names):
    """名字与会话绑定一致时不误伤: 照常取数。"""
    digest = _digest("600000")
    token = _login(client, "a")
    conv_id = _conv(client, token, "600000", "SH")
    no_network["code"] = "600000"

    r = _response(client, token, conv_id,
                  _body("浦发银行 缠论结构怎么样", _snap(digest, symbol="600000")))
    assert r.status_code == 200, r.text
    assert "【缠论结构·只读观测】600000" in holder.outputs[-1]
    assert _short(digest) in holder.outputs[-1]


# ── ② 完整摘要不被持久化 ─────────────────────────────────────────────────

def _copilot_blob(conv_id: int) -> tuple[str, list]:
    from src.db.models import CopilotRun, CopilotStep
    from src.web.database import SessionLocal

    db = SessionLocal()
    try:
        runs = [r.run_id for r in db.query(CopilotRun)
                .filter(CopilotRun.conversation_id == conv_id).all()]
        steps = (db.query(CopilotStep).filter(CopilotStep.run_id.in_(runs)).all()
                 if runs else [])
        run_text = "\n".join(
            f"{r.question or ''}\n{r.error or ''}" for r in db.query(CopilotRun)
            .filter(CopilotRun.run_id.in_(runs)).all()) if runs else ""
        step_text = "\n".join(f"{s.tool}\n{s.args or ''}\n{s.result_digest or ''}"
                              for s in steps)
        return run_text + "\n" + step_text, steps
    finally:
        db.close()


def test_full_digest_is_not_persisted_into_trace_or_messages(
        client, accounts, feed, holder, caplog):
    digest = _digest()
    token = _login(client, "a")
    conv_id = _conv(client, token, "600000", "SH")

    with caplog.at_level("INFO"):
        r = _response(client, token, conv_id, _body(snapshot=_snap(digest)))
    assert r.status_code == 200, r.text
    assert "分型 " in holder.outputs[-1]

    blob, steps = _copilot_blob(conv_id)
    assert steps, "本轮应当留下轨迹"
    assert _DIGEST64.search(blob) is None, "copilot_steps/run 里出现了完整摘要"
    assert _short(digest) in blob, "轨迹里应保留可核对的前 8 位(便于治理页对图)"

    for m in _msgs(conv_id):
        assert _DIGEST64.search(m.content or "") is None
    assert _DIGEST64.search(caplog.text) is None, "日志里不许出现完整摘要"


def test_redaction_only_touches_digest_tool():
    """其它工具结果里的普通 64hex 不能被一起打断(避免"全局破坏哈希")。"""
    from src.db.models import CopilotStep
    from src.harness.redaction import redact_step_args, redact_step_result
    from src.harness.trace import CopilotTrace
    from src.web.database import SessionLocal

    chan = "a" * 64
    other = "b" * 64
    kept = redact_step_args("get_stock_quote", {"sig": chan})
    assert kept["sig"] == chan, "非摘要工具的哈希必须原样保留"
    assert redact_step_result("get_stock_quote", other) == other
    assert redact_step_result("get_chan_structure", f"摘要 {chan}") == f"摘要 {_short(chan)}"
    # 嵌套结构也要收缩
    nested = redact_step_args("get_chan_structure",
                              {"a": [{"digest": chan}], "b": {"c": chan}})
    assert nested["a"][0]["digest"] == _short(chan)
    assert nested["b"]["c"] == _short(chan)

    trace = CopilotTrace(conversation_id=None, user_id=None)
    trace.step("get_chan_structure", {"symbol": "600000", "snapshot_digest": chan},
               result=f"摘要 {chan}")
    trace.step("get_stock_quote", {"sig": other}, result=other)
    db = SessionLocal()
    try:
        rows = (db.query(CopilotStep).filter(CopilotStep.run_id == trace.run_id)
                .order_by(CopilotStep.seq.asc()).all())
    finally:
        db.close()
    assert len(rows) == 2
    assert chan not in (rows[0].args or "") and chan not in (rows[0].result_digest or "")
    assert _short(chan) in rows[0].args and _short(chan) in rows[0].result_digest
    # 非摘要工具原样保留
    assert other in rows[1].args and other in rows[1].result_digest


# ── ③ 旧摘要不能当本轮快照用 ──────────────────────────────────────────────

def test_previous_turn_digest_cannot_be_reused(client, accounts, feed, holder):
    digest = _digest()
    token = _login(client, "a")
    conv_id = _conv(client, token, "600000", "SH")

    assert _response(client, token, conv_id, _body(snapshot=_snap(digest))).status_code == 200
    assert _short(digest) in holder.outputs[-1]

    holder.outputs.clear()
    # 第二轮: 客户端没带快照, 模型"回忆"上一轮的摘要
    holder.args = {"symbol": "600000", "instrument": "stock", "days": 60,
                   "snapshot_digest": digest}
    r = _response(client, token, conv_id, _body("600000 现在缠论结构怎么样"))
    assert r.status_code == 200, r.text

    out = holder.outputs[-1]
    assert "缺少图表快照摘要" in out and "未读取任何结构数据" in out
    assert digest not in out and "分型 " not in out
    assert "模型自带的 snapshot_digest 一律不接受" in out
    # 这一轮的轨迹里也不许留下旧摘要
    blob, _steps = _copilot_blob(conv_id)
    assert digest not in blob


# ── 并发交错: 同一会话一轮带快照、一轮不带 ─────────────────────────────────

def test_same_conversation_interleaved_turn_without_snapshot_is_strict(
        client, accounts, feed, holder):
    import httpx

    from src.web.app import app

    digest = _digest("600000")
    token = _login(client, "a")
    conv_id = _conv(client, token, "600000", "SH")

    async def _scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
            async def _one(body):
                r = await ac.post(f"/api/chat/conversations/{conv_id}/messages/stream",
                                  headers=_headers(token), json=body)
                return r.status_code, r.text

            return await asyncio.gather(
                _one(_body("600000 缠论结构怎么看", _snap(digest))),
                _one(_body("这只票缠论结构怎么样", None)),
            )

    res = asyncio.run(_scenario())
    assert [code for code, _ in res] == [200, 200]

    structured = [o for o in holder.outputs if _short(digest) in o and "分型 " in o]
    missing = [o for o in holder.outputs if "缺少图表快照摘要" in o]
    assert len(structured) == 1, [o[:80] for o in holder.outputs]
    assert missing, "不带快照的那一轮必须走严格缺失态, 不能读到并发轮的快照"
    assert digest not in "\n".join(holder.outputs)


# ── 附带修正: 被 demo 限流拒绝的消息不许改会话标的 ─────────────────────────

def _demo_user():
    from src.web.database import SessionLocal
    from src.web.models import User

    db = SessionLocal()
    try:
        row = db.query(User).filter(User.id == DEMO_ID).first()
        if row is None:
            from src.web.api.auth import hash_password

            row = User(id=DEMO_ID, username="demo", password_hash=hash_password(DEMO_PW),
                       role="member", is_active=True, token_version=1)
            db.add(row)
            db.commit()
            db.refresh(row)
        return row
    finally:
        db.close()


def test_demo_rate_limit_rejection_does_not_rebind_conversation(accounts, feed, holder):
    """429 的判定必须先于 bind: 被拒的消息按契约"拒绝且未保存", 不许改会话标的。

    直接调用端点函数(不经中间件): demo 账号在 HTTP 中间件里被"只读"拦掉, 走不到 429 分支。
    """
    from fastapi import HTTPException

    from src.web.api import chat as chat_api
    from src.web.database import SessionLocal
    from src.web.models import ChatConversation, ChatMessage

    digest = _digest("600519")
    user = _demo_user()
    db = SessionLocal()
    try:
        conv = ChatConversation(user_id=user.id, title="audit", stock_symbol="600000",
                                stock_market="SH")
        db.add(conv)
        db.commit()
        db.refresh(conv)
        conv_id = conv.id
        body = chat_api.SendMessageBody(
            content="600519 缠论结构怎么看", stock_symbol="600519", stock_market="SH",
            chart_snapshot=_snap(digest, symbol="600519"),
        )

        import src.core.demo_limit as dl

        orig = dl.allow
        dl.allow = lambda uid: False
        try:
            with pytest.raises(HTTPException) as exc:
                asyncio.run(chat_api.send_message(conv_id, body, user=user))
        finally:
            dl.allow = orig
        assert exc.value.status_code == 429

        row = db.query(ChatConversation).filter(ChatConversation.id == conv_id).first()
        db.refresh(row)
        assert row.stock_symbol == "600000", "被 429 拒绝的消息把会话标的改掉了"
        assert db.query(ChatMessage).filter(ChatMessage.conversation_id == conv_id).count() == 0
        db.delete(row)
        db.commit()
    finally:
        db.close()
