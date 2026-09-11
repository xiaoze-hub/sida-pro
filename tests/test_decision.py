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


def test_decide_fund_net_reads_dict_field(monkeypatch):
    """回归(2026-09-11 老板报"资金不显示"): compute_pool_flow 返回 **dict**,
    decide() 曾用属性访问 .main_net → AttributeError 被吞 → 资金恒 None →
    决策卡片永远"信号不全(缺失: 资金)"。此测试锁死 dict 取值路径。"""
    import src.core.ai_activity as ai_act
    import src.core.dark_pool_flow as dpf
    import src.core.decision as core_mod
    import src.core.gs_strategy as gs
    import src.collectors.kline_collector as kc

    class _K:
        def __init__(self) -> None:
            self.date = "2026-09-10"
            self.open = self.close = self.high = self.low = 10.0
            self.volume = 1000.0

    class _Collector:
        def __init__(self, _mc) -> None:
            pass

        def get_klines(self, _symbol, days=120):  # noqa: ARG002
            return [_K() for _ in range(30)]

    monkeypatch.setattr(kc, "KlineCollector", _Collector)
    monkeypatch.setattr(gs, "eval_gs", lambda bars: {"ok": True})
    monkeypatch.setattr(gs, "trend_label", lambda res: "G信号")
    monkeypatch.setattr(ai_act, "eval_activity", lambda bars: {"activity": 2.5})
    monkeypatch.setattr(dpf, "compute_pool_flow", lambda s: {"main_net": 12345.0, "coverage": "full"})

    out = core_mod.decide("002361")
    assert out["parts"]["fund_net"] == 12345.0, out
    assert "缺失: 资金" not in out["reason"]

    # 池流不可得(None) → 资金 None, 不编造
    monkeypatch.setattr(dpf, "compute_pool_flow", lambda s: None)
    out2 = core_mod.decide("002361")
    assert out2["parts"]["fund_net"] is None
