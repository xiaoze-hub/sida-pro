# -*- coding: utf-8 -*-
"""聚宝盆选股测试(数智决策 规格 §4.2, 2026-10-10)。

官方选股流程: **暗盘资金流入 AND AI机构活跃度>6(>12更佳) AND GS在G区+G信号 AND 问财关键词**。
本文件钉死:
  ① 逐条件通过/未过明细(证据) —— 每条件 status ∈ pass/fail/degraded/na + evidence;
  ② 任一必需条件缺数据 → 显式降级, **该股不出池**(in_pool=False);
  ③ 问财降级: 传了关键词但源不可用 → 问财条件 degraded, 全池不出且 note 显式;
  ④ 活跃度 >12 标"更佳"(better=True);
  ⑤ 结果走 biz_cache(命中不重算); API 契约 POST /api/stock-pool/jbp。

**禁真网络**: 取数函数(日K/暗盘/问财)一律 monkeypatch。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.core import jbp_pool as jbp


# ══════════════════════════════════════════════════════════════════════
# evaluate_one 纯函数: 逐条件明细
# ══════════════════════════════════════════════════════════════════════
def _by_key(row):
    return {c["key"]: c for c in row["conditions"]}


def test_evaluate_one_all_pass():
    row = jbp.evaluate_one(
        "000001", dark_net=5_000_000.0, activity=8.0,
        gs_eval={"zone": "G区", "signal": "G"}, wencai_hit=True, wencai_required=True,
    )
    assert row["in_pool"] is True
    conds = _by_key(row)
    assert conds["dark_inflow"]["status"] == "pass" and conds["dark_inflow"]["met"] is True
    assert conds["activity"]["status"] == "pass"
    assert conds["gs"]["status"] == "pass"
    assert conds["wencai"]["status"] == "pass"
    # 证据化: 每条件带 evidence
    assert conds["activity"]["evidence"]["activity"] == 8.0
    assert conds["dark_inflow"]["evidence"]["dark_net"] == 5_000_000.0


def test_evaluate_one_better_flag_over_12():
    row = jbp.evaluate_one("000001", dark_net=1.0, activity=13.0,
                           gs_eval={"zone": "G区", "signal": "G"})
    conds = _by_key(row)
    assert conds["activity"]["status"] == "pass"
    assert conds["activity"]["evidence"]["better"] is True
    assert row["better"] is True and row["activity_level"] == "更佳"
    assert "更佳" in conds["activity"]["detail"]


def test_evaluate_one_activity_below_line_fails():
    row = jbp.evaluate_one("000002", dark_net=1.0, activity=5.9,
                           gs_eval={"zone": "G区", "signal": "G"})
    assert row["in_pool"] is False
    assert _by_key(row)["activity"]["status"] == "fail"
    assert _by_key(row)["activity"]["evidence"]["better"] is False


def test_evaluate_one_gs_needs_zone_g_and_signal():
    # 在 G 区但无 G 信号(仅区间) → 未过
    row = jbp.evaluate_one("000003", dark_net=1.0, activity=9.0,
                           gs_eval={"zone": "G区", "signal": None})
    assert row["in_pool"] is False
    assert _by_key(row)["gs"]["status"] == "fail"
    # S 区 → 未过
    row2 = jbp.evaluate_one("000003", dark_net=1.0, activity=9.0,
                            gs_eval={"zone": "S区", "signal": "S"})
    assert _by_key(row2)["gs"]["status"] == "fail"


def test_evaluate_one_missing_data_degrades_and_not_in_pool():
    # 暗盘缺
    r1 = jbp.evaluate_one("000004", dark_net=None, activity=9.0,
                          gs_eval={"zone": "G区", "signal": "G"})
    assert _by_key(r1)["dark_inflow"]["status"] == "degraded"
    assert r1["in_pool"] is False
    # 活跃度缺(无日K)
    r2 = jbp.evaluate_one("000004", dark_net=1.0, activity=None, gs_eval=None)
    assert _by_key(r2)["activity"]["status"] == "degraded"
    assert _by_key(r2)["gs"]["status"] == "degraded"
    assert r2["in_pool"] is False


def test_evaluate_one_wencai_optional_na():
    row = jbp.evaluate_one("000005", dark_net=1.0, activity=9.0,
                           gs_eval={"zone": "G区", "signal": "G"}, wencai_required=False)
    assert _by_key(row)["wencai"]["status"] == "na"
    assert row["in_pool"] is True  # 可选条件不阻断


def test_evaluate_one_wencai_required_but_unavailable_degrades():
    row = jbp.evaluate_one("000006", dark_net=1.0, activity=9.0,
                           gs_eval={"zone": "G区", "signal": "G"},
                           wencai_hit=None, wencai_required=True)
    assert _by_key(row)["wencai"]["status"] == "degraded"
    assert row["in_pool"] is False


# ══════════════════════════════════════════════════════════════════════
# screen: 取数(monkeypatch) + 逐条件 + 降级
# ══════════════════════════════════════════════════════════════════════
def _patch_fetch(monkeypatch, *, dark, acts, zones, signals, wencai=None, wencai_avail=True):
    import src.core.ai_activity as ai
    import src.core.gs_strategy as gs

    monkeypatch.setattr(jbp, "_bars_of", lambda s: [{"symbol": s, "close": 1.0}])
    monkeypatch.setattr(jbp, "_dark_net_of", lambda s: dark.get(s))
    monkeypatch.setattr(ai, "eval_activity", lambda bars: {"activity": acts.get(bars[0]["symbol"])})
    monkeypatch.setattr(gs, "eval_gs",
                        lambda bars: {"zone": zones.get(bars[0]["symbol"]),
                                      "signal": signals.get(bars[0]["symbol"])})
    if wencai is not None or not wencai_avail:
        monkeypatch.setattr(jbp, "_wencai_symbols",
                            lambda kw: {"available": wencai_avail,
                                        "symbols": set(wencai or []), "note": "stub"})


def test_screen_and_conditions(monkeypatch):
    _patch_fetch(
        monkeypatch,
        dark={"000001": 5e6, "000002": -1e6, "000003": None, "000004": 1e6},
        acts={"000001": 8.0, "000002": 8.0, "000003": 8.0, "000004": 2.0},
        zones={"000001": "G区", "000002": "G区", "000003": "G区", "000004": "G区"},
        signals={"000001": "G", "000002": "G", "000003": "G", "000004": "G"},
    )
    out = jbp.screen(["000001", "000002", "000003", "000004"])
    assert out["universe"] == 4 and out["scanned"] == 4
    by = {r["symbol"]: r for r in out["rows"]}
    assert by["000001"]["in_pool"] is True          # 全过
    assert by["000002"]["in_pool"] is False         # 暗盘流出 → 未过
    assert _by_key(by["000002"])["dark_inflow"]["status"] == "fail"
    assert by["000003"]["in_pool"] is False         # 暗盘缺 → 降级
    assert _by_key(by["000003"])["dark_inflow"]["status"] == "degraded"
    assert by["000004"]["in_pool"] is False         # 活跃度不足 → 未过
    assert _by_key(by["000004"])["activity"]["status"] == "fail"
    assert out["in_pool"] == 1
    # 入池的排在前
    assert out["rows"][0]["symbol"] == "000001"
    # filters 记录了本次实际条件
    assert out["filters"]["activity"].startswith("活跃度>6")


def test_screen_wencai_filter(monkeypatch):
    _patch_fetch(
        monkeypatch,
        dark={"000001": 1e6, "000002": 1e6},
        acts={"000001": 8.0, "000002": 8.0},
        zones={"000001": "G区", "000002": "G区"},
        signals={"000001": "G", "000002": "G"},
        wencai=["000001"], wencai_avail=True,
    )
    out = jbp.screen(["000001", "000002"], wencai_keywords="均线多头排列")
    assert out["wencai"]["provided"] is True and out["wencai"]["hits"] == 1
    by = {r["symbol"]: r for r in out["rows"]}
    assert by["000001"]["in_pool"] is True
    assert by["000002"]["in_pool"] is False
    assert _by_key(by["000002"])["wencai"]["status"] == "fail"


def test_screen_wencai_down_all_degrade(monkeypatch):
    _patch_fetch(
        monkeypatch,
        dark={"000001": 1e6},
        acts={"000001": 8.0},
        zones={"000001": "G区"},
        signals={"000001": "G"},
        wencai_avail=False,
    )
    out = jbp.screen(["000001"], wencai_keywords="任意条件")
    assert out["wencai"]["available"] is False
    assert out["in_pool"] == 0
    assert out["rows"][0]["in_pool"] is False
    assert _by_key(out["rows"][0])["wencai"]["status"] == "degraded"
    assert out["note"] and "问财" in out["note"]


# ══════════════════════════════════════════════════════════════════════
# 缓存 + API 契约
# ══════════════════════════════════════════════════════════════════════
def test_screen_cached_hits_cache(monkeypatch):
    from src.web.api import stock_pool as sp

    calls = {"n": 0}

    def fake_screen(codes, kw=None):
        calls["n"] += 1
        return {"universe": len(codes), "rows": [], "mark": calls["n"]}

    monkeypatch.setattr(jbp, "screen", fake_screen)
    r1 = sp.screen_cached(["000001", "000002"])
    assert r1["universe"] == 2
    # 第二次: 即便 screen 抛错也应命中缓存(不再重算)
    monkeypatch.setattr(jbp, "screen",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("should not recompute")))
    r2 = sp.screen_cached(["000001", "000002"])
    assert r2["universe"] == 2 and calls["n"] == 1


class _FakeUser:
    id = "u-test"


@pytest.fixture()
def client():
    from src.web.api.auth import get_current_user
    from src.web.app import app

    app.dependency_overrides[get_current_user] = lambda: _FakeUser()
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_jbp_api_contract(client, monkeypatch):
    captured = {}

    def fake_cached(symbols, kw=None):
        captured["symbols"] = symbols
        captured["kw"] = kw
        return {"universe": len(symbols), "scanned": len(symbols), "in_pool": 1,
                "rows": [{"symbol": symbols[0], "in_pool": True, "conditions": []}],
                "wencai": {"provided": bool(kw)}, "filters": {}, "note": None}

    from src.web.api import stock_pool as sp

    monkeypatch.setattr(sp, "screen_cached", fake_cached)
    r = client.post("/api/stock-pool/jbp",
                    headers={"Authorization": "Bearer test-token"},  # Bearer 免 CSRF
                    json={"symbols": ["000001", "abc", "000002"], "wencai_keywords": "非ST"})
    assert r.status_code == 200
    d = r.json()["data"]
    assert d["universe"] == 2                 # "abc" 被过滤
    assert d["in_pool"] == 1
    assert captured["symbols"] == ["000001", "000002"]
    assert captured["kw"] == "非ST"


def test_jbp_api_invalid_symbols_returns_empty(client):
    r = client.post("/api/stock-pool/jbp",
                    headers={"Authorization": "Bearer test-token"},
                    json={"symbols": ["abc", "12"]})
    assert r.status_code == 200
    d = r.json()["data"]
    assert d["universe"] == 0 and d["rows"] == []
