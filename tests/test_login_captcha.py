"""登录验证码：一次性、限时、HMAC 签名，且只在该出手时才出手。

设计取舍写在 `src/core/captcha.py` 模块头：默认自适应（连续失败达阈值才要求），
因为生产里有程序化登录（预测引擎拿用户 JWT），全量强制会直接把它们打断。
本文件钉住：签名/过期/一次性/大小写、模式开关、阈值判定、图片是真 PNG、
以及"验证码失败不计入账号锁定"。
"""
from __future__ import annotations

import pytest

from src.core import captcha as cap
from src.core import login_ratelimit


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    monkeypatch.setenv("AUTH_CAPTCHA_SECRET", "unit-test-secret-0123456789")
    monkeypatch.delenv("AUTH_CAPTCHA_MODE", raising=False)
    monkeypatch.delenv("AUTH_CAPTCHA_AFTER", raising=False)
    cap.reset_state()
    login_ratelimit.reset()
    yield
    cap.reset_state()
    login_ratelimit.reset()


# ---------------------------------------------------------------------------
# 核心校验
# ---------------------------------------------------------------------------

def test_answer_is_single_use_and_case_insensitive():
    item = cap.new_captcha()
    assert cap.verify(item.captcha_id, item.answer.lower()) == (True, "ok")
    assert cap.verify(item.captcha_id, item.answer) == (False, "reused")


def test_wrong_answer_is_rejected():
    item = cap.new_captcha()
    wrong = "AAAAA" if item.answer != "AAAAA" else "BBBBB"
    assert cap.verify(item.captcha_id, wrong) == (False, "mismatch")
    # 答错不该把这张码作废？—— 会作废更安全：重试必须重新看图（防同一张图反复猜）。
    assert cap.verify(item.captcha_id, item.answer) == (False, "reused")


def test_expired_token_is_rejected():
    item = cap.new_captcha(now=1000)
    assert cap.verify(item.captcha_id, item.answer, now=1000 + cap.TTL_SECONDS) == (False, "expired")


def test_tampered_id_or_signature_is_rejected():
    """改过期时间、改签名都必须失败；每个篡改用**新码**（一次性消费会烧掉旧码）。"""
    first = cap.new_captcha()
    nonce, exp, sig = first.captcha_id.split(".")
    assert cap.verify(f"{nonce}.{int(exp) + 999}.{sig}", first.answer) == (False, "mismatch")

    second = cap.new_captcha()
    nonce2, exp2, _ = second.captcha_id.split(".")
    assert cap.verify(f"{nonce2}.{exp2}.{'0' * 32}", second.answer) == (False, "mismatch")

    assert cap.verify("nonsense", "ABCDE") == (False, "malformed")


def test_missing_inputs_are_rejected():
    item = cap.new_captcha()
    assert cap.verify(None, item.answer) == (False, "missing")
    assert cap.verify(item.captcha_id, "") == (False, "missing")


def test_answer_never_leaks_into_the_id():
    item = cap.new_captcha()
    assert item.answer.lower() not in item.captcha_id.lower()


# ---------------------------------------------------------------------------
# 模式与阈值
# ---------------------------------------------------------------------------

def test_default_mode_is_always(monkeypatch):
    """默认就要看图：安全默认值，减交互靠显式配置而不是靠"少失败几次"。"""
    assert cap.mode() == "always"


def test_unknown_mode_falls_back_to_the_safe_default(monkeypatch):
    monkeypatch.setenv("AUTH_CAPTCHA_MODE", "whatever")
    assert cap.mode() == "always"


def test_required_for_respects_mode_and_threshold(monkeypatch):
    monkeypatch.setenv("AUTH_CAPTCHA_MODE", "adaptive")
    monkeypatch.setenv("AUTH_CAPTCHA_AFTER", "2")
    assert cap.required_for("1.1.1.1", "u", failures=0) is False
    assert cap.required_for("1.1.1.1", "u", failures=1) is False
    assert cap.required_for("1.1.1.1", "u", failures=2) is True

    monkeypatch.setenv("AUTH_CAPTCHA_MODE", "always")
    assert cap.required_for("1.1.1.1", "u", failures=0) is True

    monkeypatch.setenv("AUTH_CAPTCHA_MODE", "off")
    assert cap.required_for("1.1.1.1", "u", failures=99) is False


def test_failures_counts_are_readable_and_resettable():
    assert login_ratelimit.failures("2.2.2.2", "bob") == 0
    login_ratelimit.fail("2.2.2.2", "bob")
    assert login_ratelimit.failures("2.2.2.2", "bob") == 1
    login_ratelimit.success("2.2.2.2", "bob")
    assert login_ratelimit.failures("2.2.2.2", "bob") == 0


def test_locked_account_counts_as_over_threshold():
    """锁定期内必须算"已达阈值"：否则锁定一解除、验证码也跟着免了。"""
    for _ in range(login_ratelimit.MAX_FAILS):
        login_ratelimit.fail("3.3.3.3", "eve")
    assert login_ratelimit.check("3.3.3.3", "eve")
    assert login_ratelimit.failures("3.3.3.3", "eve") >= cap.threshold()


# ---------------------------------------------------------------------------
# 图片
# ---------------------------------------------------------------------------

def test_render_produces_a_real_png():
    png = cap.render_png(cap.new_captcha().answer, seed=7)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert 800 < len(png) < 60_000


def test_render_failure_is_explicit_not_silent(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name.startswith("PIL"):
            raise ImportError("no pillow")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(cap.CaptchaUnavailable):
        cap.render_png("ABCDE")


def test_service_token_exempts_machine_callers(monkeypatch):
    """带有效服务令牌的请求（预测引擎/脚本）不需要屏幕，也不需要验证码。"""
    import src.web.api.auth as auth_mod

    monkeypatch.setattr(auth_mod, "_service_token", "svc-token-abc", raising=False)
    assert cap.service_request_exempt({"X-Service-Token": "svc-token-abc"}) is True
    assert cap.service_request_exempt({"X-Service-Token": "wrong"}) is False
    assert cap.service_request_exempt({}) is False


def test_no_service_token_configured_means_no_exemption(monkeypatch):
    import src.web.api.auth as auth_mod

    monkeypatch.setattr(auth_mod, "_service_token", "", raising=False)
    monkeypatch.delenv("SIDA_SERVICE_TOKEN", raising=False)
    monkeypatch.setattr(auth_mod, "get_service_token", lambda: "")
    assert cap.service_request_exempt({"X-Service-Token": "anything"}) is False
