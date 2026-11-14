# -*- coding: utf-8 -*-
"""外发结果必须**可证**: 200/HTML、非 JSON、无 ack 一律不算 sent(2026-11-12 复审六)。

三类"假成功"在旧实现里都能发生:

1. `_send_hermes` 在 `resp.json()` 失败时把 `data` 置空, 于是 `status` 缺失被当成成功
   → HTTP 200 + HTML(登录页/反代错误页/维护页)被写成 `push_status=sent`;
2. `resp.json()` 抛的 `JSONDecodeError` 是 `ValueError` 子类, `classify()` 里
   `ValueError` 分支更靠前 → 非 JSON 回包被判成「渠道配置不完整」, 运维去查一个
   根本没坏的配置;
3. `_send_custom` 遇到未知渠道类型只 `logger.warning` 就返回 None, 上层把"没发"
   当成功。

以及**历史数据**: 修复前落库的 `push_error` / `push_channels` 里可能已经有
webhook key / 代理账号密码。这些行是既成事实 —— 本文件验证出口处按形态白名单
收敛(不认的换成兜底类别), 且**数据库里的原值一个字节都没被改**(不原地清洗/删除)。

全部用 tmp_path 下的真实 SQLite + 127.0.0.1 上的临时 HTTP 服务(mock 外部传输),
不访问任何真实渠道。
"""

from __future__ import annotations

import html
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import src.web.models  # noqa: F401  注册 ORM 模型
from src.core import net_guard, notify_center, notify_errors
from src.core.notifier import NotifierManager
from src.web.database import Base
from src.web.models import Notification, NotifyChannel

WECOM_KEY = str(__import__("uuid").uuid4())
HERMES_SECRET = __import__("secrets").token_hex(16)
DB_PASSWORD = "S1da-Guard-Pa55"
HISTORIC_TOKEN = __import__("secrets").token_hex(16)


class _Gateway(BaseHTTPRequestHandler):
    """Hermes 网关的**替身**: 用类属性控制响应, 记录收到的请求。"""

    status = 200
    content_type = "application/json"
    body = b'{"status": "delivered"}'
    received: list[dict] = []

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        type(self).received.append(
            {
                "path": self.path,
                "signature": self.headers.get("X-Hub-Signature-256") or "",
                "body": raw,
            }
        )
        self.send_response(type(self).status)
        self.send_header("Content-Type", type(self).content_type)
        self.send_header("Content-Length", str(len(type(self).body)))
        self.end_headers()
        self.wfile.write(type(self).body)

    def log_message(self, fmt, *args):  # noqa: D401 - 静默
        return


@pytest.fixture()
def gateway():
    _Gateway.received = []
    _Gateway.status = 200
    _Gateway.content_type = "application/json"
    _Gateway.body = b'{"status": "delivered"}'
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Gateway)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield srv, int(srv.server_address[1])
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=5)


@pytest.fixture(autouse=True)
def _allow_loopback(monkeypatch):
    """本地 mock 网关在 127.0.0.1: 按运维放行开关显式授权(不放开其它内网)。"""
    monkeypatch.setenv(net_guard.ENV_ALLOW_PRIVATE_HOSTS, "127.0.0.1")


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    db_file = tmp_path / f"notify_delivery_{DB_PASSWORD}.db"
    engine = create_engine(
        f"sqlite:///{db_file}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(notify_center, "SessionLocal", Session)
    yield engine, Session
    engine.dispose()


def _add_channel(Session, **kw) -> int:
    db = Session()
    try:
        ch = NotifyChannel(
            user_id=None,
            name=kw.pop("name", "渠道"),
            type=kw.pop("type"),
            config=kw.pop("config"),
            enabled=True,
        )
        db.add(ch)
        db.commit()
        return int(ch.id)
    finally:
        db.close()


def _push(Session):
    nid = notify_center.push_notification(
        "❌ 盘中监控 执行失败", "详情", category="agent_run", level="error", user_id=None
    )
    db = Session()
    try:
        return db.query(Notification).filter(Notification.id == nid).first()
    finally:
        db.close()


def _hermes_url(port: int) -> str:
    return f"http://127.0.0.1:{port}/hooks/sida?token={WECOM_KEY}"


def test_hermes_http200_html_is_not_reported_sent(isolated_db, gateway):
    """200 + HTML(登录页/反代错误页): 可证**没有**回执 → failed(bad_response)。"""
    _srv, port = gateway
    _Gateway.status = 200
    _Gateway.content_type = "text/html"
    _Gateway.body = (
        b"<!DOCTYPE html><html><head><title>Sign in</title></head>"
        b"<body>Please log in</body></html>"
    )
    engine, Session = isolated_db
    _add_channel(
        Session,
        name="中转",
        type="hermes",
        config={"webhook_url": _hermes_url(port), "secret": HERMES_SECRET},
    )

    n = _push(Session)
    assert n.push_status == "failed", "HTTP 200 + HTML 被写成 sent = 谎报送达"
    assert "响应无法解析" in (n.push_error or "")
    assert WECOM_KEY not in (n.push_error or "")
    assert len(_Gateway.received) == 1, "请求确实发出去了(不是没发)"  # 真实外发尝试


@pytest.mark.parametrize(
    "body",
    [
        b"<html><body>maintenance</body></html>",
        b"",
        b"OK",  # 纯文本 200
        b'["delivered"]',  # JSON 但不是对象
    ],
)
def test_hermes_non_json_or_wrong_shape_200_is_failure(isolated_db, gateway, body):
    _srv, port = gateway
    _Gateway.status = 200
    _Gateway.content_type = "text/html"
    _Gateway.body = body
    engine, Session = isolated_db
    _add_channel(
        Session, name="中转", type="hermes", config={"webhook_url": _hermes_url(port)}
    )
    n = _push(Session)
    assert n.push_status == "failed"
    assert "响应无法解析" in (n.push_error or "")


@pytest.mark.parametrize(
    "body,expected",
    [
        (b'{"status": "delivered"}', "sent"),
        (b'{"status": "ok"}', "sent"),
        (b'{"ok": true}', "sent"),
    ],
)
def test_hermes_explicit_ack_is_sent(isolated_db, gateway, body, expected):
    """明确 ack 才算成功(不破坏正常通知)。"""
    _srv, port = gateway
    _Gateway.status = 200
    _Gateway.content_type = "application/json"
    _Gateway.body = body
    engine, Session = isolated_db
    _add_channel(
        Session, name="中转", type="hermes", config={"webhook_url": _hermes_url(port)}
    )
    n = _push(Session)
    assert n.push_status == expected
    assert (n.push_error or "") == ""
    assert (n.push_channels or [])[0]["status"] == "sent"


def test_hermes_200_without_ack_is_unconfirmed_not_sent(isolated_db, gateway):
    """200 + `{}` / 未知状态词: **未知** → 不写 sent, 文案是"未确认送达"。"""
    _srv, port = gateway
    _Gateway.status = 200
    _Gateway.body = b"{}"
    engine, Session = isolated_db
    _add_channel(
        Session, name="中转", type="hermes", config={"webhook_url": _hermes_url(port)}
    )
    n = _push(Session)
    assert n.push_status == "failed"
    assert "未确认送达" in (n.push_error or "")


def test_hermes_explicit_reject_status_is_failure(isolated_db, gateway):
    _srv, port = gateway
    _Gateway.status = 200
    _Gateway.body = b'{"status": "failed", "reason": "upstream 403"}'
    engine, Session = isolated_db
    _add_channel(
        Session, name="中转", type="hermes", config={"webhook_url": _hermes_url(port)}
    )
    n = _push(Session)
    assert n.push_status == "failed"
    assert "upstream" not in (n.push_error or ""), "provider 原文不能进 push_error"


def test_http_error_wins_over_body_parsing(isolated_db, gateway):
    """403 + HTML: 必须判「凭证被拒」, 不能被正文解析失败抢走类别。"""
    _srv, port = gateway
    _Gateway.status = 403
    _Gateway.content_type = "text/html"
    _Gateway.body = b"<html>forbidden</html>"
    engine, Session = isolated_db
    _add_channel(
        Session, name="中转", type="hermes", config={"webhook_url": _hermes_url(port)}
    )
    n = _push(Session)
    assert n.push_status == "failed"
    assert "渠道凭证被拒绝" in (n.push_error or "")
    assert "状态码 403" in (n.push_error or "")


def test_json_decode_error_classified_before_value_error():
    """`JSONDecodeError` 是 ValueError 子类: 分类必须落在 bad_response。"""
    try:
        json.loads("<html>not json</html>")
    except ValueError as exc:
        assert notify_errors.classify(exc) == "bad_response"
    # 真正的配置类 ValueError 仍然是 config_invalid(改动不能把两者混在一起)。
    assert notify_errors.classify(ValueError("缺少 webhook_url")) == "config_invalid"


def test_unknown_custom_channel_type_is_failure_not_success():
    """未知渠道类型: 旧实现 warning 后当成功 → 现在必须抛(否则"没发"写 sent)。"""
    import asyncio

    with pytest.raises(ValueError):
        asyncio.run(NotifierManager()._send_custom("carrier_pigeon", {}, "t", "c"))


def test_historic_rows_are_sanitized_on_read_and_never_mutated(isolated_db):
    """历史行(修复前写入的泄漏文本): 出口安全, 数据库原样保留。"""
    engine, Session = isolated_db
    leaky_error = (
        f"hermes 发送失败: post http://gw.internal/hooks?token={WECOM_KEY} "
        f"-> 500 (secret={HERMES_SECRET})"
    )
    leaky_channels = [
        {
            "id": 3,
            "name": "中转",
            "type": "hermes",
            "status": "failed",
            "error": f"token={HISTORIC_TOKEN}",
            "delivered_at": "",
            # 历史里可能混进任意上下文: 出口必须丢掉这些键
            "raw_exception": f"http://gw.internal/?token={WECOM_KEY}",
            "debug_payload": {"Authorization": f"Bearer {HISTORIC_TOKEN}"},
        }
    ]
    db = Session()
    try:
        n = Notification(
            user_id="1",
            category="system",
            level="error",
            title="历史通知",
            body="正文",
            push_status="failed",
            push_error=leaky_error,
            push_channels=leaky_channels,
        )
        db.add(n)
        db.commit()
        nid = int(n.id)
    finally:
        db.close()

    from src.web.api.notifications import _to_out, get_notification_status

    db = Session()
    try:
        row = db.query(Notification).filter(Notification.id == nid).first()
        out = _to_out(row)
    finally:
        db.close()

    for text in (out.push_error, json.dumps(out.push_channels, ensure_ascii=False)):
        for leak in (WECOM_KEY, HERMES_SECRET, HISTORIC_TOKEN, "gw.internal", "http://"):
            assert leak not in text, f"历史数据在出口泄漏: {leak!r} in {text!r}"
    assert "失败（" in out.push_error and "参考号: ERR-" in out.push_error

    # 未白名单的键被丢弃(不让历史里的任意上下文穿到前端)。
    assert out.push_channels and "raw_exception" not in out.push_channels[0]
    assert "debug_payload" not in out.push_channels[0]

    # /status 端点同口径
    db = Session()
    try:
        from src.web.models import User

        user = db.query(User).filter(User.id == "1").first()
        if user is None:
            user = User(
                id="1", username="hist", password_hash="x", role="owner", is_active=True
            )
            db.add(user)
            db.commit()
        status = get_notification_status(nid, db=db, user=user)
    finally:
        db.close()
    assert WECOM_KEY not in json.dumps(status, ensure_ascii=False, default=str)
    assert HISTORIC_TOKEN not in json.dumps(status, ensure_ascii=False, default=str)

    # **数据库未被原地清洗**: 原值(含泄漏文本)一个字节都没动。
    db = Session()
    try:
        raw = db.query(Notification).filter(Notification.id == nid).first()
        assert raw.push_error == leaky_error
        assert raw.push_channels == leaky_channels
    finally:
        db.close()


def test_new_failures_still_store_only_bounded_text(isolated_db, gateway):
    """新写入的失败行仍然只有「类别 + 关联号」(不因本次改动回退)。"""
    _srv, port = gateway
    _Gateway.status = 500
    _Gateway.content_type = "text/html"
    _Gateway.body = b"<html>boom</html>"
    engine, Session = isolated_db
    _add_channel(
        Session, name="中转", type="hermes", config={"webhook_url": _hermes_url(port)}
    )
    n = _push(Session)
    assert n.push_status == "failed"
    assert "失败（" in (n.push_error or "")
    for leak in (WECOM_KEY, "127.0.0.1", str(port), "http://", "boom"):
        assert leak not in (n.push_error or ""), f"泄漏 {leak!r}"
