"""W3.4/B3 口径治理测试: caliber 契约 + 资金流出口标注 + 文档红线落地。

验收(方案 B3): 用一只股票同时取逐笔与东财口径, 构造方向相反的场景 →
输出明确说明口径差异并优先采信逐笔(代码层可验证, 不再只靠 prompt)。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.core.caliber import (  # noqa: E402
    CAPITAL_FLOW_TAG,
    CaliberTag,
    CaliberViolationError,
    DIRECTION_EASTMONEY4,
    DIRECTION_TICK,
    reconcile_direction,
    require_directional,
)

TICK = CaliberTag(
    caliber="tick",
    direction_semantics=DIRECTION_TICK,
    source="腾讯逐笔(active_buy-active_sell)",
)


# ---------- require_directional: 方向性判定出口校验 ----------


def test_require_directional_passes_tick():
    require_directional(TICK, "主力意图判定")  # 不抛即通过


@pytest.mark.parametrize("tag", [CAPITAL_FLOW_TAG])
def test_require_directional_rejects_non_tick(tag):
    with pytest.raises(CaliberViolationError) as exc:
        require_directional(tag, "主力意图判定")
    msg = str(exc.value)
    assert "tick" in msg and "主力意图" in msg
    assert "get_main_intent" in msg


def test_require_directional_rejects_ths_and_unknown():
    ths = CaliberTag(caliber="ths", direction_semantics="x", source="thsdk_dde")
    unknown = CaliberTag(caliber="unknown", direction_semantics="x", source="?")
    for tag in (ths, unknown):
        with pytest.raises(CaliberViolationError):
            require_directional(tag, "主力意图判定")


# ---------- reconcile_direction: 双口径在场时的裁决 ----------


def test_reconcile_conflict_states_difference_and_prefers_tick():
    # 验收场景: 逐笔判流入、东财四档判流出 → 明确说明口径差异 + 优先逐笔
    res = reconcile_direction(TICK, +1.2e8, CAPITAL_FLOW_TAG, -5.0e7)
    assert res["agree"] is False
    assert "口径冲突" in res["statement"]
    assert "以逐笔为准" in res["statement"]
    assert "禁" in res["statement"]  # 禁止据此下主力意图结论


def test_reconcile_agree_reports_consistent_direction():
    res = reconcile_direction(TICK, +1.2e8, CAPITAL_FLOW_TAG, +3.0e7)
    assert res["agree"] is True
    assert "方向一致" in res["statement"]


def test_reconcile_requires_tick_as_main():
    with pytest.raises(CaliberViolationError):
        reconcile_direction(CAPITAL_FLOW_TAG, +1e8, TICK, -1e8)


# ---------- CAPITAL_FLOW_TAG / CapitalFlow 数据类携带口径 ----------


def test_capital_flow_tag_is_eastmoney4_with_ui_label():
    assert CAPITAL_FLOW_TAG.caliber == "eastmoney4"
    assert "东财四档" in CAPITAL_FLOW_TAG.ui_label()
    assert "禁用" in CAPITAL_FLOW_TAG.ui_label()


def test_capital_flow_tag_to_dict_roundtrip():
    d = CAPITAL_FLOW_TAG.to_dict()
    assert set(d) == {"caliber", "direction_semantics", "source", "label"}
    assert d["caliber"] == "eastmoney4"


def test_capital_flow_dataclass_defaults_and_tag():
    from src.collectors.capital_flow_collector import CapitalFlow

    flow = CapitalFlow(
        symbol="000001",
        name="测试股",
        main_net_inflow=1.0,
        main_net_inflow_pct=1.0,
        super_net_inflow=0.5,
        big_net_inflow=0.5,
        mid_net_inflow=-0.3,
        small_net_inflow=-0.7,
    )
    assert flow.caliber == "eastmoney4"
    assert flow.direction_semantics == DIRECTION_EASTMONEY4
    assert flow.caliber_tag() is CAPITAL_FLOW_TAG


# ---------- get_capital_flow_summary: 摘要 dict 必须带口径标签(不打网络) ----------


def _fake_flow(main: float = 1.2e8) -> "CapitalFlow":
    from src.collectors.capital_flow_collector import CapitalFlow

    return CapitalFlow(
        symbol="000001",
        name="测试股",
        main_net_inflow=main,
        main_net_inflow_pct=6.0,
        super_net_inflow=0.8e8,
        big_net_inflow=0.4e8,
        mid_net_inflow=-0.3e8,
        small_net_inflow=-0.5e8,
        main_net_5d=2e8,
        date="2026-09-09",
    )


def test_summary_dict_carries_caliber_labels(monkeypatch):
    from src.collectors.capital_flow_collector import CapitalFlowCollector

    monkeypatch.setattr(
        CapitalFlowCollector, "get_capital_flow", lambda self, symbol: _fake_flow()
    )
    coll = CapitalFlowCollector.__new__(CapitalFlowCollector)  # 不触发网络初始化
    summary = coll.get_capital_flow_summary("000001")
    assert summary["caliber"] == "eastmoney4"
    assert summary["direction_semantics"] == DIRECTION_EASTMONEY4
    assert "东财四档" in summary["label"]
    assert "禁用" in summary["label"]
    assert summary["source"]


# ---------- chat._fetch_capital_flow_context: prompt 注入口径说明(不打网络) ----------


def test_chat_capital_flow_context_has_caliber_label(monkeypatch):
    from src.collectors.capital_flow_collector import CapitalFlowCollector

    monkeypatch.setattr(
        CapitalFlowCollector,
        "get_capital_flow_summary",
        lambda self, symbol: {
            "status": "主力明显流入",
            "main_net_inflow": 1.2e8,
            "main_net_inflow_pct": 6.0,
            "super_net_inflow": 0.8e8,
            "big_net_inflow": 0.4e8,
            "mid_net_inflow": -0.3e8,
            "small_net_inflow": -0.5e8,
            "trend_5d": "5日净流入2.00亿",
            "date": "2026-09-09",
            **CAPITAL_FLOW_TAG.to_dict(),
        },
    )
    from src.web.api import chat as chat_mod

    out = asyncio.run(chat_mod._fetch_capital_flow_context("000001", "CN"))
    assert "【口径: 东财四档" in out
    assert "口径说明" in out
    assert "禁止用于主力意图" in out
    assert "优先采信逐笔" in out


# ---------- 文档红线落地(结构性验收) ----------


def test_project_map_no_longer_recommends_get_capital_flow():
    text = (ROOT / "docs" / "PROJECT_MAP.md").read_text(encoding="utf-8")
    # 旧推荐话术必须删除
    assert "chat 的 get_capital_flow" not in text
    # 现行口径: panwatch_bridge 资金面走逐笔, get_capital_flow 标禁用
    assert "get_main_intent" in text
    assert "禁止" in text and "get_capital_flow" in text


def test_agents_md_has_executable_caliber_rule():
    text = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert "require_directional" in text
    assert "CaliberViolationError" in text
    assert "src/core/caliber.py" in text


def test_caliber_matrix_exists_and_covers_matrix_shape():
    path = ROOT / "docs" / "_frozen" / "caliber_matrix.md"
    assert path.exists(), "docs/_frozen/caliber_matrix.md 必须存在"
    rows = [
        ln
        for ln in path.read_text(encoding="utf-8").splitlines()
        if ln.startswith("|") and "---" not in ln
    ]
    assert len(rows) >= 6, f"口径矩阵至少 5 个指标行+表头, 实际 {len(rows)} 行"
    body = "\n".join(rows)
    sources = sum(1 for kw in ("东财", "腾讯", "同花顺", "通达信") if kw in body)
    assert sources >= 3, "矩阵需覆盖 ≥3 个数据源家族"


def test_signal_pack_passes_summary_through_with_caliber():
    # 结构性验证: signal_pack 取数入口即 get_capital_flow_summary(已带口径标签),
    # 且 SignalPack.capital_flow 直接吃 summary dict → 标签透传成立
    src = (ROOT / "src" / "core" / "signals" / "signal_pack.py").read_text(
        encoding="utf-8"
    )
    assert "get_capital_flow_summary" in src
    assert "capital_flow=flow_map.get(sym)" in src
