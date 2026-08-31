"""微信数智分析BOT 双向对话 worker。

后台常驻协程: 对每个已绑定 wechat_ilink 渠道(iLink 微信账号)长轮询 getupdates,
收到用户消息 → 调 SIDA 对话助手(8000, 带数据工具)→ sendmessage 回复微信。

由 server.py lifespan 启动(容器内自运行, 零外部依赖)。
"""

import asyncio
import logging
import os
import time
from collections import deque

import httpx

from src.core import wechat_ilink

logger = logging.getLogger(__name__)

# 内部对话助手 API(PANWATCH_URL 默认 127.0.0.1:8000, 容器内即主服务)
PANWATCH_URL = os.getenv("PANWATCH_URL", "http://127.0.0.1:8000").rstrip("/")
POLL_INTERVAL = 1.0  # 两次长轮询之间的间隔(秒)
MAX_MSG_IDS = 500  # 每账号去重窗口
REPLY_TIMEOUT = 90.0  # AI 回复超时(工具调用可能较慢)
MAX_REPLY_LEN = 1500  # 微信回复截断长度


class _AccountState:
    """单账号轮询状态(内存)。"""

    def __init__(self, channel_id: int, user_id: str, cfg: dict):
        self.channel_id = channel_id
        self.user_id = user_id
        self.cfg = cfg
        self.sync_buf = ""
        self.seen: deque[str] = deque(maxlen=MAX_MSG_IDS)
        self.conversation_id: str | None = None
        self.typing_ticket: str | None = None
        self.initialized = False  # 首轮只建游标不回复(避免回复历史消息)


async def _ask_ai(
    user_text: str, conv_id: str | None, user_id: str, image_data: str | None = None
) -> tuple[str, str]:
    """调 SIDA 对话助手(容器内自产服务 token), 返回 (回复文本, conversation_id)。

    image_data: 可选图片 base64 data URL(多模态, 模型直接看图)。
    """
    from src.web.api.auth import create_token
    from src.web.database import SessionLocal
    from src.web.models import User

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            raise RuntimeError(f"用户不存在: {user_id}")
        token, _ = create_token(user)
    finally:
        db.close()

    headers = {"Authorization": f"Bearer {token}"}
    payload: dict = {"content": user_text}
    if image_data:
        payload["image_data"] = image_data
    async with httpx.AsyncClient(timeout=REPLY_TIMEOUT) as client:
        if not conv_id:
            r = await client.post(f"{PANWATCH_URL}/api/chat/conversations", json={}, headers=headers)
            r.raise_for_status()
            data = r.json()
            conv_id = str((data.get("data") or data).get("id") or "")
        if not conv_id:
            raise RuntimeError("对话会话创建失败")
        r = await client.post(
            f"{PANWATCH_URL}/api/chat/conversations/{conv_id}/messages",
            json=payload,
            headers=headers,
        )
        r.raise_for_status()
        data = r.json()
        inner = data.get("data") or data
        reply = str(
            inner.get("content") or inner.get("reply") or inner.get("message") or ""
        ).strip()
        return reply, conv_id


def _persist_cfg(channel_id: int, **fields):
    """把字段写回 notify_channels.config(供 notifier 推送复用最新 context_token)。"""
    try:
        from src.web.database import SessionLocal
        from src.web.models import NotifyChannel

        db = SessionLocal()
        try:
            row = db.query(NotifyChannel).filter(NotifyChannel.id == channel_id).first()
            if row:
                cfg = dict(row.config or {})
                cfg.update(fields)
                row.config = cfg
                db.commit()
        finally:
            db.close()
    except Exception as exc:
        logger.warning(f"渠道 {channel_id} config 持久化失败: {exc}")


def _load_accounts() -> list[tuple[int, str, dict]]:
    """读取所有启用的 wechat_ilink 渠道(扫码绑定的微信账号), 返回 (channel_id, user_id, config)。"""
    try:
        from src.web.database import SessionLocal
        from src.web.models import NotifyChannel

        db = SessionLocal()
        try:
            rows = (
                db.query(NotifyChannel)
                .filter(NotifyChannel.type == "wechat_ilink", NotifyChannel.enabled.is_(True))
                .all()
            )
            result = []
            for row in rows:
                cfg = dict(row.config or {})
                if cfg.get("token") and cfg.get("user_id"):
                    result.append((row.id, str(row.user_id or ""), cfg))
            return result
        finally:
            db.close()
    except Exception as exc:
        logger.warning(f"读取微信渠道失败: {exc}")
        return []


async def _account_loop(state: _AccountState):
    """单账号长轮询: 收消息 → AI 回复 → 回微信。"""
    account = {
        "token": state.cfg.get("token"),
        "base_url": state.cfg.get("base_url") or None,
    }
    peer = str(state.cfg.get("user_id") or "")
    if not account["token"] or not peer:
        return

    while True:
        try:
            updates = await wechat_ilink.get_updates(account, state.sync_buf)
            ret = updates.get("ret")
            errcode = updates.get("errcode")
            if ret not in (0, None) or errcode not in (0, None):
                logger.warning(
                    f"微信 getupdates 错误 ret={ret} errcode={errcode} {updates.get('errmsg')}"
                )
                await asyncio.sleep(5)
                continue

            new_buf = str(updates.get("get_updates_buf") or "")
            if new_buf:
                state.sync_buf = new_buf

            msgs = updates.get("msgs") or []
            for msg in msgs:
                from_id = str(msg.get("from_user_id") or "")
                if from_id != peer:
                    continue  # 只处理绑定用户自己的消息
                msg_id = str(msg.get("msg_id") or msg.get("client_id") or "") or f"{time.time()}"
                if msg_id in state.seen:
                    continue
                state.seen.append(msg_id)

                ctx = str(msg.get("context_token") or "").strip()
                if ctx:
                    state.cfg["context_token"] = ctx

                text, img_data = await _extract_text(msg, account)
                if not text:
                    continue
                if not state.initialized:
                    continue  # 首轮建游标, 不回复历史消息

                logger.info(f"微信收到用户消息: {text[:40]}")
                # 发送"正在输入"状态(微信侧显示, 需 typing_ticket)
                try:
                    if not state.typing_ticket:
                        cfg_resp = await wechat_ilink.get_config(
                            account, peer, ctx or state.cfg.get("context_token")
                        )
                        state.typing_ticket = str(cfg_resp.get("typing_ticket") or "") or None
                    if state.typing_ticket:
                        await wechat_ilink.send_typing(account, peer, state.typing_ticket, 1)
                except Exception as exc:
                    logger.debug(f"typing 状态发送失败(可忽略): {exc}")
                try:
                    reply, state.conversation_id = await _ask_ai(
                        text, state.conversation_id, state.user_id, img_data
                    )
                except Exception as exc:
                    logger.warning(f"AI 回复失败: {exc}")
                    reply = "🤖 数智分析BOT 暂时无法处理, 请稍后重试。"
                # 停止"正在输入"
                try:
                    if state.typing_ticket:
                        await wechat_ilink.send_typing(account, peer, state.typing_ticket, 2)
                except Exception as exc:
                    logger.debug(f"typing 停止发送失败(可忽略): {exc}")
                reply = reply[:MAX_REPLY_LEN]
                try:
                    await wechat_ilink.send_text(
                        account, peer, reply, context_token=ctx or state.cfg.get("context_token")
                    )
                except Exception as exc:
                    logger.warning(f"微信回复发送失败: {exc}")
                    _persist_cfg(state.channel_id, context_token=state.cfg.get("context_token", ""))
            state.initialized = True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(f"微信轮询异常: {exc}")
            await asyncio.sleep(POLL_INTERVAL)


def _extract_text_sync(msg: dict) -> str:
    """从 iLink 消息里提取文本(支持 text_item 与直接文本字段)。"""
    items = msg.get("item_list") or []
    texts = []
    for it in items:
        if isinstance(it, dict):
            t = it.get("text_item") or {}
            if isinstance(t, dict) and t.get("text"):
                texts.append(str(t["text"]))
    if texts:
        return "\n".join(texts)
    return str(msg.get("text") or "").strip()


async def _extract_text(msg: dict, account: dict) -> tuple[str, str | None]:
    """从 iLink 消息提取可读文本 + 图片 data URL(多模态)。

    支持:
      - type=1 文本: text_item.text
      - type=2 图片: 下载解密 → OCR 文本化 + 返回图片 base64 data URL(模型直接看图)
      - type=4 文件: 下载解密 → 按扩展名解析 → "[文件: <文件名>] 内容摘要"
      - type=3 语音: 暂不支持, 拼提示让 AI 回复用户改用文字
    任一媒体处理失败不影响其余 item(失败项降级为提示文本)。
    返回 (text, image_data_url|None)。
    """
    from src.core import media_utils

    items = msg.get("item_list") or []
    texts = []
    image_data: str | None = None
    for it in items:
        if not isinstance(it, dict):
            continue
        item_type = it.get("type")
        if item_type == wechat_ilink.ITEM_TEXT:
            t = it.get("text_item") or {}
            if isinstance(t, dict) and t.get("text"):
                texts.append(str(t["text"]))
        elif item_type == wechat_ilink.ITEM_IMAGE:
            try:
                data = await wechat_ilink.download_media(account, it)
                ocr_text, _saved = media_utils.image_to_text(data)
                # 多模态: 原始图片转 data URL 给模型看图(OCR 文本兜底)
                if image_data is None:
                    try:
                        import base64 as _b64

                        image_data = f"data:image/png;base64,{_b64.b64encode(data).decode('ascii')}"
                    except Exception:
                        image_data = None
                texts.append(f"[图片内容] {ocr_text}" if ocr_text else "[图片内容] (未能识别出文字)")
            except Exception as exc:
                logger.warning(f"图片消息处理失败: {exc}")
                texts.append("[图片内容] (下载/识别失败)")
        elif item_type == wechat_ilink.ITEM_FILE:
            file_name = str((it.get("file_item") or {}).get("file_name") or "文件")
            try:
                data = await wechat_ilink.download_media(account, it)
                saved = media_utils.save_bytes(data, file_name)
                summary = media_utils.file_to_text(saved)
                if summary:
                    texts.append(f"[文件: {file_name}] 内容摘要:\n{summary}")
                else:
                    texts.append(f"[文件: {file_name}] (无法解析内容, 仅收到文件名)")
            except Exception as exc:
                logger.warning(f"文件消息处理失败: {exc}")
                texts.append(f"[文件: {file_name}] (下载失败)")
        elif item_type == wechat_ilink.ITEM_VOICE:
            texts.append("[语音消息] 暂不支持语音消息, 请改用文字或图片。")
        # type=5 视频等其他类型暂忽略
    if texts:
        return "\n".join(texts), image_data
    return str(msg.get("text") or "").strip(), image_data


async def wechat_bot_worker():
    """主 worker: 加载账号并并发轮询。账号变化时自动增删协程。"""
    logger.info("微信数智分析BOT worker 启动")
    tasks: dict[int, asyncio.Task] = {}
    while True:
        accounts = _load_accounts()
        active_ids = {cid for cid, _, _ in accounts}
        # 新增账号
        for cid, uid, cfg in accounts:
            if cid not in tasks or tasks[cid].done():
                state = _AccountState(cid, uid, cfg)
                tasks[cid] = asyncio.create_task(_account_loop(state), name=f"wechat-bot-{cid}")
        # 移除已停用的账号
        for cid in list(tasks):
            if cid not in active_ids and not tasks[cid].done():
                tasks[cid].cancel()
                tasks.pop(cid, None)
        await asyncio.sleep(30)  # 每 30s 刷新账号列表
