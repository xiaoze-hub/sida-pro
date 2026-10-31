"""登录验证码（图片识别）—— 自建、无外部服务、一次性。

## 关键取舍

**验证码在口令校验之前**：否则 `401/428` 本身就泄露"这个口令对不对"，等于白送一个
口令预言机。

**答案只存服务端**（DB 表 `captcha_challenges`，见 `src/web/api/captcha_store.py`）：
返回给客户端的 `captcha_id` 只提交 `nonce|exp` 的 HMAC 签名，**不提交答案**。把答案签进
token 会让脚本拿到 token 后离线枚举答案（5 位 × 30 字符 ≈ 2400 万次 HMAC ≈ 几秒钟），
"每次都要看图"就退化成"每次都要一点 CPU"。答案存服务端后，猜必须走服务器并受限速约束。

**机器身份豁免**：生产里预测引擎/脚本走 `/api/auth/login` 拿 JWT，它们没有屏幕。默认
`always`，但带有效 `X-Service-Token` 的请求豁免（仓库既有的机器身份）——判断落在 web 层
`captcha_store.service_request_exempt`，core 不反向依赖 web（B4.1 门禁）。
`AUTH_CAPTCHA_MODE=adaptive` 退化为"连续失败达阈值才要求"，`off` 关闭。

## 安全边界（不夸大）

图片是**人机区分**，不是加密：OCR 仍有成功率，真正的护栏仍是限速 + 失败锁定 + scrypt
口令哈希。验证码错误**不计入**账号失败次数（否则拿别人用户名刷码就能锁号）。
"""
from __future__ import annotations

import hashlib
import hmac
import io
import os
import random
import secrets
import time
from dataclasses import dataclass

CODE_LENGTH = 5
# 去掉 0/O/1/I/l 这类易混字符：用户看错一次就要重来，安全收益却为零。
ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
TTL_SECONDS = 180

_MODES = ("adaptive", "always", "off")


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


def required(*, failures: int) -> bool:
    """本次登录是否需要验证码。`off` 一律否；`always` 一律是；`adaptive` 看失败次数。"""
    current = mode()
    if current == "off":
        return False
    if current == "always":
        return True
    return failures >= threshold()


# ---------------------------------------------------------------------------
# 签名（只提交 nonce|exp，不提交答案）
# ---------------------------------------------------------------------------

def _secret() -> str:
    """签名密钥：`AUTH_CAPTCHA_SECRET` / `JWT_SECRET` 优先，否则复用库内 JWT secret。

    **不在这里缓存**：`get_jwt_secret()` 自己带缓存、且 `secret_rotation` 轮换时会清它；
    我们再存一份副本，轮换后老 worker 会一直用旧密钥（`always` 模式下 428 到重启为止）。
    库不可用时返回**一次性的临时密钥**（不落缓存）：该次验证码大概率验不过，用户刷新即可，
    绝不因为拿不到密钥就放行。
    """
    env = (os.getenv("AUTH_CAPTCHA_SECRET") or os.getenv("JWT_SECRET") or "").strip()
    if len(env.encode("utf-8")) >= 16:
        return env
    try:
        from src.core.auth_tokens import get_jwt_secret

        return get_jwt_secret()
    except Exception:  # noqa: BLE001 - 库不可用不该让登录接口 500
        return secrets.token_hex(32)


def _sign(nonce: str, exp: int) -> str:
    payload = f"captcha|{nonce}|{exp}".encode("utf-8")
    return hmac.new(_secret().encode("utf-8"), payload, hashlib.sha256).hexdigest()[:32]


def answer_digest(answer: str) -> str:
    """答案摘要（落库用）：库里不出现明文答案，比对用常量时间。"""
    material = ("answer|" + (answer or "").strip().upper()).encode("utf-8")
    return hmac.new(_secret().encode("utf-8"), material, hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class Captcha:
    captcha_id: str
    answer: str
    nonce: str
    expires_at: float  # epoch 秒


def new_captcha(*, now: float | None = None) -> Captcha:
    ts = time.time() if now is None else now
    answer = "".join(secrets.choice(ALPHABET) for _ in range(CODE_LENGTH))
    nonce = secrets.token_urlsafe(16)
    exp = int(ts + TTL_SECONDS)
    return Captcha(
        captcha_id=f"{nonce}.{exp}.{_sign(nonce, exp)}",
        answer=answer,
        nonce=nonce,
        expires_at=float(exp),
    )


def parse_token(captcha_id: str | None, *, now: float | None = None) -> tuple[str | None, str]:
    """校验 token 的**格式/签名/有效期**，返回 `(nonce, reason)`。

    只证明"这张码是我们发的、还没过期"；答案对不对由服务端存储比对（见 captcha_store）。
    """
    if not captcha_id:
        return None, "missing"
    parts = captcha_id.split(".")
    if len(parts) != 3:
        return None, "malformed"
    nonce, exp_raw, sig = parts
    try:
        exp = int(exp_raw)
    except ValueError:
        return None, "malformed"
    if exp <= (time.time() if now is None else now):
        return None, "expired"
    # compare_digest 对 str 只接受 ASCII；captcha_id 是用户可控输入，
    # 直接比 str 会被 `x.9999999999.验证码` 打成 500。统一转 bytes 比较。
    if not hmac.compare_digest(sig.encode("utf-8"), _sign(nonce, exp).encode("utf-8")):
        return None, "mismatch"
    return nonce, "ok"


# ---------------------------------------------------------------------------
# 图片渲染（Pillow）
# ---------------------------------------------------------------------------

_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
)


def _font(size: int):
    from PIL import ImageFont

    for path in _FONT_CANDIDATES:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    # 位图默认字体无视 size（≈11px），在 132x44 上再旋转/加噪基本看不清 ——
    # 生产镜像已有 DejaVu（实测），走到这里说明镜像缺字体：宁可报错也不要发废图。
    raise CaptchaUnavailable("缺少可用字体（dejavu/liberation/noto 都不存在）")


def render_png(answer: str, *, seed: int | None = None) -> bytes:
    """把答案渲染成 PNG（逐字旋转 + 干扰线 + 噪点）。失败抛 CaptchaUnavailable。"""
    try:
        from PIL import Image, ImageDraw
    except Exception as e:  # noqa: BLE001
        raise CaptchaUnavailable(f"Pillow 不可用: {e}") from e

    rnd = random.Random(seed)
    width, height = 132, 44
    try:
        font = _font(30)
        image = Image.new("RGB", (width, height), (247, 249, 252))
        draw = ImageDraw.Draw(image)
        step = width // (len(answer) + 1)
        # Pillow 9.1+ 把常量挪到 Image.Resampling；老版本仍只有 Image.BICUBIC。
        resample = getattr(getattr(Image, "Resampling", Image), "BICUBIC")
        for index, char in enumerate(answer):
            glyph = Image.new("RGBA", (36, 44), (0, 0, 0, 0))
            ImageDraw.Draw(glyph).text((4, 2), char, font=font, fill=(28, 42, 66, 255))
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
    except CaptchaUnavailable:
        raise
    except Exception as e:  # noqa: BLE001
        raise CaptchaUnavailable(f"验证码渲染失败: {e}") from e
