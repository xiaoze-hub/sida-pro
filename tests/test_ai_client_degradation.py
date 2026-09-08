"""0.3(2026-09-08): LLM 降级必须抛 LLMDegradedError, 不再返回伪装成内容的字符串。

mock SDK 抛 429 / 超时 / 连接错误三类, 断言 chat / chat_multi / chat_with_tools
全部抛 LLMDegradedError(限流提示曾被当日报推给老板的根因整改, 风险方案 0.3)。
"""
from types import SimpleNamespace

import httpx
import pytest

from src.core import ai_client as ai_mod
from src.core.ai_client import AIClient, LLMDegradedError


class _Fake429(Exception):
    status_code = 429


class _Fake500(Exception):
    status_code = 503


def _make_client(scene: str) -> AIClient:
    # scene 唯一, 避免复用进程级令牌桶影响断言
    return AIClient(
        base_url="http://mock.local/v1",
        api_key="sk-test-not-real",
        model="mock-model",
        scene=scene,
    )


def _mock_sdk(monkeypatch, client: AIClient, exc: Exception):
    """把 AsyncOpenAI 客户端整体替换为直接抛 exc 的桩。"""

    async def _create(**kwargs):
        raise exc

    monkeypatch.setattr(
        client,
        "client",
        SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=_create))
        ),
    )


@pytest.mark.parametrize(
    "exc",
    [
        _Fake429("rate limited"),
        _Fake500("service unavailable"),
        httpx.ReadTimeout("read timed out"),
        httpx.ConnectError("connection refused"),
    ],
    ids=["429", "503", "read-timeout", "connect-error"],
)
@pytest.mark.parametrize(
    "method,args",
    [
        ("chat", ("sys", "user")),
        ("chat_multi", ([{"role": "user", "content": "hi"}],)),
        ("chat_with_tools", ([{"role": "user", "content": "hi"}], [])),
    ],
)
async def test_all_methods_raise_typed_error_not_string(
    monkeypatch, exc, method, args
):
    """429/超时/连接错误 → LLMDegradedError, 绝不返回降级字符串。"""
    ai = _make_client(f"deg-{method}-{id(exc)}")
    _mock_sdk(monkeypatch, ai, exc)
    # 重试able错误跳过真实退避睡眠, 保持测试毫秒级
    monkeypatch.setattr(ai_mod, "_CB_RETRY_MAX", 0)

    with pytest.raises(LLMDegradedError) as ei:
        await getattr(ai, method)(*args)

    # 异常文本必须可区分于正常 LLM 内容
    assert ei.value.scene
    assert ei.value.reason
    assert not isinstance(ei.value, str)


async def test_bucket_exhausted_raises_without_sdk_call(monkeypatch):
    """令牌桶耗尽 → 直接抛 LLMDegradedError, 不触达 SDK。"""
    ai = _make_client("deg-bucket")
    called = {"n": 0}

    async def _create(**kwargs):
        called["n"] += 1
        return None

    monkeypatch.setattr(
        ai,
        "client",
        SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=_create))
        ),
    )
    # 真实耗尽令牌桶(默认 rate=10/min, scene 唯一从 0 开始), __slots__ 不可打桩
    while ai._cb.acquire():
        pass

    with pytest.raises(LLMDegradedError):
        await ai.chat("sys", "user")
    assert called["n"] == 0, "令牌桶耗尽时不应发起 SDK 调用"


async def test_chat_multi_logs_usage(monkeypatch):
    """0.3: chat_multi 此前从不记账, 现在必须调用 _log_usage。"""
    ai = _make_client("usage-multi")
    usage = SimpleNamespace(
        prompt_tokens=10, completion_tokens=5, total_tokens=15
    )
    msg = SimpleNamespace(content="ok")
    resp = SimpleNamespace(usage=usage, choices=[SimpleNamespace(message=msg)])

    async def _create(**kwargs):
        return resp

    monkeypatch.setattr(
        ai,
        "client",
        SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=_create))
        ),
    )
    logged = []
    monkeypatch.setattr(
        ai, "_log_usage",
        lambda scene, model, u, ms: logged.append((scene, model, u.total_tokens)),
    )

    await ai.chat_multi([{"role": "user", "content": "hi"}])

    assert logged == [(None, "mock-model", 15)]


class TestCostTrackerFailClosed:
    """0.3 验收: 预算查询失败必须保守拦截(exceeded=True), 除非显式 TA_BUDGET_FAIL_OPEN=1。"""

    def _break_db(self, monkeypatch):
        def _boom():
            raise RuntimeError("db down")

        monkeypatch.setattr(
            "src.agents.tradingagents.cost_tracker.SessionLocal", _boom
        )

    def test_db_failure_fails_closed(self, monkeypatch):
        from src.agents.tradingagents.cost_tracker import check_budget

        monkeypatch.delenv("TA_BUDGET_FAIL_OPEN", raising=False)
        self._break_db(monkeypatch)
        result = check_budget(monthly_budget_usd=10.0)
        assert result["exceeded"] is True
        assert "保守拦截" in result["reason"]

    def test_db_failure_with_env_switch_fails_open(self, monkeypatch):
        from src.agents.tradingagents.cost_tracker import check_budget

        monkeypatch.setenv("TA_BUDGET_FAIL_OPEN", "1")
        self._break_db(monkeypatch)
        result = check_budget(monthly_budget_usd=10.0)
        assert result["exceeded"] is False
        assert result["remaining"] == 10.0


class TestConstructorTimeout:
    def test_default_read_timeout(self):
        ai = _make_client(f"timeout-default-{id(self)}")
        timeout = ai.client.timeout
        assert timeout.read == 90.0
        assert timeout.connect == 10.0

    def test_env_override_clamped_to_upper_bound(self, monkeypatch):
        monkeypatch.setenv("LLM_READ_TIMEOUT", "1500")
        ai = _make_client(f"timeout-high-{id(self)}")
        assert ai.client.timeout.read == 300.0

    def test_env_override_clamped_to_lower_bound(self, monkeypatch):
        monkeypatch.setenv("LLM_READ_TIMEOUT", "1")
        ai = _make_client(f"timeout-low-{id(self)}")
        assert ai.client.timeout.read == 10.0

    def test_env_invalid_value_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("LLM_READ_TIMEOUT", "not-a-number")
        ai = _make_client(f"timeout-bad-{id(self)}")
        assert ai.client.timeout.read == 90.0
