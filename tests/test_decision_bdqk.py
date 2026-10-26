"""决策提胜率 B: 内外盘七口诀叠加确认(入合成 + 入账)的钉子(2026-10-10)。

钉什么:
  ① 叠加/压制路径: 『真金进攻』(看涨)与共振同向 → 动手加成(verdict 不变, 标记确认);
     『诱多出货』/『主力撤退』(看跌/警惕)→ 直接压制动手为看看(差异可见);
     中性口诀(多空平衡/控盘洗盘/双小单)→ 不叠加;
  ② 缺数据显式(难归集): 无口诀命中(None)→ effect=none; 逐笔数据不足 → effect=unavailable;
     口诀与主力意图方向背离 → confirm 撤销为 neutral(以主力意图为准), 均**不硬凑**;
  ③ 入账: 叠加确认/压制结果写 decision_log(bdqk_confirm / bdqk_suppress) + regime,
     无价/无 kinds → 不写(不硬填 0);
  ④ decide() 真实 emit 路径: 七口诀解析经唯一 IO 口, 难归集不影响主流程。

禁真网络: 口诀判定走纯函数; IO 口全部 stub。
"""
from __future__ import annotations

import sqlalchemy as sa

from src.core.decision import synthesize, decide
from src.core.mnemonic_overlay import eval_bdqk_effect
from src.web.migrations import _m175_decision_log, _m184_decision_log_regime

# 行1 首次共振(向好→动手): G信号 + 活跃度≥强势线 + 资金流入
ROW1 = ("G信号", 3.5, 1.0, 1.0e8, 0.1e8)


def _mn(name, direction, divergence=False):
    return {"mnemonic": name, "direction": direction, "divergence": divergence, "detail": "x"}


# ── ① 叠加 / 压制 / 中性 ────────────────────────────────────────────────────
def test_confirm_bull_mnemonic_boosts_touch():
    """真金进攻 + 动手 → 动手保持, 标记叠加确认(verdict 不变)。"""
    o = synthesize(*ROW1, bdqk=_mn("真金进攻", "看涨"))
    assert o["verdict"] == "动手"
    assert o["bdqk_effect"] == "confirm"
    assert o["bdqk_signal_kind"] == "bdqk_confirm"
    assert o["bdqk_adjusted"] is False
    assert "叠加确认" in o["reason"]
    assert o["pioneer_terms"]["主力动向"] == "真金进攻"  # 决策先锋口径「主力动向」


def test_suppress_trap_mnemonic_overrides_touch():
    """诱多出货 + 动手 → 直接压制为看看(差异可见), 入账 bdqk_suppress。"""
    o = synthesize(*ROW1, bdqk=_mn("诱多出货", "警惕"))
    assert o["verdict"] == "看看"
    assert o["bdqk_effect"] == "suppress"
    assert o["bdqk_signal_kind"] == "bdqk_suppress"
    assert o["bdqk_adjusted"] is True
    assert "压制" in o["reason"]


def test_suppress_bear_mnemonic_overrides_touch():
    o = synthesize(*ROW1, bdqk=_mn("主力撤退", "看跌"))
    assert o["verdict"] == "看看" and o["bdqk_effect"] == "suppress"


def test_neutral_mnemonic_does_not_stack():
    o = synthesize(*ROW1, bdqk=_mn("多空平衡", "观望"))
    assert o["verdict"] == "动手"  # 中性不改判定
    assert o["bdqk_effect"] == "neutral"
    assert o["bdqk_signal_kind"] is None
    assert "不叠加" in o["bdqk_note"]


def test_confirm_not_applied_when_base_is_not_touch():
    """confirm 但基线非动手: 如实说"未生效", 不硬改 verdict。"""
    o = synthesize("G区间", 4.0, 1.5, 2.0e8, 0.5e8, bdqk=_mn("真金进攻", "看涨"))  # 行2 拐点→看看
    assert o["verdict"] == "看看"
    assert o["bdqk_effect"] == "confirm" and o["bdqk_adjusted"] is False
    assert "未生效" in o["bdqk_note"]


# ── ② 缺数据显式 / 背离 ─────────────────────────────────────────────────────
def test_unresolved_when_data_insufficient():
    eff = eval_bdqk_effect(_mn("数据不足", "中性"))
    assert eff["effect"] == "unavailable" and eff["signal_kind"] is None
    assert "难归集" in eff["note"]


def test_no_mnemonic_is_none_effect_not_fabricated():
    eff = eval_bdqk_effect(None)
    assert eff["effect"] == "none" and eff["name"] is None
    o = synthesize(*ROW1, bdqk=None)
    assert o["bdqk"]["name"] is None and o["bdqk_effect"] == "none"
    assert o["bdqk_signal_kind"] is None


def test_unknown_mnemonic_name_is_unresolved():
    eff = eval_bdqk_effect(_mn("莫名其妙口诀", "中性"))
    assert eff["effect"] == "unavailable" and "未列入叠加表" in eff["note"]


def test_divergence_cancels_confirm():
    """口诀与主力资金意图方向背离 → confirm 撤销为 neutral(以主力意图为准)。"""
    eff = eval_bdqk_effect(_mn("真金进攻", "看涨", divergence=True))
    assert eff["effect"] == "neutral" and eff["signal_kind"] is None
    assert "背离" in eff["note"]


# ── ③ / ④ 入账(decision_log bdqk_*) ─────────────────────────────────────────
def _engine():
    eng = sa.create_engine("sqlite://")
    with eng.connect() as conn:
        _m175_decision_log(conn)
        _m184_decision_log_regime(conn)
    return eng


class _K:
    def __init__(self) -> None:
        self.date = "2026-10-10"
        self.open = self.close = self.high = self.low = 10.0
        self.volume = 1000.0


class _Collector:
    def __init__(self, _mc) -> None:
        pass

    def get_klines(self, _symbol, days=120):  # noqa: ARG002
        return [_K() for _ in range(40)]


def _patch_decide(monkeypatch, eng, mnemonic):
    import src.collectors.kline_collector as kc
    import src.core.ai_activity as ai_act
    import src.core.decision as core_mod
    import src.core.dark_pool_flow as dpf
    import src.core.gs_strategy as gs
    import src.db.session as session_mod
    import src.core.market_regime as mr
    import src.core.mnemonic_overlay as mo

    monkeypatch.setattr(kc, "KlineCollector", _Collector)
    monkeypatch.setattr(gs, "eval_gs", lambda bars: {"ok": True})
    monkeypatch.setattr(gs, "trend_label", lambda res: "G信号")
    monkeypatch.setattr(ai_act, "eval_activity", lambda bars: {"activity": 3.5})
    monkeypatch.setattr(dpf, "compute_pool_flow", lambda s: {"main_net": 1.0e8})
    monkeypatch.setattr(mr, "current_regime", lambda *a, **k: {"regime": "ebb", "label": "退潮"})
    monkeypatch.setattr(mo, "resolve_mnemonic", lambda symbol: mnemonic)
    monkeypatch.setattr(session_mod, "get_write_engine", lambda: eng)
    return core_mod


def test_decide_emits_bdqk_confirm_row(monkeypatch):
    eng = _engine()
    core_mod = _patch_decide(monkeypatch, eng, _mn("真金进攻", "看涨"))
    out = core_mod.decide("002361", "CN")
    assert out["verdict"] == "动手" and out["bdqk_signal_kind"] == "bdqk_confirm"
    with eng.connect() as conn:
        rows = conn.execute(
            sa.text("SELECT signal_kind, regime, price_at_signal FROM decision_log "
                    "WHERE symbol='002361'")
        ).fetchall()
    assert len(rows) == 1
    kind, regime, price = rows[0]
    assert kind == "bdqk_confirm" and regime == "ebb"  # regime 随信号落库(可分桶)
    assert abs(float(price) - 10.0) < 1e-9


def test_decide_emits_bdqk_suppress_row(monkeypatch):
    eng = _engine()
    core_mod = _patch_decide(monkeypatch, eng, _mn("诱多出货", "警惕"))
    out = core_mod.decide("002362", "CN")
    assert out["verdict"] == "看看" and out["bdqk_signal_kind"] == "bdqk_suppress"
    with eng.connect() as conn:
        kinds = [r[0] for r in conn.execute(
            sa.text("SELECT signal_kind FROM decision_log WHERE symbol='002362'"))]
    assert kinds == ["bdqk_suppress"]


def test_decide_no_mnemonic_does_not_emit(monkeypatch):
    """难归集(None) / 中性 → 不入账(无 bdqk kind)。"""
    eng = _engine()
    core_mod = _patch_decide(monkeypatch, eng, None)
    out = core_mod.decide("002363", "CN")
    assert out["bdqk_signal_kind"] is None
    with eng.connect() as conn:
        n = conn.execute(sa.text(
            "SELECT COUNT(*) FROM decision_log WHERE symbol='002363'")).scalar()
    assert int(n) == 0


def test_log_overlay_skips_when_no_price(monkeypatch):
    """无价 → 不入账(不硬填 0)。"""
    eng = _engine()
    import src.core.decision as core_mod
    import src.db.session as session_mod

    monkeypatch.setattr(session_mod, "get_write_engine", lambda: eng)
    n = core_mod._log_decision_overlay("002364", {
        "bdqk_signal_kind": "bdqk_confirm", "last_close": None, "regime": "ebb",
    })
    assert n == 0
    with eng.connect() as conn:
        cnt = conn.execute(sa.text(
            "SELECT COUNT(*) FROM decision_log WHERE symbol='002364'")).scalar()
    assert int(cnt) == 0
