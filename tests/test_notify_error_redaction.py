# -*- coding: utf-8 -*-
"""通知外发异常不得进入 push_status/API/UI/日志(2026-11-12 复审五)。

上一轮只把 **日志** 改成了白名单(`log_failure`), 但同一段异常文本还在往
`notifications.push_status/push_error/push_channels` 里写 `str(e)[:400]` —— 那是
**落库并对外**的字段:

- `GET /api/notifications` / `GET /api/notifications/{id}/status` 原样返回;
- 前端通知中心直接把 `push_error` 与每渠道 `error` 渲染出来(见
  frontend/src/pages/Notifications.tsx);
- 页面截图、工单导出就离开服务端。

而异常文本里天然有: 代理串里的账号密码、webhook URL 的 key/token、provider 回包。
另一个同样要命的问题是 **语义**: 读渠道表失败(库挂了/权限不对)被写成了
`skipped` + 「未配置通知渠道」—— 用户以为没配渠道, 实际是库故障, 真实失败被藏了。

本文件的做法(全部是**真实失败**, 不是 mock 出来的异常):
1. tmp_path 下真实 SQLite + 真实 `notify_channels` 行, `config` 里放的是**非占位**
   形态的假 key/token/代理账号密码, 指向 `127.0.0.1` 上一个**已关闭端口** —— 用
   httpx 真实的 `ConnectError` 打出来, 不连任何外网/生产;
2. 读渠道表失败用真实的 `DROP TABLE notify_channels` 触发 `OperationalError`;
3. 断言三处出口(push_error / push_channels[].error / 服务端日志)都只有
   「固定类别 + 关联号」, 且没有 token/URL/代理密码/SQL/DSN/堆栈;
4. 断言数据库故障是 `failed`(不是未配置渠道), 真没配渠道时仍然是 `skipped`。
"""

from __future__ import annotations

import logging
import socket
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

import src.web.models  # noqa: F401  注册 ORM 模型
from src.core import notify_center
from src.core import notify_errors
from src.core.notifier import NotifierManager
from src.web.database import Base
from src.web.models import Notification, NotifyChannel

# 非占位形态的假凭据: 任何一处落在 push_error/日志里都会被下面的断言抓到。
WECOM_KEY = "6c1f0a2e-84b7-4d9a-9f9e-5a1b7d3c2e11"
TG_TOKEN = "7001234567:AAFakeTelegramTokenLive9xQ2"
TG_CHAT = "-1002233445566"
HERMES_SECRET = "hs_9f2b7c41de8a4c3bb6f0aa1d5e77"
PROXY_USER = "svcproxy"
PROXY_PASS = "Pr0xyPa55w0rd!"
PUSHPLUS_TOKEN = "pp_01f5d8c9a77b4e2f8c1d33aa"
DB_PASSWORD = "S1da-Guard-Pa55"


def _closed_port() -> int:
    """拿一个**刚刚关闭**的本地端口: connect 会立刻 ECONNREFUSED(真实失败)。"""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = int(s.getsockname()[1])
    s.close()
    return port


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    """临时 SQLite(不是仓库/生产库), notify_center.SessionLocal 指过去。"""
    db_file = tmp_path / f"notify_redaction_{DB_PASSWORD}.db"
    engine = create_engine(
        f"sqlite:///{db_file}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(notify_center, "SessionLocal", Session)
    assert str(engine.url) != "sqlite:////data/sida.db"
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


def _row(Session, nid: int) -> Notification:
    db = Session()
    try:
        return db.query(Notification).filter(Notification.id == nid).first()
    finally:
        db.close()


def _logged(caplog) -> str:
    return "\n".join(
        rec.getMessage() for rec in caplog.records if rec.name.startswith("src.")
    )


def _assert_public(text: str, *, action_label: str = "") -> None:
    """对外文案必须是「固定类别 + 关联号」, 且不含任何原文/凭据。"""
    assert "失败（" in text, f"缺少可读失败类别: {text!r}"
    assert "参考号: ERR-" in text, f"缺少关联号: {text!r}"
    if action_label:
        assert action_label in text, f"缺少类别文案 {action_label!r}: {text!r}"


def _assert_no_secret(text: str, port: int) -> None:
    for leak in (
        WECOM_KEY,
        TG_TOKEN,
        TG_CHAT,
        HERMES_SECRET,
        PROXY_USER,
        PROXY_PASS,
        PUSHPLUS_TOKEN,
        DB_PASSWORD,
        "127.0.0.1",
        f":{port}",
        "http://127.0.0.1",
        "https://",
        "token=",
        "secret=",
        "SELECT",
        "INSERT",
        "notify_channels",
        "sqlite:///",
        "Traceback",
        "parameters",
    ):
        assert leak not in text, f"泄漏: {leak!r} in {text!r}"


def test_real_network_failure_stays_public_in_db_api_and_log(
    isolated_db, caplog, monkeypatch
):
    """真实 ConnectError(本地已关闭端口): push_error/push_channels/API/日志都干净。"""
    engine, Session = isolated_db
    port = _closed_port()
    _add_channel(
        Session,
        name="中转",
        type="hermes",
        config={
            "webhook_url": f"http://127.0.0.1:{port}/hooks/sida?token={WECOM_KEY}",
            "secret": HERMES_SECRET,
        },
    )

    with caplog.at_level(logging.DEBUG):
        nid = notify_center.push_notification(
            "❌ 因子刷新 执行失败",
            "详情",
            category="strategy",
            level="error",
            user_id=None,
        )

    assert isinstance(nid, int) and nid > 0, "站内通知本身必须照常落库(可用性)"
    n = _row(Session, nid)
    assert n.push_status == "failed", "真实外发失败必须写 failed"
    _assert_public(n.push_error, action_label="网络连接失败")
    _assert_no_secret(n.push_error, port)

    channels = list(n.push_channels or [])
    assert channels and channels[0]["status"] == "failed"
    _assert_public(channels[0]["error"], action_label="网络连接失败")
    _assert_no_secret(channels[0]["error"], port)

    # API 出口: 通知列表/状态端点直接复用这个序列化函数。
    from src.web.api.notifications import _to_out

    out = _to_out(n)
    _assert_no_secret(out.push_error, port)
    _assert_no_secret(str(out.push_channels), port)

    log_text = _logged(caplog)
    assert "通知渠道外发失败" in log_text
    assert "ERR-" in log_text
    _assert_no_secret(log_text, port)
    assert all(rec.exc_info is None for rec in caplog.records), "仍带异常堆栈"
    # 关联号必须能把对外文案与服务端日志串起来。
    ref = n.push_error.split("参考号: ")[-1].strip()
    assert ref in log_text, "push_error 的关联号在日志里找不到, 排障会断链"


def test_telegram_proxy_failure_never_echoes_proxy_credentials(
    isolated_db, caplog, monkeypatch
):
    """代理账号密码 + bot token: 失败只报「代理不可用」, 不落 URL/凭据。"""
    engine, Session = isolated_db
    port = _closed_port()
    proxy = f"http://{PROXY_USER}:{PROXY_PASS}@127.0.0.1:{port}"
    _add_channel(
        Session,
        name="TG",
        type="telegram",
        config={"bot_token": TG_TOKEN, "chat_id": TG_CHAT, "proxy": proxy},
    )

    with caplog.at_level(logging.DEBUG):
        nid = notify_center.push_notification(
            "告警", "正文", category="system", level="error"
        )

    n = _row(Session, nid)
    assert n.push_status == "failed"
    _assert_public(n.push_error, action_label="代理不可用")
    _assert_no_secret(n.push_error, port)
    log_text = _logged(caplog)
    _assert_no_secret(log_text, port)
    # 日志侧用有限枚举键(中文文案在 push_error 里), 两边靠同一个关联号对齐。
    assert "category=proxy_failed" in log_text, "日志里要有可分类的类别(否则运维只能猜)"


def test_channel_table_failure_is_failed_not_unconfigured(isolated_db, caplog):
    """读渠道表真实失败(DROP TABLE): 必须是 failed + 数据库不可用, 不是 skipped。"""
    engine, Session = isolated_db
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE notify_channels"))

    with caplog.at_level(logging.DEBUG):
        nid = notify_center.push_notification(
            "❌ 盘中监控 执行失败", "详情", category="agent_run", level="error", user_id="7"
        )

    assert isinstance(nid, int) and nid > 0, "渠道读不出来也要先把站内通知落库"
    n = _row(Session, nid)
    assert n.push_status == "failed", "数据库故障不能被写成 skipped"
    assert "未配置通知渠道" not in (n.push_error or ""), "把库故障说成未配置渠道 = 谎报"
    _assert_public(n.push_error, action_label="数据库不可用")
    _assert_no_secret(n.push_error, _closed_port())

    log_text = _logged(caplog)
    assert "通知渠道读取失败" in log_text
    _assert_no_secret(log_text, 0)
    assert all(rec.exc_info is None for rec in caplog.records)


def test_no_channels_still_reports_skipped(isolated_db):
    """兼容行为: 表在但一条启用渠道都没有 → 仍是 skipped/未配置通知渠道。"""
    engine, Session = isolated_db
    nid = notify_center.push_notification(
        "ℹ️ 提示", "正文", category="system", level="info"
    )
    n = _row(Session, nid)
    assert n.push_status == "skipped"
    assert n.push_error == "未配置通知渠道"


def test_channel_impl_returning_raw_exception_text_is_still_redacted(
    isolated_db, monkeypatch
):
    """出口形态白名单: 上游若把 `str(exc)` 直接塞进回执, 也不能进 push_error。"""

    class LeakyManager:
        async def notify_with_result(self, title, content, images=None, **kw):
            return {
                "success": False,
                "error": f"wecom 发送失败: post http://qyapi?key={WECOM_KEY} -> 403",
                "channels": [
                    {"type": "wecom", "success": False, "error": f"token={WECOM_KEY}"}
                ],
            }

    def fake_build(*, user_id=None, channel_ids=None):
        records = [
            {"id": 1, "name": "企微", "type": "wecom", "status": "pending", "error": ""}
        ]
        return LeakyManager(), records, {"requested": None, "granted": [1], "denied": []}

    monkeypatch.setattr(notify_center, "_build_notifier_checked", fake_build)
    engine, Session = isolated_db

    nid = notify_center.push_notification(
        "告警", "正文", category="system", level="error"
    )
    n = _row(Session, nid)
    assert n.push_status == "failed"
    assert WECOM_KEY not in (n.push_error or "")
    assert "qyapi" not in (n.push_error or "")
    _assert_public(n.push_error)
    for ch in n.push_channels or []:
        assert WECOM_KEY not in str(ch), f"渠道回执泄漏: {ch}"
        _assert_public(ch["error"])


def test_success_path_keeps_sent_status(isolated_db, monkeypatch):
    """保留真实成功状态: 全渠道成功仍是 sent, 且 push_error 为空。"""

    class OkManager:
        async def notify_with_result(self, title, content, images=None, **kw):
            return {
                "success": True,
                "channels": [
                    {"type": "wecom", "success": True, "delivered_at": "2026-11-12T00:00:00+00:00"}
                ],
            }

    def fake_build(*, user_id=None, channel_ids=None):
        records = [
            {"id": 1, "name": "企微", "type": "wecom", "status": "pending", "error": ""}
        ]
        return OkManager(), records, {"requested": None, "granted": [1], "denied": []}

    monkeypatch.setattr(notify_center, "_build_notifier_checked", fake_build)
    engine, Session = isolated_db

    nid = notify_center.push_notification(
        "✅ 完成", "正文", category="system", level="success"
    )
    n = _row(Session, nid)
    assert n.push_status == "sent"
    assert (n.push_error or "") == ""
    assert (n.push_channels or [])[0]["status"] == "sent"


def test_runtime_error_inside_coroutine_is_failed_not_pending(isolated_db, monkeypatch):
    """协程内部抛的 RuntimeError 不能被当成「已在事件循环里」而漏成 pending。"""
    class BoomManager:
        async def notify_with_result(self, title, content, images=None, **kw):
            raise RuntimeError(f"wecom post failed: key={WECOM_KEY}")

    def fake_build(*, user_id=None, channel_ids=None):
        records = [
            {"id": 1, "name": "企微", "type": "wecom", "status": "pending", "error": ""}
        ]
        return BoomManager(), records, {"requested": None, "granted": [1], "denied": []}

    monkeypatch.setattr(notify_center, "_build_notifier_checked", fake_build)
    engine, Session = isolated_db

    nid = notify_center.push_notification(
        "告警", "正文", category="system", level="error"
    )
    n = _row(Session, nid)
    assert n.push_status == "failed", "真没发出去却写 pending = 静默失败"
    _assert_public(n.push_error)
    assert WECOM_KEY not in (n.push_error or "")
    assert not notify_center._BACKGROUND_TASKS, "不该给协程内部错误登记后台任务"


def test_categories_are_closed_and_values_are_bounded():
    """失败类别是闭合集合; 未知类别不能把传入值回显到对外文案。"""
    assert "db_unavailable" in notify_errors.CATEGORY_LABELS
    text = notify_errors.failure_text("totally-unknown", ref="ERR-ABCD1234")
    assert "totally-unknown" not in text
    assert "参考号: ERR-ABCD1234" in text

    # 内部枚举(静默时段)是允许的: 它本身就是有界标识, 不含凭据。
    assert notify_errors.public_failure_text("quiet_hours") == "quiet_hours"
    # 任何不像「类别 + 关联号」的文本都被兜底替换。
    replaced = notify_errors.public_failure_text("SSL: bad certificate for host x")
    assert "SSL" not in replaced and "失败（" in replaced


def test_notifier_manager_logs_are_whitelisted_not_raw(isolated_db, caplog):
    """NotifierManager 自身: 注册失败 → 白名单日志; 外发失败 → 固定类别文案。

    全程只连 `127.0.0.1` 上已关闭的端口(真实 ConnectError), 不碰任何外网。
    """
    import asyncio

    mgr = NotifierManager()
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(ValueError):
            mgr.add_channel("pushplus", {"token": ""})
        add_log = _logged(caplog)
    assert "通知渠道注册失败" in add_log
    assert "ERR-" in add_log
    # 旧实现是 `logger.error(f"注册通知渠道失败: {e}")` —— 这里必须没有原文。
    assert "PushPlus 需要 token" not in add_log
    assert all(rec.exc_info is None for rec in caplog.records)

    port = _closed_port()
    mgr2 = NotifierManager()
    mgr2.add_channel(
        "hermes",
        {
            "webhook_url": f"http://127.0.0.1:{port}/hook?token={WECOM_KEY}",
            "secret": HERMES_SECRET,
        },
    )
    caplog.clear()
    with caplog.at_level(logging.DEBUG):
        result = asyncio.run(mgr2.notify_with_result("标题", "正文"))

    assert result["success"] is False
    error = str(result.get("error") or "")
    _assert_public(error, action_label="网络连接失败")
    _assert_no_secret(error, port)
    log_text = _logged(caplog)
    _assert_no_secret(log_text, port)
    assert all(rec.exc_info is None for rec in caplog.records)
