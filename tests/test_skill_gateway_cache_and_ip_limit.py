"""Skill Gateway: 3 分钟结果缓存 + 领 key 的 IP 限制（2026-11-03）。

背景（生产实测）：
- 一把 guest key 被 8 台云主机轮询，46 次调用/2m40s → 突增冻结；
- 冻掉后 3 分钟内换 IP 领新 key 继续打，按 key 冻结拦不住换 key；
- 原来"每小时 5 次"的领 key 限制是**进程内**内存计数，且用 `request.client.host`，
  在 nginx 后面拿到的是 127.0.0.1 —— 等于全站共用一个桶。

本文件守住：
1. 缓存 key 是**稳定哈希**：同一请求在不同 `PYTHONHASHSEED` 的进程里必须算出同一个 key
   （原实现用 `hash(frozenset(args))`，每个进程随机化 ⇒ 跨进程永不命中）；
2. 缓存 TTL 是 180 秒，参数/格式不同不互相命中；
3. 同 IP 30 分钟内第二次领 key 必须 429 并给出 Retry-After；
4. 领 key 的 IP 取自 XFF 感知判定，不是 `request.client.host`。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

from src.web.api import skills_gateway as gw

ROOT = Path(__file__).resolve().parents[1]


class _FakeCache:
    """最小 biz_cache 替身：记录 set 的 ttl，供断言。"""

    def __init__(self) -> None:
        self.data: dict[str, object] = {}
        self.ttls: dict[str, int | None] = {}

    def get_json(self, key):
        return self.data.get(key)

    def set_json(self, key, value, ttl=None):
        self.data[key] = value
        self.ttls[key] = ttl
        return True


@pytest.fixture()
def fake_cache(monkeypatch):
    fake = _FakeCache()
    monkeypatch.setattr(gw, "biz_cache", fake)
    return fake


# ── 缓存 ────────────────────────────────────────────────────────────────


def test_缓存TTL是3分钟():
    assert gw._SKILL_RUN_CACHE_TTL_S == 180


def test_缓存key稳定_不随hashseed变化():
    """原实现用 hash(frozenset(...))：Python 每进程随机 salt → 跨 worker 永不命中。"""
    code = (
        "import sys; sys.path.insert(0, %r);"
        "from src.web.api.skills_gateway import _run_cache_key as k;"
        "print(k('get_stock_quote', 'json', {'symbol': '600000', 'market': 'CN'}))"
    ) % str(ROOT)
    keys = set()
    for seed in ("1", "2"):
        env = dict(os.environ, PYTHONHASHSEED=seed, SIDA_DB_URL="sqlite://")
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env)
        assert out.returncode == 0, out.stderr[-400:]
        keys.add(out.stdout.strip().splitlines()[-1])
    assert len(keys) == 1, f"不同 PYTHONHASHSEED 得到不同 key: {keys}"


def test_缓存key对参数顺序不敏感_对参数值敏感():
    a = gw._run_cache_key("s", "json", {"a": 1, "b": 2})
    b = gw._run_cache_key("s", "json", {"b": 2, "a": 1})
    c = gw._run_cache_key("s", "json", {"a": 1, "b": 3})
    d = gw._run_cache_key("s", "html", {"a": 1, "b": 2})
    assert a == b, "dict 顺序不同必须命中同一缓存"
    assert a != c, "参数值不同不能命中"
    assert a != d, "输出格式不同不能命中"


def test_缓存读写走biz_cache且带180秒ttl(fake_cache):
    key = gw._run_cache_key("get_stock_quote", "json", {"symbol": "600000"})
    assert gw._hot_cache_get(key) is None
    gw._hot_cache_set(key, {"skill": "get_stock_quote", "result": "x"})
    assert fake_cache.ttls[key] == 180
    assert gw._hot_cache_get(key) == {"skill": "get_stock_quote", "result": "x"}


def test_缓存层异常不影响请求(monkeypatch):
    class _Boom:
        def get_json(self, key):
            raise RuntimeError("redis down")

        def set_json(self, key, value, ttl=None):
            raise RuntimeError("redis down")

    monkeypatch.setattr(gw, "biz_cache", _Boom())
    assert gw._hot_cache_get("k") is None          # 降级为未命中
    gw._hot_cache_set("k", {"a": 1})               # 不抛


# ── 领 key 的 IP 限制 ───────────────────────────────────────────────────


def test_同IP30分钟只能领一把(fake_cache):
    ip = "219.139.229.21"
    gw._check_key_issue_ip(ip)          # 第一次: 放行
    gw._mark_key_issue_ip(ip)
    with pytest.raises(HTTPException) as ei:
        gw._check_key_issue_ip(ip)
    assert ei.value.status_code == 429
    assert "30 分钟" in ei.value.detail
    assert int(ei.value.headers["Retry-After"]) > 0


def test_不同IP互不影响(fake_cache):
    gw._check_key_issue_ip("1.1.1.1")
    gw._mark_key_issue_ip("1.1.1.1")
    gw._check_key_issue_ip("2.2.2.2")   # 不抛
    assert gw._KEY_ISSUE_IP_WINDOW_S == 1800


def test_窗口过期后可再领(fake_cache):
    ip = "3.3.3.3"
    gw._check_key_issue_ip(ip)
    gw._mark_key_issue_ip(ip)
    # 把记账时间往前拨 31 分钟, 模拟窗口过期(Redis TTL 到期后 key 会消失,
    # 这里用旧时间戳覆盖同一效果)
    fake_cache.data[f"{gw._KEY_ISSUE_IP_KEY}:{ip}"] = 0.0
    gw._check_key_issue_ip(ip)          # 不抛


def test_领key用XFF感知的真实IP():
    """nginx 后面 request.client.host 是 127.0.0.1；必须能取到 XFF 里的真实 IP。"""
    from starlette.requests import Request

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/guest-key",
        "headers": [(b"x-forwarded-for", b"219.139.229.21, 10.0.0.5")],
        "client": ("127.0.0.1", 12345),
        "query_string": b"",
    }
    assert gw._client_ip(Request(scope)) == "219.139.229.21"

    # 直连 peer 是公网时不信 XFF(防伪造绕过)
    scope2 = dict(scope, client=("8.8.8.8", 12345))
    assert gw._client_ip(Request(scope2)) == "8.8.8.8"


# ── 端到端: 同一请求第二次命中缓存, 不再执行上游 ──────────────────────


def test_同一请求第二次命中缓存(monkeypatch, tmp_path):
    """run_skill 的真实路径: 缓存 key + 命中重建响应 + 命中仍记账。"""
    import asyncio
    from datetime import datetime

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from src.agents.chat.registry import CHAT_TOOL_REGISTRY, ChatTool
    from src.db.models import Base, SkillUsage

    eng = create_engine(f"sqlite:///{tmp_path/'gw.db'}")
    Base.metadata.create_all(
        bind=eng,
        tables=[SkillUsage.__table__, __import__("src.db.models", fromlist=["SkillApiKey"]).SkillApiKey.__table__],
    )
    db = sessionmaker(bind=eng)()

    calls = {"n": 0}

    async def _handler(_db, _args, _user):
        calls["n"] += 1
        return f"报价快照 #{calls['n']}"

    # 用真实开放 skill 的名字(档位校验按 OPEN_SKILLS 名单走), 只把 handler 换成计数桩
    probe = "get_stock_quote"
    assert probe in gw.OPEN_SKILLS, "选一个真实开放 skill 做探针"
    monkeypatch.setitem(
        CHAT_TOOL_REGISTRY, probe,
        ChatTool(name=probe, schema=None, handler=_handler, caliber="tick"),
    )
    fake = _FakeCache()
    monkeypatch.setattr(gw, "biz_cache", fake)
    monkeypatch.setattr(
        gw, "_resolve_call_identity",
        lambda *a, **k: gw._CallIdentity(
            channel="api", api_key_id=1, user_id=None, rate_key="k",
            tier="free", daily_limit=10_000, key_row=None,
        ),
    )
    monkeypatch.setattr(gw, "_check_rate_limit", lambda *a, **k: None)

    body = gw.SkillRunRequest(args={"symbol": "600000", "market": "CN"}, format="json")

    class _Req:
        headers: dict = {}

    r1 = asyncio.run(gw.run_skill(probe, body, _Req(), db))
    r2 = asyncio.run(gw.run_skill(probe, body, _Req(), db))

    assert calls["n"] == 1, "第二次必须走缓存, 不能再次执行上游"
    assert r1.result == r2.result
    assert r2.duration_ms == r1.duration_ms
    # 命中仍记账: 两次调用两条 usage(配额不能因命中而被绕过)
    assert db.query(SkillUsage).count() == 2
    db.close()
    eng.dispose()
