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


# ── 盘后批量判定(2026-09-11 老板"可以") ──────────────────────────────────────
def test_build_batch_content_formats_rows():
    txt = rai.build_batch_content(
        [
            {"symbol": "300563", "name": "神宇股份", "trend": "G区间", "activity": 26.68, "level": "大牛", "fund_net": 2.3e8, "level3": "强"},
            {"symbol": "600519", "name": "贵州茅台", "trend": "S信号", "activity": None, "level": None, "fund_net": None, "level3": "无"},
        ]
    )
    assert "300563|神宇股份|G区间|26.68(大牛)|+2.30亿|强" in txt
    assert "600519|贵州茅台|S信号|无数据(未知)|无数据|无" in txt


def test_parse_batch_verdicts_filters_and_tolerates():
    content = (
        "好的, 结果如下:\n"
        '[{"symbol":"300563","verdict":"强共振","confidence":0.9,"summary":"三对","risk":"涨幅大"},'
        '{"symbol":"999999","verdict":"强共振","confidence":0.9,"summary":"不在池内"},'
        '{"symbol":"603421","verdict":"超级共振","confidence":2,"summary":"非法枚举","risk":""}]'
    )
    out = rai.parse_batch_verdicts(content, ["300563", "603421"])
    assert [x["symbol"] for x in out] == ["300563", "603421"]  # 池外的 999999 被丢弃
    assert out[0]["verdict"] == "强共振" and out[0]["risk"] == "涨幅大"
    assert out[1]["verdict"] == rai.VERDICT_UNKNOWN and out[1]["confidence"] == 1.0
    assert rai.parse_batch_verdicts("没有 JSON", ["300563"]) == []


def test_run_daily_verdicts_upserts_and_is_idempotent(monkeypatch):
    """批量判定: 落库 + 幂等; LLM 失败只记账不抛。"""
    from sqlalchemy import create_engine, text as _t
    from sqlalchemy.pool import StaticPool

    import src.db.session as dbs
    from src.web.migrations import _m161_resonance_scan_table, _m162_resonance_ai_verdicts_table

    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    with eng.begin() as conn:
        _m161_resonance_scan_table(conn)
        _m162_resonance_ai_verdicts_table(conn)
        conn.execute(
            _t(
                "INSERT INTO resonance_scan (trade_date, symbol, name, trend, activity, level, fund_net, hits, resonance, near)"
                " VALUES ('20260911','300563','神宇股份','G区间',26.68,'大牛',2.3e8,3,1,0),"
                "        ('20260911','603421','鼎信通讯','G信号',25.17,'大牛',6.9e7,3,1,0)"
            )
        )
    monkeypatch.setattr(dbs, "engine", eng)
    from sqlalchemy.orm import sessionmaker

    monkeypatch.setattr(dbs, "SessionLocal", sessionmaker(bind=eng))  # 防误连真实库

    class _AI:
        async def chat(self, system, user, temperature=0.2):  # noqa: ARG002
            return '[{"symbol":"300563","verdict":"强共振","confidence":0.9,"summary":"三对","risk":"涨幅大"},{"symbol":"603421","verdict":"弱共振","confidence":0.6,"summary":"资金偏弱","risk":""}]'

    monkeypatch.setattr(rai, "_build_batch_client", lambda db=None: _AI())
    out = asyncio.run(rai.run_daily_verdicts(limit=10))
    assert out["ok"] is True and out["verdicts"] == 2 and out["calls"] == 1
    asyncio.run(rai.run_daily_verdicts(limit=10))  # 幂等重跑
    with eng.begin() as conn:
        n = conn.execute(_t("SELECT COUNT(*) FROM resonance_ai_verdicts")).scalar()
        v = conn.execute(_t("SELECT verdict FROM resonance_ai_verdicts WHERE symbol='300563'")).scalar()
    assert n == 2 and v == "强共振"

    class _Boom:
        async def chat(self, system, user, temperature=0.2):  # noqa: ARG002
            raise RuntimeError("llm down")

    monkeypatch.setattr(rai, "_build_batch_client", lambda db=None: _Boom())
    out2 = asyncio.run(rai.run_daily_verdicts(limit=10))
    assert out2["ok"] is False and out2["errors"] == 1  # 不抛, 如实记错
