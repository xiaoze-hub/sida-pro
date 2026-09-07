"""方向2回归: 决策合成(verdict 三态 + 缺数看看 + 端点不 500)。"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.core.decision import synthesize
from src.web.api import decision as decision_api


def test_synthesize_verdict_domain():
    for trend in ["G信号", "G区间", "S信号", "S区间", None]:
        out = synthesize(trend, 2.5, 1.0, 1e8, 0.5e8)
        assert out["verdict"] in ("动手", "看看", "别碰"), out
        assert out["reason"] and out["parts"]["trend"] == (trend or "无数据")


def test_synthesize_missing_is_kan():
    out = synthesize(None, None, None, None)
    assert out["verdict"] == "看看" and "信号不全" in out["reason"]
    out2 = synthesize("G信号", None, None, None)
    assert out2["verdict"] == "看看"  # 缺两项不开多


def test_endpoint_never_500(monkeypatch):
    import src.core.decision as core_mod
    import src.web.api.decision as mod

    def _boom(symbol, market="CN"):
        raise RuntimeError("nope")

    def _ok(symbol, market="CN"):
        return {"symbol": symbol, "verdict": "看看", "reason": "看看: 测试", "phase": "分歧",
                "row": 3, "parts": {}}

    app = FastAPI()
    app.include_router(mod.router, prefix="/d")
    c = TestClient(app, raise_server_exceptions=False)
    monkeypatch.setattr(core_mod, "decide", _boom)
    r = c.get("/d/600519")
    assert r.status_code == 500 and "决策计算失败" in r.text
    monkeypatch.setattr(core_mod, "decide", _ok)
    r = c.get("/d/600519")
    assert r.status_code == 200 and r.json()["verdict"] == "看看"
