# -*- coding: utf-8 -*-
"""聊天每轮安全传入并校验 K 线快照的后端上下文链（2026-11-17）。

覆盖的是**真实链路**「HTTP 路由 → 鉴权/权限 → 快照复核（既有只读端点）→ 本轮 ContextVar
→ 缠论工具 → 落库」，不是 helper 单测：

  ① 本轮有快照 → 工具拿到同一份摘要，并把「模型自带的 digest」覆盖掉（模型不得劫持参数）;
  ② 第二轮不带 → 严格缺失态，**不沿用**上一轮快照;
  ③ 两账号 / 两路并发串流各带各的快照，互不串（ContextVar 按请求隔离）;
  ④ 断连（is_disconnected）与取消（CancelledError）都 reset，值不残留;
  ⑤ 切标的（快照与会话标的不一致）→ 显式缺失，聊天继续，不套用;
  ⑥ 伪造身份/权限：服务令牌 403、guest 403、客户端自造字段 422（不依赖客户端 userid）;
  ⑦ 旧摘要 → 409，且消息不落库；服务端不自行刷新、不另取快照;
  ⑧ chart_snapshot 严格 schema：大小写/长度/枚举/多字段/类型 → 422;
  ⑨ 真正写进 DB 的 history / initial_context 里没有摘要;
  ⑩ 本轮 pytest 的 engine 是 scratch 下的隔离 SQLite。

只替换数据源边界（`kl._pg_klines`）与模型（`_get_ai_client`）；摘要、分型、笔、鉴权、
配额、落库全部走真实实现。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from src.collectors.kline_collector import KlineData
from src.web.api import chan_snapshot as chan
from src.web.api import chat as chat_api
from src.web.api import klines as kl

NOW = datetime(2026, 11, 17, 14, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
TODAY = "2026-11-17"
COMPLETED = ["2026-11-09", "2026-11-10", "2026-11-11", "2026-11-12", "2026-11-13", "2026-11-16"]

OWNER_ID = "c0ffee00-3333-4333-8444-chatown00001"
MEMBER_A = "c0ffee00-3333-4333-8444-chatma000001"
MEMBER_B = "c0ffee00-3333-4333-8444-chatmb000001"
GUEST_ID = "c0ffee00-3333-4333-8444-chatgs000001"
for _uid in (OWNER_ID, MEMBER_A, MEMBER_B, GUEST_ID):
    assert len(_uid) == 36, "users.id 是 VARCHAR(36)"

PASSWORDS = {"owner": "chan-ctx-owner-pw", "a": "chan-ctx-member-pw",
             "b": "chan-ctx-member-b-pw", "guest": "chan-ctx-guest-pw"}
FINAL_TEXT = "（模拟）已按本轮图表快照读取缠论结构。"
QUESTION = "600000 缠论结构怎么看"

_OHLC = [
    (9.2, 9.8, 10.0, 9.0),
    (9.8, 10.8, 11.0, 9.5),
    (10.8, 11.8, 12.0, 10.5),
    (11.0, 10.4, 11.2, 10.0),
    (10.4, 11.3, 11.5, 9.8),
    (10.7, 9.4, 10.8, 9.2),
]


def _k(day: str, ohlc: tuple[float, float, float, float]) -> KlineData:
    open_, close, high, low = ohlc
    return KlineData(date=day, open=open_, close=close, high=high, low=low, volume=1000.0)


def _copy(bar: KlineData) -> KlineData:
    return KlineData(date=bar.date, open=bar.open, close=bar.close,
                     high=bar.high, low=bar.low, volume=bar.volume)


class _Feed:
    """可变的 `_pg_klines` 替身：换 bars 即模拟「图表已经变了 / 换了数据源」。"""

    def __init__(self, bars: list[KlineData]):
        self.bars = bars
        self.calls = 0

    def __call__(self, symbol, market_code, days):
        self.calls += 1
        bars = list(self.bars)
        try:
            n = int(days)
        except (TypeError, ValueError):
            n = 0
        if n > 0:
            bars = bars[-n:]
        return ([_copy(bar) for bar in bars], bars[-1].date)


def _completed_feed() -> _Feed:
    return _Feed([_k(day, ohlc) for day, ohlc in zip(COMPLETED, _OHLC)])


# ── 数据源替身 / 图表摘要（真实 get_klines + attach_snapshot_digest）─────────

@pytest.fixture()
def feed(monkeypatch):
    f = _completed_feed()
    monkeypatch.setattr(kl, "_pg_klines", f)
    monkeypatch.setattr(kl, "_finalize_daily_bars", lambda bars, *a, **k: (bars, None))
    monkeypatch.setattr(kl, "_kl_cache", {})
    monkeypatch.setattr(chan, "_now", lambda: NOW)
    return f


def _digest(symbol: str = "600000", days: int = 60, instrument: str = "stock") -> str:
    resp = kl.get_klines(symbol=symbol, market="CN", days=days, interval="1d",
                         instrument=instrument)
    block = resp.get("chan_snapshot") or {}
    value = block.get("digest")
    assert isinstance(value, str) and len(value) == 64, block
    return value


def _snap(digest: str, symbol: str = "600000", instrument: str = "stock",
          days: int = 60, market: str = "CN", interval: str = "1d",
          schema: str = "sida-chan-snapshot-v2", **extra) -> dict:
    body = {"schema": schema, "symbol": symbol, "market": market,
            "instrument": instrument, "interval": interval, "days": days,
            "digest": digest}
    body.update(extra)
    return body


# ── 账号 / 客户端 ─────────────────────────────────────────────────────────

def _conftest():
    for name in ("conftest", "tests.conftest"):
        module = sys.modules.get(name)
        if module is not None and hasattr(module, "purge_users"):
            return module
    raise RuntimeError("pytest 尚未加载 conftest")


@pytest.fixture()
def accounts(monkeypatch):
    """真实 users 行（owner/member x2/guest），登录走真实 /api/auth/login。"""
    from src.web.api.auth import hash_password
    from src.web.database import SessionLocal
    from src.web.models import ChatConversation, ChatMessage, User

    rows = (
        (OWNER_ID, "chan-ctx-owner", "owner", PASSWORDS["owner"]),
        (MEMBER_A, "chan-ctx-member-a", "member", PASSWORDS["a"]),
        (MEMBER_B, "chan-ctx-member-b", "member", PASSWORDS["b"]),
        (GUEST_ID, "chan-ctx-guest", "guest", PASSWORDS["guest"]),
    )
    db = SessionLocal()
    try:
        # SQLite 行 id 会被复用: 已被 purge 的会话可能留下孤儿消息, 让新会话"凭空"带上历史。
        # 只清孤儿(会话已不存在), 不动别人的数据。
        db.query(ChatMessage).filter(
            ~ChatMessage.conversation_id.in_(db.query(ChatConversation.id))
        ).delete(synchronize_session=False)
        for uid, name, role, pw in rows:
            db.add(User(id=uid, username=name, password_hash=hash_password(pw),
                        role=role, is_active=True, token_version=1))
        db.commit()
    finally:
        db.close()

    # 惰性 bootstrap 不许再建出另一个 owner（那会污染后续文件）
    monkeypatch.setenv("AUTH_USERNAME", "chan-ctx-owner")
    monkeypatch.setenv("AUTH_PASSWORD", PASSWORDS["owner"])
    yield {"owner": OWNER_ID, "a": MEMBER_A, "b": MEMBER_B, "guest": GUEST_ID}

    db = SessionLocal()
    try:
        _conftest().purge_users(db, ids=[OWNER_ID, MEMBER_A, MEMBER_B, GUEST_ID])
        db.commit()
    finally:
        db.close()


class _FakeToolCall:
    def __init__(self, name: str, arguments: str):
        self.id = "call_1"
        self.function = type("F", (), {"name": name, "arguments": arguments})()


class _FakeMsg:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


_SNAP_BLOCK_RE = re.compile(
    r"symbol=(\d{6}) market=(\w+) instrument=(\w+) interval=(\w+) days=(\d+)"
)
#: system 里只给前 8 位(完整摘要属于本轮凭据, 见 harness/redaction.py)。
_DIGEST_RE = re.compile(r"snapshot_digest=([0-9a-f]{8})…")


def _short(digest: str) -> str:
    """摘要的可见形态(前 8 位 + …)—— 工具/轨迹/日志都只应出现这一种。"""
    return digest[:8] + "…"


class _Holder:
    """一次测试内所有「模型实例」的共享账本（并发安全：只 append）。"""

    def __init__(self):
        self.args: dict | None = None   # 显式指定（"模型自带参数/伪造 digest"用例）
        self.outputs: list[str] = []    # 工具返回原文（真实 handler 产出）
        self.systems: list[str] = []    # 本轮 system prompt（用于证明注入了什么）

    def from_prompt(self, messages: list[dict]) -> dict:
        system = str((messages[0] or {}).get("content") or "")
        block = _SNAP_BLOCK_RE.search(system)
        # 模型拿不到完整摘要(系统只给前 8 位), 因此它"自带摘要"的路径必须无从发生;
        # 这里恒为空串, 与真实模型的可见信息一致。
        if not block:
            return {"symbol": "600000", "instrument": "stock", "days": 60,
                    "snapshot_digest": ""}
        return {
            "symbol": block.group(1),
            "market": block.group(2),
            "instrument": block.group(3),
            "interval": block.group(4),
            "days": int(block.group(5)),
            "snapshot_digest": "",
        }


class _ScriptedAI:
    """脚本化模型：第 1 轮请求 get_chan_structure，第 2 轮读工具结果后作答。"""

    def __init__(self, holder: _Holder):
        self.holder = holder
        self.round = 0

    def _args(self, messages) -> dict:
        if self.holder.args is not None:
            return dict(self.holder.args)
        return self.holder.from_prompt(messages)

    def _call_msg(self, messages) -> _FakeMsg:
        return _FakeMsg(content=None, tool_calls=[_FakeToolCall(
            "get_chan_structure", json.dumps(self._args(messages), ensure_ascii=False),
        )])

    def _collect(self, messages) -> None:
        for m in reversed(messages):
            if isinstance(m, dict) and m.get("role") == "tool":
                self.holder.outputs.append(str(m.get("content") or ""))
                return

    async def chat_with_tools(self, messages, tools, temperature=0.5):
        self.holder.systems.append(str((messages[0] or {}).get("content") or ""))
        if self.round == 0:
            self.round += 1
            return self._call_msg(messages)
        self._collect(messages)
        return _FakeMsg(content=FINAL_TEXT, tool_calls=None)

    async def chat_with_tools_stream(self, messages, tools, temperature=0.5):
        self.holder.systems.append(str((messages[0] or {}).get("content") or ""))
        if self.round == 0:
            self.round += 1
            yield "tool_calls", self._call_msg(messages)
            return
        self._collect(messages)
        for part in (FINAL_TEXT[:8], FINAL_TEXT[8:]):
            yield "delta", part

    async def chat_multi(self, messages, temperature=0.4):
        return "fallback reply"


@pytest.fixture()
def holder(monkeypatch):
    h = _Holder()
    monkeypatch.setattr(
        chat_api, "_get_ai_client",
        lambda db, model_id=None, user=None: _ScriptedAI(h),
    )
    return h


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr("src.web.middleware.RATE_LIMIT_ENABLED", False)
    from src.web.app import app

    return TestClient(app)


def _login(client, account: str) -> str:
    username = {"owner": "chan-ctx-owner", "a": "chan-ctx-member-a",
                "b": "chan-ctx-member-b", "guest": "chan-ctx-guest"}[account]
    r = client.post("/api/auth/login",
                    json={"username": username, "password": PASSWORDS[account]})
    assert r.status_code == 200, r.text
    return r.json()["data"]["token"]


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _conv(client, token: str, symbol: str | None = None,
          market: str | None = None) -> int:
    r = client.post("/api/chat/conversations", headers=_headers(token),
                    json={"stock_symbol": symbol, "stock_market": market})
    assert r.status_code == 200, r.text
    return r.json()["data"]["id"]


def _send(client, token, conv_id, payload):
    return client.post(f"/api/chat/conversations/{conv_id}/messages",
                       headers=_headers(token), json=payload)


def _body(text: str = QUESTION, snapshot: dict | None = None) -> dict:
    body = {"content": text}
    if snapshot is not None:
        body["chart_snapshot"] = snapshot
    return body


def _msgs(conv_id: int) -> list:
    from src.web.database import SessionLocal
    from src.web.models import ChatMessage

    db = SessionLocal()
    try:
        return (db.query(ChatMessage)
                .filter(ChatMessage.conversation_id == conv_id)
                .order_by(ChatMessage.id.asc()).all())
    finally:
        db.close()


def _conv_row(conv_id: int):
    from src.web.database import SessionLocal
    from src.web.models import ChatConversation

    db = SessionLocal()
    try:
        return db.query(ChatConversation).filter(ChatConversation.id == conv_id).first()
    finally:
        db.close()


def _no_secret_in(text: str) -> None:
    """历史/初始上下文里不许出现摘要（64 位十六进制）、schema 名或快照字段名。"""
    assert not re.search(r"\b[0-9a-f]{64}\b", text or ""), f"发现摘要: {text[:200]}"
    for leaked in ("sida-chan-snapshot", "snapshot_digest", "chan_snapshot"):
        assert leaked not in (text or ""), f"落库内容含 {leaked}: {text[:200]}"


# ── ① 本轮快照进入工具，覆盖模型参数 ─────────────────────────────────────

def test_turn_snapshot_reaches_tool_and_overrides_model_digest(client, accounts, feed, holder):
    digest = _digest()
    token = _login(client, "a")
    conv_id = _conv(client, token, "600000", "SH")

    # 模型故意带一个伪造的 digest（真实旧实现会拿它去算/直接 409）
    holder.args = {"symbol": "600000", "instrument": "stock", "days": 60,
                   "snapshot_digest": "f" * 64}

    r = _send(client, token, conv_id, _body(snapshot=_snap(digest)))
    assert r.status_code == 200, r.text

    out = holder.outputs[-1]
    assert "【缠论结构·只读观测】600000 CN stock" in out
    assert _short(digest) in out, "工具必须以本轮复核过的摘要为准(只展示前 8 位)"
    assert digest not in out, "完整摘要不许出现在工具结果里(会经轨迹/回答被持久化)"
    assert "f" * 64 not in out
    assert "[快照归属: 本轮消息随附的图表快照" in out
    assert "未验证用户屏幕" in out
    assert "分型 " in out

    # 本轮 system 明确给出可信来源（只在本轮消息里）
    system = holder.systems[-1]
    # 注入的是带着具体参数的那一块（配方 prompt 里也提到这个名字, 不能只认标题）
    assert "【本轮图表快照（消息入口时已复核）】\nsymbol=600000 market=CN" in system
    assert _short(digest) in system
    assert digest not in system, "system 里只给前 8 位, 否则模型会把它回显进回答/轨迹"
    assert "不等于验证了用户屏幕" in system or "没有、也不能验证用户屏幕" in system

    # 落库的历史与 initial_context 里没有任何摘要
    rows = _msgs(conv_id)
    assert [m.role for m in rows] == ["user", "assistant"]
    for m in rows:
        _no_secret_in(m.content)
    assert _conv_row(conv_id).initial_context in (None, "")
    # 库里那份系统提示常量也没被改写
    _no_secret_in(chat_api.SYSTEM_PROMPT)


def test_second_turn_without_snapshot_is_strict_missing(client, accounts, feed, holder):
    digest = _digest()
    token = _login(client, "a")
    conv_id = _conv(client, token, "600000", "SH")

    assert _send(client, token, conv_id, _body(snapshot=_snap(digest))).status_code == 200
    assert "分型 " in holder.outputs[-1]

    calls_before = feed.calls
    holder.args = None
    holder.outputs.clear()
    # 第二轮换一句话问（同一句会触发"重复提问"注入, 那不是本轮要测的东西）
    r = _send(client, token, conv_id, _body("600000 现在分型结构怎么样"))
    assert r.status_code == 200, r.text

    out = holder.outputs[-1]
    assert "缺少图表快照摘要" in out and "未读取任何结构数据" in out
    assert "[快照归属" not in out
    assert digest not in out
    assert "【本轮图表快照（消息入口时已复核）】\nsymbol=" not in holder.systems[-1]
    assert digest not in holder.systems[-1]
    assert feed.calls == calls_before, "没有快照时不许自己取一张新快照"


def test_tool_fills_args_from_turn_snapshot_when_model_omits_them(
        client, accounts, feed, holder):
    """模型只写了 snapshot_digest 空串（没给 symbol/instrument/days）：仍按本轮快照取数。"""
    digest = _digest()
    token = _login(client, "a")
    conv_id = _conv(client, token, "600000", "SH")

    holder.args = {"snapshot_digest": ""}
    r = _send(client, token, conv_id, _body(snapshot=_snap(digest)))
    assert r.status_code == 200, r.text
    out = holder.outputs[-1]
    assert "【缠论结构·只读观测】600000 CN stock" in out and _short(digest) in out


def test_model_cannot_apply_snapshot_to_another_symbol(client, accounts, feed, holder):
    digest = _digest()
    token = _login(client, "a")
    conv_id = _conv(client, token, "600000", "SH")

    # 本轮快照是 600000，模型却想算 600519 → 拒绝，不跨标的套用
    holder.args = {"symbol": "600519", "instrument": "stock", "days": 60,
                   "snapshot_digest": digest}
    r = _send(client, token, conv_id, _body(snapshot=_snap(digest)))
    assert r.status_code == 200, r.text
    out = holder.outputs[-1]
    assert "不一致" in out and "未读取任何结构数据" in out
    assert "分型 " not in out


# ── ② 两账号隔离 ─────────────────────────────────────────────────────────

def test_two_accounts_keep_their_own_snapshot(client, accounts, feed, holder):
    digest_a = _digest("600000")
    digest_b = _digest("600519")
    assert digest_a != digest_b

    token_a, token_b = _login(client, "a"), _login(client, "b")
    conv_a = _conv(client, token_a, "600000", "SH")
    conv_b = _conv(client, token_b, "600519", "SH")

    assert _send(client, token_a, conv_a, _body(snapshot=_snap(digest_a))).status_code == 200
    out_a = holder.outputs[-1]
    assert "【缠论结构·只读观测】600000" in out_a and _short(digest_a) in out_a

    holder.outputs.clear()
    assert _send(client, token_b, conv_b,
                 _body("600519 缠论结构怎么看", _snap(digest_b, symbol="600519"))).status_code == 200
    out_b = holder.outputs[-1]
    assert "【缠论结构·只读观测】600519" in out_b and _short(digest_b) in out_b
    assert _short(digest_a) not in out_b

    # B 拿着 A 的快照（标的不一致）→ 整份丢弃，不得套到 600519 上
    holder.outputs.clear()
    r = _send(client, token_b, conv_b,
              _body("600519 缠论结构怎么看", _snap(digest_a, symbol="600000")))
    assert r.status_code == 200, r.text
    out_cross = holder.outputs[-1]
    assert _short(digest_a) not in out_cross and "分型 " not in out_cross
    assert "缺少图表快照摘要" in out_cross or "不一致" in out_cross


# ── ③ 切标的：显式缺失，聊天继续 ─────────────────────────────────────────

def test_symbol_switch_drops_snapshot_but_keeps_chat(client, accounts, feed, holder):
    token = _login(client, "a")
    conv_id = _conv(client, token, "600000", "SH")
    digest_a = _digest("600000")

    # 用户这轮明确问 000001（文字代码优先），图表还停在 600000 → 快照必须作废
    r = _send(client, token, conv_id,
              _body("000001 缠论结构怎么看", _snap(digest_a, symbol="600000")))
    assert r.status_code == 200, r.text
    out = holder.outputs[-1]
    assert _short(digest_a) not in out
    assert "分型 " not in out
    assert "不一致" in out or "缺少图表快照摘要" in out
    # 显式告警进了本轮 system（模型知道"这轮没有可信快照"）
    assert "不一致" in holder.systems[-1] or "已丢弃" in holder.systems[-1]

    # 会话绑定跟随文字更新为 000001
    assert _conv_row(conv_id).stock_symbol == "000001"
    for m in _msgs(conv_id):
        _no_secret_in(m.content)


# ── ④ 旧摘要 → 409，且消息不落库 ─────────────────────────────────────────

def test_stale_digest_is_409_and_nothing_is_saved(client, accounts, feed, holder):
    digest = _digest()
    token = _login(client, "a")
    conv_id = _conv(client, token, "600000", "SH")

    # 图表已经变了（末根 bar 收盘价不同），但客户端还拿着旧摘要
    feed.bars[-1] = _k(COMPLETED[-1], (10.7, 9.9, 10.8, 9.2))
    kl._kl_cache.clear()

    r = _send(client, token, conv_id, _body(snapshot=_snap(digest)))
    assert r.status_code == 409, r.text
    detail = json.dumps(r.json(), ensure_ascii=False)
    assert "图表快照已过期" in detail and "未保存" in detail
    assert digest not in detail          # 不回显摘要
    assert _msgs(conv_id) == [], "409 时这条消息不许落库（更不许回答）"

    # 流式入口同样要在**开流前**给真 409（不能 200 + error 事件）
    with client.stream("POST", f"/api/chat/conversations/{conv_id}/messages/stream",
                       headers=_headers(token), json=_body(snapshot=_snap(digest))) as resp:
        assert resp.status_code == 409
    assert _msgs(conv_id) == []

    # 服务端没有"自行刷新"：数据库里依然没有摘要
    assert _conv_row(conv_id).initial_context in (None, "")


# ── ⑤ 鉴权 / 权限：不依赖客户端 userid ───────────────────────────────────

def test_service_token_cannot_carry_chart_snapshot(client, accounts, feed, monkeypatch):
    """服务通道两层都进不来: HTTP 层被 get_current_user 挡(401), 复核函数对服务主体 403。"""
    import asyncio as _asyncio

    from fastapi import HTTPException

    from src.harness.context import ChartSnapshotBody, prepare_chart_snapshot
    from src.web.api import auth as auth_api
    from src.web.database import SessionLocal

    digest = _digest()
    monkeypatch.setenv("SIDA_SERVICE_TOKEN", "test-service-token-1117")
    monkeypatch.setattr(auth_api, "_service_token", "")

    r = client.post("/api/chat/conversations",
                    headers={"X-Service-Token": "test-service-token-1117"})
    assert r.status_code == 401, r.text

    body = chat_api.SendMessageBody(
        content=QUESTION, chart_snapshot=ChartSnapshotBody(**_snap(digest)))
    conv = type("C", (), {"stock_symbol": "600000", "stock_market": "SH"})()
    db = SessionLocal()
    try:
        with pytest.raises(HTTPException) as exc:
            _asyncio.run(prepare_chart_snapshot(db, conv, body, auth_api.ServicePrincipal()))
    finally:
        db.close()
    assert exc.value.status_code == 403
    assert "真实用户" in str(exc.value.detail)


def test_guest_without_quote_permission_is_403(client, accounts, feed):
    """guest(演示号) 是只读浏览: 写入口被路由拒; 复核函数本身也按同一权限口径拒绝。"""
    import asyncio as _asyncio

    from fastapi import HTTPException

    from src.harness.context import ChartSnapshotBody, prepare_chart_snapshot
    from src.web.database import SessionLocal
    from src.web.models import User

    digest = _digest()
    token = _login(client, "guest")
    r = client.post("/api/chat/conversations", headers=_headers(token),
                    json={"stock_symbol": "600000", "stock_market": "SH"})
    assert r.status_code == 403, r.text

    conv = type("C", (), {"stock_symbol": "600000", "stock_market": "SH"})()
    body = chat_api.SendMessageBody(
        content=QUESTION, chart_snapshot=ChartSnapshotBody(**_snap(digest)))
    db = SessionLocal()
    try:
        guest = db.query(User).filter(User.id == GUEST_ID).first()
        with pytest.raises(HTTPException) as exc:
            _asyncio.run(prepare_chart_snapshot(db, conv, body, guest))
    finally:
        db.close()
    assert exc.value.status_code == 403
    assert "无权携带图表快照" in str(exc.value.detail)


def test_client_cannot_smuggle_identity_fields(client, accounts, feed):
    digest = _digest()
    token = _login(client, "a")
    conv_id = _conv(client, token, "600000", "SH")

    body = _body(snapshot=_snap(digest, user_id=MEMBER_B))
    r = _send(client, token, conv_id, body)
    assert r.status_code == 422, r.text


# ── ⑥ 严格 schema：畸形一律 422 ──────────────────────────────────────────

@pytest.mark.parametrize("mutate", [
    lambda d: _snap(d["digest"].upper()),
    lambda d: _snap(d["digest"][:63]),
    lambda d: _snap("z" * 64),
    lambda d: _snap(d["digest"], symbol="60000"),
    lambda d: _snap(d["digest"], market="US"),
    lambda d: _snap(d["digest"], instrument="auto"),
    lambda d: _snap(d["digest"], interval="1m"),
    lambda d: _snap(d["digest"], days=0),
    lambda d: _snap(d["digest"], days=2001),
    lambda d: _snap(d["digest"], schema="sida-chan-snapshot-v1"),
    lambda d: _snap(d["digest"], days="60"),
    lambda d: {"symbol": "600000", "instrument": "stock", "days": 60},   # 缺 schema/digest
])
def test_malformed_chart_snapshot_is_422(client, accounts, feed, mutate):
    digest = _digest()
    token = _login(client, "a")
    conv_id = _conv(client, token, "600000", "SH")

    snap = mutate({"digest": digest})
    r = _send(client, token, conv_id, _body(snapshot=snap))
    assert r.status_code == 422, (snap, r.text)
    assert _msgs(conv_id) == []


# ── ⑦ 并发：两路串流各带各的快照 ─────────────────────────────────────────

def test_concurrent_streams_do_not_share_snapshot(client, accounts, feed, holder):
    import httpx

    from src.web.app import app

    digest_a = _digest("600000")
    digest_b = _digest("600519")

    async def _scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
            async def _tok(name: str, pw: str) -> str:
                r = await ac.post("/api/auth/login", json={"username": name, "password": pw})
                assert r.status_code == 200, r.text
                return r.json()["data"]["token"]

            token_a = await _tok("chan-ctx-member-a", PASSWORDS["a"])
            token_b = await _tok("chan-ctx-member-b", PASSWORDS["b"])

            async def _mk_conv(token, symbol):
                r = await ac.post("/api/chat/conversations", headers=_headers(token),
                                  json={"stock_symbol": symbol, "stock_market": "SH"})
                assert r.status_code == 200, r.text
                return r.json()["data"]["id"]

            conv_a = await _mk_conv(token_a, "600000")
            conv_b = await _mk_conv(token_b, "600519")

            payloads = [
                (token_a, conv_a, _body(snapshot=_snap(digest_a))),
                (token_b, conv_b, _body("600519 缠论结构怎么看",
                                        _snap(digest_b, symbol="600519"))),
            ]

            async def _one(token, conv_id, body):
                r = await ac.post(f"/api/chat/conversations/{conv_id}/messages/stream",
                                  headers=_headers(token), json=body)
                assert r.status_code == 200, r.text
                return r.text

            return await asyncio.gather(*[_one(*p) for p in payloads])

    texts = asyncio.run(_scenario())
    assert all(FINAL_TEXT in t for t in texts), texts

    joined = "\n".join(holder.outputs)
    assert "【缠论结构·只读观测】600000" in joined and _short(digest_a) in joined
    assert "【缠论结构·只读观测】600519" in joined and _short(digest_b) in joined
    # 每条工具结果只能带自己的摘要
    for out in holder.outputs:
        if "600000" in out:
            assert _short(digest_b) not in out
        if "600519" in out:
            assert _short(digest_a) not in out


# ── ⑧ 断连 / 取消：finally 必须 reset ────────────────────────────────────

class _FakeRequest:
    def __init__(self, disconnect_after: int = 0):
        self.remaining = disconnect_after
        self.calls = 0

    async def is_disconnected(self) -> bool:
        self.calls += 1
        if self.remaining <= 0:
            return True
        self.remaining -= 1
        return False


def _drive(conv_id: int, body, user, request):
    """直接驱动真实 handler 的 StreamingResponse（同一 context，便于断言 reset）。"""

    async def _run():
        resp = await chat_api.send_message_stream(conv_id, body, request, user)
        chunks = []
        async for chunk in resp.body_iterator:
            chunks.append(chunk)
        return chunks

    return _run()


@pytest.fixture()
def ctx_spy(monkeypatch):
    """记录 exit_request_ctx 之前/之后的 ContextVar 值（证明 reset 发生在 finally）。"""
    from src.harness import context as ctx

    seen: list = []
    real = ctx.exit_request_ctx

    def _spy(tokens):
        seen.append(("before", ctx.REQ_CHAN_SNAPSHOT.get()))
        real(tokens)
        seen.append(("after", ctx.REQ_CHAN_SNAPSHOT.get()))

    monkeypatch.setattr(chat_api, "_exit_request_ctx", _spy)
    return seen


def test_disconnect_resets_turn_snapshot(client, accounts, feed, holder, ctx_spy):
    from src.harness.context import REQ_CHAN_SNAPSHOT
    from src.web.database import SessionLocal
    from src.web.models import ChatConversation, User

    digest = _digest()
    token = _login(client, "a")
    conv_id = _conv(client, token, "600000", "SH")

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == MEMBER_A).first()
        conv = db.query(ChatConversation).filter(ChatConversation.id == conv_id).first()
        body = chat_api.SendMessageBody(**{"content": QUESTION,
                                           "chart_snapshot": _snap(digest)})
        # 开流前就"断开"：走 is_disconnected 分支提前 break，finally 仍要 reset
        chunks = asyncio.run(_drive(conv_id, body, user, _FakeRequest(disconnect_after=0)))
    finally:
        db.close()

    assert any("event: done" in c for c in chunks), chunks
    assert REQ_CHAN_SNAPSHOT.get() is None
    assert ctx_spy and ctx_spy[0][0] == "before" and ctx_spy[-1] == ("after", None)
    assert ctx_spy[0][1] and ctx_spy[0][1]["symbol"] == "600000", ctx_spy[0]


def test_cancellation_resets_turn_snapshot(client, accounts, feed, holder, ctx_spy,
                                           monkeypatch):
    from src.harness.context import REQ_CHAN_SNAPSHOT
    from src.web.database import SessionLocal
    from src.web.models import User

    digest = _digest()
    token = _login(client, "a")
    conv_id = _conv(client, token, "600000", "SH")

    class _Cancelling(_ScriptedAI):
        async def chat_with_tools_stream(self, messages, tools, temperature=0.5):
            raise asyncio.CancelledError()
            yield  # pragma: no cover - 只为签名

    monkeypatch.setattr(chat_api, "_get_ai_client",
                        lambda db, model_id=None, user=None: _Cancelling(holder))

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == MEMBER_A).first()
        body = chat_api.SendMessageBody(**{"content": QUESTION,
                                           "chart_snapshot": _snap(digest)})
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(_drive(conv_id, body, user, _FakeRequest(disconnect_after=9)))
    finally:
        db.close()

    assert REQ_CHAN_SNAPSHOT.get() is None
    assert ctx_spy[-1] == ("after", None)
    assert ctx_spy[0][1] and ctx_spy[0][1]["symbol"] == "600000"


def test_enter_exit_resets_on_exception(accounts):
    """token 机制本身：哪怕块内抛异常，exit 之后值也回到 None（且不污染外层上下文）。"""
    from src.harness.context import (
        REQ_CHAN_SNAPSHOT,
        ChartSnapshotBody,
        TurnChartSnapshot,
        enter_request_ctx,
        exit_request_ctx,
    )

    ref = {"symbol": "600000", "market": "CN", "instrument": "stock",
           "interval": "1d", "days": 60, "digest": "a" * 64, "as_of_day": "2026-11-16"}
    body = chat_api.SendMessageBody(**{"content": QUESTION})
    body.chart_snapshot = ChartSnapshotBody(**_snap("a" * 64))
    messages = [{"role": "system", "content": "x"}]
    conv = type("C", (), {"stock_symbol": "600000", "stock_market": "SH"})()

    assert REQ_CHAN_SNAPSHOT.get() is None
    toks = enter_request_ctx(None, conv, body, messages,
                             chart_snapshot=TurnChartSnapshot(ref=ref, note="note"))
    try:
        assert REQ_CHAN_SNAPSHOT.get() == ref, "本轮必须能读到"
        raise RuntimeError("boom")
    except RuntimeError:
        pass
    finally:
        exit_request_ctx(toks)
    assert REQ_CHAN_SNAPSHOT.get() is None, "异常路径也必须 reset"
    assert messages[0]["content"].endswith("note")


# ── ⑨ 隔离：真正的 engine 必须是 scratch SQLite ───────────────────────────

def test_suite_uses_isolated_scratch_sqlite(accounts):
    from src.web.database import SessionLocal

    data_dir = Path(os.environ["DATA_DIR"]).resolve()
    assert data_dir.name.startswith("sida_test_data_"), f"DATA_DIR 不是临时目录: {data_dir}"
    session = SessionLocal()
    try:
        dialect = session.get_bind().dialect.name
        url = str(session.get_bind().url)
    finally:
        session.close()
    assert dialect == "sqlite", f"测试库必须是隔离 SQLite, 实际: {dialect}"
    db_path = Path(url.split("sqlite:///", 1)[-1]).resolve()
    assert any(p.name.startswith("sida_test_data_") for p in db_path.parents), db_path
    repo_data = (Path(__file__).resolve().parent.parent / "data").resolve()
    assert repo_data not in db_path.parents


# ── ⑩ 校验失败不许悄悄改会话标的 / 校验后不许再用旧快照 ─────────────────────

@pytest.fixture()
def clean_convs(accounts):
    """本组用例自建的会话连同消息一起删干净。

    SQLite 不 AUTOINCREMENT: 会话行删掉后 id 会被后面的用例复用, 而 purge_users
    只删引用 users 的表 —— 消息表按 conversation_id 引用, 会留成孤儿并"挂"到
    复用了同一 id 的新会话上(test_chat_stream 因此会多出历史消息)。收尾时一并清掉。
    """
    yield
    from src.web.database import SessionLocal
    from src.web.models import ChatConversation, ChatMessage

    db = SessionLocal()
    try:
        ids = [row[0] for row in db.query(ChatConversation.id)
               .filter(ChatConversation.user_id == MEMBER_A).all()]
        if ids:
            db.query(ChatMessage).filter(
                ChatMessage.conversation_id.in_(ids)
            ).delete(synchronize_session=False)
            db.query(ChatConversation).filter(
                ChatConversation.id.in_(ids)
            ).delete(synchronize_session=False)
            db.commit()
    finally:
        db.close()


def _set_symbol(conv_id: int, symbol: str, market: str = "SH") -> None:
    """直接写库模拟「另一轮对话已经提交了切标的」（不是本请求的路径）。"""
    from src.web.database import SessionLocal
    from src.web.models import ChatConversation

    db = SessionLocal()
    try:
        conv = db.query(ChatConversation).filter(ChatConversation.id == conv_id).first()
        assert conv is not None
        conv.stock_symbol = symbol
        conv.stock_market = market
        db.commit()
    finally:
        db.close()


def test_stale_digest_does_not_rebind_conversation(client, accounts, feed, holder, clean_convs):
    """409 的那条消息不许留痕: 既不入库, 也不许把会话标的改成它请求的 B。

    回归(2026-11-17): 非流式入口先 `_bind_conv_symbol` 再校验快照 —— bind 内部
    `db.commit()`, 于是"409 已拒绝且未保存"的消息悄悄把 A 会话改成 B。
    """
    stale = _digest("600519")
    token = _login(client, "a")
    conv_id = _conv(client, token, "600000", "SH")

    feed.bars[-1] = _k(COMPLETED[-1], (10.7, 9.9, 10.8, 9.2))   # 图表已变 → 摘要过期
    kl._kl_cache.clear()

    payload = {"content": "600519 缠论结构怎么看", "stock_symbol": "600519",
               "stock_market": "SH",
               "chart_snapshot": _snap(stale, symbol="600519")}
    r = _send(client, token, conv_id, payload)
    assert r.status_code == 409, r.text

    row = _conv_row(conv_id)
    assert row.stock_symbol == "600000", "409 的消息不许改会话标的"
    assert row.stock_market == "SH"
    assert _msgs(conv_id) == []
    assert holder.systems == [] and holder.outputs == [], "409 不许进模型/工具"


def test_stream_preflight_rejection_does_not_rebind_conversation(client, accounts, feed, holder, clean_convs):
    """流式入口同理: 开流前的 409 也必须零副作用(不 commit 任何 bind)。"""
    stale = _digest("600519")
    token = _login(client, "a")
    conv_id = _conv(client, token, "600000", "SH")

    feed.bars[-1] = _k(COMPLETED[-1], (10.7, 9.9, 10.8, 9.2))
    kl._kl_cache.clear()

    payload = {"content": "600519 缠论结构怎么看", "stock_symbol": "600519",
               "stock_market": "SH",
               "chart_snapshot": _snap(stale, symbol="600519")}
    with client.stream("POST", f"/api/chat/conversations/{conv_id}/messages/stream",
                       headers=_headers(token), json=payload) as resp:
        assert resp.status_code == 409, resp.read()

    row = _conv_row(conv_id)
    assert row.stock_symbol == "600000", "preflight 拒绝不许先 bind 再校验"
    assert _msgs(conv_id) == []
    assert holder.systems == [] and holder.outputs == []


def test_snapshot_dropped_when_conversation_switches_after_preflight(
        client, accounts, feed, holder, monkeypatch, clean_convs):
    """TOCTOU: preflight 通过后、gen 真正取会话前，另一轮把会话切到了别的票。

    旧快照是 600000 的，本轮已经没有 600000 的会话身份了 —— 必须丢弃而不是
    拿旧摘要去回一份「另一只票的结构」。
    """
    digest = _digest("600000")
    token = _login(client, "a")
    conv_id = _conv(client, token, "600000", "SH")

    real = chat_api._preflight_chart_snapshot
    switched: list[bool] = []

    async def _preflight_then_other_turn(conversation_id, body, user):
        snap = await real(conversation_id, body, user)
        assert snap is not None and snap.ref and snap.ref["symbol"] == "600000", snap
        if not switched:
            switched.append(True)
            _set_symbol(conversation_id, "600519")   # 另一轮已提交的切标的
        return snap

    monkeypatch.setattr(chat_api, "_preflight_chart_snapshot", _preflight_then_other_turn)

    # 正文不带代码/锚点: 本轮标的只能来自会话(正是被并发改写的那一份)
    with client.stream("POST", f"/api/chat/conversations/{conv_id}/messages/stream",
                       headers=_headers(token),
                       json=_body("这只票缠论结构怎么样", _snap(digest))) as resp:
        assert resp.status_code == 200, resp.read()
        text = "".join(resp.iter_text())

    assert switched == [True]
    assert FINAL_TEXT in text, text
    system = holder.systems[-1]
    assert digest not in system, "旧快照摘要不许注入本轮 system"
    assert "【本轮图表快照（消息入口时已复核）】\nsymbol=" not in system
    assert "不一致" in system or "已丢弃" in system

    out = holder.outputs[-1]
    assert digest not in out and "分型 " not in out
    assert "缺少图表快照摘要" in out

    row = _conv_row(conv_id)
    assert row.stock_symbol == "600519", "本轮的旧快照不许把会话改回 600000"
    for m in _msgs(conv_id):
        _no_secret_in(m.content)
