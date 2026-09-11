"""三指标共振 AI 判定测试(2026-09-11, 老板"要接入ai分析, 给出是否共振")。

覆盖: 提示词拼装(缺数据如实标注) / LLM JSON 解析容错(脏输出→无法判定不编造) /
接口层(LLM 可用 → 结构化判定; LLM 异常 → 诚实降级保留规则判定)。
"""
from __future__ import annotations

import asyncio
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import src.core.resonance_ai as rai
import src.core.resonance_scan as rscan
import src.web.api.resonance as api

DETAIL = {
    "symbol": "300563",
    "available": True,
    "trade_date": "20260911",
    "trend": "G区间",
    "activity": 26.68,
    "level": "大牛",
    "fund_net": 2.3e8,
    "hits": [True, True, True],
    "resonance": True,
    "near": False,
    "level3": "强",
}


def test_build_user_content_marks_missing_not_guessed():
    txt = rai.build_user_content("600519", "贵州茅台", {**DETAIL, "fund_net": None, "activity": None})
    assert "资金(主力净流入): 无数据" in txt
    assert "强度(AI机构活跃度): 无数据" in txt
    txt2 = rai.build_user_content("300563", "神宇股份", DETAIL, [{"date": "20260910", "activity": 20.1}, {"date": "20260911", "activity": 26.68}])
    assert "+2.30亿" in txt2 and "0911:26.68" in txt2


def test_parse_ai_verdict_handles_dirty_output():
    ok = rai.parse_ai_verdict(
        '答案如下: {"resonance":"强共振","confidence":1.4,"summary":"趋势+强度+资金三对","reasons":["G区","活跃26.7"],"risks":[],"watch":["次日量能"],"missing":[]}'
    )
    assert ok["resonance"] == rai.VERDICT_STRONG
    assert ok["confidence"] == 1.0  # clamp
    assert ok["parse_error"] is False and ok["reasons"][0] == "G区"

    bad = rai.parse_ai_verdict("我觉得还行")  # 非 JSON → 无法判定, 不编造
    assert bad["resonance"] == rai.VERDICT_UNKNOWN and bad["parse_error"] is True

    weird = rai.parse_ai_verdict('{"resonance":"超级共振"}')  # 非约定枚举 → 无法判定
    assert weird["resonance"] == rai.VERDICT_UNKNOWN

    empty = rai.parse_ai_verdict(None)
    assert empty["resonance"] == rai.VERDICT_UNKNOWN


class _FakeAIClient:
    def __init__(self, content: str | None = None, boom: bool = False):
        self.content = content
        self.boom = boom
        self.last_system = ""

    async def chat(self, system: str, user: str, temperature: float = 0.2):  # noqa: ARG002
        self.last_system = system
        if self.boom:
            raise RuntimeError("llm down")
        return self.content


def _client(monkeypatch, ai: _FakeAIClient):
    import src.web.api.chat as chat_api

    monkeypatch.setattr(chat_api, "_get_ai_client", lambda db, model_id=None, user=None: ai)
    monkeypatch.setattr(rscan, "symbol_detail", lambda sym, days=60: {**DETAIL, "symbol": sym})
    monkeypatch.setattr(
        rscan, "activity_series", lambda sym, days=120: {"items": [{"date": "20260911", "activity": 26.68}]}
    )
    api._ai_cache.clear()
    app = FastAPI()
    app.include_router(api.router, prefix="/api/resonance")
    return TestClient(app)


def test_analyze_returns_structured_verdict_and_adds_compliance(monkeypatch):
    ai = _FakeAIClient(
        content='{"resonance":"强共振","confidence":0.8,"summary":"三指标对齐","reasons":["趋势G区","活跃度26.68"],"risks":["涨幅已大"],"watch":["次日承接"],"missing":[]}'
    )
    client = _client(monkeypatch, ai)
    r = client.post("/api/resonance/analyze/300563")
    assert r.status_code == 200
    d = r.json()
    assert d["available"] is True
    assert d["ai"]["resonance"] == "强共振" and d["ai"]["reasons"][0] == "趋势G区"
    assert d["rule"]["level3"] == "强"
    assert "不构成投资建议" in ai.last_system  # D3 合规护栏必须挂上


def test_analyze_degrades_honestly_when_llm_fails(monkeypatch):
    client = _client(monkeypatch, _FakeAIClient(boom=True))
    d = client.post("/api/resonance/analyze/300563").json()
    assert d["available"] is False and d["ai"] is None
    assert d["rule"]["level3"] == "强"  # 规则判定保留
    assert "AI 不可用" in d["reason"]


def test_analyze_rejects_bad_symbol(monkeypatch):
    client = _client(monkeypatch, _FakeAIClient(content="{}"))
    assert client.post("/api/resonance/analyze/abc").status_code == 400
