import hashlib
import hmac
import json
import logging
import os
import re
from datetime import datetime, timezone

import apprise
import asyncio
import httpx

from src.core.error_report import log_failure, new_error_ref
from src.core.net_guard import (
    TargetBlocked,
    TargetUnresolvable,
    guarded_request,
    validate_proxy_target,
    validate_target,
)
from src.core.notify_errors import (
    NotifyFailure,
    as_failure,
    classify,
)
from src.core.third_party_log_guard import (
    install_third_party_log_guard,
    third_party_log_scope,
)

logger = logging.getLogger(__name__)

# apprise / httpx 的日志正文里有渠道凭据(device_key、webhook key、bot token)和完整
# URL —— 它们会被 DBLogHandler 原样写进日志表。这里装的是 **LogRecord 层**的替换,
# 不是调级别: 见 src/core/third_party_log_guard.py 的说明。
install_third_party_log_guard()


def get_global_proxy() -> str:
    """获取全局 HTTP 代理设置"""
    try:
        from src.db.session import SessionLocal
        from src.db.models import AppSettings

        db = SessionLocal()
        try:
            setting = (
                db.query(AppSettings).filter(AppSettings.key == "http_proxy").first()
            )
            return setting.value if setting and setting.value else ""
        finally:
            db.close()
    except Exception:
        return ""


def sanitize_for_telegram(content: str) -> str:
    """清理内容以适配 Telegram（移除 HTML 和 Markdown 格式）"""
    # 移除 HTML 标签
    content = re.sub(r"</?table[^>]*>", "", content)
    content = re.sub(r"</?thead[^>]*>", "", content)
    content = re.sub(r"</?tbody[^>]*>", "", content)
    content = re.sub(r"</?tr[^>]*>", "\n", content)
    content = re.sub(r"</?th[^>]*>", " | ", content)
    content = re.sub(r"</?td[^>]*>", " | ", content)
    content = re.sub(r"</?div[^>]*>", "", content)
    content = re.sub(r"</?span[^>]*>", "", content)
    content = re.sub(r"</?p[^>]*>", "\n", content)
    content = re.sub(r"<br\s*/?>", "\n", content)

    # 移除 Markdown 格式
    # markdown 链接 [label](url) → "label url":Telegram 内联链接对 localhost/IP:端口 等
    # 非公网地址不渲染(标签退化成纯文本点不了),裸 URL 则会被自动识别为可点击,更稳。
    content = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r"\1 \2", content)
    content = re.sub(r"^#{1,6}\s*", "", content, flags=re.MULTILINE)  # 移除标题 #
    content = re.sub(r"\*\*(.+?)\*\*", r"\1", content)  # 移除粗体 **
    content = re.sub(r"\*(.+?)\*", r"\1", content)  # 移除斜体 *
    content = re.sub(r"__(.+?)__", r"\1", content)  # 移除粗体 __
    content = re.sub(r"_(.+?)_", r"\1", content)  # 移除斜体 _
    content = re.sub(r"~~(.+?)~~", r"\1", content)  # 移除删除线
    content = re.sub(r"`(.+?)`", r"\1", content)  # 移除行内代码
    content = re.sub(
        r"^\s*[-*+]\s+", "· ", content, flags=re.MULTILINE
    )  # 列表符号改为 ·
    content = re.sub(
        r"^\s*\d+\.\s+", "", content, flags=re.MULTILINE
    )  # 移除有序列表数字

    # 清理多余空白
    content = re.sub(r"\n\s*\n\s*\n", "\n\n", content)
    content = re.sub(r" +", " ", content)
    return content.strip()


# 渠道类型定义 (label + 表单字段)
CHANNEL_TYPES = {
    "telegram": {
        "label": "Telegram",
        "fields": ["bot_token", "chat_id", "proxy"],
    },
    "bark": {
        "label": "Bark",
        "fields": ["device_key", "server_url"],
    },
    "dingtalk": {
        "label": "钉钉机器人",
        "fields": [
            "token",
            "secret",
            "phones",
            "keyword",
        ],  # keyword 选填：安全设置为“关键字”时自动附加
    },
    "wecom": {
        "label": "企业微信机器人",
        "fields": ["webhook_key"],
    },
    "hermes": {
        "label": "Hermes 中转(企微/TG等)",
        "fields": ["webhook_url", "secret"],
    },
    "wechat_ilink": {
        "label": "个人微信(iLink)",
        "fields": [],
        "hint": "个人微信直通(腾讯官方 iLink 通道, 推送以「数智分析BOT」自称)。设置页扫码绑定即可, 无需手填。",
    },
    "lark": {
        "label": "飞书机器人",
        "fields": ["webhook_token"],
    },
    "serverchan": {
        "label": "Server酱",
        "fields": ["sendkey"],
    },
    "pushplus": {
        "label": "PushPlus",
        "fields": ["token", "topic"],
    },
    "discord": {
        "label": "Discord",
        "fields": ["webhook_id", "webhook_token"],
    },
    "pushover": {
        "label": "Pushover",
        "fields": ["user_key", "app_token"],
    },
}

# 通过 Apprise 支持的渠道类型（无代理配置时）
_APPRISE_TYPES = {"telegram", "bark", "dingtalk", "lark", "discord", "pushover"}

# 自定义实现的渠道类型（带代理或特殊需求）
_CUSTOM_IMPL_TYPES = {"wecom", "serverchan", "pushplus", "hermes", "wechat_ilink"}

_CUSTOM_REQUIRED_FIELDS = {
    "wecom": ("webhook_key",),
    "serverchan": ("sendkey",),
    "pushplus": ("token",),
    "hermes": ("webhook_url",),
    "wechat_ilink": (),  # 扫码绑定写入 token/base_url/user_id, 无需手填校验
}

# 支持 Markdown 的渠道（不需要 sanitize）
_MARKDOWN_CHANNELS = {"wecom", "serverchan", "pushplus", "dingtalk", "lark", "discord"}

# 不支持 Markdown 的渠道（需要 sanitize）
_PLAIN_TEXT_CHANNELS = {"telegram", "bark", "pushover"}


def build_apprise_url(channel_type: str, config: dict) -> str | None:
    """
    根据渠道类型和配置构建 Apprise URL

    Returns:
        Apprise URL 或 None（如果需要使用自定义方式发送，如带代理的 Telegram）
    """
    if channel_type == "telegram":
        bot_token = config.get("bot_token", "")
        chat_id = config.get("chat_id", "")
        if not bot_token or not chat_id:
            raise ValueError("Telegram 需要 bot_token 和 chat_id")
        # 如果配置了代理（渠道级或全局），返回 None，使用自定义方式发送
        proxy = config.get("proxy", "").strip() or get_global_proxy()
        if proxy:
            return None
        return f"tgram://{bot_token}/{chat_id}"

    elif channel_type == "bark":
        device_key = config.get("device_key", "")
        server_url = config.get("server_url", "").strip("/")
        if not device_key:
            raise ValueError("Bark 需要 device_key")
        if server_url:
            host = server_url.replace("https://", "").replace("http://", "")
            return f"bark://{host}/{device_key}/"
        return f"bark://{device_key}/"

    elif channel_type == "dingtalk":
        # Apprise 钉钉格式：
        # - 无加签：dingtalk://{access_token}/
        # - 加签：  dingtalk://{secret}@{access_token}/
        # - @手机号：在 URL 末尾追加 ?to=13800138000,13900139000
        token = (config.get("token") or "").strip()
        secret = (config.get("secret") or "").strip()
        phones = (config.get("phones") or "").strip()
        if not token:
            raise ValueError("钉钉需要 token")
        base = f"dingtalk://{secret}@{token}/" if secret else f"dingtalk://{token}/"
        if phones:
            # 仅保留数字和逗号
            phone_list = [
                re.sub(r"[^0-9]", "", p)
                for p in phones.split(",")
                if re.sub(r"[^0-9]", "", p)
            ]
            if phone_list:
                base += f"?to={','.join(phone_list)}"
        return base

    elif channel_type == "lark":
        webhook_token = config.get("webhook_token", "")
        if not webhook_token:
            raise ValueError("飞书需要 webhook_token")
        return f"lark://{webhook_token}/"

    elif channel_type == "discord":
        webhook_id = config.get("webhook_id", "")
        webhook_token = config.get("webhook_token", "")
        if not webhook_id or not webhook_token:
            raise ValueError("Discord 需要 webhook_id 和 webhook_token")
        return f"discord://{webhook_id}/{webhook_token}/"

    elif channel_type == "pushover":
        user_key = config.get("user_key", "")
        app_token = config.get("app_token", "")
        if not user_key or not app_token:
            raise ValueError("Pushover 需要 user_key 和 app_token")
        return f"pover://{user_key}@{app_token}/"

    else:
        raise ValueError(f"不支持的 Apprise 渠道类型: {channel_type}")


def _extract_context_token(updates: dict, user_id: str) -> str | None:
    """从 iLink getupdates 响应中提取指定用户的最新 context_token(最新优先)。"""
    msgs = updates.get("msgs") or []
    for m in reversed(msgs):
        if str(m.get("from_user_id") or "") == user_id:
            ctx = str(m.get("context_token") or "").strip()
            if ctx:
                return ctx
    return None


#: Hermes 网关明确"已送达/已接收"的状态词(全部小写比较, 有界枚举)。
_HERMES_OK_STATUS = frozenset({"delivered", "ok", "sent", "success", "accepted"})
#: Hermes 网关明确"没送到"的状态词。
_HERMES_BAD_STATUS = frozenset(
    {"failed", "error", "rejected", "undelivered", "not_delivered", "denied", "dropped"}
)

#: iLink 只应指向腾讯自己的域名; 可用环境变量追加自建/测试主机(白名单, 不按 IP 放量)。
ENV_ILINK_EXTRA_HOSTS = "SIDA_NOTIFY_ILINK_HOSTS"
_ILINK_HOST_SUFFIX = ".weixin.qq.com"


def _raise_for_http_status(resp, *, channel_type: str) -> None:
    """**HTTP 错误优先**: 非 2xx 先按协议失败归类, 再去解析正文。

    顺序很重要: 403 的 HTML 错误页如果先 `resp.json()`, 会被判成「响应无法解析」,
    运维就会去查渠道格式而不是凭证/权限。状态码是**有界数值**, 可以带进对外文案。
    """
    code = int(getattr(resp, "status_code", 0) or 0)
    if code < 400:
        return
    raise NotifyFailure(
        "auth_rejected" if code in (401, 403) else "http_rejected",
        channel_type=channel_type,
        status=code,
    )


def _hermes_delivery_state(resp) -> str:
    """Hermes 网关 200 回包的**可证状态**: `confirmed` / `rejected` / `unknown`。

    HTTP 200 **不等于送达**: 网关前面可能挂着登录页/反代错误页(HTML)、维护页或
    任何非 JSON 正文。旧实现 `except Exception: data = {}` 让非 JSON 200 直接落到
    `data.get("status") is None` 的"成功"分支 —— 于是"网关其实没转发"被写成 sent。
    现在的口径:

    - 正文不是 JSON / 不是对象 → `bad_response`(可证失败: 没有送达回执);
    - 明确的状态词/布尔 ack → confirmed 或 rejected;
    - 200 但**没有任何可识别的 ack** → `unknown`(上层写 `not_delivered`, 不虚报 sent)。
    """
    try:
        data = resp.json()
    except (ValueError, UnicodeDecodeError) as exc:  # JSONDecodeError 是 ValueError 子类
        raise NotifyFailure(
            "bad_response", channel_type="hermes", status=int(getattr(resp, "status_code", 0) or 0)
        ) from exc
    if not isinstance(data, dict):
        raise NotifyFailure("bad_response", channel_type="hermes")

    status = str(data.get("status") or "").strip().lower()
    if status in _HERMES_OK_STATUS:
        return "confirmed"
    if status in _HERMES_BAD_STATUS:
        return "rejected"
    for key in ("delivered", "ok", "success", "accepted"):
        value = data.get(key)
        if value is True:
            return "confirmed"
        if value is False:
            return "rejected"
    return "unknown"


def assert_ilink_base_url(base_url: str) -> str:
    """iLink `base_url` 的主机白名单(防把渠道改成打内网的跳板)。

    `base_url` 在扫码绑定时由腾讯返回, 但 `PUT /api/channels` 可以改写它 ——
    它是本仓**唯一**由第三方值决定、又能被用户覆盖的出站目标。iLink 的 HTTP 栈在
    `src.core.wechat_ilink` 内部(自己 new httpx client, 无法在此处固定 IP), 所以这里
    用**域名后缀白名单**而不是 IP 判定: 对固定域名不存在 rebinding 问题。
    合法自建/测试主机用 `SIDA_NOTIFY_ILINK_HOSTS`(逗号分隔)显式追加。
    """
    from urllib.parse import urlsplit

    raw = str(base_url or "").strip() or "https://ilinkai.weixin.qq.com"
    try:
        parsed = urlsplit(raw)
        host = (parsed.hostname or "").strip().lower()
        scheme = (parsed.scheme or "").lower()
    except ValueError as exc:
        raise TargetBlocked("bad_url") from exc
    if scheme not in ("http", "https") or not host:
        raise TargetBlocked("bad_scheme")
    if host.endswith(_ILINK_HOST_SUFFIX) or host == "weixin.qq.com":
        return raw
    extra = {
        item.strip().lower()
        for item in (os.environ.get(ENV_ILINK_EXTRA_HOSTS, "") or "").replace(";", ",").split(",")
        if item.strip()
    }
    if host in extra:
        return raw
    raise TargetBlocked("host_not_allowed")


def _validate_outbound_url(url: str) -> None:
    """配置期校验: **只为"危险目标"拦人**(`TargetBlocked`); DNS 解析失败放行。

    为什么 DNS 失败不拦: 配置期解析不到 ≠ 配置非法 —— 内网 split-DNS、域名刚建还没生效、
    解析器临时抖动都会命中, 拦下来等于"保存渠道看运气", 是典型误伤合法配置。真正的安全
    决策在**连接那一刻**(`guarded_request` 会再校验一次), 解析结果指向内网照样会被拦,
    所以这里放行不降低防护强度; 解析失败会在发送/测试时如实报 `dns_failed`
    (「目标域名解析失败」), 而不是谎称"配置无效"。
    """
    try:
        validate_target(url)
    except TargetUnresolvable:
        logger.debug("配置期目标暂不可解析, 留待发送时复核(不落地址)")
        return


def _bark_server_url(config: dict) -> str:
    """自建 Bark 服务器地址(空 = apprise 默认 api.day.app)。"""
    raw = str(config.get("server_url") or "").strip().rstrip("/")
    if not raw:
        return ""
    if not raw.lower().startswith(("http://", "https://")):
        raw = f"https://{raw}"
    return raw


class NotifierManager:
    """通知管理器: Apprise 渠道 + 自定义渠道"""

    def __init__(self, policy=None):
        self._ap = apprise.Apprise()
        self._custom_channels: list[tuple[str, dict]] = []
        self._channel_count = 0
        # 钉钉关键字（可选）：若群机器人启用“关键字”安全校验，则自动附加
        self._dingtalk_keywords: set[str] = set()
        self.policy = policy

    def add_channel(self, channel_type: str, config: dict) -> bool:
        """添加通知渠道，配置无效时显式报错。"""
        try:
            if channel_type == "bark" and _bark_server_url(config):
                # 自建 Bark: apprise 自己解析域名、自己发请求, 我们无法把连接固定到
                # 校验过的 IP(DNS rebinding 窗口就在 apprise 内部)。所以自建服务器
                # 改走本模块的直连实现(pinning + 协议校验), 协议与 apprise 一致
                # (POST {server}/push), 默认 api.day.app 仍走 apprise 不变。
                if not str(config.get("device_key") or "").strip():
                    raise ValueError("Bark 需要 device_key")
                _validate_outbound_url(f"{_bark_server_url(config)}/push")
                self._custom_channels.append((channel_type, config))
                self._channel_count += 1
                logger.info("注册自定义通知渠道: bark (自建服务器, 直连)")
                return True
            if channel_type in _APPRISE_TYPES:
                url = build_apprise_url(channel_type, config)
                if url is None:
                    # 需要自定义实现（如带代理的 Telegram）
                    self._custom_channels.append((channel_type, config))
                    self._channel_count += 1
                    logger.info(f"注册自定义通知渠道: {channel_type} (带代理)")
                elif self._ap.add(url):
                    self._channel_count += 1
                    logger.info(f"注册通知渠道: {channel_type}")
                else:
                    raise ValueError(f"{channel_type} 渠道 URL 无效")
                if channel_type == "dingtalk":
                    kw = (config.get("keyword") or "").strip()
                    if kw:
                        self._dingtalk_keywords.add(kw)
            elif channel_type in _CUSTOM_IMPL_TYPES:
                missing = [
                    key for key in _CUSTOM_REQUIRED_FIELDS[channel_type]
                    if not str(config.get(key, "")).strip()
                ]
                if missing:
                    raise ValueError(f"{channel_type} 缺少必填配置: {', '.join(missing)}")
                # 配置期就把"指向内网/元数据"的目标拦掉: 用户能在设置页立刻看到
                # blocked_target, 而不是等到半夜推送时才失败。
                if channel_type == "hermes":
                    _validate_outbound_url(str(config.get("webhook_url") or ""))
                elif channel_type == "wechat_ilink":
                    assert_ilink_base_url(str(config.get("base_url") or ""))
                self._custom_channels.append((channel_type, config))
                self._channel_count += 1
                logger.info(f"注册自定义通知渠道: {channel_type}")
            else:
                raise ValueError(f"不支持的通知渠道: {channel_type}")
        except ValueError as e:
            # 渠道配置里的 key/token 可能被拼进异常文本 → 只落关联号 + 类型名。
            log_failure(
                logger,
                "通知渠道注册",
                e,
                ref=new_error_ref(),
                context={"channel_type": str(channel_type or "")},
            )
            raise
        return True

    async def notify(self, title: str, content: str, images: list[str] | None = None):
        """向所有已注册渠道发送通知（忽略错误）"""
        await self.notify_with_result(title, content, images)

    async def notify_with_result(
        self,
        title: str,
        content: str,
        images: list[str] | None = None,
        *,
        bypass_quiet_hours: bool = False,
    ) -> dict:
        """向所有已注册渠道发送通知，返回结果"""
        if self._channel_count == 0:
            logger.warning("没有可用的通知渠道")
            return {"success": False, "error": "没有可用的通知渠道"}

        # Quiet hours
        try:
            if not bypass_quiet_hours and getattr(self, "policy", None):
                if self.policy.is_quiet_now():
                    logger.info("当前处于通知静默时段，跳过发送")
                    return {"success": False, "skipped": "quiet_hours"}
        except Exception:
            # do not block sends on policy errors
            pass

        # 准备纯文本版本（用于不支持 Markdown 的渠道）
        plain_content = sanitize_for_telegram(content)

        # 准备附件
        attachments = None
        if images:
            attachments = apprise.AppriseAttachment()
            for img_path in images:
                if img_path and os.path.exists(img_path):
                    attachments.add(img_path)

        errors = []
        # v2.1 §11.5: 每渠道独立 delivered_at (ISO) + channel_id, 用于送达回执 API
        sent_at = datetime.now(timezone.utc).isoformat()
        channel_results: list[dict] = []

        # 若配置了钉钉关键字，自动追加在内容末尾以通过“关键字”校验
        if self._dingtalk_keywords:
            suffix = " " + " ".join(sorted(self._dingtalk_keywords))
            if suffix.strip() not in plain_content:
                plain_content = (plain_content + "\n" + suffix).strip()
            if suffix.strip() not in content:
                content = (content + "\n" + suffix).strip()

        retry_attempts = 0
        backoff = 0.0
        try:
            if getattr(self, "policy", None):
                retry_attempts = max(0, int(self.policy.retry_attempts))
                backoff = float(self.policy.retry_backoff_seconds or 0.0)
        except Exception:
            retry_attempts = 0
            backoff = 0.0

        async def _sleep_retry(i: int):
            if backoff <= 0:
                return
            await asyncio.sleep(backoff * (2 ** max(0, i - 1)))

        # Apprise 渠道（使用纯文本，因为 Telegram 等不支持 Markdown）
        if len(self._ap) > 0:
            apprise_ok = False
            last_err = ""
            for attempt in range(0, retry_attempts + 1):
                attempt_ref = new_error_ref()
                try:
                    # apprise 的日志正文里带渠道 URL / bark device_key / bot token, 而
                    # apprise 不在传输层噪音名单里 → 这些 DEBUG 会进 DB 日志表。作用域
                    # 把本轮关联号绑上去, 正文由 third_party_log_guard 整体替换成
                    # 「级别 + 类别 + 关联号」。
                    with third_party_log_scope(ref=attempt_ref, category="apprise"):
                        success = await self._ap.async_notify(
                            title=title,
                            body=plain_content,
                            body_format=apprise.NotifyFormat.TEXT,
                            attach=attachments,
                        )
                    if success:
                        apprise_ok = True
                        # v2.1 §11.5: 每渠道带 delivered_at 时间戳
                        channel_results.append({"type": "apprise", "success": True, "delivered_at": sent_at})
                        logger.info(f"Apprise 通知发送成功: {title}")
                        break
                    # apprise 只回 bool: 没有异常可分类, 归"未确认送达", 但仍给
                    # 关联号 —— 否则前端只看到一句无法追查的失败。
                    failure = NotifyFailure(
                        "not_delivered",
                        channel_type="apprise",
                        action="Apprise 通知发送",
                        ref=attempt_ref,
                    )
                    last_err = failure.public_message
                    log_failure(
                        logger,
                        "Apprise 通知外发",
                        None,
                        ref=failure.ref,
                        error_type="NotDelivered",
                        context={"channel_type": "apprise"},
                    )
                except Exception as e:
                    # httpx/apprise 的异常文本里可能有 webhook URL 与 token。
                    failure = as_failure(
                        e,
                        channel_type="apprise",
                        action="Apprise 通知发送",
                        ref=attempt_ref,
                    )
                    last_err = failure.public_message
                    log_failure(
                        logger,
                        "Apprise 通知外发",
                        e,
                        ref=failure.ref,
                        context={"channel_type": "apprise", "category": failure.category},
                    )
                if attempt < retry_attempts:
                    await _sleep_retry(attempt + 1)
            if not apprise_ok:
                # 失败也带时间戳 (failed_at, 不算 delivered)
                channel_results.append({"type": "apprise", "success": False, "error": last_err, "failed_at": sent_at})
                errors.append(last_err or "Apprise 通知发送失败")

        # 自定义渠道（根据渠道类型自动选择格式）
        for ch_type, config in self._custom_channels:
            ch_ok = False
            last_err = ""
            for attempt in range(0, retry_attempts + 1):
                attempt_ref = new_error_ref()
                try:
                    # 支持 Markdown 的渠道使用原始内容，否则使用纯文本
                    ch_content = (
                        content if ch_type in _MARKDOWN_CHANNELS else plain_content
                    )
                    # 自定义渠道也走 httpx(其 INFO 日志带完整 URL: key=/bot<token>),
                    # 同样把正文替换成「类别 + 关联号」。
                    with third_party_log_scope(
                        ref=attempt_ref, category=str(ch_type or "custom")
                    ):
                        receipt = await self._send_custom(ch_type, config, title, ch_content)
                    ch_ok = True
                    # v2.1 §11.5: 每渠道带 delivered_at 时间戳 + channel_id 便于前端标识
                    result_item = {
                        "type": ch_type,
                        "success": True,
                        "delivered_at": datetime.now(timezone.utc).isoformat(),
                    }
                    if receipt:
                        result_item["receipt"] = receipt
                    channel_results.append(result_item)
                    break
                except Exception as e:
                    # 同上: 渠道 URL/token 就在异常链里, 对外只给类别 + 关联号。
                    failure = as_failure(
                        e,
                        channel_type=str(ch_type),
                        action=f"{ch_type} 通知发送",
                        ref=attempt_ref,
                    )
                    last_err = failure.public_message
                    log_failure(
                        logger,
                        "通知渠道外发",
                        e,
                        ref=failure.ref,
                        context={"channel_type": str(ch_type), "category": failure.category},
                    )
                if attempt < retry_attempts:
                    await _sleep_retry(attempt + 1)
            if not ch_ok:
                channel_results.append({"type": ch_type, "success": False, "error": last_err, "failed_at": sent_at})
                errors.append(last_err or f"{ch_type} 发送失败")

        if errors:
            return {"success": False, "error": "; ".join(errors), "channels": channel_results}
        return {"success": True, "channels": channel_results}

    async def _send_custom(self, ch_type: str, config: dict, title: str, content: str):
        """发送自定义渠道通知"""
        if ch_type == "telegram":
            await self._send_telegram(config, title, content)
        elif ch_type == "wecom":
            await self._send_wecom(config, title, content)
        elif ch_type == "serverchan":
            return await self._send_serverchan(config, title, content)
        elif ch_type == "pushplus":
            return await self._send_pushplus(config, title, content)
        elif ch_type == "hermes":
            await self._send_hermes(config, title, content)
        elif ch_type == "wechat_ilink":
            return await self._send_wechat_ilink(config, title, content)
        elif ch_type == "bark":
            return await self._send_bark(config, title, content)
        else:
            # 旧实现只 warning 就 return None, 上层把"没发"当成功写 sent —— 未知类型
            # 是**配置错误**, 必须失败(否则用户看到"已送达"但一条都没发)。
            raise ValueError(f"未知的自定义渠道类型: {ch_type}")

    async def _send_telegram(self, config: dict, title: str, content: str):
        """Telegram Bot API（支持代理）

        Telegram 老 Markdown 解析很脆弱:
        - 不认 `**粗体**`(只认 `*粗体*`),GitHub 风格会导致 Can't find end of entity
        - 不认 `### 标题`(把 # 当普通字符,但 ### 后面可能被截断)
        - 单条上限 4096 字符,超过会被截断破坏实体
        发送前做兼容性预处理 + 截断。
        """
        bot_token = config.get("bot_token", "")
        chat_id = config.get("chat_id", "")
        # 渠道级代理优先，否则使用全局代理
        proxy = config.get("proxy", "").strip() or get_global_proxy()

        if not bot_token or not chat_id:
            raise ValueError("Telegram 需要 bot_token 和 chat_id")

        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        # 用现成的 sanitize_for_telegram 把 markdown 完全剥成纯文本,
        # 避免 `**粗体**` / `## 标题` / 未闭合实体导致 Telegram parse 失败。
        # 标题外层手动加 `*...*` 让其加粗(Telegram 老 Markdown 只认单星号)。
        safe_title = sanitize_for_telegram(title) if title else ""
        safe_content = sanitize_for_telegram(content)
        text = f"*{safe_title}*\n\n{safe_content}" if safe_title else safe_content
        # Telegram 单条上限 4096,留点 buffer 给末尾提示
        if len(text) > 3900:
            # 正文末尾若带详情链接(经 sanitize 后已是裸 URL),直接截断会把它砍掉 →
            # 用户点不到。先抽出来,截断正文后再拼回末尾。
            link_m = re.search(r"(https?://[^\s)]+)\s*$", text)
            if link_m:
                notice = f"\n\n…内容过长已截断,完整报告 👉 {link_m.group(1)}"
            else:
                notice = "\n\n…内容过长已截断,完整报告请在 SIDA 查看"
            text = text[: 3900 - len(notice)].rstrip() + notice
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "Markdown",
        }

        # 配置代理
        transport = None
        if proxy:
            # 代理地址也是用户可写的(渠道 config.proxy): 它决定服务端 TCP 连去哪儿。
            # 按 URL 解析而不是 `startswith("http")` —— 旧写法让 socks5h:// 整条绕过校验。
            # 口径是 `no_metadata`: 本机/局域网的 clash、squid 是合法常态
            # (127.0.0.1:7890), 但指向云元数据段(169.254.0.0/16)从来不是正常配置。
            try:
                validate_proxy_target(proxy)
            except TargetUnresolvable as exc:
                # 代理域名解析不了 = 代理不可用, 不是"渠道配置无效"也不是"目标网络不可达"。
                raise NotifyFailure("proxy_failed", channel_type="telegram") from exc
            try:
                transport = httpx.AsyncHTTPTransport(proxy=proxy)
            except ImportError as exc:
                # socks 代理需要 socksio: 部署里没装 → 代理用不了(仍然是有界类别)。
                raise NotifyFailure("proxy_failed", channel_type="telegram") from exc
            # 代理 URL 里常带 `http://user:pass@host:port`, 不能整串进日志。
            logger.debug("Telegram 渠道使用代理发送(地址不落日志)")

        try:
            async with httpx.AsyncClient(transport=transport, timeout=30) as client:
                resp = await client.post(url, json=payload)
                _raise_for_http_status(resp, channel_type="telegram")
                data = resp.json()
                if not data.get("ok"):
                    # description 是 provider 原文(可能回显 bot token), 不复述。
                    code = data.get("error_code")
                    raise NotifyFailure(
                        "auth_rejected" if code == 401 else "http_rejected",
                        channel_type="telegram",
                        status=code if isinstance(code, int) else None,
                    )
                logger.info(f"Telegram 通知发送成功: {title}")
        except NotifyFailure:
            raise
        except httpx.ProxyError as e:
            raise NotifyFailure("proxy_failed", channel_type="telegram") from e
        except httpx.TimeoutException as e:
            raise NotifyFailure("timeout", channel_type="telegram") from e
        except httpx.ConnectError as e:
            # 走代理时连不上通常就是代理本身不可用, 但代理串不能出现在文案里。
            raise NotifyFailure(
                "proxy_failed" if proxy else "network_unreachable",
                channel_type="telegram",
            ) from e
        except Exception as e:
            raise NotifyFailure(classify(e), channel_type="telegram") from e

    async def _send_wecom(self, config: dict, title: str, content: str):
        """企业微信机器人 Webhook"""
        key = config.get("webhook_key", "")
        if not key:
            raise ValueError("企业微信需要 webhook_key")

        url = f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={key}"
        text = f"## {title}\n\n{content}" if title else content
        payload = {"msgtype": "markdown", "markdown": {"content": text}}

        # P1: 构造器统一 timeout=30, 防无默认超时挂死
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, json=payload, timeout=30)
            # 企微网关/反代出错时回 HTML 200/5xx: 先按协议判, 再解析 errcode。
            _raise_for_http_status(resp, channel_type="wecom")
            data = resp.json()
            if data.get("errcode") != 0:
                # errmsg 是企微原文, 有时会把 webhook key 回显出来 —— 不复述。
                code = data.get("errcode")
                raise NotifyFailure(
                    "auth_rejected" if code == 93000 else "http_rejected",
                    channel_type="wecom",
                    status=code if isinstance(code, int) else None,
                )
            logger.info(f"企业微信通知发送成功: {title}")

    async def _send_bark(self, config: dict, title: str, content: str):
        """自建 Bark 服务器(Bark v2 `POST /push`), 目标 IP 已校验并固定。

        默认 `api.day.app` 仍走 apprise(host 固定、非用户输入); 只有用户填了
        `server_url` 才走这里 —— 这样"任意 URL"这条输入不会经过 apprise 自己的
        DNS 解析(那是 rebinding 防护够不到的地方)。
        """
        device_key = str(config.get("device_key") or "").strip()
        if not device_key:
            raise ValueError("Bark 需要 device_key")
        server = _bark_server_url(config)
        if not server:
            raise ValueError("Bark 自建直连缺少 server_url")

        payload = {
            "device_key": device_key,
            "title": title or "通知",
            "body": content or "",
        }
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await guarded_request(
                client, "POST", f"{server}/push", json_body=payload
            )
        _raise_for_http_status(resp, channel_type="bark")
        try:
            data = resp.json()
        except (ValueError, UnicodeDecodeError) as exc:
            raise NotifyFailure("bad_response", channel_type="bark") from exc
        code = data.get("code") if isinstance(data, dict) else None
        if str(code) != "200":
            # Bark 用 code 表达业务结果(400 device key 无效 / 500 服务端错), 只带有界数值。
            raise NotifyFailure(
                "auth_rejected" if str(code) in ("400", "401", "403") else "http_rejected",
                channel_type="bark",
                status=int(code) if str(code).lstrip("-").isdigit() else None,
            )
        logger.info("Bark(自建)通知发送成功: %s", title)
        return {"accepted": True}

    async def _send_hermes(self, config: dict, title: str, content: str):
        """Hermes 中转 webhook。

        用途: 企微群机器人 webhook 不可用时(管理员未开放/uaKey 过期),
        借道 Hermes 网关那条活着的企微长连发消息。
        Hermes 侧用 `--deliver-only` 直转, 零 LLM 开销。

        **200 不等于送达**(2026-11-12 复审六): 网关前面可能挂着登录页、反代错误页、
        维护页 —— 那些都是 HTTP 200 + HTML。旧实现在 `resp.json()` 失败时把 `data`
        置空, 于是 `status` 缺失被当成成功, `push_status` 写 sent: 用户以为收到,
        实际一条没发。现在按协议判定, 区分**可证成功 / 可证失败 / 未知**,
        未知一律不写 sent(类别 `not_delivered`= "渠道未确认送达")。
        """
        url = (config.get("webhook_url") or "").strip()
        secret = (config.get("secret") or "").strip()
        if not url:
            raise ValueError("Hermes 中转需要 webhook_url")

        payload = {"title": title or "SIDA 通知", "body": content or ""}
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        headers = {"Content-Type": "application/json"}
        if secret:
            sig = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()
            headers["X-Hub-Signature-256"] = f"sha256={sig}"

        # P1: 构造器统一 timeout=30, 防无默认超时挂死
        async with httpx.AsyncClient(timeout=30) as client:
            # guarded_request: DNS 只解析一次, 校验过的 IP 就是连接的 IP(Host/SNI 保留
            # 原域名), 且不跟随重定向(302 → 内网是经典 SSRF 绕过)。
            resp = await guarded_request(
                client, "POST", url, headers=headers, content=raw
            )
        # 非 2xx 先按协议归类(401/403 → 凭证被拒), 再谈正文。
        _raise_for_http_status(resp, channel_type="hermes")
        state = _hermes_delivery_state(resp)
        if state == "rejected":
            raise NotifyFailure(
                "http_rejected",
                channel_type="hermes",
                status=int(getattr(resp, "status_code", 0) or 0),
            )
        if state != "confirmed":
            # 200 但没有任何可识别 ack: 不猜、不宣称送达。
            raise NotifyFailure("not_delivered", channel_type="hermes")
        logger.info("Hermes 中转通知发送成功: %s", title)
        return {"delivery": "confirmed"}

    async def _send_wechat_ilink(self, config: dict, title: str, content: str):
        """个人微信 iLink 直连发送(腾讯官方通道)。

        config 由扫码绑定写入: token/base_url/user_id/context_token。
        调 src.core.wechat_ilink.send_text -> 腾讯官方 iLink sendmessage。
        context_token 过期时自动 getupdates 刷新并重试一次。
        """
        token = str(config.get("token") or "").strip()
        # base_url 可被 PUT /api/channels 改写 → 必须过域名白名单, 否则这条渠道就是
        # 一个"向任意主机发 POST"的跳板。
        base_url = assert_ilink_base_url(str(config.get("base_url") or "").strip())
        wechat_user_id = str(config.get("user_id") or "").strip()
        context_token = str(config.get("context_token") or "").strip() or None
        if not token or not wechat_user_id:
            raise ValueError("微信渠道需要 token/user_id(请重新扫码绑定)")

        from src.core import wechat_ilink

        account = {"token": token, "base_url": base_url or None}
        # 消息以「数智分析BOT」自称(iLink bot 微信列表名由腾讯侧固定, 无法修改)
        text = f"【数智分析BOT】{title}\n{content}" if title else f"【数智分析BOT】{content}"

        try:
            resp = await wechat_ilink.send_text(
                account, wechat_user_id, text, context_token=context_token
            )
        except Exception as exc:
            # 会话 token 过期/失效 → getupdates 拉最新 context_token 重试一次
            # iLink 的异常文本带 token/接口地址; 首次失败仍可重试, 只记关联号。
            first_ref = new_error_ref()
            log_failure(
                logger,
                "个人微信(iLink)首次发送(将刷新会话token重试)",
                exc,
                ref=first_ref,
                context={"channel_type": "wechat_ilink"},
            )
            try:
                updates = await wechat_ilink.get_updates(account)
                new_ctx = _extract_context_token(updates, wechat_user_id)
                if not new_ctx:
                    raise NotifyFailure("not_delivered", channel_type="wechat_ilink")
                resp = await wechat_ilink.send_text(
                    account, wechat_user_id, text, context_token=new_ctx
                )
                self._persist_context_token(config, new_ctx)
            except Exception as exc2:
                failure = as_failure(
                    exc2, channel_type="wechat_ilink", action="个人微信(iLink)通知发送"
                )
                if failure is exc2:  # 已经是安全异常: 不自我链式, 避免环状 traceback
                    raise
                raise failure from exc2
        message_id = str(resp.get("message_id") or "")[:128] if isinstance(resp, dict) else ""
        logger.info(f"个人微信(iLink)通知发送成功: {title}")
        return {"ok": True, "message_id": message_id}

    def _persist_context_token(self, config: dict, new_ctx: str):
        """把刷新后的 context_token 写回 DB(notify_channels.config)。"""
        try:
            from src.db.session import SessionLocal
            from src.db.models import NotifyChannel

            db = SessionLocal()
            try:
                channel_id = config.get("channel_id") or config.get("id")
                if channel_id:
                    row = db.query(NotifyChannel).filter(NotifyChannel.id == channel_id).first()
                else:
                    row = (
                        db.query(NotifyChannel)
                        .filter(
                            NotifyChannel.type == "wechat_ilink",
                            NotifyChannel.config["user_id"].astext == config.get("user_id", ""),
                        )
                        .first()
                    )
                if row:
                    cfg = dict(row.config or {})
                    cfg["context_token"] = new_ctx
                    row.config = cfg
                    db.commit()
            finally:
                db.close()
        except Exception as exc:
            # 这里是 DB 写: 异常文本自带 SQL/绑定参数/DSN。
            log_failure(
                logger,
                "微信 context_token 持久化",
                exc,
                ref=new_error_ref(),
                context={"channel_type": "wechat_ilink"},
            )

    async def _send_serverchan(self, config: dict, title: str, content: str):
        sendkey = config.get("sendkey", "")
        if not sendkey:
            raise ValueError("Server酱需要 sendkey")

        url = f"https://sctapi.ftqq.com/{sendkey}.send"
        payload = {"title": title or "通知", "desp": content}

        # P1: 构造器统一 timeout=30, 防无默认超时挂死
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, json=payload, timeout=30)
            _raise_for_http_status(resp, channel_type="serverchan")
            data = resp.json()
            if data.get("code") != 0:
                # provider 原文带 sendkey 的风险, 不复述。
                raise NotifyFailure("http_rejected", channel_type="serverchan")
            logger.info(f"Server酱通知发送成功: {title}")

    async def _send_pushplus(self, config: dict, title: str, content: str):
        """PushPlus 推送"""
        token = str(config.get("token", "")).strip()
        if not token:
            raise ValueError("PushPlus 需要 token")

        url = "https://www.pushplus.plus/send"
        payload = {
            "token": token,
            "title": title or "通知",
            "content": content,
            "template": "markdown",
        }
        topic = str(config.get("topic", "")).strip()
        if topic:
            payload["topic"] = topic

        proxy = get_global_proxy()
        transport = httpx.AsyncHTTPTransport(proxy=proxy) if proxy else None
        # P1: 构造器统一 timeout=30, 防无默认超时挂死
        async with httpx.AsyncClient(transport=transport, timeout=30) as client:
            resp = await client.post(url, json=payload, timeout=30)
            resp.raise_for_status()
            try:
                data = resp.json()
            except Exception as exc:
                raise NotifyFailure("bad_response", channel_type="pushplus") from exc
            if str(data.get("code")) != "200":
                # msg 是 provider 原文(可能回显 token), 只保留有界数值码。
                code = data.get("code")
                raise NotifyFailure(
                    "auth_rejected" if str(code) in ("903", "401", "403") else "http_rejected",
                    channel_type="pushplus",
                    status=int(code) if str(code).lstrip("-").isdigit() else None,
                )
            logger.info(f"PushPlus 通知发送成功: {title}")
            # data 为 PushPlus 消息流水号，可用于设置页确认 API 已接收。
            return {"accepted": True, "message_id": str(data.get("data") or "")[:128]}
