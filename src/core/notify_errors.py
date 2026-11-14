"""通知外发失败的**对外口径**: 固定失败类别 + 关联号(2026-11-12 复审五)。

## 为什么单独一个模块

`src/core/notify_center.py` 会把失败写进 `notifications.push_status/push_error/
push_channels`, 而这三个字段是**持久化并且对外可读**的:

- `GET /api/notifications` 与 `GET /api/notifications/{id}/status` 原样返回;
- 前端通知中心直接渲染 `push_error` / 每渠道 `error`;
- 页面一截图、日志一导出、工单一带, 就离开了服务端。

而 `src/core/notifier.py` 各渠道的异常文本里天然带:

- 代理地址里的账号密码(`http://user:pass@proxy.internal:8080`);
- 企微/飞书/钉钉 webhook 的 key、Telegram bot token、PushPlus/Bark token;
- provider 的原始回包(`resp.text`), 写 `context_token` 时的 DSN / SQL。

所以旧口径「`str(e)[:400]` 写库 + `logger.exception`」等于把渠道密钥、库连接串
持久化到通知表并回给浏览器。修法不是事后擦(见 `error_report` 里那段为什么正则
洗不干净), 而是**不给原文进入的机会**:

- **对外**(`push_status`/`push_error`/`push_channels`/HTTP 响应): 只有
  `"{动作}失败（{固定类别}）；参考号: ERR-XXXXXXXX"` —— 没有 URL、token、
  回包正文、SQL、DSN;
- **对内**(服务端日志): 由调用方 `error_report.log_failure` 记关联号 + 异常类型
  + 帧位置 + 白名单上下文(`channel_type`/`category`), 同一个关联号把两边串起来。

## 类别必须是闭合集合, 且只由异常**类型**决定

- 类别是有限的枚举(`CATEGORY_LABELS` 的键), 不是自由文本 —— 前端/运维可以按
  类别统计, 也不会因为 provider 回显一句 `invalid token abc123` 就把密钥写进库。
- **绝不读 `str(exc)`** 做判断: 那等于把分类逻辑建在不可信内容上(provider 的报错
  里就带 key), 而且判定结果本身也可能被回显进日志。这里只看异常类型(以及
  HTTP 状态码/int 型 errcode 这种有界数值)。
"""

from __future__ import annotations

import hashlib
import json
import re

import httpx

from src.core.error_report import new_error_ref

#: 固定失败类别 → 用户可读文案。**只增不改**: 已落库的旧 `push_error` 里可能出现
#: 这些中文, 前端和客服口径要对得上。
CATEGORY_LABELS: dict[str, str] = {
    "config_invalid": "渠道配置不完整或无效",
    "invalid_target_url": "目标地址为空或格式无效",
    "invalid_target_scheme": "目标协议不被允许(仅支持http/https)",
    "invalid_target_host": "目标地址缺少主机名",
    "auth_rejected": "渠道凭证被拒绝",
    "network_unreachable": "网络连接失败",
    "dns_failed": "目标域名解析失败",
    "proxy_failed": "代理不可用",
    "timeout": "请求超时",
    "http_rejected": "服务端拒绝该消息",
    "bad_response": "服务端响应无法解析",
    "blocked_target": "目标地址不被允许(内网/本地需运维显式授权)",
    # iLink 用的是**域名**白名单, 与"内网地址"不是一回事: 说成"内网需授权"会把
    # 运维带到错误的排查方向(实际要加 SIDA_NOTIFY_ILINK_HOSTS)。
    "target_host_not_allowed": "目标域名不在运维放行列表",
    "pinned_proxy_unsupported": "本版本不支持经代理固定HTTPS目标(需直连或NO_PROXY)",
    "db_unavailable": "数据库不可用",
    "not_delivered": "渠道未确认送达",
    "internal": "内部错误",
}

#: `TargetBlocked.reason` → 对外类别。**按 reason 分流**, 不把所有 `TargetBlocked`
#: 都说成"目标地址不被允许(内网/本地需运维显式授权)":
#: - 格式/scheme/缺主机是**配置形态**问题, 用户改 URL 就能修;
#: - 回环/私网/元数据是**策略**拦截, 才需要运维放行;
#: - 解析不到是网络/DNS 问题, 与安全拦截无关。
TARGET_BLOCKED_CATEGORY: dict[str, str] = {
    "empty_url": "invalid_target_url",
    "bad_url": "invalid_target_url",
    "bad_scheme": "invalid_target_scheme",
    "no_host": "invalid_target_host",
    "empty_resolution": "dns_failed",
    "metadata_host": "blocked_target",
    "host_not_allowed": "target_host_not_allowed",
    "loopback": "blocked_target",
    "link_local": "blocked_target",
    "private": "blocked_target",
    "reserved": "blocked_target",
    "multicast": "blocked_target",
    "unspecified": "blocked_target",
}

#: `notifications.push_status` / `push_channels[].status` 的合法枚举。历史行里可能
#: 混进任意文本(甚至整段异常), 出口只放行这些值, 其余置空 —— 不猜测、不改库。
PUBLIC_PUSH_STATUS = frozenset({"pending", "sent", "failed", "skipped"})

#: 兜底类别。任何没被识别的异常都落这里 —— 不认识就少说, 不猜。
DEFAULT_CATEGORY = "internal"

#: 401/403 这类「凭证问题」单独成一类, 便于运维直接去改 token 而不是查网络。
_AUTH_STATUS = frozenset({401, 403})


def label_for(category: str) -> str:
    """类别 → 可读文案(未知类别按 internal 处理, 绝不回显传入值)。"""
    return CATEGORY_LABELS.get(str(category), CATEGORY_LABELS[DEFAULT_CATEGORY])


def failure_text(
    category: str,
    *,
    action: str = "通知发送",
    ref: str | None = None,
    status: int | None = None,
) -> str:
    """拼一条可公开展示的失败说明: 动作 + 固定类别(+ 有界状态码) + 关联号。

    `status` 只接受 int(HTTP 状态码/渠道 errcode), 是数值不是文本 —— 加它不
    引入任何可泄漏内容, 但能省掉一轮「403 还是 500」的排查。文案统一写
    「状态码」: 渠道的 errcode(企微 93000 / PushPlus 903)不是 HTTP 状态码,
    写成 HTTP 会把人带偏。
    """
    ref = ref or new_error_ref()
    suffix = f"（状态码 {int(status)}）" if isinstance(status, int) else ""
    return f"{action}失败（{label_for(category)}{suffix}）；参考号: {ref}"


def stable_error_ref(seed: str) -> str:
    """由**内容**派生的稳定关联号(只读收敛/兜底文案用)。

    为什么不用 `new_error_ref()`: 历史行是只读收敛 —— 同一条记录每次 GET 都重新生成
    一个 uuid 的话, 用户刷新一次页面参考号就变一次, 而且那个号在服务端日志里根本不存在,
    既不可追查也不可复现。这里用内容哈希: 同一行永远同一个号, 相同文案的多行共用一个号
    (便于按号聚合), 且哈希是单向的 —— 拿到号推不出原文/凭据。
    """
    digest = hashlib.sha256(str(seed or "").encode("utf-8", "replace")).hexdigest()
    return f"ERR-{digest[:8].upper()}"


class NotifyFailure(Exception):
    """通知链路的真实失败: `str()` 只有「固定类别 + 关联号」。

    注意**不能**继承 `RuntimeError`: `notify_center.push_notification` 用
    `except RuntimeError` 判断「已经在事件循环里, 转异步发送」, 如果本异常是
    RuntimeError 的子类, 所有真实外发失败都会被误判成「需要异步」而静默丢掉。
    """

    def __init__(
        self,
        category: str = DEFAULT_CATEGORY,
        *,
        channel_type: str = "",
        action: str = "通知发送",
        ref: str | None = None,
        status: int | None = None,
    ) -> None:
        self.category = category if category in CATEGORY_LABELS else DEFAULT_CATEGORY
        self.channel_type = str(channel_type or "")
        self.action = action
        self.ref = ref or new_error_ref()
        self.status = status if isinstance(status, int) else None
        self.public_message = failure_text(
            self.category, action=action, ref=self.ref, status=self.status
        )
        super().__init__(self.public_message)

    def __str__(self) -> str:  # 任何残留的 `str(e)` 也只能拿到安全文案
        return self.public_message


def classify(exc: BaseException) -> str:
    """按异常**类型**映射到固定类别; 不读异常文本。

    判定顺序即优先级, 三条硬约束:

    1. **HTTP 协议错误优先**: `HTTPStatusError`(服务端已经明确回了非 2xx)必须先于
       任何"解析/配置"判定 —— 否则 200/403 这种事会被下游包装成别的类别, 运维会
       去查并没有坏的配置。
    2. **`JSONDecodeError` 在 `ValueError` 之前**: 它是 ValueError 的子类, 顺序反了
       会被判成 `config_invalid`(渠道配置不完整), 而我们真正想说的是"服务端回了
       非 JSON/HTML"。这是同一类错误最常见的误分类。
    3. 只在**类型**上判断, 不读 `str(exc)` —— provider 的报错里就带 key。
    """
    if isinstance(exc, NotifyFailure):
        return exc.category

    # 出站目标被策略拒绝(SSRF 防护), 有独立类别便于运维改配置而不是查网络。
    from src.core.net_guard import ProxyPinUnsupported, TargetBlocked, TargetUnresolvable

    if isinstance(exc, TargetBlocked):
        # 按 reason 分流: 格式/scheme/缺主机 → 配置错误; 回环/私网/元数据 → 策略拦截;
        # 空解析结果 → DNS 问题。旧实现把所有 TargetBlocked 都归 blocked_target,
        # 于是"URL 少写主机名"会被提示成"内网地址需运维授权", 方向完全错。
        return TARGET_BLOCKED_CATEGORY.get(exc.reason, "blocked_target")
    if isinstance(exc, ProxyPinUnsupported):
        # 不是"目标危险", 也不是"网络坏": 是 HTTPS 经代理时固定地址与 SNI 不可兼得,
        # 需要运维改 NO_PROXY。单独成类, 否则会被 ValueError 分支判成配置无效。
        return "pinned_proxy_unsupported"
    if isinstance(exc, TargetUnresolvable):
        # DNS 解析失败是**网络/环境**问题, 与"网络连接失败(连不上已解析的地址)"分开:
        # 前者要查域名/解析器, 后者要查路由/防火墙。旧口径混成 network_unreachable。
        return "dns_failed"

    # httpx: 先判 ProxyError(代理隧道失败), 再判超时(ConnectTimeout 同时也是
    # ConnectError 的子类, 顺序反了会把超时说成"网络不可达")。
    if isinstance(exc, httpx.ProxyError):
        return "proxy_failed"
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    if isinstance(exc, httpx.ConnectError):
        return "network_unreachable"
    if isinstance(exc, httpx.HTTPStatusError):
        return "auth_rejected" if exc.response is not None and exc.response.status_code in _AUTH_STATUS else "http_rejected"

    # 回包不是 JSON(HTML 登录页/网关错误页/纯文本 200) —— 必须在 ValueError 之前。
    if isinstance(exc, (json.JSONDecodeError, UnicodeDecodeError)):
        return "bad_response"

    try:  # SQLAlchemy 只在真用到时导入, 免得纯渠道测试多拉一坨依赖
        from sqlalchemy.exc import SQLAlchemyError

        if isinstance(exc, SQLAlchemyError):
            return "db_unavailable"
    except Exception:  # noqa: BLE001 - 导入失败不影响分类, 落到 internal
        pass

    if isinstance(exc, ValueError):
        # 渠道配置缺字段/形态错(notifier 内部校验用 ValueError), 文案由我们自己拼。
        return "config_invalid"
    if isinstance(exc, (KeyError, IndexError, TypeError, AttributeError)):
        # provider 回包结构变了(少字段/类型不符) —— 属于「响应无法解析」。
        return "bad_response"
    return DEFAULT_CATEGORY


def as_failure(
    exc: BaseException,
    *,
    channel_type: str = "",
    action: str = "通知发送",
    ref: str | None = None,
    status: int | None = None,
) -> NotifyFailure:
    """把任意异常收敛成 `NotifyFailure`(幂等: 已是本类型且不要新关联号则原样返回)。

    已经是 `NotifyFailure` 时**保留它的有界状态码**: HTTP 403 / 企微 errcode 这类
    数值是对外文案里唯一可公开的诊断信息, 重新包装时丢掉会让"403 还是 500"重新变成
    猜谜(而且 FP 上没有别的线索)。
    """
    if isinstance(exc, NotifyFailure):
        if ref is None:
            return exc
        return NotifyFailure(
            exc.category,
            channel_type=channel_type or exc.channel_type,
            action=action,
            ref=ref,
            status=exc.status if status is None else status,
        )
    return NotifyFailure(
        classify(exc),
        channel_type=channel_type,
        action=action,
        ref=ref,
        status=status,
    )


#: 对外失败文案的合法形态: 「{动作}失败（{固定类别}(（状态码 N）)?）；参考号: ERR-XXXXXXXX」。
_PUBLIC_ERROR_PART_RE = re.compile(
    r"^.+?失败（[^（）\s]{1,40}(?:（状态码 -?\d{1,6}）)?）；参考号: ERR-[0-9A-F]{8}$"
)
#: 内部枚举回执: 没有正文, 本身就是有界标识。**必须是已知枚举**, 不能是"任意小写
#: 标识" —— 否则一段 32 位小写十六进制 token 也能原样穿过白名单(实测漏洞)。
_KNOWN_INTERNAL_CODES = frozenset({"quiet_hours"})
_INTERNAL_ENUM_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


def _is_known_internal_code(text: str) -> bool:
    return bool(_INTERNAL_ENUM_RE.match(text)) and text in _KNOWN_INTERNAL_CODES


def public_push_status(value) -> str:
    """`push_status` 出口收敛: 只放行固定枚举, 历史行的任意文本一律置空。

    为什么不能原样回显: 该字段读自数据库, 修复前可能被写成整段异常/URL。前端对
    未知值会直接 `{item.push_status}` 渲染, 等于把历史脏数据重新送到浏览器。
    """
    raw = str(value or "").strip()
    return raw if raw in PUBLIC_PUSH_STATUS else ""


def public_failure_text(text, *, action: str = "通知发送") -> str:
    """只放行「固定类别 + 关联号」形态的失败文案, 其余换成兜底类别。

    为什么还需要这层: 写库/对外的是 `notifications.push_error` 与
    `push_channels[].error`。渠道实现是我们自己写的(`NotifyFailure`), 但回执也
    可能来自定制分支、历史数据、测试替身或未来新增的渠道 —— 只要有人往里塞
    `str(exc)`, 泄漏就又开了一个口子。这里按**形态白名单**判定: 认不出的文本一律
    丢弃, 换成「内部错误 + 新关联号」, 让出口口径不随上游实现漂移。
    """
    raw = str(text or "").strip()
    if not raw:
        return ""
    parts = [p.strip() for p in raw.split("; ") if p.strip()]
    if parts and all(
        _PUBLIC_ERROR_PART_RE.match(p) or _is_known_internal_code(p) for p in parts
    ):
        return raw[:400]
    # 兜底号必须与内容绑定: 同一段原文每次收敛都得到同一个号(见 stable_error_ref)。
    return failure_text(
        "internal", action=action, ref=stable_error_ref(raw)
    )[:400]


#: 我们自己写的**有界**推送原因(不是异常文本), 历史行里也合法:
#: `push_status=skipped` 等分支写的是中文固定句, 不是渠道回执。
_KNOWN_REASON_PREFIXES = (
    "未配置通知渠道",
    "级别不外发",
    "异步发送中",
    "渠道选择集",
)

#: `push_channels[]` 允许对外的字段。历史行里可能混进别的键(甚至整段异常上下文),
#: 一律丢弃 —— 前端只渲染 status/error/delivered_at 这几个。
_PUBLIC_CHANNEL_KEYS = (
    "id",
    "name",
    "type",
    "status",
    "success",
    "error",
    "delivered_at",
    "failed_at",
    "receipt",
)

#: 历史 `receipt` 的**键**白名单。实际渠道只会回这些标识(accepted/ok/message_id/
#: delivery); 历史行里可能存了 `raw_response` 之类的整段回包 —— 不枚举的一律丢。
_PUBLIC_RECEIPT_KEYS = frozenset(
    {
        "status",
        "code",
        "errcode",
        "message_id",
        "msg_id",
        "ok",
        "success",
        "accepted",
        "delivered",
        "delivery",
        "queued",
        "retry_after",
    }
)
#: receipt 里的字符串值只接受有界标识形态(字母数字/._:-+), 不含 URL/空格/引号。
_BOUNDED_RECEIPT_VALUE_RE = re.compile(r"^[A-Za-z0-9_.:+-]{1,64}$")
#: receipt 不只是"键"要枚举, 每类键的**值**也有固定形态:
#: - 状态类只允许已知回执词(避免历史行把 token 塞进 status 原样回显);
#: - ID 类允许有界标识(真实渠道回 message_id);
#: - 计数/错误码只允许整数。
_RECEIPT_ENUM_KEYS = frozenset(
    {"status", "delivery", "ok", "success", "accepted", "delivered", "queued"}
)
_RECEIPT_INT_KEYS = frozenset({"code", "errcode", "retry_after"})
_RECEIPT_ENUM_VALUES = frozenset(
    {
        "ok",
        "sent",
        "delivered",
        "accepted",
        "failed",
        "error",
        "queued",
        "success",
        "confirmed",
        "rejected",
        "unknown",
        "pending",
        "true",
        "false",
    }
)
#: ISO-8601 形态的时间戳: 历史行里的时间字段同样不能是任意文本。
_ISO_TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[T ][0-9:.+\-Z]{0,32}$")
_CHANNEL_TYPE_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")

_MAX_CHANNEL_TEXT = 160


def _safe_channel_status(value) -> str:
    raw = str(value or "").strip()
    return raw if raw in PUBLIC_PUSH_STATUS else ""


def _safe_timestamp(value) -> str:
    raw = str(value or "").strip()
    return raw if _ISO_TIME_RE.match(raw) else ""


def _safe_receipt_value(key: str, value):
    """receipt 值 → 按**键类型**收敛后的有界标量; 不可识别返回 `_DROP`。"""
    if isinstance(value, bool):
        return value
    if key in _RECEIPT_ENUM_KEYS:
        raw = str(value or "").strip().lower()
        return raw if raw in _RECEIPT_ENUM_VALUES else _DROP
    if key in _RECEIPT_INT_KEYS:
        if isinstance(value, int):
            return value if -10**12 < value < 10**12 else _DROP
        return _DROP
    # ID 类: 有界标识, 允许整数或短字符串。
    if isinstance(value, int):
        return value if -10**12 < value < 10**12 else _DROP
    if isinstance(value, str):
        raw = value.strip()
        return raw if _BOUNDED_RECEIPT_VALUE_RE.match(raw) else _DROP
    return _DROP


_DROP = object()


def _safe_receipt(value) -> dict:
    """历史 `receipt` → 严格枚举白名单(键 + 值都收敛)。"""
    if not isinstance(value, dict):
        return {}
    out: dict = {}
    for key, item in list(value.items())[:8]:
        name = str(key)
        if name not in _PUBLIC_RECEIPT_KEYS:
            continue
        safe = _safe_receipt_value(name, item)
        if safe is not _DROP:
            out[name] = safe
    return out


def _safe_reason_text(raw: str) -> bool:
    """站内自有原因: 允许中文固定句, 但出现 URL/JSON/SQL/长标识就否决。"""
    return (
        bool(raw)
        and len(raw) <= 200
        and not any(mark in raw for mark in _RISKY_MARKERS)
        and _LONG_IDENT_RE.search(raw) is None
    )


def looks_public_error(text: str) -> bool:
    """是否是「固定类别 + 关联号」形态, 或是我们自己写的有界原因。"""
    raw = str(text or "").strip()
    if not raw:
        return True
    if raw.startswith(_KNOWN_REASON_PREFIXES):
        return _safe_reason_text(raw)
    parts = [p.strip() for p in raw.split("; ") if p.strip()]
    return bool(parts) and all(
        _PUBLIC_ERROR_PART_RE.match(p) or _is_known_internal_code(p) for p in parts
    )


#: 历史文案里出现这些标记 = 不像"人写的短原因", 更像 URL/JSON/SQL/令牌。
_RISKY_MARKERS = ("://", "=", "@", "{", "}", "\\", "Bearer", "Traceback", "SELECT ", "INSERT ")
#: 高熵/长标识(令牌、UUID、域名段的形态): 16 个以上的连续标识字符一律不认。
_LONG_IDENT_RE = re.compile(r"[A-Za-z0-9_\-\.]{16,}")
#: 判为"良性旧原因"的最大长度(如「网络超时」)。
_BENIGN_MAX = 80
#: 良性旧原因只允许**中文 + 中文标点 + 空白**: 出现任何拉丁字母/数字都可能是
#: token 的一部分(如 "token: abc123" 不超长、也不含风险标记), 一律不认。
_BENIGN_REASON_RE = re.compile(r"^[\u4e00-\u9fff\u3000-\u303f\s，。、；：！？（）]{1,80}$")


def _looks_benign_historic(raw: str) -> bool:
    """历史行里"人写的短中文原因"的保守判定(如「网络超时」)。

    注意这是**第二道**放宽: 第一道是形态白名单(固定类别 + 关联号)。这里的用意是
    不要把所有旧行都压成「内部错误」。判据是**枚举式**的: 只允许中文/中文标点,
    任何拉丁字母或数字(可能是 token 片段)直接不认。漏判方向永远是"更保守"
    (多脱敏, 不少脱敏)。
    """
    return bool(raw) and _BENIGN_REASON_RE.match(raw) is not None


def public_saved_error(text, *, action: str = "历史推送") -> str:
    """**读取历史数据时**的安全出口: 只放行有界文案, 其余换成兜底类别。

    为什么需要它: `notifications.push_error` / `push_channels` 是**落库**字段,
    修复前写进去的行里可能已经有 webhook key、代理账号密码或 provider 回包。
    这些行是历史事实, **不能**在读取时原地改写/删除(那是改数据, 也撤不回来),
    所以出口处按**形态白名单**判定: 认不出的文本替换成
    「历史推送失败（内部错误）；参考号: ERR-XXXXXXXX」。

    代价说清楚: 历史行里那条真正的失败原因在页面上变成一句兜底文案 —— 这是
    有意的取舍(原文可能就是把凭据打进了通知中心), 排障请看服务端日志。
    """
    raw = str(text or "").strip()
    if not raw:
        return ""
    if looks_public_error(raw) or _looks_benign_historic(raw):
        return raw[:400]
    return failure_text(
        "internal", action=action, ref=stable_error_ref(raw)
    )[:400]


def public_channel_list(records) -> list[dict]:
    """把落库的 `push_channels` 收敛成可对外的列表(只改出口形态, 不改数据库)。

    历史行除了 `error` 之外, `status` / `type` / 时间戳 / `receipt` 也可能被写成
    任意文本(旧版直接把 provider 回执/异常上下文塞进来)。这里全部按**严格枚举/
    形态白名单**收敛: 认不出的值置空或丢弃, 绝不把原值透给浏览器。
    """
    out: list[dict] = []
    for item in records or []:
        if not isinstance(item, dict):
            continue
        entry: dict = {}
        for key in _PUBLIC_CHANNEL_KEYS:
            if key not in item:
                continue
            value = item[key]
            if key == "error":
                entry["error"] = public_saved_error(value)
            elif key == "status":
                entry["status"] = _safe_channel_status(value)
            elif key == "type":
                raw = str(value or "").strip()
                entry["type"] = raw if _CHANNEL_TYPE_RE.match(raw) else ""
            elif key in ("delivered_at", "failed_at"):
                entry[key] = _safe_timestamp(value)
            elif key == "id":
                if isinstance(value, int) and not isinstance(value, bool):
                    entry["id"] = value
                elif isinstance(value, str) and value.isdigit():
                    entry["id"] = int(value)
                else:
                    entry["id"] = 0
            elif key == "success":
                if isinstance(value, bool):
                    entry["success"] = value
            elif key == "receipt":
                entry["receipt"] = _safe_receipt(value)
            else:
                entry[key] = str(value or "")[:_MAX_CHANNEL_TEXT]
        out.append(entry)
    return out


__all__ = [
    "CATEGORY_LABELS",
    "DEFAULT_CATEGORY",
    "PUBLIC_PUSH_STATUS",
    "TARGET_BLOCKED_CATEGORY",
    "NotifyFailure",
    "as_failure",
    "classify",
    "failure_text",
    "label_for",
    "looks_public_error",
    "public_channel_list",
    "public_failure_text",
    "public_push_status",
    "public_saved_error",
    "stable_error_ref",
]
