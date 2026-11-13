# -*- coding: utf-8 -*-
"""通知渠道 SSRF 防护(2026-11-12 复审六)。

威胁: `notify_channels.config` 里的 `webhook_url` / `server_url` / `base_url` 是
**用户可写的任意 URL**, 而 member 也能建/改自己的渠道。不校验就等于给登录用户一个
"服务器替我去请求"的原语: 云元数据(169.254.169.254)、同机管理面(127.0.0.1:8080)、
内网主机(10.x/192.168.x)全都在射程内。

本文件验证:

1. 危险目标在**配置期**就被拒(`blocked_target`), 且错误文案里没有 URL/主机名;
2. 公网目标被**固定到已校验的 IP**(同一请求只解析一次) —— 这是 DNS rebinding 的
   可靠解法; 服务端仍看到原始 Host(虚拟主机/证书不受影响);
3. 运维放行开关(`SIDA_NOTIFY_PRIVATE_HOSTS`)能放行**指定的**内网/回环目标,
   不等于放开全部内网;
4. 多 A 记录里混一条内网地址 → 整体拒绝(夹带不放行);
5. 不跟随重定向(302 → 内网是经典绕过)。

全程只连 `127.0.0.1` 上的**临时本地 HTTP 服务**(mock 外部传输), 不访问任何真实
渠道或生产环境。
"""

from __future__ import annotations

import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from src.core import net_guard
from src.core.net_guard import (
    TargetBlocked,
    guarded_request,
    validate_target,
)
from src.core.notifier import NotifierManager

# 非占位形态的假凭据(用于确认"拒绝文案里不会回显目标")
FAKE_DEVICE_KEY = "barkdev_9f2b7c41de8a4c3bb6f0aa1d5e77"
FAKE_WECOM_KEY = str(__import__("uuid").uuid4())


def _closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = int(s.getsockname()[1])
    s.close()
    return port


class _Recorder(BaseHTTPRequestHandler):
    """记录收到的请求头/路径, 回一个 JSON 确认。"""

    received: list[dict] = []
    status = 200
    body = b'{"status": "delivered"}'

    def do_POST(self):  # noqa: N802 - http.server 约定
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        type(self).received.append(
            {"path": self.path, "host": self.headers.get("Host") or ""}
        )
        payload = type(self).body
        self.send_response(type(self).status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):  # noqa: N802
        type(self).received.append(
            {"path": self.path, "host": self.headers.get("Host") or ""}
        )
        self.send_response(302)
        self.send_header("Location", "http://169.254.169.254/latest/meta-data/")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):  # 静默: 不要把访问行打到测试输出里
        return


@pytest.fixture()
def local_server():
    _Recorder.received = []
    _Recorder.status = 200
    _Recorder.body = b'{"status": "delivered"}'
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Recorder)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield srv, int(srv.server_address[1])
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=5)


def _assert_no_target_in_text(text: str, *needles: str) -> None:
    for needle in needles:
        assert needle not in text, f"文案泄漏目标信息: {needle!r} in {text!r}"


@pytest.mark.parametrize(
    "url,reason",
    [
        ("http://169.254.169.254/latest/meta-data/", "link_local"),
        ("http://127.0.0.1:8080/api/admin", "loopback"),
        ("http://[::1]:8080/", "loopback"),
        ("http://10.32.5.9/hook", "private"),
        ("http://192.168.1.20:9000/push", "private"),
        ("http://172.16.4.4/", "private"),
        ("http://0.0.0.0:8000/", "unspecified"),
        ("http://metadata.google.internal/computeMetadata/v1/", "metadata_host"),
        ("file:///etc/passwd", "bad_scheme"),
        ("gopher://127.0.0.1:6379/_INFO", "bad_scheme"),
    ],
)
def test_dangerous_targets_are_blocked_at_config_time(monkeypatch, url, reason):
    """内网/回环/链路本地(元数据)/非法协议: 配渠道时就拒, 且不回显目标。"""
    monkeypatch.delenv(net_guard.ENV_ALLOW_PRIVATE_HOSTS, raising=False)
    with pytest.raises(TargetBlocked) as excinfo:
        validate_target(url)
    assert excinfo.value.reason == reason
    text = str(excinfo.value)
    _assert_no_target_in_text(text, "169.254", "127.0.0.1", "10.32", "192.168", "::1")
    assert "目标地址被拒绝" in text


def test_ipv4_mapped_ipv6_loopback_is_not_a_bypass(monkeypatch):
    """`::ffff:127.0.0.1` 必须按 IPv4 判: 字符串前缀判断会漏掉它。"""
    monkeypatch.delenv(net_guard.ENV_ALLOW_PRIVATE_HOSTS, raising=False)
    with pytest.raises(TargetBlocked) as excinfo:
        validate_target("http://[::ffff:127.0.0.1]:8080/")
    assert excinfo.value.reason == "loopback"


def test_operator_allowlist_unlocks_only_the_named_target(monkeypatch):
    """放行是按目标生效的: 放行 127.0.0.1 不等于放行 10.x。"""
    monkeypatch.setenv(net_guard.ENV_ALLOW_PRIVATE_HOSTS, "127.0.0.1")
    target = validate_target("http://127.0.0.1:8099/hook")
    assert target.allowlisted is True
    with pytest.raises(TargetBlocked):
        validate_target("http://10.32.5.9/hook")


def test_cidr_allowlist(monkeypatch):
    monkeypatch.setenv(net_guard.ENV_ALLOW_PRIVATE_HOSTS, "192.168.1.0/24")
    assert validate_target("http://192.168.1.44:9000/push").allowlisted is True
    with pytest.raises(TargetBlocked):
        validate_target("http://192.168.2.44:9000/push")


def test_pinned_url_keeps_host_header_and_sni(monkeypatch):
    """https 目标: 连接用 IP, Host 头与 TLS SNI 仍是原域名(否则证书/路由全废)。"""
    monkeypatch.setattr(
        net_guard,
        "_resolve",
        lambda host, port: ["93.184.216.34"],  # 文档保留段, 不产生任何连接
    )
    target = validate_target("https://notify.example.com:8443/hooks/sida?x=1")
    assert target.ip == "93.184.216.34"
    assert target.pinned_url() == "https://93.184.216.34:8443/hooks/sida?x=1"
    assert target.host_header == "notify.example.com:8443"
    assert target.extensions == {"sni_hostname": "notify.example.com"}

    default_port = validate_target("https://notify.example.com/hooks/sida")
    assert default_port.pinned_url() == "https://93.184.216.34/hooks/sida"
    assert default_port.host_header == "notify.example.com"


def test_mixed_a_record_with_private_ip_is_rejected(monkeypatch):
    """多 A 记录夹一条内网地址 → 整体拒绝(不能"挑安全那条就连")。"""
    monkeypatch.delenv(net_guard.ENV_ALLOW_PRIVATE_HOSTS, raising=False)
    monkeypatch.setattr(
        net_guard,
        "_resolve",
        lambda host, port: ["93.184.216.34", "10.0.0.7"],
    )
    with pytest.raises(TargetBlocked) as excinfo:
        validate_target("https://hook.example.com/x")
    assert excinfo.value.reason == "private"


def test_guarded_request_pins_ip_and_resolves_exactly_once(monkeypatch, local_server):
    """DNS rebinding 的实际防线: **同一请求只解析一次**, 连接的就是校验过的 IP。

    这里把 `hook.test` 解析到 127.0.0.1(并放行), 请求 URL 用的是域名。
    断言: ①本地服务真的收到了请求 ②Host 头是原域名 ③getaddrinfo 只被调了 1 次
    (httpx/httpcore 拿到的是 IP 字面量, 不会再解析一次)。
    """
    srv, port = local_server
    monkeypatch.setenv(net_guard.ENV_ALLOW_PRIVATE_HOSTS, "hook.test")
    calls: list[tuple[str, int]] = []
    real_getaddrinfo = socket.getaddrinfo

    def fake_getaddrinfo(host, port_, *args, **kwargs):
        if str(host) == "hook.test":
            calls.append((str(host), int(port_)))
            return [
                (
                    socket.AF_INET,
                    socket.SOCK_STREAM,
                    6,
                    "",
                    ("127.0.0.1", port_),
                )
            ]
        return real_getaddrinfo(host, port_, *args, **kwargs)

    monkeypatch.setattr(net_guard.socket, "getaddrinfo", fake_getaddrinfo)

    import asyncio

    import httpx

    async def _run():
        async with httpx.AsyncClient(timeout=10) as client:
            return await guarded_request(
                client,
                "POST",
                f"http://hook.test:{port}/hooks/sida?token={FAKE_WECOM_KEY}",
                json_body={"hello": "world"},
            )

    resp = asyncio.run(_run())
    assert resp.status_code == 200
    assert len(calls) == 1, "同一请求内解析了多次 → 校验用的 IP 与连接用的 IP 可被换掉"
    assert _Recorder.received == [
        {"path": f"/hooks/sida?token={FAKE_WECOM_KEY}", "host": f"hook.test:{port}"}
    ]


def test_redirect_is_not_followed(monkeypatch, local_server):
    """302 → 内网: 不许跟随(httpx 默认不跟, 这里把默认写死并锁住)。"""
    srv, port = local_server
    monkeypatch.setenv(net_guard.ENV_ALLOW_PRIVATE_HOSTS, "127.0.0.1")

    import asyncio

    import httpx

    async def _run():
        async with httpx.AsyncClient(timeout=10) as client:
            return await guarded_request(client, "GET", f"http://127.0.0.1:{port}/jump")

    resp = asyncio.run(_run())
    assert resp.status_code == 302
    # 只发生了一次请求(没有去跟 Location), 且没有对元数据地址产生任何请求。
    assert len(_Recorder.received) == 1


def test_hermes_channel_with_metadata_url_is_rejected_at_registration(monkeypatch):
    """`add_channel` 是配置校验入口: 指向元数据的 hermes 渠道建不起来。"""
    monkeypatch.delenv(net_guard.ENV_ALLOW_PRIVATE_HOSTS, raising=False)
    mgr = NotifierManager()
    with pytest.raises(TargetBlocked):
        mgr.add_channel(
            "hermes",
            {"webhook_url": f"http://169.254.169.254/latest/meta-data/?k={FAKE_WECOM_KEY}"},
        )


def test_bark_custom_server_is_rejected_when_private(monkeypatch):
    """自建 Bark 指向内网: 配置期拒绝(文案不含地址)。"""
    monkeypatch.delenv(net_guard.ENV_ALLOW_PRIVATE_HOSTS, raising=False)
    mgr = NotifierManager()
    with pytest.raises(TargetBlocked) as excinfo:
        mgr.add_channel(
            "bark",
            {"device_key": FAKE_DEVICE_KEY, "server_url": "http://192.168.1.9:8080"},
        )
    _assert_no_target_in_text(str(excinfo.value), "192.168", "8080")


def test_telegram_proxy_to_metadata_is_blocked_but_local_proxy_still_allowed(
    monkeypatch,
):
    """代理字段: 拦元数据段, 但**不**拦 127.0.0.1:7890 这类本机代理(合法常态)。"""
    import asyncio

    monkeypatch.delenv(net_guard.ENV_ALLOW_PRIVATE_HOSTS, raising=False)
    mgr = NotifierManager()
    mgr.add_channel(
        "telegram",
        {
            "bot_token": "7001234567:AAFakeTelegramTokenLive9xQ2",
            "chat_id": "1",
            "proxy": "http://169.254.169.254:80",
        },
    )
    result = asyncio.run(mgr.notify_with_result("标题", "正文"))
    assert result["success"] is False
    assert "目标地址不被允许" in str(result.get("error") or "")

    # 本机代理只做"元数据段"判定: 不会因为 127.0.0.1 被拒(会因连不上而失败, 但不是被拦)
    mgr2 = NotifierManager()
    mgr2.add_channel(
        "telegram",
        {
            "bot_token": "7001234567:AAFakeTelegramTokenLive9xQ2",
            "chat_id": "1",
            "proxy": f"http://127.0.0.1:{_closed_port()}",
        },
    )
    result2 = asyncio.run(mgr2.notify_with_result("标题", "正文"))
    assert result2["success"] is False
    assert "目标地址不被允许" not in str(result2.get("error") or "")


def test_ilink_base_url_host_whitelist(monkeypatch):
    """iLink `base_url` 可被 PUT 改写 → 只允许腾讯域名(或运维显式追加)。"""
    from src.core.notifier import assert_ilink_base_url

    assert (
        assert_ilink_base_url("https://ilinkai.weixin.qq.com")
        == "https://ilinkai.weixin.qq.com"
    )
    assert assert_ilink_base_url("")  # 空 → 默认官方域名
    with pytest.raises(TargetBlocked) as excinfo:
        assert_ilink_base_url("http://169.254.169.254/")
    assert excinfo.value.reason == "host_not_allowed"

    monkeypatch.setenv("SIDA_NOTIFY_ILINK_HOSTS", "ilink.test.local")
    assert assert_ilink_base_url("https://ilink.test.local") == "https://ilink.test.local"


def test_channels_api_rejects_blocked_config_without_echoing_target(monkeypatch):
    """设置页拿到的是「固定类别 + 关联号」, 不是用户填的 URL。"""
    from fastapi import HTTPException

    from src.web.api.channels import _validate_channel

    monkeypatch.delenv(net_guard.ENV_ALLOW_PRIVATE_HOSTS, raising=False)
    with pytest.raises(HTTPException) as excinfo:
        _validate_channel(
            "hermes", {"webhook_url": f"http://127.0.0.1:9/hook?token={FAKE_WECOM_KEY}"}
        )
    assert excinfo.value.status_code == 400
    detail = str(excinfo.value.detail)
    assert "目标地址不被允许" in detail and "参考号: ERR-" in detail
    _assert_no_target_in_text(detail, "127.0.0.1", FAKE_WECOM_KEY, "http://")


def test_guard_module_has_no_wildcard_default(monkeypatch):
    """默认必须是否决式的: 没配放行表时不放行任何内网目标。"""
    monkeypatch.delenv(net_guard.ENV_ALLOW_PRIVATE_HOSTS, raising=False)
    with pytest.raises(TargetBlocked):
        validate_target("http://127.0.0.1:22/")
