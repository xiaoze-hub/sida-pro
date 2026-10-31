"""验证码核心：签发/签名/有效期/渲染 —— 不含存储，也不含 web 依赖。

关键性质（都是审查逼出来的）：
- token 里**没有答案**：拿到 captcha_id 也无法离线枚举答案，只能走服务器；
- 一次性/过期/篡改由 `parse_token` + 存储层体现；
- 缺字体时**报错**，不拿 ~11px 的位图默认字体发废图。
"""
from __future__ import annotations

import pytest

from src.core import captcha as cap


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setenv("AUTH_CAPTCHA_SECRET", "unit-test-secret-0123456789")
    monkeypatch.delenv("AUTH_CAPTCHA_MODE", raising=False)
    monkeypatch.delenv("AUTH_CAPTCHA_AFTER", raising=False)


# ---------------------------------------------------------------------------
# 模式与阈值
# ---------------------------------------------------------------------------

def test_default_mode_is_always():
    """默认就要看图：安全默认值，减交互靠显式配置而不是"少失败几次"。"""
    assert cap.mode() == "always"
    assert cap.required(failures=0) is True


def test_unknown_mode_falls_back_to_safe_default(monkeypatch):
    monkeypatch.setenv("AUTH_CAPTCHA_MODE", "whatever")
    assert cap.mode() == "always"


def test_adaptive_mode_requires_captcha_after_threshold(monkeypatch):
    monkeypatch.setenv("AUTH_CAPTCHA_MODE", "adaptive")
    monkeypatch.setenv("AUTH_CAPTCHA_AFTER", "2")
    assert cap.required(failures=0) is False
    assert cap.required(failures=1) is False
    assert cap.required(failures=2) is True


def test_off_mode_never_requires(monkeypatch):
    monkeypatch.setenv("AUTH_CAPTCHA_MODE", "off")
    assert cap.required(failures=99) is False


# ---------------------------------------------------------------------------
# token
# ---------------------------------------------------------------------------

def test_token_does_not_contain_the_answer():
    """把答案签进 token = 离线预言机（2400 万次 HMAC ≈ 几秒）。必须不签。"""
    item = cap.new_captcha()
    assert item.answer.lower() not in item.captcha_id.lower()
    assert cap.answer_digest(item.answer) not in item.captcha_id


def test_parse_token_accepts_fresh_and_rejects_expired():
    item = cap.new_captcha(now=1000)
    assert cap.parse_token(item.captcha_id, now=1000) == (item.nonce, "ok")
    assert cap.parse_token(item.captcha_id, now=1000 + cap.TTL_SECONDS) == (None, "expired")


def test_parse_token_rejects_tampering():
    item = cap.new_captcha()
    nonce, exp, sig = item.captcha_id.split(".")
    assert cap.parse_token(f"{nonce}.{int(exp) + 999}.{sig}") == (None, "mismatch")
    assert cap.parse_token(f"{nonce}.{exp}.{'0' * 32}") == (None, "mismatch")
    assert cap.parse_token("nonsense") == (None, "malformed")
    assert cap.parse_token(None) == (None, "missing")


def test_answer_digest_is_case_insensitive_and_not_plaintext():
    item = cap.new_captcha()
    assert cap.answer_digest(item.answer.lower()) == cap.answer_digest(item.answer)
    assert item.answer not in cap.answer_digest(item.answer)


def test_nonces_are_unique():
    assert len({cap.new_captcha().nonce for _ in range(50)}) == 50


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------

def test_render_produces_a_real_png():
    png = cap.render_png(cap.new_captcha().answer, seed=7)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert 800 < len(png) < 60_000


def test_render_failure_is_explicit_not_silent(monkeypatch):
    """缺 Pillow / 缺字体都要显式失败：发一张看不清的图比不发更糟。"""
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name.startswith("PIL"):
            raise ImportError("no pillow")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(cap.CaptchaUnavailable):
        cap.render_png("ABCDE")


def test_missing_fonts_raise_instead_of_bitmap_fallback(monkeypatch):
    import src.core.captcha as mod

    monkeypatch.setattr(mod, "_FONT_CANDIDATES", ("/nonexistent/font.ttf",))
    with pytest.raises(cap.CaptchaUnavailable):
        cap.render_png("ABCDE")
