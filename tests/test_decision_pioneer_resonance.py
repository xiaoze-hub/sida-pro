"""A-01 回归: L2 断链不得输出方向性结论(2026-10-18 审计 P0)。

缺陷原文: `decision_pioneer.fetch_decision_pioneer` 里
`l2_inflow = l2_info.get("available") and (l2_info.get("zjl_hb") or 0) > 0`
把「真净流出 / 数据不可用 / 净额恰为 0」压成一个 False, 于是 TQ 网关抖动时一只
G 区 + 机构活跃度 5.0 的强势票被判成"⚡ 警惕背离·主力出货"(方向性误导 + 合规违禁词)。

本文件锁死三件事:
1. 三态语义: True=净流入 / False=净流出 / None=不可判定;
2. `available=False` ⇒ `resonance_warning == "unknown"` 且状态文案不含"背离";
3. 单测断言"净流出"时**不出现**违禁定性词(防回归到旧文案);
4. 全模块合规词表扫描(审计 §3.2 第 4 条)。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from src.core import decision_pioneer as dp

_MODULE = Path(dp.__file__).read_text(encoding="utf-8")

_BANNED = re.compile(r"主力建仓|主力出货|建议买入|建议卖出")


def _fake_l2(*, available: bool, zjl_wan: float | None) -> dict:
    """构造 fetch_tq_l2 的返回(注意 zjl_hb 单位是**万元**, 入参也按万元给)。"""
    if not available:
        return {"available": False}
    return {
        "available": True,
        "zjl_hb": zjl_wan,
        "cancel_buy": 0.0,
        "cancel_sell": 0.0,
        "total_buy_vol": 0.0,
        "total_sell_vol": 0.0,
    }


@pytest.fixture
def drive(monkeypatch):
    """把 fetch_decision_pioneer 的外部依赖全部替换成桩(不联网/不读库)。"""

    def _run(*, zone: str, signal: str | None, activity: float, l2: dict) -> dict:
        monkeypatch.setattr(dp, "fetch_bars", lambda *a, **k: [{"open": 1, "high": 1, "low": 1, "close": 1}] * 3)
        monkeypatch.setattr(
            dp,
            "compute_institution_activity",
            lambda bars: {"activity": activity, "level": "强势", "streak_days": 1, "ma5": None},
        )
        monkeypatch.setattr(dp, "compute_gs_windows", lambda bars: {})

        import src.core.gs_state as gs_state

        monkeypatch.setattr(
            gs_state,
            "resolve_gs_state",
            lambda bars: {
                "zone": zone,
                "label": zone,
                "signal": signal,
                "signal_date": "2026-10-16",
                "signal_confirmed": True,
                "pending": False,
                "pending_side": None,
                "pending_date": None,
                "pending_label": None,
                "note": "",
                "available": True,
                "as_of": "2026-10-17",
                "a0": 1.0,
                "bb0": 0.9,
            },
        )
        monkeypatch.setattr(dp, "fetch_tq_l2", lambda *a, **k: l2)

        import src.core.dark_flow as dark_flow

        monkeypatch.setattr(dark_flow, "compute_dark_flow", lambda *a, **k: None)
        return dp.fetch_decision_pioneer("600111")

    return _run


def test_l2_inflow_g_zone_is_resonance(drive):
    """L2 净流入 + G区 + 活跃度强 → 共振向好, 非 unknown。"""
    r = drive(zone="G区", signal=None, activity=5.0, l2=_fake_l2(available=True, zjl_wan=5000.0))
    assert "共振" in r["resonance_state"]
    assert r["resonance_warning"] != "unknown"
    assert r["l2_inflow"] is True
    assert r["l2_direction_state"] == "inflow"
    assert r["l2_evaluable"] is True


def test_l2_outflow_g_zone_is_divergence_without_banned_word(drive):
    """L2 真净流出 + G区 → 资金背离(可观测事实), 不含违禁定性词。"""
    r = drive(zone="G区", signal=None, activity=5.0, l2=_fake_l2(available=True, zjl_wan=-5000.0))
    assert "资金背离" in r["resonance_state"]
    assert r["resonance_warning"] == "caution"
    assert r["l2_inflow"] is False
    assert _BANNED.search(r["resonance_state"]) is None
    assert _BANNED.search(r["resonance_action"] or "") is None


def test_l2_unavailable_never_outputs_direction(drive):
    """**A-01 核心**: available=False(G 区强势票) ⇒ unknown, 且不得出现"背离"。"""
    r = drive(zone="G区", signal=None, activity=5.0, l2=_fake_l2(available=False, zjl_wan=None))
    assert r["resonance_warning"] == "unknown"
    assert "背离" not in r["resonance_state"]
    assert "资金维缺失" in r["resonance_state"]
    assert r["l2_inflow"] is None
    assert r["l2_evaluable"] is False
    # 断链时不得出现任何方向性措辞
    assert "流出" not in (r["resonance_action"] or "")


def test_l2_zero_is_unknown_not_outflow(drive):
    """净额恰为 0 → 方向无意义, 必须 unknown(旧实现会判成"流出")。"""
    r = drive(zone="G区", signal=None, activity=5.0, l2=_fake_l2(available=True, zjl_wan=0.0))
    assert r["l2_inflow"] is None
    assert r["resonance_warning"] == "unknown"


def test_l2_missing_net_is_unknown(drive):
    """available=True 但净额为 None(字段缺失) → unknown。"""
    r = drive(zone="G区", signal=None, activity=5.0, l2=_fake_l2(available=True, zjl_wan=None))
    assert r["l2_inflow"] is None
    assert r["resonance_warning"] == "unknown"


def test_s_zone_without_l2_does_not_claim_fund_exit(drive):
    """S 区 + L2 断链: 状态可以是风险, 但理由不许写"资金双撤退"。"""
    r = drive(zone="S区", signal=None, activity=1.0, l2=_fake_l2(available=False, zjl_wan=None))
    assert r["resonance_warning"] == "warning"
    assert "双撤退" not in (r["resonance_action"] or "")
    assert "资金维不可用" in (r["resonance_action"] or "")


def test_s_signal_with_strong_activity_and_missing_l2_does_not_claim_double_exit(drive):
    """S 信号 + 活跃度仍强 + L2 断链: 不许写"机构资金双撤退"(活跃度显然没撤退)。"""
    r = drive(zone="S区", signal="S", activity=4.5, l2=_fake_l2(available=False, zjl_wan=None))
    assert r["resonance_warning"] == "warning"
    assert "双撤退" not in (r["resonance_action"] or "")
    assert "资金维不可用" in (r["resonance_action"] or "")


def test_module_has_no_compliance_banned_words():
    """审计 §3.2 第 4 条: 源码级合规词表(注释也算)。"""
    hits = [(i, ln.strip()) for i, ln in enumerate(_MODULE.splitlines(), 1) if _BANNED.search(ln)]
    assert hits == [], f"命中合规违禁词: {hits}"
