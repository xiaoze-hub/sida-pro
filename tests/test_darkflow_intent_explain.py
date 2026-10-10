"""AI 链路 P1(2026-10-10): 主力意图 AI 解读接口(懒触发)。

`build_intent_explain_response` 是接口的可测核心: 规则结论(compute_dark_flow) + AI
「为什么/置信度/方向」。本用例禁真网络 —— monkeypatch compute_dark_flow 与
explain_main_intent, 覆盖: 成功透传规则结论+AI / 数据不足不调 LLM / 取数失败显式 /
LLM 失败显式 / 非法代码 400。
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from src.web.api import darkflow


def _dark(**kw) -> dict:
    base = {"signal": "超大单净流入, 主力吸筹迹象", "data_status": "ok"}
    base.update(kw)
    return base


def test_available_returns_rule_signal_and_ai(monkeypatch):
    monkeypatch.setattr(darkflow, "compute_dark_flow", lambda sym: _dark())
    monkeypatch.setattr(
        "src.core.intent_explain.explain_main_intent",
        lambda dark, db=None: {"direction": "吸筹", "confidence": "高", "why": "超大单+5967万但大单-8433万"},
    )
    out = darkflow.build_intent_explain_response("002361")
    assert out["available"] is True
    assert out["rule_signal"] == "超大单净流入, 主力吸筹迹象"
    assert out["direction"] == "吸筹"
    assert out["confidence"] == "高"
    assert out["why"] == "超大单+5967万但大单-8433万"
    assert out["data_status"] == "ok"


def test_insufficient_does_not_call_llm(monkeypatch):
    called: list = []
    monkeypatch.setattr(darkflow, "compute_dark_flow", lambda sym: _dark(data_status="insufficient"))
    monkeypatch.setattr(
        "src.core.intent_explain.explain_main_intent",
        lambda dark, db=None: called.append(1),
    )
    out = darkflow.build_intent_explain_response("002361")
    assert out["available"] is False
    assert "不足" in out["reason"]
    assert out["rule_signal"] == "超大单净流入, 主力吸筹迹象"  # 规则结论仍透传
    assert out["direction"] is None
    assert called == []  # 数据不足不烧 token


def test_suspect_does_not_call_llm(monkeypatch):
    monkeypatch.setattr(darkflow, "compute_dark_flow", lambda sym: _dark(data_status="suspect"))
    monkeypatch.setattr(
        "src.core.intent_explain.explain_main_intent",
        lambda dark, db=None: pytest.fail("suspect 不应调 LLM"),
    )
    out = darkflow.build_intent_explain_response("002361")
    assert out["available"] is False
    assert "异常" in out["reason"]


def test_compute_none_returns_unavailable(monkeypatch):
    monkeypatch.setattr(darkflow, "compute_dark_flow", lambda sym: None)
    out = darkflow.build_intent_explain_response("002361")
    assert out["available"] is False
    assert out["reason"]
    assert out["direction"] is None and out["why"] is None


def test_compute_raises_is_contained(monkeypatch):
    def boom(sym):
        raise RuntimeError("network down")

    monkeypatch.setattr(darkflow, "compute_dark_flow", boom)
    out = darkflow.build_intent_explain_response("002361")
    assert out["available"] is False
    assert "获取失败" in out["reason"]


def test_llm_failure_explicit_not_fabricated(monkeypatch):
    monkeypatch.setattr(darkflow, "compute_dark_flow", lambda sym: _dark())
    monkeypatch.setattr("src.core.intent_explain.explain_main_intent", lambda dark, db=None: None)
    out = darkflow.build_intent_explain_response("002361")
    assert out["available"] is False
    assert out["direction"] is None  # 不编造方向
    assert out["rule_signal"] == "超大单净流入, 主力吸筹迹象"


def test_invalid_symbol_400():
    with pytest.raises(HTTPException) as ei:
        darkflow.build_intent_explain_response("abc")
    assert ei.value.status_code == 400
