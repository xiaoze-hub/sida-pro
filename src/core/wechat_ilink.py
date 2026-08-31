"""SIDA 个人微信 iLink 直连客户端(纯 Python, 腾讯官方通道)。

参考实现: Hermes weixin.py(腾讯官方 iLink Bot API)。
能力:
  - fetch_qr(): 获取扫码二维码(供设置页扫码绑定)
  - poll_qr(): 轮询扫码状态, 成功后返回账号凭证
  - send_text(): 向已建立会话的微信用户推送文本消息

凭证结构(存 notify_channels.config): {token, base_url, user_id}
"""
import base64
import json
import time
import uuid
from urllib.parse import quote, urlparse

import httpx
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

# ---- iLink 常量(腾讯官方 iLink 协议) ----
ILINK_BASE_URL = "https://ilinkai.weixin.qq.com"
ILINK_APP_ID = "bot"
CHANNEL_VERSION = "2.2.0"
ILINK_APP_CLIENT_VERSION = (2 << 16) | (2 << 8) | 0

# 媒体 CDN(下载/上传)。可用 account.cdn_base_url 覆盖(参考 Hermes 配置)。
WEIXIN_CDN_BASE_URL = "https://novac2c.cdn.weixin.qq.com/c2c"

EP_GET_BOT_QR = "ilink/bot/get_bot_qrcode"
EP_GET_QR_STATUS = "ilink/bot/get_qrcode_status"
EP_SEND_MESSAGE = "ilink/bot/sendmessage"
EP_GET_UPDATES = "ilink/bot/getupdates"
EP_GET_CONFIG = "ilink/bot/getconfig"
EP_SEND_TYPING = "ilink/bot/sendtyping"

# item_list 消息类型
ITEM_TEXT = 1
ITEM_IMAGE = 2
ITEM_VOICE = 3
ITEM_FILE = 4
ITEM_VIDEO = 5
MSG_TYPE_BOT = 2
MSG_STATE_FINISH = 2

TYPING_START = 1
TYPING_STOP = 2

API_TIMEOUT = 15.0

# 微信 CDN 域名白名单(full_url 直连时校验, 防 SSRF)
_WEIXIN_CDN_ALLOWLIST = frozenset(
    {
        "novac2c.cdn.weixin.qq.com",
        "ilinkai.weixin.qq.com",
        "wx.qlogo.cn",
        "thirdwx.qlogo.cn",
        "res.wx.qq.com",
        "mmbiz.qpic.cn",
        "mmbiz.qlogo.cn",
    }
)


# ---- 媒体下载 + AES-128-ECB 解密(参考 Hermes weixin.py 已实现并验证) ----
def _cdn_download_url(cdn_base_url: str, encrypted_query_param: str) -> str:
    """CDN 下载 URL: base/download?encrypted_query_param=<URL 编码>。"""
    return f"{cdn_base_url.rstrip('/')}/download?encrypted_query_param={quote(encrypted_query_param, safe='')}"


def _parse_aes_key(aes_key_b64: str) -> bytes:
    """解析媒体 AES key。

    - base64 解码后 16 字节 → 直接当 key
    - 32 字节且内容是 hex 文本 → bytes.fromhex
    """
    decoded = base64.b64decode(aes_key_b64)
    if len(decoded) == 16:
        return decoded
    if len(decoded) == 32:
        text = decoded.decode("ascii", errors="ignore")
        if text and all(ch in "0123456789abcdefABCDEF" for ch in text):
            return bytes.fromhex(text)
    raise ValueError(f"unexpected aes_key format ({len(decoded)} decoded bytes)")


def _aes128_ecb_decrypt(ciphertext: bytes, key: bytes) -> bytes:
    """AES-128-ECB 解密 + PKCS7 去填充(最后 1-16 字节是 pad 长度, 校验后剥离)。"""
    cipher = Cipher(algorithms.AES(key), modes.ECB(), backend=default_backend())
    decryptor = cipher.decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    if not padded:
        return padded
    pad_len = padded[-1]
    if 1 <= pad_len <= 16 and padded.endswith(bytes([pad_len]) * pad_len):
        return padded[:-pad_len]
    return padded


def _assert_weixin_cdn_url(url: str) -> None:
    """校验 full_url 指向微信 CDN 域名(防 SSRF)。"""
    try:
        parsed = urlparse(url)
        scheme = parsed.scheme.lower()
        host = parsed.hostname or ""
    except Exception as exc:
        raise ValueError(f"无法解析媒体 URL: {url!r}") from exc
    if scheme not in {"http", "https"}:
        raise ValueError(f"媒体 URL scheme 不允许: {scheme!r}(仅 http/https)")
    if host not in _WEIXIN_CDN_ALLOWLIST:
        raise ValueError(f"媒体 URL 域名不在微信 CDN 白名单: {host!r}(防 SSRF 拒绝下载)")


async def _download_bytes(url: str, timeout_seconds: float = 60.0) -> bytes:
    """下载原始字节(CDN 密文)。"""
    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.content


def _media_ref_and_key(item: dict) -> tuple[dict, str | None]:
    """按 item type 提取媒体引用(media dict)与 AES key。

    返回 (media, aes_key_b64):
      - 图片: image_item.aeskey 是 hex 字符串 → hex→bytes→base64 当 b64 key;
              否则用 image_item.media.aes_key
      - 文件/语音/视频: 对应 *_item.media.aes_key 直接是 b64(可能为空, 即未加密)
    """
    item_type = item.get("type")
    if item_type == ITEM_IMAGE:
        image_item = item.get("image_item") or {}
        media = image_item.get("media") or {}
        aes_key_b64 = None
        aeskey_hex = str(image_item.get("aeskey") or "").strip()
        if aeskey_hex:
            try:
                aes_key_b64 = base64.b64encode(bytes.fromhex(aeskey_hex)).decode("ascii")
            except ValueError:
                aes_key_b64 = None
        if not aes_key_b64:
            aes_key_b64 = media.get("aes_key") or None
        return media, aes_key_b64
    if item_type == ITEM_FILE:
        media = (item.get("file_item") or {}).get("media") or {}
        return media, (media.get("aes_key") or None)
    if item_type == ITEM_VOICE:
        media = (item.get("voice_item") or {}).get("media") or {}
        return media, (media.get("aes_key") or None)
    if item_type == ITEM_VIDEO:
        media = (item.get("video_item") or {}).get("media") or {}
        return media, (media.get("aes_key") or None)
    raise ValueError(f"download_media: 不支持的 item type={item_type}")


async def download_media(account: dict, item: dict) -> bytes:
    """从 iLink 消息 item 下载并解密媒体(图片/文件/语音/视频), 返回原始字节。

    account: {token, base_url, user_id, 可选 cdn_base_url}
    item: getupdates 返回的 msg.item_list 里的单个元素, 形如
      {"type": 2, "image_item": {"aeskey": "<hex>", "media": {"encrypt_query_param": "...", "aes_key": "...", "full_url": "..."}}}
      {"type": 4, "file_item": {"file_name": "a.xlsx", "media": {...}}}
      {"type": 3, "voice_item": {"media": {...}}}

    下载: 优先 encrypted_query_param 拼 CDN URL; 缺失则用 full_url(校验 CDN 域名)。
    解密: 有 aes key 时 AES-128-ECB(PKCS7); 无 key(明文媒体)原样返回。
    """
    media, aes_key_b64 = _media_ref_and_key(item)

    encrypted_query_param = (
        media.get("encrypt_query_param") or media.get("encrypted_query_param") or ""
    )
    full_url = media.get("full_url") or ""
    if encrypted_query_param:
        cdn_base = (account.get("cdn_base_url") or WEIXIN_CDN_BASE_URL).rstrip("/")
        url = _cdn_download_url(cdn_base, encrypted_query_param)
    elif full_url:
        _assert_weixin_cdn_url(full_url)
        url = full_url
    else:
        raise RuntimeError("媒体 item 既没有 encrypt_query_param 也没有 full_url")

    raw = await _download_bytes(url)
    if aes_key_b64:
        raw = _aes128_ecb_decrypt(raw, _parse_aes_key(aes_key_b64))
    return raw


def _random_wechat_uin() -> str:
    return str(uuid.uuid4().int % (10**10))


def _headers(token: str | None = None, body: str = "") -> dict:
    headers = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "X-WECHAT-UIN": _random_wechat_uin(),
        "iLink-App-Id": ILINK_APP_ID,
        "iLink-App-ClientVersion": str(ILINK_APP_CLIENT_VERSION),
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


async def fetch_qr(bot_type: str = "3") -> dict:
    """获取扫码二维码。返回 {qrcode, qrcode_url}(qrcode_url 为完整可扫链接)。"""
    async with httpx.AsyncClient(timeout=API_TIMEOUT) as client:
        resp = await client.get(
            f"{ILINK_BASE_URL}/{EP_GET_BOT_QR}?bot_type={bot_type}",
            headers=_headers(),
        )
        resp.raise_for_status()
        data = resp.json()
    qrcode = str(data.get("qrcode") or "")
    qrcode_url = str(data.get("qrcode_img_content") or "") or qrcode
    if not qrcode:
        raise RuntimeError(f"iLink 二维码响应缺少 qrcode: {data}")
    return {"qrcode": qrcode, "qrcode_url": qrcode_url}


async def poll_qr(qrcode: str) -> dict:
    """轮询扫码状态。

    返回:
      {"status": "wait" | "scaned" | "expired" | ...}
      {"status": "success", "account_id", "token", "base_url", "user_id"}  扫码确认后
    """
    async with httpx.AsyncClient(timeout=API_TIMEOUT) as client:
        resp = await client.get(
            f"{ILINK_BASE_URL}/{EP_GET_QR_STATUS}?qrcode={qrcode}",
            headers=_headers(),
        )
        resp.raise_for_status()
        data = resp.json()
    status = str(data.get("status") or "wait")
    if status == "confirmed":
        return {
            "status": "success",
            "account_id": str(data.get("ilink_bot_id") or ""),
            "token": str(data.get("bot_token") or ""),
            "base_url": str(data.get("baseurl") or ILINK_BASE_URL),
            "user_id": str(data.get("ilink_user_id") or ""),
        }
    return {"status": status}


async def send_text(account: dict, to: str, text: str, context_token: str | None = None) -> dict:
    """向指定微信用户推送文本消息。

    account: {token, base_url, user_id}
    to: 接收方 peer id(形如 xxx@im.wechat), 必须与 bot 建立过会话
    context_token: iLink 会话 token(来自 getupdates, 外发必须回显最新值)
    """
    if not text or not text.strip():
        raise ValueError("send_text: 消息内容不能为空")
    message = {
        "from_user_id": "",
        "to_user_id": to,
        "client_id": f"sida-{int(time.time() * 1000)}-{uuid.uuid4().hex[:6]}",
        "message_type": MSG_TYPE_BOT,
        "message_state": MSG_STATE_FINISH,
        "item_list": [{"type": ITEM_TEXT, "text_item": {"text": text}}],
    }
    if context_token:
        message["context_token"] = context_token
    body = json.dumps(
        {"msg": message, "base_info": {"channel_version": CHANNEL_VERSION}},
        ensure_ascii=False,
    )
    base_url = (account.get("base_url") or ILINK_BASE_URL).rstrip("/")
    async with httpx.AsyncClient(timeout=API_TIMEOUT) as client:
        resp = await client.post(
            f"{base_url}/{EP_SEND_MESSAGE}",
            content=body,
            headers=_headers(account.get("token"), body),
        )
        resp.raise_for_status()
        return resp.json()


async def get_updates(account: dict, sync_buf: str = "") -> dict:
    """拉取入站消息(iLink getupdates)。

    返回原始响应: {ret, msgs: [...], get_updates_buf, longpolling_timeout_ms}
    每条 msg 含 from_user_id / context_token 等 —— context_token 是外发推送的必需参数。
    """
    body = json.dumps(
        {"get_updates_buf": sync_buf or "", "base_info": {"channel_version": CHANNEL_VERSION}},
        ensure_ascii=False,
    )
    base_url = (account.get("base_url") or ILINK_BASE_URL).rstrip("/")
    async with httpx.AsyncClient(timeout=45.0) as client:
        resp = await client.post(
            f"{base_url}/{EP_GET_UPDATES}",
            content=body,
            headers=_headers(account.get("token"), body),
        )
        resp.raise_for_status()
        return resp.json()


async def get_config(account: dict, user_id: str, context_token: str | None = None) -> dict:
    """获取用户会话配置(含 typing_ticket, 用于发送'正在输入'状态)。"""
    payload: dict = {"ilink_user_id": user_id}
    if context_token:
        payload["context_token"] = context_token
    body = json.dumps(
        {**payload, "base_info": {"channel_version": CHANNEL_VERSION}},
        ensure_ascii=False,
    )
    base_url = (account.get("base_url") or ILINK_BASE_URL).rstrip("/")
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            f"{base_url}/{EP_GET_CONFIG}",
            content=body,
            headers=_headers(account.get("token"), body),
        )
        resp.raise_for_status()
        return resp.json()


async def send_typing(
    account: dict, user_id: str, typing_ticket: str, status: int = TYPING_START
) -> None:
    """发送'正在输入/停止输入'状态(TYPING_START=1 开始, TYPING_STOP=2 结束)。"""
    payload = {
        "ilink_user_id": user_id,
        "typing_ticket": typing_ticket,
        "status": status,
    }
    body = json.dumps(
        {**payload, "base_info": {"channel_version": CHANNEL_VERSION}},
        ensure_ascii=False,
    )
    base_url = (account.get("base_url") or ILINK_BASE_URL).rstrip("/")
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            f"{base_url}/{EP_SEND_TYPING}",
            content=body,
            headers=_headers(account.get("token"), body),
        )
        resp.raise_for_status()
