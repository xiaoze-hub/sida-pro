"""钉子: `/api/auth/status` 要如实告知"邮件服务是否可用", 且**不泄露用户信息**(2026-09-19)。

背景: 邮箱注册/验证码登录在 SMTP 未配置时**发不出验证码**。前端需要提前知道, 否则用户填完表单
才撞 503(更早的版本还会显示"已发送", 那是彻底的误导)。该端点是**未鉴权**的, 所以只允许暴露
布尔配置态, 不许带任何用户数据。
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from src.web.app import app


def _status() -> dict:
    c = TestClient(app)
    r = c.get("/api/auth/status")
    assert r.status_code == 200
    return r.json()["data"]


def test_email_configured_false_when_smtp_missing(monkeypatch):
    for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASS"):
        monkeypatch.delenv(k, raising=False)
    assert _status()["email_configured"] is False


def test_email_configured_true_when_smtp_set(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_USER", "bot@example.com")
    monkeypatch.setenv("SMTP_PASS", "secret")
    assert _status()["email_configured"] is True


def test_partial_config_is_false(monkeypatch):
    """只配一半(常见的手滑)必须算**不可用** —— 否则又回到"以为配好了其实发不出"。"""
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.delenv("SMTP_USER", raising=False)
    monkeypatch.delenv("SMTP_PASS", raising=False)
    assert _status()["email_configured"] is False


def test_status_does_not_leak_user_info(monkeypatch):
    """未鉴权端点: 只许给布尔配置态, 不许带用户名/角色/邮箱等。"""
    data = _status()
    assert set(data.keys()) <= {"initialized", "email_configured"}
    raw = str(data).lower()
    for leak in ("password", "token", "username", "role", "email@"):
        assert leak not in raw
