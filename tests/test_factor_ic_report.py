"""因子 IC 归因报告:纯函数 build/parse + 主入口 generate(LLM 层 mock)。"""

from __future__ import annotations

import json

import pytest

import src.core.factor_ic_report as fic


# --------------------------- 纯函数:parse_ic_report_reply ---------------------------

def _valid_reply() -> str:
    return json.dumps({
        "summary": "alpha 与 catalyst 因子在当前窗口有真实 alpha,质量因子偏弱,风险/拥挤惩罚有效。",
        "factor_assessment": [
            {"factor_code": "alpha_score", "assessment": "有效", "note": "IC 0.12 样本充足"},
            {"factor_code": "catalyst_score", "assessment": "有效", "note": "IC 0.08 样本充足"},
            {"factor_code": "quality_score", "assessment": "存疑", "note": "IC 0.03 接近失效"},
            {"factor_code": "risk_penalty", "assessment": "有效", "note": "负 IC 惩罚有效"},
            {"factor_code": "crowd_penalty", "assessment": "失效", "note": "IC 接近 0 惩罚失效"},
            {"factor_code": "final_score", "assessment": "有效", "note": "合成因子 IC 0.11"},
        ],
        "adjustment_suggestion": "alpha/catalyst 可小幅提权,quality 降权,crowd 惩罚考虑松绑。",
        "confidence": "中",
    }, ensure_ascii=False)


def test_parse_valid_json():
    out = fic.parse_ic_report_reply(_valid_reply())
    assert out is not None
    assert out["summary"]
    assert out["confidence"] == "中"
    assert out["adjustment_suggestion"]
    codes = [a["factor_code"] for a in out["factor_assessment"]]
    assert "alpha_score" in codes
    assert all(a["assessment"] in fic.ASSESSMENT_VALUES for a in out["factor_assessment"])


def test_parse_json_with_code_fence():
    reply = f"```json\n{_valid_reply()}\n```"
    out = fic.parse_ic_report_reply(reply)
    assert out is not None
    assert out["summary"]


def test_parse_invalid_json_returns_none():
    assert fic.parse_ic_report_reply("这不是 JSON") is None
    assert fic.parse_ic_report_reply("{not valid json") is None


def test_parse_none_and_empty_returns_none():
    assert fic.parse_ic_report_reply(None) is None
    assert fic.parse_ic_report_reply("") is None
    assert fic.parse_ic_report_reply("   ") is None


def test_parse_missing_required_fields_returns_none():
    assert fic.parse_ic_report_reply(json.dumps({"summary": "x"})) is None
    assert fic.parse_ic_report_reply(json.dumps({"factor_assessment": []})) is None
    # factor_assessment 全部非法 → 空 → None
    bad = json.dumps({
        "summary": "x",
        "factor_assessment": [{"factor_code": "a", "assessment": "乱写", "note": ""}],
    })
    assert fic.parse_ic_report_reply(bad) is None


def test_parse_truncates_lengths():
    reply = json.dumps({
        "summary": "长" * 300,
        "factor_assessment": [{"factor_code": "alpha_score", "assessment": "有效", "note": "长" * 100}],
        "adjustment_suggestion": "长" * 200,
        "confidence": "高",
    }, ensure_ascii=False)
    out = fic.parse_ic_report_reply(reply)
    assert out is not None
    assert len(out["summary"]) <= fic._SUMMARY_MAX
    assert len(out["factor_assessment"][0]["note"]) <= fic._NOTE_MAX
    assert len(out["adjustment_suggestion"]) <= fic._SUGGESTION_MAX


def test_parse_default_confidence_when_invalid():
    reply = json.dumps({
        "summary": "总评",
        "factor_assessment": [{"factor_code": "alpha_score", "assessment": "有效", "note": ""}],
        "confidence": "超高",
    }, ensure_ascii=False)
    out = fic.parse_ic_report_reply(reply)
    assert out is not None
    assert out["confidence"] == "中"


# --------------------------- 纯函数:build_ic_report_prompt ---------------------------

def test_build_prompt_contains_ic_ir_and_penalty_note():
    ic_result = {
        "horizon": 5, "days": 90, "market": "CN",
        "factors": {
            "alpha_score": {"ic": 0.1234, "ir": 0.6, "sample_size": 120, "ic_periods": 30},
            "risk_penalty": {"ic": -0.05, "ir": -0.3, "sample_size": 120, "ic_periods": 30},
            "crowd_penalty": {"ic": None, "ir": None, "sample_size": 0, "ic_periods": 0},
        },
    }
    system, user = fic.build_ic_report_prompt(ic_result)
    # 数值出现
    assert "0.1234" in user
    assert "-0.0500" in user
    assert "N/A" in user
    # 惩罚因子说明出现
    assert "惩罚因子" in user
    # system 含判断规则关键点
    assert "真实 alpha" in system
    assert "负 IC = 惩罚有效" in system or "惩罚有效" in system
    assert "严格 JSON" in system


def test_build_prompt_handles_empty_factors():
    system, user = fic.build_ic_report_prompt({"horizon": 5, "days": 90, "market": "CN", "factors": {}})
    assert system
    assert user


# --------------------------- 主入口:generate_factor_ic_report(LLM 层 mock) ---------------------------

class _FakeClient:
    def __init__(self, reply):
        self._reply = reply

    async def chat(self, system, user, temperature=None):
        return self._reply


def _ic_result_with_ic():
    return {
        "horizon": 5, "days": 90, "market": "CN",
        "factors": {
            "alpha_score": {"ic": 0.12, "ir": 0.6, "sample_size": 100, "ic_periods": 30},
            "risk_penalty": {"ic": -0.05, "ir": -0.3, "sample_size": 100, "ic_periods": 30},
        },
    }


def test_generate_empty_factors_returns_none_no_llm(monkeypatch):
    monkeypatch.setattr(
        fic, "evaluate_factor_ic",
        lambda *, market=None, db=None: {"horizon": 5, "days": 90, "market": market, "factors": {}},
    )
    called = {"n": 0}

    def _boom(db=None):
        called["n"] += 1
        raise AssertionError("LLM 不应被调用")

    monkeypatch.setattr(fic, "_build_client", _boom)
    assert fic.generate_factor_ic_report(market="CN") is None
    assert called["n"] == 0


def test_generate_all_ic_none_returns_none_no_llm(monkeypatch):
    monkeypatch.setattr(
        fic, "evaluate_factor_ic",
        lambda *, market=None, db=None: {
            "horizon": 5, "days": 90, "market": market,
            "factors": {
                "alpha_score": {"ic": None, "ir": None, "sample_size": 0, "ic_periods": 0},
                "risk_penalty": {"ic": None, "ir": None, "sample_size": 0, "ic_periods": 0},
            },
        },
    )
    called = {"n": 0}

    def _boom(db=None):
        called["n"] += 1
        raise AssertionError("LLM 不应被调用")

    monkeypatch.setattr(fic, "_build_client", _boom)
    assert fic.generate_factor_ic_report(market="CN") is None
    assert called["n"] == 0


def test_generate_llm_exception_returns_none(monkeypatch):
    monkeypatch.setattr(fic, "evaluate_factor_ic", lambda *, market=None, db=None: _ic_result_with_ic())

    class _RaisingClient:
        async def chat(self, system, user, temperature=None):
            raise RuntimeError("boom")

    monkeypatch.setattr(fic, "_build_client", lambda db=None: _RaisingClient())
    assert fic.generate_factor_ic_report(market="CN") is None


def test_generate_invalid_reply_returns_none(monkeypatch):
    monkeypatch.setattr(fic, "evaluate_factor_ic", lambda *, market=None, db=None: _ic_result_with_ic())
    monkeypatch.setattr(fic, "_build_client", lambda db=None: _FakeClient("不是 JSON"))
    assert fic.generate_factor_ic_report(market="CN") is None


def test_generate_success_returns_parsed_dict(monkeypatch):
    monkeypatch.setattr(fic, "evaluate_factor_ic", lambda *, market=None, db=None: _ic_result_with_ic())
    monkeypatch.setattr(fic, "_build_client", lambda db=None: _FakeClient(_valid_reply()))
    out = fic.generate_factor_ic_report(market="CN")
    assert out is not None
    assert out["summary"]
    assert out["confidence"] == "中"
    assert any(a["factor_code"] == "alpha_score" for a in out["factor_assessment"])


def test_generate_eval_exception_returns_none(monkeypatch):
    def _raise(*, market=None, db=None):
        raise RuntimeError("db down")

    monkeypatch.setattr(fic, "evaluate_factor_ic", _raise)
    assert fic.generate_factor_ic_report(market="CN") is None
