"""登录验证码（图片识别）—— 自建、无外部服务、一次性。

## 为什么是"自适应"而不是无脑全量

生产里有**程序化登录**：预测引擎 `forecast_lib/sida_client.py` 用它拿用户 JWT，若干
脚本/探针也走 `/api/auth/login`。强制每次都验码会把这些链路直接打断（它们没有屏幕）。

所以默认 `always`，但**带有效 `X-Service-Token` 的请求豁免**（这是仓库里既有的机器身份）；
需要减少交互时可用 `AUTH_CAPTCHA_MODE=adaptive`（连续失败达阈值才要求）。暴力破解的每一次猜测都要先过图片识别，而正常用户与机器凭据一次成功
就不受影响。需要更严（例如对外演示）可设 `always`；排障可设 `off`。

## 安全边界（不夸大）

- 图片由服务端 Pillow 逐字旋转 + 干扰线/噪点生成，答对才发 token；token 为 HMAC 签名，
  **不落库**，绑定答案与过期时间，一次性消费。
- 它是**人机区分**，不是加密：OCR 有一定成功率，所以真正的护栏仍是限速 + 失败锁定 +
  scrypt 口令哈希。验证码只把"每秒几百次猜测"压到"每次都要看图"。
- 验证码错误**不计入**账号失败次数：否则攻击者可以拿别人的用户名刷验证码把号锁死。
"""
from __future__ import annotations

import hashlib
import hmac
import io
import math
import os
import random
import secrets
import threading
import time
from dataclasses import dataclass

CODE_LENGTH = 5
# 去掉 0/O/1/I/l 这类易混字符：用户看错一次就要重来，安全收益却为零。
ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
TTL_SECONDS = 180
MAX_CONSUMED = 4096

_MODES = ("adaptive", "always", "off")
_CONSUMED: dict[str, float] = {}
_CONSUMED_LOCK = threading.Lock()


class CaptchaUnavailable(RuntimeError):
    """图片生成不可用（缺 Pillow / 字体异常）：调用方应显式失败，不要静默降级成"永远通过"。"""


def mode() -> str:
    """默认 `always`：登录就要看图。机器身份走 `X-Service-Token` 豁免，不靠"少失败几次"。"""
    raw = (os.getenv("AUTH_CAPTCHA_MODE") or "").strip().lower()
    return raw if raw in _MODES else "always"


def threshold() -> int:
    try:
        value = int(os.getenv("AUTH_CAPTCHA_AFTER") or 2)
    except ValueError:
        return 2
    return max(1, min(value, 10))


_SECRET_CACHE: str | None = None


def _secret() -> str:
    """签名密钥：`AUTH_CAPTCHA_SECRET` / `JWT_SECRET` 优先，否则复用库内 JWT secret。

    库不可用（单测、启动早期）时退到**进程内随机**：验证码本身只活 180 秒且一次性，
    代价只是重启后未用的验证码作废（用户点一下刷新即可），绝不因为拿不到密钥就放行。
    """
    global _SECRET_CACHE
    env = (os.getenv("AUTH_CAPTCHA_SECRET") or os.getenv("JWT_SECRET") or "").strip()
    if len(env.encode("utf-8")) >= 16:
        return env
    if _SECRET_CACHE:
        return _SECRET_CACHE
    try:
        from src.core.auth_tokens import get_jwt_secret

        _SECRET_CACHE = get_jwt_secret()
    except Exception:  # noqa: BLE001 - 库不可用不该让登录接口 500
        _SECRET_CACHE = secrets.token_hex(32)
    return _SECRET_CACHE


def _sign(secret: str, nonce: str, exp: int, answer: str) -> str:
    payload = f"captcha|{nonce}|{exp}|{answer.strip().upper()}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()[:32]


def _prune(now: float) -> None:
    if len(_CONSUMED) <= MAX_CONSUMED:
        return
    for key, until in list(_CONSUMED.items()):
        if until <= now:
            _CONSUMED.pop(key, None)


def _consume(nonce: str, exp: float) -> bool:
    """标记一次性消费；已消费过返回 False。"""
    now = time.time()
    with _CONSUMED_LOCK:
        _prune(now)
        if nonce in _CONSUMED:
            return False
        _CONSUMED[nonce] = max(exp, now + TTL_SECONDS)
        return True


@dataclass(frozen=True)
class Captcha:
    captcha_id: str
    answer: str
    expires_at: float  # epoch 秒


def new_captcha(*, now: float | None = None) -> Captcha:
    ts = time.time() if now is None else now
    answer = "".join(secrets.choice(ALPHABET) for _ in range(CODE_LENGTH))
    nonce = secrets.token_urlsafe(9)
    exp = int(ts + TTL_SECONDS)
    captcha_id = f"{nonce}.{exp}.{_sign(_secret(), nonce, exp, answer)}"
    return Captcha(captcha_id=captcha_id, answer=answer, expires_at=float(exp))


def verify(captcha_id: str | None, answer: str | None, *, now: float | None = None) -> tuple[bool, str]:
    """校验一次性验证码。返回 `(是否通过, 原因码)`；原因码只用于日志/前端文案，不含答案。"""
    if not captcha_id or not answer:
        return False, "missing"
    parts = captcha_id.split(".")
    if len(parts) != 3:
        return False, "malformed"
    nonce, exp_raw, sig = parts
    try:
        exp = int(exp_raw)
    except ValueError:
        return False, "malformed"
    ts = time.time() if now is None else now
    if exp <= ts:
        return False, "expired"
    # 一次性消费放在**比对之前**：答错也把这张图烧掉，逼着重新看图。
    # 否则同一张图可以被反复猜（OCR/脚本 brute force 的口子）。
    if not _consume(nonce, exp):
        return False, "reused"
    expected = _sign(_secret(), nonce, exp, answer)
    if not hmac.compare_digest(sig, expected):
        return False, "mismatch"
    return True, "ok"


def reset_state() -> None:
    """测试用：清空一次性消费表。"""
    with _CONSUMED_LOCK:
        _CONSUMED.clear()


# ---------------------------------------------------------------------------
# 图片渲染（Pillow）
# ---------------------------------------------------------------------------

_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
)


def _font(size: int):
    from PIL import ImageFont

    for path in _FONT_CANDIDATES:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


def render_png(answer: str, *, seed: int | None = None) -> bytes:
    """把答案渲染成 PNG（逐字旋转 + 干扰线 + 噪点）。失败抛 CaptchaUnavailable。"""
    try:
        from PIL import Image, ImageDraw
    except Exception as e:  # noqa: BLE001
        raise CaptchaUnavailable(f"Pillow 不可用: {e}") from e

    rnd = random.Random(seed)
    width, height = 132, 44
    try:
        image = Image.new("RGB", (width, height), (247, 249, 252))
        draw = ImageDraw.Draw(image)
        font = _font(30)
        step = width // (len(answer) + 1)
        for index, char in enumerate(answer):
            glyph = Image.new("RGBA", (36, 44), (0, 0, 0, 0))
            ImageDraw.Draw(glyph).text((4, 2), char, font=font, fill=(28, 42, 66, 255))
            # Pillow 9.1+ 把常量挪到 Image.Resampling；老版本仍只有 Image.BICUBIC。
            resample = getattr(getattr(Image, "Resampling", Image), "BICUBIC")
            glyph = glyph.rotate(rnd.uniform(-18, 18), resample=resample, expand=False)
            image.paste(glyph, (8 + index * step, rnd.randint(0, 4)), glyph)
        for _ in range(2):
            pts = [(rnd.randint(0, width), rnd.randint(0, height)) for _ in range(4)]
            draw.line(pts, fill=(120, 140, 170), width=1)
        for _ in range(90):
            draw.point((rnd.randint(0, width - 1), rnd.randint(0, height - 1)),
                       fill=(150, 160, 180))
        buffer = io.BytesIO()
        image.save(buffer, format="PNG", optimize=True)
        return buffer.getvalue()
    except Exception as e:  # noqa: BLE001
        raise CaptchaUnavailable(f"验证码渲染失败: {e}") from e


def service_request_exempt(headers) -> bool:
    """带**有效服务令牌**的请求豁免验证码（预测引擎/内部脚本没有屏幕）。

    与 `auth.py` 的 `X-Service-Token` 同源同值，常量时间比较；令牌未配置则**没有**任何豁免。
    """
    try:
        from src.web.api.auth import SERVICE_TOKEN_HEADER, get_service_token

        expected = (get_service_token() or "").strip()
        supplied = str(headers.get(SERVICE_TOKEN_HEADER) or "").strip()
        return bool(expected and supplied) and hmac.compare_digest(supplied, expected)
    except Exception:  # noqa: BLE001 - 取不到令牌就不豁免
        return False


def required_for(ip: str, username: str, *, failures: int) -> bool:
    """该次登录是否需要验证码。`off` 一律否；`always` 一律是；`adaptive` 看失败次数。"""
    current = mode()
    if current == "off":
        return False
    if current == "always":
        return True
    return failures >= threshold()
