# -*- coding: utf-8 -*-
"""通知渠道的**兼容性**收口(2026-11-13): 五个已知的"防护伤到合法配置/归类错"。

上一轮(复审六)加了 SSRF pinning、第三方日志脱敏、失败文案白名单, 但留下五处会
真正伤到用户或让排障走错方向的坑:

1. **HTTPS + env 代理**: httpcore 1.0.x 的 CONNECT 隧道不读 `sni_hostname`, pinning 后
   SNI/证书按 IP 校验 → 必然握手失败。既不能放弃 pinning(SSRF), 也不能悄悄绕过运维的
   代理 → 明确拒绝并提示 `NO_PROXY`(本条验证: 拒绝可读 + `NO_PROXY` 命中时不拒绝)。
2. **URL userinfo 被静默吃掉**: `https://user:pass@host/` 保存成功、请求却不带凭证。
   现在转成 `Authorization: Basic`(不进 URL/日志), CR/LF 一律当非法 URL。
3. **DNS 解析失败归类错**: 旧口径报「渠道配置不完整或无效」/`network_unreachable`,
   把"域名解析不到"说成"配置坏了"。现在独立成 `dns_failed`; 配置期解析不到**不拦保存**
   (真正的安全决策在连接那一刻), 发送时如实报「目标域名解析失败」。
4. **历史错误参考号随机**: 同一条只读收敛的历史行每读一次换一个 ERR 号, 且那个号在
   服务端日志里根本不存在。现在由内容派生稳定号。
5. **socks 代理漏校验**: 旧代码 `startswith(("http://","https://"))` 让
   `socks5h://169.254.169.254:1080` 整条绕过, 且被归成「内部错误」。现在按 URL 解析
   校验, socks 支持但不许指元数据段。

全程只连 `127.0.0.1` 上的临时 HTTP 服务 / 已关闭端口, DNS 用 monkeypatch 固定,
不访问任何真实渠道、真实代理或生产环境。
"""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import json
import socket
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from src.core import net_guard
from src.core.net_guard import (
    ENV_ALLOW_PRIVATE_HOSTS,
    ProxyPinUnsupported,
    TargetBlocked,
    TargetUnresolvable,
    client_uses_proxy,
    guarded_request,
    validate_proxy_target,
    validate_target,
)
from src.core.notifier import NotifierManager
from src.core.notify_errors import classify, public_failure_text, public_saved_error
from src.web.api.notifications import _to_out
from src.web.models import Notification

FAKE_WEBHOOK_KEY = str(__import__("uuid").uuid4())
FAKE_BOT_TOKEN = "7001234567:AAFakeTelegramTokenLive9xQ2"
FAKE_USER = "sida"
FAKE_PASS = "p@ss:w/ord"


def _closed_port() -> int:
    """拿一个**没人监听**的本地端口(连接立即 ECONNREFUSED, 不发包出机器)。"""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = int(s.getsockname()[1])
    s.close()
    return port


class _Recorder(BaseHTTPRequestHandler):
    received: list[dict] = []
    body = b'{"status": "delivered"}'

    def do_POST(self):  # noqa: N802 - http.server 约定
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        type(self).received.append(
            {
                "path": self.path,
                "host": self.headers.get("Host") or "",
                "authorization": self.headers.get("Authorization") or "",
            }
        )
        payload = type(self).body
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        return


@pytest.fixture()
def local_server():
    _Recorder.received = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Recorder)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield srv, int(srv.server_address[1])
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=5)


@pytest.fixture(autouse=True)
def _clean_proxy_env(monkeypatch):
    """默认把代理/放行环境清干净: 用例自己需要什么就显式设什么。"""
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.lower(), raising=False)
    monkeypatch.delenv(ENV_ALLOW_PRIVATE_HOSTS, raising=False)


def _fixed_dns(monkeypatch, mapping: dict[str, str]):
    """把 `_resolve` 变成查表(不发真实 DNS); 未命中的主机照旧报解析失败。

    IP 字面量按生产实现直接返回(真实 `_resolve` 对字面量也不走 DNS)。
    """

    def fake_resolve(host, port):
        key = str(host).strip("[]").lower()
        try:
            return [str(ipaddress.ip_address(key))]
        except ValueError:
            pass
        if key in mapping:
            return [mapping[key]]
        raise TargetUnresolvable("测试: 主机不在固定表内")

    monkeypatch.setattr(net_guard, "_resolve", fake_resolve)


# --------------------------------------------------------------------------
# 2. URL userinfo: 不再静默丢失, 转成 Basic 认证(且不进 URL/repr/日志)
# --------------------------------------------------------------------------


def test_url_userinfo_is_sent_as_basic_auth(monkeypatch, local_server):
    srv, port = local_server
    _fixed_dns(monkeypatch, {"hook.test": "127.0.0.1"})
    monkeypatch.setenv(ENV_ALLOW_PRIVATE_HOSTS, "hook.test")

    expected = "Basic " + base64.b64encode(
        f"{FAKE_USER}:{FAKE_PASS}".encode()
    ).decode()
    quoted = "sida:p%40ss%3Aw%2Ford"  # p@ss:w/ord 的百分号编码
    url = f"http://{quoted}@hook.test:{port}/hook?token={FAKE_WEBHOOK_KEY}"

    target = validate_target(url)
    assert target.auth_header == expected
    # 凭据不许出现在可打印字段(日志/异常里随手 repr 也不漏)
    assert FAKE_PASS not in repr(target)
    assert "sida" not in repr(target)
    # pinning 后的 URL 里没有 userinfo(否则又会被当成 URL 的一部分发出去)
    assert "@" not in target.pinned_url()

    async def _run():
        async with httpx.AsyncClient(timeout=10) as client:
            return await guarded_request(client, "POST", url, json_body={"hello": 1})

    resp = asyncio.run(_run())
    assert resp.status_code == 200
    got = _Recorder.received[-1]
    assert got["authorization"] == expected
    assert got["host"] == f"hook.test:{port}"
    assert got["path"] == f"/hook?token={FAKE_WEBHOOK_KEY}"


def test_caller_supplied_authorization_wins(monkeypatch, local_server):
    """调用方自己给了 Authorization(如 HMAC 头之外的显式凭证)时不覆盖。"""
    srv, port = local_server
    _fixed_dns(monkeypatch, {"hook.test": "127.0.0.1"})
    monkeypatch.setenv(ENV_ALLOW_PRIVATE_HOSTS, "hook.test")

    async def _run():
        async with httpx.AsyncClient(timeout=10) as client:
            return await guarded_request(
                client,
                "POST",
                f"http://u:p@hook.test:{port}/hook",
                headers={"Authorization": "Bearer caller-token"},
                json_body={},
            )

    assert asyncio.run(_run()).status_code == 200
    assert _Recorder.received[-1]["authorization"] == "Bearer caller-token"


def test_userinfo_with_crlf_is_rejected_instead_of_header_injection():
    """userinfo 里的 CR/LF 会变成头注入 —— 归配置形态错误, 不做"清洗"。"""
    with pytest.raises(ValueError) as excinfo:
        validate_target("http://us%0d%0aX-Evil:1@example.com/hook")
    assert not isinstance(excinfo.value, TargetBlocked)
    assert "X-Evil" not in str(excinfo.value)
    assert classify(excinfo.value) == "config_invalid"


# --------------------------------------------------------------------------
# 3. DNS 解析失败: 独立类别, 且配置期不拦保存
# --------------------------------------------------------------------------


def test_dns_failure_is_classified_as_dns_failed():
    exc = TargetUnresolvable("getaddrinfo failed")
    assert classify(exc) == "dns_failed"
    text = public_failure_text("")  # 只是确保模块可用, 下面断言真文案
    assert isinstance(text, str)
    from src.core.notify_errors import failure_text

    msg = failure_text("dns_failed", action="hermes 通知发送", ref="ERR-1234ABCD")
    assert "目标域名解析失败" in msg and "配置不完整" not in msg


def test_config_time_dns_failure_does_not_block_saving_channel(monkeypatch):
    """解析不到的域名**不**让渠道存不进去(内网 DNS/域名未生效很常见)。

    安全边界不靠配置期: 发送时 `guarded_request` 会再校验, 解析到内网照样拒。
    """
    _fixed_dns(monkeypatch, {})  # 任何主机都解析失败
    mgr = NotifierManager()
    assert mgr.add_channel(
        "hermes", {"webhook_url": "https://notify.example.com/hook"}
    ) is True

    result = asyncio.run(mgr.notify_with_result("标题", "正文"))
    assert result["success"] is False
    error = str(result.get("error") or "")
    assert "目标域名解析失败" in error
    assert "配置不完整" not in error
    assert "参考号: ERR-" in error


def test_proxy_host_dns_failure_is_proxy_failed(monkeypatch):
    """代理域名解析不了说的是"代理不可用", 不是"渠道配置无效"。"""
    _fixed_dns(monkeypatch, {})  # 代理主机也解析不到
    mgr = NotifierManager()
    mgr.add_channel(
        "telegram",
        {
            "bot_token": FAKE_BOT_TOKEN,
            "chat_id": "1",
            "proxy": "http://proxy.not-resolvable.invalid:3128",
        },
    )
    result = asyncio.run(mgr.notify_with_result("标题", "正文"))
    assert result["success"] is False
    error = str(result.get("error") or "")
    assert "代理不可用" in error
    assert "目标域名解析失败" not in error


# --------------------------------------------------------------------------
# 4. 历史错误参考号: 同一行/同一文案每次读出来必须是同一个号
# --------------------------------------------------------------------------


def test_historical_failure_text_has_stable_ref():
    leaked = f"请求失败 https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={FAKE_WEBHOOK_KEY}"
    first = public_saved_error(leaked)
    second = public_saved_error(leaked)
    assert first == second, "同一段历史文案两次收敛得到不同参考号 → 前端刷新就换号"
    assert FAKE_WEBHOOK_KEY not in first
    assert "参考号: ERR-" in first

    assert public_failure_text(leaked) == public_failure_text(leaked)


def test_same_notification_row_reads_same_ref_twice():
    """API 出口(`_to_out`)对同一行两次序列化: 文案与参考号必须完全一致。"""
    row = Notification(
        id=7,
        category="system",
        level="error",
        title="推送失败",
        body="",
        link="",
        source="agent",
        trace_id="",
        push_status="failed",
        push_error=f"POST http://user:{FAKE_PASS}@hook.test/x?key={FAKE_WEBHOOK_KEY} 失败",
        push_channels=[
            {"type": "hermes", "error": f"token={FAKE_WEBHOOK_KEY}", "success": False}
        ],
        created_at=datetime(2026, 11, 12, 9, 0, 0),
    )
    first = _to_out(row)
    second = _to_out(row)
    assert first.push_error == second.push_error
    assert first.push_channels == second.push_channels
    blob = first.push_error + json.dumps(first.push_channels, ensure_ascii=False)
    assert "参考号: ERR-" in first.push_error
    for secret in (FAKE_WEBHOOK_KEY, FAKE_PASS, "hook.test"):
        assert secret not in blob


def test_benign_historic_reason_is_still_kept():
    """形态放宽那层没被稳定号改动影响: 人写的短原因原样保留。"""
    assert public_saved_error("网络超时") == "网络超时"
    assert public_saved_error("") == ""


# --------------------------------------------------------------------------
# 5. socks 代理: 不再绕过校验
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "proxy,reason",
    [
        ("socks5h://169.254.169.254:1080", "link_local"),
        ("socks5://169.254.169.254:1080", "link_local"),
        ("socks4://[::ffff:169.254.169.254]:1080", "link_local"),
        ("socks5://metadata.google.internal:1080", "metadata_host"),
        ("http://169.254.169.254:3128", "link_local"),
    ],
)
def test_socks_and_http_proxy_to_metadata_are_blocked(monkeypatch, proxy, reason):
    _fixed_dns(monkeypatch, {"metadata.google.internal": "169.254.169.254"})
    with pytest.raises(TargetBlocked) as excinfo:
        validate_proxy_target(proxy)
    assert excinfo.value.reason == reason
    assert "169.254" not in str(excinfo.value)


def test_local_socks_proxy_is_still_accepted(monkeypatch):
    """本机 clash(`socks5://127.0.0.1:7890`)是合法常态, 不许一刀切。"""
    _fixed_dns(monkeypatch, {"127.0.0.1": "127.0.0.1"})
    assert validate_proxy_target("socks5://127.0.0.1:7890") == "socks5://127.0.0.1:7890"
    assert validate_proxy_target("socks5h://127.0.0.1:7890")


def test_unsupported_proxy_scheme_is_a_config_error(monkeypatch):
    _fixed_dns(monkeypatch, {"127.0.0.1": "127.0.0.1"})
    with pytest.raises(ValueError) as excinfo:
        validate_proxy_target("ftp://127.0.0.1:21")
    assert classify(excinfo.value) == "config_invalid"


def test_socks_metadata_proxy_fails_as_blocked_not_internal(monkeypatch):
    """旧实现: socks 整条绕过校验 → 报「内部错误」; 现在必须是 blocked_target。"""
    _fixed_dns(monkeypatch, {"169.254.169.254": "169.254.169.254"})
    mgr = NotifierManager()
    mgr.add_channel(
        "telegram",
        {
            "bot_token": FAKE_BOT_TOKEN,
            "chat_id": "1",
            "proxy": "socks5h://169.254.169.254:1080",
        },
    )
    result = asyncio.run(mgr.notify_with_result("标题", "正文"))
    assert result["success"] is False
    error = str(result.get("error") or "")
    assert "目标地址不被允许" in error
    assert "内部错误" not in error


# --------------------------------------------------------------------------
# 1. HTTPS 目标经代理: 明确拒绝(不静默放弃 pinning, 也不静默绕过代理)
# --------------------------------------------------------------------------


def test_https_target_through_env_proxy_is_refused_explicitly(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", f"http://127.0.0.1:{_closed_port()}")
    _fixed_dns(monkeypatch, {"notify.example.com": "93.184.216.34"})

    async def _run():
        async with httpx.AsyncClient(timeout=5) as client:
            assert client_uses_proxy(client, "https://notify.example.com/hook") is True
            with pytest.raises(ProxyPinUnsupported) as excinfo:
                await guarded_request(
                    client, "POST", "https://notify.example.com/hook", json_body={}
                )
            return excinfo.value

    exc = asyncio.run(_run())
    text = str(exc)
    assert "NO_PROXY" in text
    # 明确"不支持", 而不是让用户以为只是暂时性网络故障(不能声称已兼容)。
    assert "不支持" in text
    # 有界文案: 不带目标/代理地址
    assert "notify.example.com" not in text
    assert "127.0.0.1" not in text
    assert classify(exc) == "pinned_proxy_unsupported"

    # 新类别也得通过出口形态白名单(否则会被二次收敛成「内部错误」)
    from src.core.notify_errors import failure_text

    label_msg = failure_text(
        "pinned_proxy_unsupported", action="hermes 通知发送", ref="ERR-1A2B3C4D"
    )
    assert public_failure_text(label_msg) == label_msg


def test_no_proxy_host_is_not_refused(monkeypatch):
    """`NO_PROXY` 命中该域名时 httpx 走直连 → 我们照常 pinning, 不该拒绝。"""
    closed = _closed_port()
    monkeypatch.setenv("HTTPS_PROXY", f"http://127.0.0.1:{_closed_port()}")
    monkeypatch.setenv("NO_PROXY", "notify.example.com")
    monkeypatch.setenv("no_proxy", "notify.example.com")
    _fixed_dns(monkeypatch, {"notify.example.com": "127.0.0.1"})
    monkeypatch.setenv(ENV_ALLOW_PRIVATE_HOSTS, "notify.example.com")

    async def _run():
        async with httpx.AsyncClient(timeout=5) as client:
            assert client_uses_proxy(client, "https://notify.example.com/hook") is False
            with pytest.raises(httpx.ConnectError):  # 直连到关闭端口? 不是拒绝
                await guarded_request(
                    client,
                    "POST",
                    f"https://notify.example.com:{closed}/hook",
                    json_body={},
                )

    asyncio.run(_run())


def test_http_target_through_proxy_is_still_allowed(monkeypatch):
    """明文 http 经转发代理不涉及 SNI: 依旧 pinning + 允许(不因代理一律拒绝)。"""
    closed = _closed_port()
    monkeypatch.setenv(ENV_ALLOW_PRIVATE_HOSTS, "hook.test")
    _fixed_dns(monkeypatch, {"hook.test": "127.0.0.1"})

    async def _run():
        async with httpx.AsyncClient(
            proxy=f"http://127.0.0.1:{closed}", timeout=5
        ) as client:
            assert client_uses_proxy(client, "http://hook.test/hook") is True
            with pytest.raises(httpx.ConnectError):  # 连代理失败, 而不是被拒绝
                await guarded_request(
                    client, "POST", "http://hook.test:8080/hook", json_body={}
                )

    asyncio.run(_run())


# --------------------------------------------------------------------------
# 6. TargetBlocked: 按 reason 分流, 不再把所有情况都说成"内网需授权"
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reason,category,fragment",
    [
        ("empty_url", "invalid_target_url", "目标地址为空或格式无效"),
        ("bad_url", "invalid_target_url", "目标地址为空或格式无效"),
        ("bad_scheme", "invalid_target_scheme", "目标协议不被允许"),
        ("no_host", "invalid_target_host", "目标地址缺少主机名"),
        ("loopback", "blocked_target", "目标地址不被允许"),
        ("metadata_host", "blocked_target", "目标地址不被允许"),
        ("host_not_allowed", "target_host_not_allowed", "目标域名不在运维放行列表"),
    ],
)
def test_target_blocked_reason_is_routed_to_accurate_category(
    reason, category, fragment
):
    """配置形态错误 → 配置类类别; 策略拦截 → blocked_target; iLink → 域名白名单类别。"""
    from src.core.notify_errors import classify
    from src.web.api.channels import _config_error_text

    exc = TargetBlocked(reason)
    assert classify(exc) == category
    text = _config_error_text(exc)
    assert fragment in text and "参考号: ERR-" in text
    if category != "blocked_target":
        # 旧实现会把 bad_url/no_host 提示成"内网/本地需运维授权", 方向全错。
        assert "内网/本地需运维显式授权" not in text
        assert "目标地址被拒绝" not in text


def test_empty_url_is_not_reported_as_bad_url():
    with pytest.raises(TargetBlocked) as excinfo:
        validate_target("")
    assert excinfo.value.reason == "empty_url"


def test_channels_api_bad_url_returns_config_hint_not_private_network_hint(
    monkeypatch,
):
    """设置页给 hermes 填一个没有主机名的 URL: 提示改 URL, 不是让运维放行内网。"""
    from fastapi import HTTPException

    from src.web.api.channels import _validate_channel

    monkeypatch.delenv(ENV_ALLOW_PRIVATE_HOSTS, raising=False)
    with pytest.raises(HTTPException) as excinfo:
        _validate_channel("hermes", {"webhook_url": "http://"})
    assert excinfo.value.status_code == 400
    detail = str(excinfo.value.detail)
    assert "目标地址缺少主机名" in detail
    assert "内网/本地需运维显式授权" not in detail


# --------------------------------------------------------------------------
# 7. 历史状态 / receipt: 严格枚举白名单(不改数据库)
# --------------------------------------------------------------------------


def test_historic_push_status_and_receipt_are_strictly_bounded():
    """历史行里的 status/时间戳/receipt 也可能是任意文本: 出口只放行枚举与短标识。"""
    secret = FAKE_WEBHOOK_KEY
    row = Notification(
        id=9,
        category="system",
        level="error",
        title="历史",
        body="",
        link="",
        source="agent",
        trace_id="",
        # 修复前可能被写成整段异常
        push_status=f"failed token={secret}",
        push_error="",
        push_channels=[
            {
                "id": "3",
                "name": "中转",
                "type": f"hermes/{secret}",
                "status": f"failed token={secret}",
                "success": "yes",  # 非 bool: 不放行
                "delivered_at": f"2026-11-12 {secret}",
                "receipt": {
                    "message_id": "msg-123",
                    "code": 200,
                    "raw_response": f"https://gw.test/?token={secret}",
                    "headers": {"Authorization": f"Bearer {secret}"},
                    "note": f"Bearer {secret}",
                },
            }
        ],
        created_at=datetime(2026, 11, 12, 9, 0, 0),
    )
    out = _to_out(row)
    blob = json.dumps(out.model_dump(), ensure_ascii=False)
    assert out.push_status == ""
    channel = out.push_channels[0]
    assert channel["status"] == ""
    assert channel["type"] == ""
    assert channel["id"] == 3
    assert "success" not in channel, "非 bool 的历史 success 不能猜成真值"
    assert channel["delivered_at"] == ""
    assert channel["receipt"] == {"message_id": "msg-123", "code": 200}
    assert secret not in blob

    # 只改出口形态, 数据库里的原值必须原样保留(不改库/不删行)。
    assert row.push_status == f"failed token={secret}"
    assert row.push_channels[0]["receipt"]["raw_response"].endswith(secret)
    assert row.push_channels[0]["status"] == f"failed token={secret}"


def test_arbitrary_lowercase_token_is_not_mistaken_for_internal_enum():
    """历史白名单不能把任意小写 token 当成内部枚举(旧实现会原样放行)。"""
    token = __import__("secrets").token_hex(16)
    text = public_saved_error(token)
    assert token not in text
    assert "参考号: ERR-" in text
    # 已知内部枚举仍然保留(有界标识)。
    assert public_saved_error("quiet_hours") == "quiet_hours"


# --------------------------------------------------------------------------
# 8. 部署配置: compose 必须透传白名单变量, .env.example/设置页要写清限制
# --------------------------------------------------------------------------


def _repo_root():
    from pathlib import Path

    return Path(__file__).resolve().parents[1]


def test_compose_passes_notify_allowlists_into_container():
    """只在宿主 .env 里写不够: compose 的 environment 不透传, 容器里读不到。"""
    import yaml

    compose = yaml.safe_load((_repo_root() / "docker-compose.yml").read_text("utf-8"))
    env = compose["services"]["sida"]["environment"]
    entries = {str(item) for item in env}
    for name in (ENV_ALLOW_PRIVATE_HOSTS, "SIDA_NOTIFY_ILINK_HOSTS"):
        assert any(item.startswith(f"{name}=") for item in entries), (
            f"{name} 没有从宿主透传到 sida 容器"
        )


def test_env_example_and_settings_warn_about_cidr_and_proxy_limits():
    root = _repo_root()
    example = (root / ".env.example").read_text("utf-8")
    # 必须是真实可复制的赋值行(不能只出现在注释示例里)。
    assert "\nSIDA_NOTIFY_PRIVATE_HOSTS=" in example
    assert "\nSIDA_NOTIFY_ILINK_HOSTS=" in example
    for needle in (
        ENV_ALLOW_PRIVATE_HOSTS,
        "SIDA_NOTIFY_ILINK_HOSTS",
        "CIDR",
        "任意地址",
        "NO_PROXY",
        "本版本不支持经代理固定",
    ):
        assert needle in example, f".env.example 缺少部署说明: {needle}"

    settings = (root / "frontend/src/pages/settings/NotifySection.tsx").read_text("utf-8")
    for needle in ("SIDA_NOTIFY_PRIVATE_HOSTS", "CIDR", "本版本不支持经代理固定目标地址", "NO_PROXY"):
        assert needle in settings, f"设置页缺少可见兼容提示: {needle}"
