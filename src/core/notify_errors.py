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

import re

import httpx

from src.core.error_report import new_error_ref

#: 固定失败类别 → 用户可读文案。**只增不改**: 已落库的旧 `push_error` 里可能出现
#: 这些中文, 前端和客服口径要对得上。
CATEGORY_LABELS: dict[str, str] = {
    "config_invalid": "渠道配置不完整或无效",
    "auth_rejected": "渠道凭证被拒绝",
    "network_unreachable": "网络连接失败",
    "proxy_failed": "代理不可用",
    "timeout": "请求超时",
    "http_rejected": "服务端拒绝该消息",
    "bad_response": "服务端响应无法解析",
    "db_unavailable": "数据库不可用",
    "not_delivered": "渠道未确认送达",
    "internal": "内部错误",
}

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
    """按异常**类型**映射到固定类别; 不读异常文本。"""
    if isinstance(exc, NotifyFailure):
        return exc.category

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
    """把任意异常收敛成 `NotifyFailure`(幂等: 已是本类型则原样返回)。"""
    if isinstance(exc, NotifyFailure) and ref is None:
        return exc
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
#: 内部枚举回执(如 quiet_hours): 没有正文, 本身就是有界标识。
_INTERNAL_ENUM_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


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
        _PUBLIC_ERROR_PART_RE.match(p) or _INTERNAL_ENUM_RE.match(p) for p in parts
    ):
        return raw[:400]
    return failure_text("internal", action=action)[:400]
