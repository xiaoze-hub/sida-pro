"""决策阈值统一配置层测试(数智决策 P1-3, 2026-10-10)。

钉死四件事:
  ① 默认值与历史硬编码**逐比特一致**(1.56 / 3.00 / 6.00) —— 语义零变化;
  ② env(`SIDA_THRESHOLD_*`)覆盖生效;
  ③ 非法值(非数 / 非有限 / 越界)一律回默认 + warn, **绝不 crash**;
  ④ 三处调用点(resonance_scan / ai_activity / resonance)真读配置(非常量)。

API 端点(`GET /api/decisions/thresholds`)返回生效值 + 来源(default/env)。禁真网络。
"""
from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from src.core import thresholds
from src.web.api.auth import get_current_user


def _clear(monkeypatch):
    for spec in thresholds.snapshot().values():
        monkeypatch.delenv(spec["env"], raising=False)


# ── ① 默认值与硬编码现值一致 ──────────────────────────────────────────────
def test_defaults_equal_hardcoded(monkeypatch):
    _clear(monkeypatch)
    assert thresholds.life_line() == 1.56
    assert thresholds.strong_line() == 3.00
    assert thresholds.bull_line() == 6.00
    snap = thresholds.snapshot()
    assert snap["life_line"]["default"] == 1.56
    assert snap["strong_line"]["default"] == 3.00
    assert snap["bull_line"]["default"] == 6.00
    for key in thresholds.KEYS:
        assert snap[key]["source"] == "default"


# ── ② env 覆盖生效 ─────────────────────────────────────────────────────────
def test_env_override_takes_effect(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("SIDA_THRESHOLD_STRONG_LINE", "4.5")
    assert thresholds.strong_line() == 4.5
    assert thresholds.source("strong_line") == "env"
    # 其它键不受影响
    assert thresholds.life_line() == 1.56
    assert thresholds.source("life_line") == "default"


# ── ③ 非法值回默认 + warn, 不 crash ────────────────────────────────────────
@pytest.mark.parametrize("bad", ["abc", "", "nan", "inf", "-inf", "0", "-1", "5000"])
def test_invalid_env_falls_back_to_default(monkeypatch, caplog, bad):
    _clear(monkeypatch)
    monkeypatch.setenv("SIDA_THRESHOLD_STRONG_LINE", bad)
    with caplog.at_level(logging.WARNING, logger="src.core.thresholds"):
        val = thresholds.strong_line()
        src = thresholds.source("strong_line")
    assert val == 3.00 and src == "default"
    if bad.strip() != "":  # 空串按"未设置"处理(不 warn, 回默认)
        assert any("strong_line" in r.getMessage() for r in caplog.records)


def test_blank_env_treated_as_unset(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("SIDA_THRESHOLD_BULL_LINE", "   ")
    assert thresholds.bull_line() == 6.00
    assert thresholds.source("bull_line") == "default"


def test_unknown_key_raises():
    with pytest.raises(KeyError):
        thresholds.value("nope")


# ── ④ 三处调用点真读配置(非常量) ──────────────────────────────────────────
def test_resonance_scan_reads_config(monkeypatch):
    import src.core.resonance_scan as rs

    _clear(monkeypatch)
    # 默认: 活跃度 4.0 >= 强势线 3.0 → 强度对 + 资金对 → 强(2 分)
    lvl, score = rs.resonance_level(trend_zone_g=True, activity=4.0, fund_net=1e6)
    assert lvl == rs.LEVEL_STRONG and score == 2
    # 覆盖强势线到 5.0: 4.0 < 5.0 → 强度对消失, 只剩资金 1 分 → 弱
    monkeypatch.setenv("SIDA_THRESHOLD_STRONG_LINE", "5.0")
    lvl2, score2 = rs.resonance_level(trend_zone_g=True, activity=4.0, fund_net=1e6)
    assert lvl2 == rs.LEVEL_WEAK and score2 == 1

    # evaluate_one 的 hits[1](强度)同源读配置
    bars = [{"date": "2026-10-10", "open": 1.0, "close": 1.0, "high": 1.0, "low": 1.0, "volume": 1.0}]
    monkeypatch.setattr(rs, "evaluate_one", rs.evaluate_one)  # 明确走真实实现
    import src.core.ai_activity as ai
    import src.core.gs_strategy as gs
    monkeypatch.setattr(gs, "eval_gs", lambda b: {"zone": "G区"})
    monkeypatch.setattr(gs, "trend_label", lambda ev: "G区间")
    monkeypatch.setattr(ai, "eval_activity", lambda b: {"activity": 4.0, "level": "强势"})
    assert rs.evaluate_one(bars, 1e6)["hits"] == [True, False, True]  # 强度被 5.0 挡住


def test_ai_activity_reads_config(monkeypatch):
    import src.core.ai_activity as ai

    _clear(monkeypatch)
    r = ai.activity_of_value(4.0)
    assert r["level"] == ai.LEVEL_STRONG and r["above_strong"] is True
    # 强势线抬到 5.0 → 4.0 掉到"生命"档
    monkeypatch.setenv("SIDA_THRESHOLD_STRONG_LINE", "5.0")
    r2 = ai.activity_of_value(4.0)
    assert r2["above_strong"] is False and r2["level"] == ai.LEVEL_LIFE
    # 生命线抬到 4.5 → 4.0 掉到"弱"档
    monkeypatch.setenv("SIDA_THRESHOLD_LIFE_LINE", "4.5")
    assert ai.activity_of_value(4.0)["level"] == ai.LEVEL_WEAK


def test_resonance_state_reads_config(monkeypatch):
    import src.core.resonance as rs

    _clear(monkeypatch)
    # G区间 + 活跃度 3.2(>=3 强势线) + 流入 → 平稳(向好)
    d = rs.evaluate_state("G区间", 3.2, None, 1e6, None)
    assert d["state"] == "平稳"
    # 强势线抬到 4.0: 3.2 跌破 → "1个走坏"(主力还没出)
    monkeypatch.setenv("SIDA_THRESHOLD_STRONG_LINE", "4.0")
    d2 = rs.evaluate_state("G区间", 3.2, None, 1e6, None)
    assert d2["state"] == "1个走坏"


# ── API 端点: 生效值 + 来源 ────────────────────────────────────────────────
@pytest.fixture()
def client():
    from src.web.app import app

    app.dependency_overrides[get_current_user] = lambda: object()
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_thresholds_endpoint_returns_value_and_source(client, monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("SIDA_THRESHOLD_STRONG_LINE", "4.5")
    r = client.get("/api/decisions/thresholds")
    assert r.status_code == 200
    data = r.json()["data"]
    items = {i["key"]: i for i in data["items"]}
    assert items["strong_line"]["value"] == 4.5
    assert items["strong_line"]["source"] == "env"
    assert items["strong_line"]["env"] == "SIDA_THRESHOLD_STRONG_LINE"
    assert items["life_line"]["value"] == 1.56
    assert items["life_line"]["source"] == "default"
    assert items["life_line"]["label"] == "生命线"


def test_thresholds_endpoint_read_only_no_write_route(client):
    # GET 之外没有写入口(本轮不做写回): POST 必被拒(405 无路由 / 403 CSRF 前置拦截), 绝不 2xx
    r = client.post("/api/decisions/thresholds")
    assert r.status_code in (403, 404, 405)
