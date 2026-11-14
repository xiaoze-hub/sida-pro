# -*- coding: utf-8 -*-
"""第三方库日志 & 微信绑定回执不得把凭据写进日志(2026-11-12 复审六)。

## 复现的真实泄漏

`apprise` 自己会打 DEBUG 正文, 里面有渠道凭据, 而 `apprise` **不在**
`_TransportNoiseFilter` 的噪音名单里 → 这些行会原样进 DB 日志表(`log_entries`):

    apprise|DEBUG|Bark Payload: {'device_key': '<device_key>', ...}
    apprise|DEBUG|Bark POST URL: http://host/push (cert_verify=True, ...)
    apprise|ERROR|Unparseable Telegram URL tgram://B<...>Z/123/

`src/web/api/wechat_bind.py` 还有一处更直接: 扫码确认但凭证不完整时
`logger.error(f"...凭证不完整: {result}")` —— `result` 里就是**完整 bot_token**。

## 本文件验证的口径

- **不是靠级别过滤**: WARNING/ERROR 正文同样被替换(级别过滤会漏, 也会在第三方升版
  后静默失效); 记录本身保留(级别/来源/时间), 正文只剩
  `[apprise] suppressed level=WARNING category=apprise ref=ERR-XXXXXXXX`;
- 异常堆栈(`exc_info`)与 `%s` 参数也一起清掉 —— 那是最容易被忽略的泄漏面;
- 关联号可用 `third_party_log_scope` 绑定, 与 `push_error` 里的参考号对齐;
- **不误伤**其它 logger(业务日志正文照旧);
- 微信绑定: 日志里没有 bot_token / base_url, 成功回执也不带 token。

全程只用真实 `apprise` 库打日志 + 本地已关闭端口(真实 ConnectError), 不访问外网。
"""

from __future__ import annotations

import asyncio
import logging
import socket

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import src.web.models  # noqa: F401  注册 ORM 模型
from src.core.notifier import NotifierManager
from src.core.third_party_log_guard import (
    install_third_party_log_guard,
    third_party_log_scope,
)
from src.web.database import Base
from src.web.models import NotifyChannel, User

# 非占位形态的假凭据: 任何一处落进日志/DB 都会被下面的断言抓到。
BARK_DEVICE_KEY = "barkdev_9f2b7c41de8a4c3bb6f0aa1d5e77"
TG_BOT_TOKEN = "7001234567:AAFakeTelegramTokenLive9xQ2"
ILINK_BOT_TOKEN = "ilink_bot_7c41de8a4c3bb6f0aa1d5e77fake"
ILINK_BASE = "https://ilinkai.weixin.qq.com"
DB_PASSWORD = "S1da-Guard-Pa55"


def _closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = int(s.getsockname()[1])
    s.close()
    return port


class _CaptureHandler(logging.Handler):
    """模拟 DBLogHandler / 控制台: 存的是 `record.getMessage()`(下游看到的样子)。"""

    def __init__(self):
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)

    def texts(self) -> list[str]:
        return [rec.getMessage() for rec in self.records]


@pytest.fixture()
def captured():
    install_third_party_log_guard()
    handler = _CaptureHandler()
    root = logging.getLogger()
    old_level = root.level
    root.setLevel(logging.DEBUG)
    root.addHandler(handler)
    try:
        yield handler
    finally:
        root.removeHandler(handler)
        root.setLevel(old_level)


def _leaks(text: str) -> list[str]:
    """所有不该出现在日志里的东西(凭据 + URL + 回包正文)。"""
    cands = [
        BARK_DEVICE_KEY,
        TG_BOT_TOKEN,
        "127.0.0.1",
        "http://",
        "https://",
        "device_key",
        "Bark Payload",
        "Bark POST URL",
        "Unparseable",
        "Authorization",
    ]
    return [c for c in cands if c in text]


def test_apprise_payload_and_url_never_reach_any_handler(captured):
    """真实 apprise: bark device_key 出现在 DEBUG payload 里 → 现在只剩安全行。"""
    port = _closed_port()
    mgr = NotifierManager()
    assert mgr._ap.add(f"bark://127.0.0.1:{port}/{BARK_DEVICE_KEY}/")
    mgr._channel_count = 1

    result = asyncio.run(mgr.notify_with_result("标题", "正文"))
    assert result["success"] is False, "连不上(payload 泄漏时段也一样)必须报失败"

    texts = captured.texts()
    apprise_lines = [
        t for t in texts if t.startswith("[apprise] suppressed")
    ]
    assert apprise_lines, "apprise 的记录应被整体换成安全行(记录本身不能消失)"
    for text in texts:
        leaks = _leaks(text)
        assert not leaks, f"日志泄漏 {leaks} in {text!r}"
    # 记录级别/来源保留: 排障仍然能知道"apprise 在 WARNING 说了句什么"
    levels = {
        rec.levelname for rec in captured.records if rec.name.startswith("apprise")
    }
    assert levels, "级别信息被抹掉了, 排障会失去唯一线索"
    assert "ref=" in apprise_lines[0]


def test_invalid_telegram_token_is_not_echoed(captured):
    """非法/被拒 token: apprise 的 ERROR 行原本会带 `tgram://B...Z/chat`。"""
    mgr = NotifierManager()
    # 非法形态 → apprise 立即 ERROR "Unparseable Telegram URL tgram://..."(不发网络请求)
    mgr._ap.add(f"tgram://{TG_BOT_TOKEN}/-1002233445566/")
    mgr._channel_count = 1
    assert captured.texts(), "apprise 至少打了一行(否则本用例什么也没验证)"
    for text in captured.texts():
        assert TG_BOT_TOKEN not in text
        assert "tgram://" not in text


def test_exception_traceback_and_args_are_stripped(captured):
    """`exc_info` / `%s` 参数同样不能穿出去(httpx 异常文本里就是完整 URL)。"""
    log = logging.getLogger("apprise.plugins.NotifyBark")
    try:
        raise RuntimeError(f"POST http://gw.local/push?device_key={BARK_DEVICE_KEY} failed")
    except RuntimeError:
        with third_party_log_scope(ref="ERR-DEADBEEF", category="bark"):
            log.exception("send failed url=%s", f"http://gw.local/?k={BARK_DEVICE_KEY}")

    record = captured.records[-1]
    assert BARK_DEVICE_KEY not in record.getMessage()
    assert record.exc_info is None and record.exc_text is None
    assert record.stack_info is None
    assert record.args == ()
    assert "ref=ERR-DEADBEEF" in record.getMessage()
    assert "category=bark" in record.getMessage()
    assert "level=ERROR" in record.getMessage()


def test_httpx_and_urllib3_url_lines_are_replaced(captured):
    """自定义渠道走 httpx: `HTTP Request: POST ...?key=<webhook key>` 同样的坑。"""
    for name in (
        "httpx",
        "httpcore.connection",
        "urllib3.connectionpool",
    ):
        logging.getLogger(name).info(
            "HTTP Request: POST https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=%s",
            "6c1f0a2e-84b7-4d9a-9f9e-5a1b7d3c2e11",
        )
    texts = [t for t in captured.texts() if "suppressed" in t]
    assert len(texts) == 3
    for text in texts:
        assert "qyapi" not in text and "key=" not in text
        assert text.split()[0] in ("[httpx]", "[httpcore]", "[urllib3]")


def test_other_loggers_are_not_touched(captured):
    """不误伤业务日志: 只有名单内的第三方 logger 被替换。"""
    logging.getLogger("src.core.notify_center").info("通知中心 正常落库 id=%s", 12)
    text = captured.texts()[-1]
    assert text == "通知中心 正常落库 id=12"


# ── 微信扫码绑定: 完整结果不得入日志 ───────────────────────────────


@pytest.fixture()
def bind_db(tmp_path, monkeypatch):
    db_file = tmp_path / f"wechat_bind_{DB_PASSWORD}.db"
    engine = create_engine(
        f"sqlite:///{db_file}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db = Session()
    user = User(
        id="u-1",
        username="binder",
        password_hash="x",
        role="owner",
        is_active=True,
    )
    db.add(user)
    db.commit()
    try:
        yield db, user
    finally:
        db.close()
        engine.dispose()


def _bind_result(**over):
    result = {
        "status": "success",
        "account_id": "bot_88f2c1",
        "token": ILINK_BOT_TOKEN,
        "base_url": ILINK_BASE,
        "user_id": "peer_9a1b@im.wechat",
    }
    result.update(over)
    return result


def test_wechat_bind_success_keeps_token_out_of_logs_and_response(
    bind_db, captured, monkeypatch
):
    """扫码确认成功: token 只进 DB, 不进日志/HTTP 响应。"""
    from src.core import wechat_ilink
    from src.web.api import wechat_bind

    db, user = bind_db
    monkeypatch.setattr(
        wechat_ilink, "poll_qr", lambda qrcode: _async_value(_bind_result())
    )

    payload = asyncio.run(wechat_bind.bind_status(qrcode="qr-1", db=db, user=user))
    assert payload["status"] == "success"
    assert ILINK_BOT_TOKEN not in str(payload)

    row = db.query(NotifyChannel).filter(NotifyChannel.type == "wechat_ilink").first()
    assert row is not None and row.config["token"] == ILINK_BOT_TOKEN

    log_text = "\n".join(captured.texts())
    assert ILINK_BOT_TOKEN not in log_text
    assert ILINK_BASE not in log_text
    assert "peer_9a1b" not in log_text


def test_wechat_bind_incomplete_credentials_logs_field_names_only(
    bind_db, captured, monkeypatch
):
    """旧实现把整个 `result`(含完整 bot_token)打进 ERROR —— 现在只报缺哪个字段。"""
    from src.core import wechat_ilink
    from src.web.api import wechat_bind

    db, user = bind_db
    monkeypatch.setattr(
        wechat_ilink,
        "poll_qr",
        lambda qrcode: _async_value(_bind_result(account_id="")),
    )

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(wechat_bind.bind_status(qrcode="qr-2", db=db, user=user))
    assert excinfo.value.status_code == 502

    log_text = "\n".join(captured.texts())
    assert ILINK_BOT_TOKEN not in log_text, "token 进了日志 = 凭据落到日志表"
    assert "account_id" in log_text, "至少要能看出缺哪个字段"
    assert all(rec.exc_info is None for rec in captured.records)


def test_wechat_bind_rejects_untrusted_base_url(bind_db, captured, monkeypatch):
    """回执里 base_url 指向元数据/内网: 拒绝绑定, 不落库。"""
    from src.core import wechat_ilink
    from src.web.api import wechat_bind

    db, user = bind_db
    monkeypatch.setattr(
        wechat_ilink,
        "poll_qr",
        lambda qrcode: _async_value(_bind_result(base_url="http://169.254.169.254")),
    )

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(wechat_bind.bind_status(qrcode="qr-3", db=db, user=user))
    assert excinfo.value.status_code == 502
    assert "169.254" not in str(excinfo.value.detail)
    assert db.query(NotifyChannel).filter(NotifyChannel.type == "wechat_ilink").count() == 0


def test_wechat_bind_poll_failure_logs_no_exception_text(bind_db, captured, monkeypatch):
    """轮询异常(iLink 文本带接口地址/令牌)只落关联号 + 类型。"""
    from src.core import wechat_ilink
    from src.web.api import wechat_bind

    db, user = bind_db

    async def boom(qrcode):
        raise RuntimeError(f"https://ilinkai.weixin.qq.com/sendmessage token={ILINK_BOT_TOKEN}")

    monkeypatch.setattr(wechat_ilink, "poll_qr", boom)
    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(wechat_bind.bind_status(qrcode="qr-4", db=db, user=user))
    assert excinfo.value.status_code == 503

    log_text = "\n".join(captured.texts())
    assert ILINK_BOT_TOKEN not in log_text
    assert "ilinkai.weixin.qq.com" not in log_text
    assert "ERR-" in log_text


async def _async_value(value):
    return value
