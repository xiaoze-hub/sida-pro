"""数智决策 P1-2(个性化) + P2-7(跨市场诚实降级) 回归。

覆盖:
- 向后兼容: 无 user_context 输出逐字段与旧版一致;
- 个性化: 注记显式、保守型阈值真实收紧、激进型放开拐点、持仓参考行(不改 verdict);
- 跨市场: HK/US 双维 verdict + basis 标注 + 『资金维无数据(非CN)』, 非 CN 不裸 400;
- user_id 隔离: 上下文仅含当前用户的自选/持仓。

禁真网络: 所有联网入口(collectors/gs/activity/fetch_decision_pioneer)均以 stub 替换。
"""
from __future__ import annotations

import uuid

import pytest

from src.core.decision import (
    BASIS_TWO_DIMENSION,
    FUND_MISSING_NOTE_NON_CN,
    synthesize,
    synthesize_two_dimension,
)

# 旧版(无上下文)返回的键集合 —— 向后兼容红线: 该路径不得多/少字段。
# 2026-10-10 A 决策提胜率: 增情绪周期条件化透明字段(regime_*) + 决策先锋口径术语
# (pioneer_terms) —— **纯增量**, 原有键语义不变。
_LEGACY_KEYS = {
    "verdict", "reason", "phase", "row", "parts",
    "regime", "regime_label", "regime_policy", "regime_note", "regime_adjusted",
    # 2026-10-10 B 决策提胜率: 内外盘七口诀叠加确认透明字段(bdqk*) —— 纯增量
    "bdqk", "bdqk_effect", "bdqk_note", "bdqk_adjusted", "bdqk_signal_kind",
    "pioneer_terms",
}


# ─────────────────────────── A. 向后兼容 ───────────────────────────
def test_synthesize_no_context_is_field_identical_to_legacy():
    out = synthesize("G区间", 5.0, 5.0, 1e8, 0.5e8)
    out_none = synthesize("G区间", 5.0, 5.0, 1e8, 0.5e8, user_context=None)
    assert out == out_none
    assert set(out.keys()) == _LEGACY_KEYS
    # 显式传 None / 空 dict 等同"无上下文"(零破坏)
    assert synthesize("G区间", 5.0, 5.0, 1e8, 0.5e8, user_context={}) == out
    # 缺数分支同样零破坏
    miss = synthesize(None, None, None, None)
    assert set(miss.keys()) == _LEGACY_KEYS and miss["verdict"] == "看看"


# ─────────────────────────── B. 个性化 ───────────────────────────
def test_conservative_tightens_manual_row3_to_watch():
    """保守型: 行的『动手』(平稳共振 row3)收紧为『看看』——真实阈值收紧。"""
    base = synthesize("G区间", 5.0, 5.0, 1e8, 0.5e8)
    assert base["verdict"] == "动手" and base["row"] == 3  # 前置: 基线确实是动手
    out = synthesize("G区间", 5.0, 5.0, 1e8, 0.5e8, user_context={"risk_profile": "conservative"})
    assert out["verdict"] == "看看"
    assert out["personalized"] is True
    assert out["personalization_notes"] and "保守型" in out["personalization_note"]
    assert out["risk_profile"] == "conservative"
    # 理由原文不乱改(语义表不动), 仅追加显式个性化注记字段
    assert out["reason"] == base["reason"]


def test_conservative_keeps_manual_on_first_resonance_rows():
    """保守型: 三指标首次/再次共振(行1/2)仍给『动手』——收紧只针对非共振档。"""
    out = synthesize("G信号", 5.0, 1.0, 1e8, 1e8, user_context={"risk_profile": "conservative"})
    assert out["row"] == 1 and out["verdict"] == "动手"
    assert out["personalization_notes"] == []


def test_aggressive_relaxes_turn_to_manual():
    """激进型: 拐点(再次共振 row2)提前放开为『动手』。"""
    base = synthesize("G区间", 10.0, 5.0, 2e8, 1e8)
    assert base["verdict"] == "看看" and base["phase"] == "拐点" and base["row"] == 2
    out = synthesize("G区间", 10.0, 5.0, 2e8, 1e8, user_context={"risk_profile": "aggressive"})
    assert out["verdict"] == "动手"
    assert out["personalized"] is True and "激进型" in out["personalization_note"]


def test_balanced_profile_changes_nothing_but_discloses_profile():
    base = synthesize("G区间", 5.0, 5.0, 1e8, 0.5e8)
    out = synthesize("G区间", 5.0, 5.0, 1e8, 0.5e8, user_context={"risk_profile": "balanced"})
    assert out["verdict"] == base["verdict"]
    assert out["personalized"] is False
    assert out["risk_profile"] == "balanced"  # 透明: 显式声明用了哪档
    assert out["personalization_notes"] == []


def test_holding_reference_row_computes_pnl_without_touching_verdict():
    base = synthesize("G区间", 5.0, 5.0, 1e8, 0.5e8)
    out = synthesize(
        "G区间", 5.0, 5.0, 1e8, 0.5e8,
        user_context={"holds": True, "cost_price": 10.0, "quantity": 1000, "last_close": 11.0},
    )
    assert out["verdict"] == base["verdict"]  # verdict 语义表未被改
    assert out["position"]["pnl_pct"] == pytest.approx(10.0)
    assert "浮盈 +10.00%" in out["position"]["note"]
    assert out["personalized"] is True


def test_holding_missing_cost_is_explicit_no_fabrication():
    out = synthesize(
        "G信号", 5.0, 1.0, 1e8, 0.5e8,
        user_context={"holds": True, "cost_price": None, "quantity": None, "last_close": 11.0},
    )
    assert out["position"]["pnl_pct"] is None
    assert out["position"]["note"] == "已持仓：持仓成本无数据"


# ─────────────────────────── C. 双维(非 CN) ───────────────────────────
@pytest.mark.parametrize("trend,activity,verdict", [
    ("G信号", 5.0, "动手"),
    ("G区间", 3.5, "动手"),
    ("G区间", 1.0, "看看"),   # G 但活跃度跌破强势线
    ("S信号", 1.0, "别碰"),
    ("S区间", 9.0, "看看"),   # S 但活跃度站上强势线
])
def test_two_dimension_verdict_table(trend, activity, verdict):
    out = synthesize_two_dimension(trend, activity, 5.0)
    assert out["verdict"] == verdict, out
    assert out["basis"] == BASIS_TWO_DIMENSION
    assert out["fund_note"] == FUND_MISSING_NOTE_NON_CN
    assert out["parts"]["fund_net"] is None  # 不静默 None: 有显式标注


def test_two_dimension_missing_is_watch_and_discloses_fund_absence():
    out = synthesize_two_dimension(None, None, None)
    assert out["verdict"] == "看看"
    assert "信号不全" in out["reason"]
    assert FUND_MISSING_NOTE_NON_CN in out["reason"]
    assert out["basis"] == BASIS_TWO_DIMENSION


def test_two_dimension_reason_carries_fund_note():
    out = synthesize_two_dimension("G信号", 5.0, 1.0)
    assert FUND_MISSING_NOTE_NON_CN in out["reason"]
    assert out["verdict"] == "动手"


def test_two_dimension_conservative_tightens_manual():
    out = synthesize_two_dimension("G信号", 5.0, 1.0, {"risk_profile": "conservative"})
    assert out["verdict"] == "看看"
    assert out["personalized"] is True and "保守型" in out["personalization_note"]


# ─────────────────────────── D. decide() 跨市场路由 ───────────────────────────
class _K:
    def __init__(self) -> None:
        self.date = "2026-10-09"
        self.open = self.close = self.high = self.low = 10.0
        self.volume = 1000.0


class _Collector:
    def __init__(self, _mc) -> None:
        pass

    def get_klines(self, _symbol, days=120):  # noqa: ARG002
        return [_K() for _ in range(40)]


def _patch_common(monkeypatch, trend="G信号", activity=5.0):
    import src.collectors.kline_collector as kc
    import src.core.ai_activity as ai_act
    import src.core.gs_strategy as gs

    monkeypatch.setattr(kc, "KlineCollector", _Collector)
    monkeypatch.setattr(gs, "eval_gs", lambda bars: {"ok": True})
    monkeypatch.setattr(gs, "trend_label", lambda res: trend)
    monkeypatch.setattr(ai_act, "eval_activity", lambda bars: {"activity": activity})


def test_decide_hk_uses_two_dimension_never_fund(monkeypatch):
    import src.core.decision as core_mod
    import src.core.dark_pool_flow as dpf

    _patch_common(monkeypatch)

    def _boom(_s):
        raise AssertionError("非 CN 不得走 A 股资金口径")

    monkeypatch.setattr(dpf, "compute_pool_flow", _boom)

    out = core_mod.decide("00700", "HK")
    assert out["verdict"] == "动手"
    assert out["basis"] == BASIS_TWO_DIMENSION
    assert out["fund_note"] == FUND_MISSING_NOTE_NON_CN
    assert out["parts"]["fund_net"] is None
    assert out["symbol"] == "00700"


def test_decide_us_two_dimension(monkeypatch):
    import src.core.decision as core_mod

    _patch_common(monkeypatch, trend="S信号", activity=1.0)
    out = core_mod.decide("AAPL", "US")
    assert out["verdict"] == "别碰"
    assert out["basis"] == BASIS_TWO_DIMENSION


def test_decide_non_cn_collector_failure_degrades_not_raises(monkeypatch):
    import src.collectors.kline_collector as kc
    import src.core.decision as core_mod

    def _boom(_mc):
        raise RuntimeError("no source")

    monkeypatch.setattr(kc, "KlineCollector", _boom)
    out = core_mod.decide("00700", "HK")  # 不抛
    assert out["verdict"] == "看看"
    assert out["basis"] == BASIS_TWO_DIMENSION
    assert out["fund_note"] == FUND_MISSING_NOTE_NON_CN


def test_decide_cn_no_bars_has_no_basis_key(monkeypatch):
    """CN 向后兼容: 无降级标记(basis 只在非 CN 出现)。"""
    import src.collectors.kline_collector as kc
    import src.core.decision as core_mod

    class _Empty:
        def __init__(self, _mc) -> None:
            pass

        def get_klines(self, _symbol, days=120):  # noqa: ARG002
            return []

    monkeypatch.setattr(kc, "KlineCollector", _Empty)
    out = core_mod.decide("002361", "CN")
    assert out["verdict"] == "看看"
    assert "basis" not in out
    assert "fund_note" not in out


# ─────────────────────────── E. user_id 隔离 ───────────────────────────
def _seed_rows(session, uid, symbol, market, *, cost=None, qty=None, style="swing"):
    """只建 Stock/Account/Position(不建 User 行) —— 供无需 ORM User 的用例。"""
    from src.web.models import Account, Position, Stock

    stock = Stock(user_id=uid, symbol=symbol, name="测试", market=market)
    session.add(stock)
    session.flush()
    acc = Account(user_id=uid, name="acc", available_funds=0.0)
    session.add(acc)
    session.flush()
    if cost is not None:
        session.add(Position(user_id=uid, account_id=acc.id, stock_id=stock.id,
                             cost_price=cost, quantity=qty, trading_style=style))
    session.commit()
    return stock, acc


def _seed_user_with_stock(session, symbol, market, *, cost=None, qty=None, style="swing"):
    from factories import make_user

    uid = str(uuid.uuid4())
    user = make_user(id=uid, username=f"dec_{uid[:8]}", role="owner")
    session.add(user)
    session.flush()
    _seed_rows(session, uid, symbol, market, cost=cost, qty=qty, style=style)
    return user


def test_build_user_context_isolates_by_user():
    from src.web.api.decision import _build_user_context
    from src.web.database import SessionLocal

    with SessionLocal() as s:
        u_a = _seed_user_with_stock(s, "600519", "CN", cost=10.0, qty=100, style="long")
        u_b = _seed_user_with_stock(s, "600519", "CN")

        ctx_a = _build_user_context(s, u_a, "600519", "CN")
        ctx_b = _build_user_context(s, u_b, "600519", "CN")

    assert ctx_a["holds"] is True and ctx_a["in_watchlist"] is True
    assert ctx_a["cost_price"] == 10.0 and ctx_a["quantity"] == 100
    assert ctx_a["risk_profile"] == "conservative"  # 全 long → 保守型
    # B 只有自选, 无持仓 → 不串 A 的持仓
    assert ctx_b["holds"] is False and ctx_b["cost_price"] is None
    assert ctx_b["risk_profile"] is None


def test_build_user_context_other_symbol_not_held():
    from src.web.api.decision import _build_user_context
    from src.web.database import SessionLocal

    with SessionLocal() as s:
        u = _seed_user_with_stock(s, "600519", "CN", cost=10.0, qty=100)
        ctx = _build_user_context(s, u, "000001", "CN")

    assert ctx["holds"] is False and ctx["in_watchlist"] is False


# ─────────────────────────── F. API 端点 ───────────────────────────
def _client_for(router, user):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.web.api.auth import get_current_user
    from src.web.database import get_db

    app = FastAPI()
    app.include_router(router, prefix="/d")
    app.dependency_overrides[get_current_user] = lambda: user

    def _db():
        from src.web.database import SessionLocal

        s = SessionLocal()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _db
    return TestClient(app, raise_server_exceptions=False)


def test_decision_endpoint_passes_user_context(monkeypatch):
    """端点把**当前用户**上下文叠加在(预落库的)全局基底之上(user_id 隔离红线)。

    2026-10-10 冗余设计改为读时叠加: 基底(无用户维度)落缓存 → 端点命中后用
    `_build_user_context` 的上下文做个性化; 断言响应带 personalized 字段, 且**全局
    缓存行不被污染**(基底仍为无个性化的『动手』)。
    """
    from types import SimpleNamespace

    from src.core import decision_cache as dcache
    from factories import make_user
    from src.web.api import decision as decision_api
    from src.web.database import SessionLocal

    uid = str(uuid.uuid4())
    with SessionLocal() as s:
        s.add(make_user(id=uid, username=f"dec_{uid[:8]}", role="owner"))
        s.flush()
        _seed_rows(s, uid, "600519", "CN", cost=11.0, qty=200, style="long")
    user = SimpleNamespace(id=uid, username="dec-owner", role="owner")

    # 全局基底(无用户维度): 动手 row3 —— 保守型会收紧为『看看』
    dcache.clear_decision_cache()
    dcache.put_cached_decision(
        "600519", "CN",
        {"symbol": "600519", "verdict": "动手", "reason": "动手: 趋势G信号", "phase": "向好",
         "row": 3, "parts": {}, "last_close": 11.0},
        ttl_s=300,
    )

    def _no_compute(*a, **k):
        raise AssertionError("缓存命中不得重算")

    monkeypatch.setattr(dcache, "compute_base", _no_compute)

    c = _client_for(decision_api.router, user)
    r = c.get("/d/600519?market=CN")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["cached"] is True
    # 用户上下文生效: 全持仓 long → 保守型 → 动手收紧为看看; 附持仓行
    assert body["risk_profile"] == "conservative"
    assert body["verdict"] == "看看"
    assert body["position"]["cost_price"] == 11.0
    # 全局缓存未被个性化污染
    rec = dcache.get_cached_decision("600519", "CN")
    assert rec["payload"]["verdict"] == "动手" and "personalized" not in rec["payload"]
    dcache.clear_decision_cache()


def test_decision_pioneer_non_cn_degrades_no_400(monkeypatch):
    from src.web.api import decision_pioneer as dp
    from factories import make_user

    monkeypatch.setattr(
        dp, "fetch_decision_pioneer",
        lambda symbol, market="CN": {"symbol": symbol, "market": market,
                                     "institution_activity": {"activity": 5.0},
                                     "gs": {"state": "G区"}},
    )
    user = make_user(username=f"dp_{uuid.uuid4().hex[:8]}", role="owner")
    c = _client_for(dp.router, user)
    r = c.get("/d/00700?market=HK")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["l2_supported"] is False
    assert body["degraded"] is True
    assert "仅 CN 支持 L2 主力" in body["note"]


def test_decision_pioneer_history_non_cn_degrades_no_400():
    from src.web.api import decision_pioneer as dp
    from factories import make_user

    user = make_user(username=f"dph_{uuid.uuid4().hex[:8]}", role="owner")
    c = _client_for(dp.router, user)
    r = c.get("/d/00700/history?market=HK")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["rows"] == [] and body["degraded"] is True and body["l2_supported"] is False


def test_decision_pioneer_rejects_bad_symbol_per_market():
    from src.web.api import decision_pioneer as dp
    from factories import make_user

    user = make_user(username=f"dpbad_{uuid.uuid4().hex[:8]}", role="owner")
    c = _client_for(dp.router, user)
    assert c.get("/d/123?market=CN").status_code == 400   # CN 需 6 位
    assert c.get("/d/123456?market=HK").status_code == 400  # HK 需 5 位
