"""出站目标安全校验 + **DNS 固定(pinning)**: 防通知渠道 SSRF / DNS rebinding。

## 为什么要这个

`notify_channels.config` 里有三个字段是**任意 URL**, 且写入路径对普通 member 开放
(`POST/PUT /api/channels` 只校验归属, 不校验目标):

- `hermes.webhook_url`  -> 本仓自己用 httpx POST;
- `bark.server_url`     -> 自建 Bark 服务器地址;
- `wechat_ilink.base_url` -> 扫码绑定时由腾讯返回, 但之后可被 PUT 改写。

于是一个登录用户可以把 `webhook_url` 指到 `http://169.254.169.254/latest/meta-data/`
(云元数据)、`http://127.0.0.1:8080/api/...`(同机管理面)、`http://10.0.0.5/`(内网),
再触发一次通知, 服务端就会替他去请求 —— 典型 SSRF(可盲打内网/元数据)。

## 口径: 只校验「用户可写的目标」, 不碰固定服务端

Telegram `api.telegram.org`、企微 `qyapi.weixin.qq.com`、Server酱/PushPlus 这些
**代码里写死的域名**不经过本模块: 它们不是用户可控输入, 加校验只会引入生产噪音。

## 两道防线

1. **解析即校验**: `socket.getaddrinfo` 拿到全部 A/AAAA, 任一落入
   回环/私网/链路本地(含 `169.254.169.254`)/保留/组播/未指定 → 拒绝。
   IPv4-mapped IPv6(`::ffff:127.0.0.1`)先拆出 IPv4 再判, 不能靠字符串前缀。
2. **连接到已校验的 IP(pinning)**: 拿到通过的 IP 后, 把请求 URL 的 host 换成该 IP,
   同时显式带 `Host` 头 + `extensions={"sni_hostname": 原域名"}`
   (httpcore 1.x 会用它做 TLS SNI 与证书校验, 见 `_async/connection.py::_connect`)。
   这样**同一请求内只解析一次**, 攻击者无法在"校验通过"和"真正连接"之间把 DNS
   改指内网 —— 这是 rebinding 的可靠解法, 而不是"校验完再让它自己解析一遍"的假防护。
   也**不跟随重定向**(httpx 默认 False, 这里显式写死), 否则 302 到内网同样是 SSRF。

   ### 经代理的 HTTPS: 明确拒绝, 不静默降级

   httpcore 1.0.x 的 CONNECT 隧道 (`_async/http_proxy.py::AsyncTunnelHTTPConnection`)
   **不读 `sni_hostname`**, 它写死用 `remote_origin.host` 做 SNI/证书校验。而 pinning 后
   这个 host 是 IP 字面量 → 无 SNI, 证书按 IP 校验必然失败(`IP address mismatch`)。
   也就是说 env `HTTPS_PROXY`/`ALL_PROXY` 存在时, "固定 IP" 与"原域名 TLS" 两者
   httpcore 不让我们兼得:

   - 放弃 pinning 改成让代理解析 → DNS rebinding 窗口重新打开(代理侧解析, 我们验不到);
   - 静默跳过代理直连 → 悄悄改变出口路由(合规/只有代理能出网的部署会莫名不通)。

   两种都不做: `ProxyPinUnsupported` 明确拒绝(有界文案提示把目标域名加进 `NO_PROXY`,
   这样 httpx 的 mounts 不含该 host, 我们照常 pinning + SNI)。明文 `http://` 目标经代理
   不受影响(转发代理不会自己解析目标, 我们发的就是 IP)。

   ### URL 里的 userinfo(`https://user:pass@host/`)

   不再被 `urlsplit` 静默吃掉: 解析成 `Authorization: Basic ...` 显式带上(调用方已给
   `Authorization` 时以调用方为准), 且**不放进** `GuardedTarget` 的可打印字段(`repr=False`),
   任何日志/异常都拿不到明文。含控制字符(CR/LF 注入头)的 userinfo 算配置形态错误
   (`ValueError` → `config_invalid`), 不冒充"目标被拦"。

## 合法内网部署怎么放行

自建 Hermes 网关 / Bark 常常就在同机或局域网(`http://127.0.0.1:8099`, `http://10.0.0.8`)。
这类"授权目标"由**运维**用环境变量显式放行, 不是由用户自己在渠道配置里点开关 ——
否则 member 自己勾一个 `allow_private` 就能绕过全部防护:

    SIDA_NOTIFY_PRIVATE_HOSTS=127.0.0.1,::1,hermes-gw.internal,192.168.1.0/24

值支持: 主机名(精确匹配, 不分大小写) / IP / CIDR; `*` 表示完全放开(不建议, 仅兜底)。
放行是**按目标**生效的: 主机名放行只影响该主机, 不会顺带放开它的邻居。

被拒绝时抛 `TargetBlocked`(ValueError 子类), 上层按 `blocked_target` 类别对外
("目标地址不被允许"), 并提示用上面的环境变量授权 —— 不会静默失败, 也不会把
URL/凭据写进日志或 `push_error`。
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import ipaddress
import os
import socket
from dataclasses import dataclass, field
from urllib.parse import unquote, urlsplit

ENV_ALLOW_PRIVATE_HOSTS = "SIDA_NOTIFY_PRIVATE_HOSTS"

#: 云元数据服务的固定入口。它们本身就落在链路本地段(会被 IP 判定拦掉), 这里额外
#: 按**域名**拦一层: 万一将来部署在把 169.254.0.0/16 视为可路由的环境里, 域名判定
#: 仍然生效。
_METADATA_HOSTNAMES = frozenset(
    {
        "metadata",
        "metadata.google.internal",
        "metadata.goog",
        "instance-data",
        "instance-data.ec2.internal",
    }
)

#: 允许的 URL scheme。不做 gopher/file/ldap —— 通知渠道只需要 HTTP(S)。
_ALLOWED_SCHEMES = frozenset({"http", "https"})

#: 代理 URL 允许的 scheme: http(s) 转发代理 + socks(httpx 走 socksio)。
_PROXY_SCHEMES = frozenset({"http", "https", "socks4", "socks4a", "socks5", "socks5h"})
_PROXY_DEFAULT_PORTS = {"http": 80, "https": 443, "socks4": 1080, "socks4a": 1080, "socks5": 1080, "socks5h": 1080}


class TargetBlocked(ValueError):
    """目标地址被策略拒绝。

    `reason` 是**有界枚举**(可安全落日志/进对外类别文案), 不含 URL、host、token;
    `str(self)` 也只有这一句固定文案 —— 调用方即使随手 `str(e)` 也不会泄漏目标。
    """

    #: reason -> 对外可读短语(不含任何目标信息)
    _LABELS = {
        "bad_url": "目标地址格式无效",
        "bad_scheme": "目标协议不被允许",
        "no_host": "目标地址缺少主机名",
        "unresolvable": "目标域名解析失败",
        "empty_resolution": "目标域名没有可用地址",
        "metadata_host": "目标是云元数据服务",
        "host_not_allowed": "目标域名不在允许列表",
        "loopback": "目标是本机回环地址",
        "link_local": "目标是链路本地地址",
        "private": "目标是内网地址",
        "reserved": "目标是保留地址",
        "multicast": "目标是组播地址",
        "unspecified": "目标是未指定地址",
    }

    def __init__(self, reason: str = "bad_url") -> None:
        self.reason = reason if reason in self._LABELS else "bad_url"
        self.public_label = self._LABELS[self.reason]
        super().__init__(f"目标地址被拒绝({self.public_label})")


class TargetUnresolvable(OSError):
    """DNS 解析失败(与「被策略拒绝」区分: 这是网络问题, 不是安全拦截)。"""


class ProxyPinUnsupported(ValueError):
    """HTTPS 目标经代理时无法同时做到「固定已校验的 IP」与「按原域名做 TLS」。

    httpcore 1.0.x 的 CONNECT 隧道忽略 `sni_hostname`, 用 URL host(= 我们固定后的 IP)
    做 SNI 与证书校验, 必然握手失败。这里**明确拒绝**, 不静默放弃 pinning(那是 SSRF)
    也不静默绕过代理(那是偷偷改出口)。文案有界: 不含目标/代理地址。
    """

    def __init__(self) -> None:
        super().__init__(
            "HTTPS 目标经代理时无法固定已校验地址; 请把目标域名加入 NO_PROXY 或改用直连"
        )


@dataclass(frozen=True)
class GuardedTarget:
    """校验通过的目标 + 固定后的连接参数。"""

    scheme: str
    host: str  # 原始域名(用于 Host 头 / TLS SNI)
    port: int
    ip: str  # 已校验、本次请求固定连接的 IP
    path: str  # 含 query, 原样保留
    allowlisted: bool = False
    #: URL userinfo 换来的 Basic 认证头。**repr=False**: 凭据不许跟着对象进日志/异常。
    auth_header: str | None = field(default=None, repr=False)

    @property
    def is_default_port(self) -> bool:
        return (self.scheme == "https" and self.port == 443) or (
            self.scheme == "http" and self.port == 80
        )

    @property
    def host_header(self) -> str:
        """HTTP `Host` 头: 非默认端口要带上, 否则部分虚拟主机路由会 404。"""
        if self.is_default_port:
            return self.host
        return f"{self.host}:{self.port}"

    def pinned_url(self) -> str:
        """把 host 换成已校验 IP 的 URL(IPv6 需要方括号)。"""
        host_part = f"[{self.ip}]" if ":" in self.ip else self.ip
        if self.is_default_port:
            return f"{self.scheme}://{host_part}{self.path}"
        return f"{self.scheme}://{host_part}:{self.port}{self.path}"

    @property
    def extensions(self) -> dict:
        """给 httpcore 的扩展: TLS 用原域名做 SNI/证书校验(连的是 IP)。"""
        if self.scheme == "https":
            return {"sni_hostname": self.host}
        return {}


def _allow_entries() -> tuple[frozenset[str], tuple]:
    """读取运维放行表: (主机名集合, IP/CIDR 网段元组)。`*` 用空串标记。"""
    raw = os.environ.get(ENV_ALLOW_PRIVATE_HOSTS, "") or ""
    hosts: set[str] = set()
    nets: list = []
    for item in raw.replace(";", ",").split(","):
        value = item.strip()
        if not value:
            continue
        if value == "*":
            hosts.add("*")
            continue
        try:
            nets.append(ipaddress.ip_network(value, strict=False))
            continue
        except ValueError:
            pass
        hosts.add(value.lower())
    return frozenset(hosts), tuple(nets)


IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


def _normalize_ip(value: str) -> IPAddress:
    ip = ipaddress.ip_address(value)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        # ::ffff:127.0.0.1 必须按 127.0.0.1 判, 否则 is_private 会给出 False。
        return ip.ipv4_mapped
    return ip


def _deny_reason_for_ip(ip: IPAddress) -> str | None:
    """按 IP 归类拒绝原因; 允许返回 None。顺序即优先级(元数据/链路本地先判)。"""
    if ip.is_unspecified:
        return "unspecified"
    if ip.is_loopback:
        return "loopback"
    if ip.is_link_local:
        return "link_local"
    if ip.is_multicast:
        return "multicast"
    if ip.is_private:
        return "private"
    if ip.is_reserved:
        return "reserved"
    return None


def _is_allowlisted(host: str, ip: IPAddress) -> bool:
    hosts, nets = _allow_entries()
    if "*" in hosts:
        return True
    if host.lower() in hosts:
        return True
    return any(ip in net for net in nets if net.version == ip.version)


def _resolve(host: str, port: int) -> list[str]:
    """解析主机的全部地址(A/AAAA)。IP 字面量直接返回, 不走 DNS。"""
    try:
        return [str(ipaddress.ip_address(host.strip("[]")))]
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:  # 含 socket.gaierror
        raise TargetUnresolvable(f"目标域名无法解析: {type(exc).__name__}") from exc
    seen: list[str] = []
    for info in infos:
        addr = str(info[4][0])
        if addr not in seen:
            seen.append(addr)
    return seen


#: `policy` 取值:
#: - `"public"`: 只允许公网地址(默认)。Webhook / 自建推送服务器这类**带路径的
#:   HTTP 目标**用它 —— 命中即等于让服务器替你请求任意内网接口。
#: - `"no_metadata"`: 只拦链路本地/未指定/组播/保留 + 元数据域名, **放行回环与私网**。
#:   给「代理地址」用: 本机/局域网的 clash、squid 是合法常态(`127.0.0.1:7890`),
#:   一刀切会把它们全打死; 但代理指向 `169.254.169.254` 从来不是正常配置。
_POLICIES = frozenset({"public", "no_metadata"})


def _basic_auth_header(parts) -> str | None:
    """URL userinfo → `Authorization: Basic ...`。

    旧实现把 URL 交给 `urlsplit` 后只用 host/port 重建 URL, userinfo 就**静默消失**了:
    用户配 `https://user:pass@host/hook` 看起来保存成功, 实际请求不带凭证(服务端 401),
    而设置页不会报任何错。这里按 httpx/curl 的语义显式转成请求头, 且:
    - 只落在 `GuardedTarget.auth_header`(repr=False), 不进 URL、不进日志;
    - 含 CR/LF/NUL 的 userinfo 视为**头注入**, 直接 `bad_url`, 不做"清洗"。
    """
    if parts.username is None and parts.password is None:
        return None
    user = unquote(parts.username or "")
    password = unquote(parts.password or "")
    if any(ch in "\r\n\x00" for ch in user + password):
        # 这是**配置形态**错误(URL 里藏了头注入), 不是"目标危险": 用 ValueError 归
        # config_invalid, 不要用 TargetBlocked(那会被对外说成"内网需授权", 方向全错)。
        raise ValueError("目标地址的认证信息含非法字符")
    try:
        token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    except UnicodeError as exc:  # 极端编码问题: 归类为 URL 无效, 不去猜
        raise TargetBlocked("bad_url") from exc
    return f"Basic {token}"


def validate_target(
    url: str, *, require_public: bool = True, policy: str = "public"
) -> GuardedTarget:
    """校验 URL 目标并返回可固定连接的信息; 不合规抛 `TargetBlocked`。

    - `require_public=False`: 只做格式/scheme 校验(用于**代码内固定域名**), 不做
      IP 段拦截。
    - `policy="no_metadata"`: 见 `_POLICIES`, 供代理地址这类"私网合法"的目标使用。
    - 解析失败抛 `TargetUnresolvable`(上层归 `dns_failed`, 不是安全拦截)。
    """
    raw = str(url or "").strip()
    if not raw:
        raise TargetBlocked("bad_url")
    try:
        parts = urlsplit(raw)
        scheme = (parts.scheme or "").lower()
        host = (parts.hostname or "").strip()
        port = parts.port
    except ValueError as exc:  # 端口非数字等
        raise TargetBlocked("bad_url") from exc
    # userinfo 单独解析: 它的问题是"配置形态错"(ValueError → config_invalid), 不是
    # "目标被拦"; 放在上面那个 try 里会被误包成 TargetBlocked。
    auth_header = _basic_auth_header(parts)
    if scheme not in _ALLOWED_SCHEMES:
        raise TargetBlocked("bad_scheme")
    if not host:
        raise TargetBlocked("no_host")
    if port is None:
        port = 443 if scheme == "https" else 80

    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"

    host_clean = host.strip("[]").lower()
    if host_clean in _METADATA_HOSTNAMES:
        raise TargetBlocked("metadata_host")

    if not require_public:
        addrs = _resolve(host_clean, port)
        if not addrs:
            raise TargetBlocked("empty_resolution")
        return GuardedTarget(
            scheme, host_clean, port, addrs[0], path, auth_header=auth_header
        )

    addrs = _resolve(host_clean, port)
    if not addrs:
        raise TargetBlocked("empty_resolution")

    policy = policy if policy in _POLICIES else "public"
    if policy == "no_metadata":
        # 代理目标: 只有云元数据段/组播/保留算危险, 回环与私网放行。
        for addr in addrs:
            ip = _normalize_ip(addr)
            reason = _deny_reason_for_ip(ip)
            if reason in ("link_local", "unspecified", "multicast", "reserved"):
                raise TargetBlocked(reason)
        return GuardedTarget(
            scheme, host_clean, port, addrs[0], path, auth_header=auth_header
        )

    # 严格口径: **任一**解析结果危险就整体拒绝 —— 多 A 记录里混一条内网地址是
    # 经典 SSRF 夹带手法, 不能"挑到安全的那条就放行"。
    chosen: tuple[str, bool] | None = None
    for addr in addrs:
        ip = _normalize_ip(addr)
        reason = _deny_reason_for_ip(ip)
        if reason is None:
            chosen = chosen or (addr, False)
            continue
        if _is_allowlisted(host_clean, ip):
            chosen = chosen or (addr, True)
            continue
        raise TargetBlocked(reason)
    if chosen is None:
        raise TargetBlocked("empty_resolution")
    return GuardedTarget(
        scheme, host_clean, port, chosen[0], path, chosen[1], auth_header=auth_header
    )


def validate_proxy_target(proxy_url: str) -> str:
    """校验**代理地址**(渠道 `proxy` 字段): 返回原样代理串。

    为什么单独一个入口: 旧实现写的是 `if proxy.startswith(("http://", "https://"))`,
    于是 `socks5h://169.254.169.254:1080` **整条绕过**校验(还能让代理解析任意域名)。
    这里按 URL 解析, 不靠字符串前缀:

    - scheme 必须是我们支持的代理协议(`http/https/socks4/socks4a/socks5/socks5h`);
      其它(如 `ftp://`)抛 ValueError → 归 `config_invalid`(不是"被拦"也不是"网络不可达");
    - 主机沿用 `policy="no_metadata"`: 本机/局域网的 clash、squid 合法, 云元数据/链路本地/
      保留/组播一律拒;
    - DNS 解析失败抛 `TargetUnresolvable`, 由调用方归 `proxy_failed`(代理的问题, 不是
      渠道配置的问题)。

    不做 IP 固定: httpx 自己连代理(它会在连接时再解析一次), 这里只保证"不会指到元数据段"。
    """
    raw = str(proxy_url or "").strip()
    if not raw:
        raise ValueError("代理地址为空")
    try:
        parts = urlsplit(raw)
        scheme = (parts.scheme or "").lower()
        host = (parts.hostname or "").strip()
        port = parts.port
    except ValueError as exc:  # 端口非数字
        raise ValueError("代理地址格式无效") from exc
    if scheme not in _PROXY_SCHEMES:
        raise ValueError(f"不支持的代理协议: {scheme or '(缺省)'}")
    if not host:
        raise ValueError("代理地址缺少主机名")
    if port is None:
        port = _PROXY_DEFAULT_PORTS.get(scheme, 1080)

    host_clean = host.strip("[]").lower()
    if host_clean in _METADATA_HOSTNAMES:
        raise TargetBlocked("metadata_host")
    addrs = _resolve(host_clean, port)
    if not addrs:
        raise TargetBlocked("empty_resolution")
    for addr in addrs:
        ip = _normalize_ip(addr)
        reason = _deny_reason_for_ip(ip)
        if reason in ("link_local", "unspecified", "multicast", "reserved"):
            raise TargetBlocked(reason)
    return raw


def client_uses_proxy(client, url: str) -> bool:
    """这次请求是否会经**代理**发出(env 代理 + `NO_PROXY` 都算)。

    直接问 httpx 自己: `_transport_for_url` 会按 mounts 选出真正要用的 transport,
    `NO_PROXY` 命中的主机拿到的就是直连 transport。判据是底层 pool 的类型:
    `AsyncHTTPProxy`(http/https 代理) / `AsyncSOCKSProxy`(socks)。读不到内部结构时
    保守返回 False(与修复前行为一致, 不因探测失败而拒绝合法请求)。
    """
    try:
        import httpx

        transport = client._transport_for_url(httpx.URL(url))
        pool = getattr(transport, "_pool", None)
    except Exception:  # noqa: BLE001 - 探测失败不影响主流程
        return False
    return type(pool).__name__ in ("AsyncHTTPProxy", "AsyncSOCKSProxy")


async def guarded_request(
    client,
    method: str,
    url: str,
    *,
    headers: dict | None = None,
    content: bytes | None = None,
    json_body=None,
    timeout: float | None = None,
):
    """发一个**目标已校验且已固定 IP** 的请求(不跟随重定向)。

    只解析一次 DNS: 校验用的地址就是连接用的地址。附加 `Host` 头与 TLS SNI,
    所以被访问的服务仍看到原始域名(虚拟主机/证书都不受影响)。

    HTTPS + 代理(env `HTTPS_PROXY`/`ALL_PROXY`) 会**明确拒绝**(`ProxyPinUnsupported`):
    httpcore 的隧道不认 `sni_hostname`, 固定 IP 后 TLS 必然失败; 既不能为了跑通而放弃
    pinning, 也不能悄悄绕过运维配的代理。`NO_PROXY` 命中该域名时 httpx 走直连, 照常可用。
    """
    target = await asyncio.to_thread(validate_target, url)
    if target.scheme == "https" and client_uses_proxy(client, url):
        raise ProxyPinUnsupported()

    merged = {k: v for k, v in (headers or {}).items()}
    # httpx 会按 URL 里的 host(此时是 IP)自动生成 Host, 必须显式覆盖。
    for key in [k for k in merged if str(k).lower() == "host"]:
        merged.pop(key)
    merged["Host"] = target.host_header
    # URL userinfo 换来的 Basic 认证; 调用方显式给了 Authorization 时以调用方为准。
    if target.auth_header and not any(
        str(k).lower() == "authorization" for k in merged
    ):
        merged["Authorization"] = target.auth_header
    kwargs = {
        "headers": merged,
        "extensions": target.extensions,
        "follow_redirects": False,
    }
    if content is not None:
        kwargs["content"] = content
    if json_body is not None:
        kwargs["json"] = json_body
    if timeout is not None:
        kwargs["timeout"] = timeout
    return await client.request(method, target.pinned_url(), **kwargs)


__all__ = [
    "ENV_ALLOW_PRIVATE_HOSTS",
    "GuardedTarget",
    "ProxyPinUnsupported",
    "TargetBlocked",
    "TargetUnresolvable",
    "client_uses_proxy",
    "guarded_request",
    "validate_proxy_target",
    "validate_target",
]
