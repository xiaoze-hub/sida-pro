import hashlib
import hmac
import json
import logging
import os
import re

import apprise
import asyncio
import httpx

logger = logging.getLogger(__name__)


def get_global_proxy() -> str:
    """获取全局 HTTP 代理设置"""
    try:
        from src.web.database import SessionLocal
        from src.web.models import AppSettings

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
                self._custom_channels.append((channel_type, config))
                self._channel_count += 1
                logger.info(f"注册自定义通知渠道: {channel_type}")
            else:
                raise ValueError(f"不支持的通知渠道: {channel_type}")
        except ValueError as e:
            logger.error(f"注册通知渠道失败: {e}")
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
                try:
                    success = await self._ap.async_notify(
                        title=title,
                        body=plain_content,
                        body_format=apprise.NotifyFormat.TEXT,
                        attach=attachments,
                    )
                    if success:
                        apprise_ok = True
                        channel_results.append({"type": "apprise", "success": True})
                        logger.info(f"Apprise 通知发送成功: {title}")
                        break
                    last_err = "Apprise 通知发送失败（可能是网络问题或配置错误）"
                    logger.error(f"{last_err}: {title}")
                except Exception as e:
                    last_err = f"Apprise 通知异常: {e}"
                    logger.error(last_err)
                if attempt < retry_attempts:
                    await _sleep_retry(attempt + 1)
            if not apprise_ok:
                channel_results.append({"type": "apprise", "success": False, "error": last_err})
                errors.append(last_err or "Apprise 通知发送失败")

        # 自定义渠道（根据渠道类型自动选择格式）
        for ch_type, config in self._custom_channels:
            ch_ok = False
            last_err = ""
            for attempt in range(0, retry_attempts + 1):
                try:
                    # 支持 Markdown 的渠道使用原始内容，否则使用纯文本
                    ch_content = (
                        content if ch_type in _MARKDOWN_CHANNELS else plain_content
                    )
                    receipt = await self._send_custom(ch_type, config, title, ch_content)
                    ch_ok = True
                    result_item = {"type": ch_type, "success": True}
                    if receipt:
                        result_item["receipt"] = receipt
                    channel_results.append(result_item)
                    break
                except Exception as e:
                    last_err = f"{ch_type} 发送失败: {e}"
                    logger.error(last_err)
                if attempt < retry_attempts:
                    await _sleep_retry(attempt + 1)
            if not ch_ok:
                channel_results.append({"type": ch_type, "success": False, "error": last_err})
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
        else:
            logger.warning(f"未知的自定义渠道类型: {ch_type}")

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
            transport = httpx.AsyncHTTPTransport(proxy=proxy)
            logger.debug(f"Telegram 使用代理: {proxy}")

        try:
            async with httpx.AsyncClient(transport=transport, timeout=30) as client:
                resp = await client.post(url, json=payload)
                data = resp.json()
                if not data.get("ok"):
                    raise RuntimeError(f"Telegram API 错误: {data.get('description')}")
                logger.info(f"Telegram 通知发送成功: {title}")
        except httpx.ConnectError as e:
            if proxy:
                raise RuntimeError(f"连接代理失败 ({proxy}): {e}")
            else:
                raise RuntimeError(f"无法连接 Telegram API（可能需要配置代理）: {e}")
        except httpx.TimeoutException:
            raise RuntimeError("请求超时（网络问题或代理配置错误）")
        except Exception as e:
            if (
                "ConnectError" in str(type(e).__name__)
                or "connection" in str(e).lower()
            ):
                if not proxy:
                    raise RuntimeError(f"网络连接失败，建议配置代理: {e}")
            raise

    async def _send_wecom(self, config: dict, title: str, content: str):
        """企业微信机器人 Webhook"""
        key = config.get("webhook_key", "")
        if not key:
            raise ValueError("企业微信需要 webhook_key")

        url = f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={key}"
        text = f"## {title}\n\n{content}" if title else content
        payload = {"msgtype": "markdown", "markdown": {"content": text}}

        async with httpx.AsyncClient() as client:
            resp = await client.post(url, json=payload, timeout=30)
            data = resp.json()
            if data.get("errcode") != 0:
                raise RuntimeError(f"企业微信发送失败: {data.get('errmsg')}")
            logger.info(f"企业微信通知发送成功: {title}")

    async def _send_hermes(self, config: dict, title: str, content: str):
        """Hermes 中转 webhook。

        用途: 企微群机器人 webhook 不可用时(管理员未开放/uaKey 过期),
        借道 Hermes 网关那条活着的企微长连发消息。
        Hermes 侧用 `--deliver-only` 直转, 零 LLM 开销。
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

        async with httpx.AsyncClient() as client:
            resp = await client.post(url, content=raw, headers=headers, timeout=30)
            if resp.status_code != 200:
                raise RuntimeError(
                    f"Hermes 中转失败: HTTP {resp.status_code} {resp.text[:200]}"
                )
            try:
                data = resp.json()
            except Exception:
                data = {}
            if data.get("status") not in (None, "delivered", "ok"):
                raise RuntimeError(f"Hermes 中转未送达: {data}")
            logger.info(f"Hermes 中转通知发送成功: {title}")

    async def _send_wechat_ilink(self, config: dict, title: str, content: str):
        """个人微信 iLink 直连发送(腾讯官方通道)。

        config 由扫码绑定写入: token/base_url/user_id/context_token。
        调 src.core.wechat_ilink.send_text -> 腾讯官方 iLink sendmessage。
        context_token 过期时自动 getupdates 刷新并重试一次。
        """
        token = str(config.get("token") or "").strip()
        base_url = str(config.get("base_url") or "").strip()
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
            logger.warning(f"微信 iLink 首次发送失败({exc}), 刷新 context_token 重试")
            try:
                updates = await wechat_ilink.get_updates(account)
                new_ctx = _extract_context_token(updates, wechat_user_id)
                if not new_ctx:
                    raise RuntimeError(f"未能获取会话 token: {exc}")
                resp = await wechat_ilink.send_text(
                    account, wechat_user_id, text, context_token=new_ctx
                )
                self._persist_context_token(config, new_ctx)
            except Exception as exc2:
                raise RuntimeError(f"微信 iLink 发送失败: {exc2}")
        message_id = str(resp.get("message_id") or "")[:128] if isinstance(resp, dict) else ""
        logger.info(f"个人微信(iLink)通知发送成功: {title}")
        return {"ok": True, "message_id": message_id}

    def _persist_context_token(self, config: dict, new_ctx: str):
        """把刷新后的 context_token 写回 DB(notify_channels.config)。"""
        try:
            from src.web.database import SessionLocal
            from src.web.models import NotifyChannel

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
            logger.warning(f"context_token 持久化失败: {exc}")

    async def _send_serverchan(self, config: dict, title: str, content: str):
        sendkey = config.get("sendkey", "")
        if not sendkey:
            raise ValueError("Server酱需要 sendkey")

        url = f"https://sctapi.ftqq.com/{sendkey}.send"
        payload = {"title": title or "通知", "desp": content}

        async with httpx.AsyncClient() as client:
            resp = await client.post(url, json=payload, timeout=30)
            data = resp.json()
            if data.get("code") != 0:
                raise RuntimeError(f"Server酱发送失败: {data.get('message')}")
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
        async with httpx.AsyncClient(transport=transport) as client:
            resp = await client.post(url, json=payload, timeout=30)
            resp.raise_for_status()
            try:
                data = resp.json()
            except Exception as exc:
                raise RuntimeError("PushPlus 返回了无效 JSON") from exc
            if str(data.get("code")) != "200":
                message = str(data.get("msg") or "未知错误")
                if message == "服务端验证错误":
                    message += "（可能是重复消息，或 token/topic 参数无效）"
                raise RuntimeError(f"PushPlus 发送失败: {message}")
            logger.info(f"PushPlus 通知发送成功: {title}")
            # data 为 PushPlus 消息流水号，可用于设置页确认 API 已接收。
            return {"accepted": True, "message_id": str(data.get("data") or "")[:128]}
