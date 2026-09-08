"""风险方案 0.0 回归: AI 裁判配置/结论只走主服务 HTTP, 异常一律 abstain。

- resolve_referee_model_cfg: referee 场景绑定命中 → 用绑定; 未绑定 → 回落
  设置页 forecast_llm_*(显式配置); 均不可得 / API 不可达 / 响应异常 →
  RefereeConfigUnavailable, 绝不回落硬编码 agnes。
- evaluate_prediction: 任何异常(无凭据/建会话失败/回复不可解析/配置不可得)
  → verdict=abstain, 绝不伪造成 confirm; abstain 不落 prediction_referee_evals。
- 评估 prompt 含口径裁决规则(优先采信 get_main_intent / 禁用 get_capital_flow
  直接下主力派发/吸筹结论)。
"""
from __future__ import annotations

import os
import sys
import tempfile

import pytest

_FX = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "forecast_lib"))
if _FX not in sys.path:
    sys.path.insert(0, _FX)

# 引擎自身历史库指向临时文件, 避免任何未打桩的落库路径误写真实数据
os.environ.setdefault(
    "FORECAST_DB_PATH",
    os.path.join(tempfile.gettempdir(), "test_ai_referee_http_forecast.db"),
)

import ai_referee  # noqa: E402


class _Resp:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


def _fake_requests(post_results):
    calls = []

    def _post(url, json=None, headers=None, timeout=None):
        calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        item = post_results[min(len(calls) - 1, len(post_results) - 1)]
        if isinstance(item, Exception):
            raise item
        return item

    import types

    return types.SimpleNamespace(post=_post), calls


@pytest.fixture()
def _http_env(monkeypatch):
    monkeypatch.setattr(ai_referee, "get_panwatch_url", lambda: "http://panwatch-test:8000")
    monkeypatch.setattr(ai_referee, "auth_headers", lambda: {"X-Service-Token": "t0"})
    recorded = []
    monkeypatch.setattr(
        ai_referee, "record_referee_eval", lambda *a, **k: recorded.append(a)
    )
    return recorded


# ── resolve_referee_model_cfg: 配置只经 HTTP, 无任何硬编码回落 ────────────────

def test_resolve_binding_hit(monkeypatch):
    monkeypatch.setattr(
        ai_referee.panwatch_client,
        "request_json",
        lambda *a, **k: {
            "code": 0,
            "data": {
                "referee": {
                    "ai_model_id": 7, "model": "m-ref", "name": "n",
                    "base_url": "https://x/v1", "api_key": "sk-ref",
                },
                "llm": {},
            },
        },
    )
    cfg = ai_referee.resolve_referee_model_cfg()
    assert cfg["ai_model_id"] == 7
    assert cfg["api_key"] == "sk-ref"
    assert cfg["base_url"] == "https://x/v1"


def test_resolve_fallback_to_forecast_llm(monkeypatch):
    """无 referee 绑定时回落设置页显式配置的 forecast_llm_*(非硬编码)。"""
    monkeypatch.setattr(
        ai_referee.panwatch_client,
        "request_json",
        lambda *a, **k: {
            "data": {
                "referee": None,
                "llm": {"base_url": "https://y/v1", "model": "m-llm", "api_key": "sk-llm"},
            }
        },
    )
    cfg = ai_referee.resolve_referee_model_cfg()
    assert cfg["api_key"] == "sk-llm"
    assert not cfg.get("ai_model_id")


def test_resolve_raises_when_nothing_configured(monkeypatch):
    monkeypatch.setattr(
        ai_referee.panwatch_client,
        "request_json",
        lambda *a, **k: {"data": {"referee": None, "llm": {}}},
    )
    with pytest.raises(ai_referee.RefereeConfigUnavailable):
        ai_referee.resolve_referee_model_cfg()


def test_resolve_raises_on_api_down(monkeypatch):
    def _boom(*a, **k):
        raise ConnectionError("connection refused")

    monkeypatch.setattr(ai_referee.panwatch_client, "request_json", _boom)
    with pytest.raises(ai_referee.RefereeConfigUnavailable, match="不可达"):
        ai_referee.resolve_referee_model_cfg()


def test_resolve_raises_on_non_dict(monkeypatch):
    monkeypatch.setattr(
        ai_referee.panwatch_client, "request_json", lambda *a, **k: "not-a-dict"
    )
    with pytest.raises(ai_referee.RefereeConfigUnavailable):
        ai_referee.resolve_referee_model_cfg()


# ── prompt 口径裁决规则 ────────────────────────────────────────────────────

def test_eval_message_arbitration_rules():
    msg = ai_referee._build_eval_message("600519", "贵州茅台", 1700.0, {}, "up", 1.0)
    assert "优先采信 get_main_intent" in msg
    assert "严禁用 get_capital_flow" in msg
    assert "abstain" in msg


# ── _parse_verdict: abstain 合法, 未知 verdict 拒绝 ─────────────────────────

def test_parse_verdict_accepts_abstain():
    v = ai_referee._parse_verdict(
        '```json\n{"verdict": "abstain", "direction": null, "reason": "缺资金面证据"}\n```'
    )
    assert v is not None
    assert v["verdict"] == "abstain"
    assert v["direction"] is None


def test_parse_verdict_rejects_unknown():
    assert ai_referee._parse_verdict('{"verdict": "maybe", "reason": "x"}') is None


# ── evaluate_prediction: 任何异常 → abstain, 绝不伪 confirm ─────────────────

def test_evaluate_abstain_on_network_error(_http_env, monkeypatch):
    fake, _calls = _fake_requests([ConnectionError("boom")])
    monkeypatch.setattr(ai_referee, "requests", fake)
    res = ai_referee.evaluate_prediction(
        "600519", "贵州茅台", 1700.0, {}, "up", 1.0,
        model_cfg={"base_url": "u", "api_key": "k", "model": "m"},
    )
    assert res["verdict"] == "abstain"
    assert res["direction"] is None
    assert "裁判不可用" in res["reason"]
    assert _http_env == []  # abstain 不落 prediction_referee_evals


def test_evaluate_abstain_when_no_credentials(_http_env, monkeypatch):
    monkeypatch.setattr(ai_referee, "auth_headers", lambda: {})
    fake, calls = _fake_requests([AssertionError("不该发请求")])
    monkeypatch.setattr(ai_referee, "requests", fake)
    res = ai_referee.evaluate_prediction(
        "600519", "贵州茅台", 1700.0, {}, "up", 1.0, model_cfg=None
    )
    assert res["verdict"] == "abstain"
    assert calls == []


def test_evaluate_abstain_when_config_unavailable(_http_env, monkeypatch):
    """model_cfg=None 且配置 API 拒答 → abstain, 不回落硬编码模型。"""
    def _deny(*a, **k):
        raise ai_referee.RefereeConfigUnavailable("referee 场景未绑定模型")

    monkeypatch.setattr(ai_referee.panwatch_client, "request_json", _deny)
    fake, calls = _fake_requests([AssertionError("不该发请求")])
    monkeypatch.setattr(ai_referee, "requests", fake)
    res = ai_referee.evaluate_prediction("600519", "贵州茅台", 1700.0, {}, "up", 1.0)
    assert res["verdict"] == "abstain"
    assert calls == []
    assert "裁判不可用" in res["reason"]


def test_evaluate_confirm_records_eval(_http_env, monkeypatch):
    fake, calls = _fake_requests([
        _Resp({"data": {"id": 42}}),
        _Resp({"data": {"content": '前缀 {"verdict": "confirm", "direction": null, "reason": "盘面支持"} 后缀'}}),
    ])
    monkeypatch.setattr(ai_referee, "requests", fake)
    res = ai_referee.evaluate_prediction(
        "600519", "贵州茅台", 1700.0, {}, "up", 1.0,
        model_cfg={"ai_model_id": 7, "base_url": "u", "api_key": "k", "model": "m"},
    )
    assert res["verdict"] == "confirm"
    assert res["conv_id"] == 42
    assert len(_http_env) == 1
    assert _http_env[0][2] == "confirm"  # record_referee_eval(run_id, symbol, verdict, ...)
    # 建会话请求带服务令牌与 ai_model_id
    assert calls[0]["headers"].get("X-Service-Token") == "t0"
    assert calls[0]["json"].get("ai_model_id") == 7
