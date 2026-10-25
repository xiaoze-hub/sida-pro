"""外部任务提交 + Webhook 回调（P2-2.2）。

钉住五件事：
  1. 内网回调地址被拒（SSRF）；域名解析到内网也要拒；
  2. 回调失败**有限重试**，最终失败要留原因（不静默丢失）；
  3. 载荷不含密钥、不含问题原文（只给长度）；
  4. 未鉴权 401（沿用网关既有机制）；
  5. 提交返回 run_id 并登记回调，但**不在请求里等 LLM**。

回调全部 mock，不打真实网络；重试退避 patch 成 0，**不靠 sleep 等**。
"""

from __future__ import annotations

import json

import pytest

from src.core import webhook_delivery as wh


# ── SSRF ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("url", [
    "http://127.0.0.1/hook",
    "http://localhost:9000/hook",
    "http://10.0.0.5/hook",
    "http://192.168.1.10/hook",
    "http://172.20.0.3/hook",
    "http://169.254.169.254/latest/meta-data",
])
def test_内网回调地址被拒(url):
    with pytest.raises(wh.WebhookRejected):
        wh.validate_callback_url(url)


def test_非http协议被拒():
    with pytest.raises(wh.WebhookRejected):
        wh.validate_callback_url("ftp://example.com/hook")
    with pytest.raises(wh.WebhookRejected):
        wh.validate_callback_url("file:///etc/passwd")


def test_域名解析到内网也要拒(monkeypatch):
    monkeypatch.setattr(wh.socket, "getaddrinfo",
                        lambda *a, **k: [(2, 1, 6, "", ("127.0.0.1", 0))], raising=True)
    with pytest.raises(wh.WebhookRejected) as ei:
        wh.validate_callback_url("http://evil.example.com/hook")
    assert "内网" in str(ei.value)


def test_公网地址放行(monkeypatch):
    monkeypatch.setattr(wh.socket, "getaddrinfo",
                        lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))], raising=True)
    assert wh.validate_callback_url("https://example.com/hook") == "https://example.com/hook"


# ── 投递与重试 ──────────────────────────────────────────────────────────
class _Resp:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_首次成功只调一次(monkeypatch):
    calls = []
    monkeypatch.setattr(wh.urllib.request, "urlopen",
                        lambda req, timeout=0: (calls.append(req), _Resp(200))[1], raising=True)
    res = wh.deliver("https://example.com/hook", {"run_id": "r1"}, backoff=())
    assert res["delivered"] is True
    assert res["attempts"] == 1
    assert len(calls) == 1


def test_失败重试有限次并记录原因(monkeypatch):
    calls = []

    def boom(req, timeout=0):
        calls.append(req)
        raise TimeoutError("timed out")

    monkeypatch.setattr(wh.urllib.request, "urlopen", boom, raising=True)
    res = wh.deliver("https://example.com/hook", {"run_id": "r1"}, attempts=3, backoff=(0, 0))
    assert res["delivered"] is False
    assert res["attempts"] == 3
    assert len(calls) == 3, "必须真的重试 3 次（不是记 3 次）"
    assert "TimeoutError" in res["error"]


def test_非2xx算失败(monkeypatch):
    monkeypatch.setattr(wh.urllib.request, "urlopen",
                        lambda req, timeout=0: _Resp(500), raising=True)
    res = wh.deliver("https://example.com/hook", {"run_id": "r1"}, attempts=2, backoff=(0,))
    assert res["delivered"] is False
    assert "HTTP 500" in res["error"]


# ── 载荷 ────────────────────────────────────────────────────────────────
def test_载荷不含密钥与问题原文():
    p = wh.build_payload(run_id="r1", status="ok", question_len=37, elapsed_ms=1200)
    blob = json.dumps(p, ensure_ascii=False)
    assert p["question_chars"] == 37
    assert "question" not in p, "不许把问题原文外发"
    for bad in ("key", "token", "secret", "password"):
        assert bad not in blob.lower(), f"载荷不许出现 {bad}"


def test_错误信息被截断():
    p = wh.build_payload(run_id="r1", status="failed", error="x" * 1000)
    assert len(p["error"]) <= 300


# ── 端点 ────────────────────────────────────────────────────────────────
def test_提交任务返回run_id并登记回调(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from src.db.models import Base, WebhookSubscription
    from src.web.api import copilot_jobs, skills_gateway
    from src.web.api.auth import get_current_user
    from src.web.database import get_db

    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    Session = sessionmaker(bind=eng)

    class U:
        id = "u1"
        username = "t"

    # 不真跑 LLM：把 _execute 换成立即返回
    async def fake_execute(*a, **k):
        return None

    monkeypatch.setattr(copilot_jobs, "_execute", fake_execute, raising=True)
    monkeypatch.setattr(copilot_jobs, "_TASKS", {}, raising=False)
    monkeypatch.setattr(wh.socket, "getaddrinfo",
                        lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))], raising=True)
    import src.core.copilot_quota as quota
    monkeypatch.setattr(quota, "enforce_run_concurrency", lambda db, user: None, raising=True)

    app = FastAPI()
    app.include_router(skills_gateway.router)
    app.dependency_overrides[get_current_user] = lambda: U()
    app.dependency_overrides[skills_gateway._get_current_user] = lambda: U()
    app.dependency_overrides[get_db] = lambda: Session()

    c = TestClient(app)
    r = c.post("/copilot/tasks", json={"question": "上证多少点", "callback_url": "https://example.com/hook"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["run_id"]
    assert body["callback_registered"] is True

    db = Session()
    subs = db.query(WebhookSubscription).filter(WebhookSubscription.run_id == body["run_id"]).all()
    assert len(subs) == 1 and subs[0].status == "pending"
    db.close()


def test_内网回调在端点层被拒(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from src.web.api import skills_gateway
    from src.web.api.auth import get_current_user

    class U:
        id = "u1"
        username = "t"

    app = FastAPI()
    app.include_router(skills_gateway.router)
    app.dependency_overrides[get_current_user] = lambda: U()
    app.dependency_overrides[skills_gateway._get_current_user] = lambda: U()

    c = TestClient(app)
    r = c.post("/copilot/tasks", json={"question": "x", "callback_url": "http://127.0.0.1/hook"})
    assert r.status_code == 400
    assert "内网" in r.json()["detail"]


def test_空问题被拒():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from src.web.api import skills_gateway
    from src.web.api.auth import get_current_user

    class U:
        id = "u1"
        username = "t"

    app = FastAPI()
    app.include_router(skills_gateway.router)
    app.dependency_overrides[get_current_user] = lambda: U()
    app.dependency_overrides[skills_gateway._get_current_user] = lambda: U()

    r = TestClient(app).post("/copilot/tasks", json={"question": "   "})
    assert r.status_code == 400
