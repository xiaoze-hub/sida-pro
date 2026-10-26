"""Harness 配方验收契约的守卫测试（对应 specs/harness/recipes.md）。

这些用例守的是「配方结构」而不是「提示词写得好不好」：
配方决定模型能看到哪些工具、必须写哪几段、最多调几次 —— 改坏任何一处都只会静默变差。
"""

from __future__ import annotations

from pathlib import Path

import yaml

RECIPE_DIR = Path(__file__).resolve().parent.parent / "src" / "harness" / "recipes"


def _all_recipes() -> list[dict]:
    out = []
    for p in sorted(RECIPE_DIR.glob("*.yaml")):
        d = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        d.setdefault("id", p.stem)
        d["_file"] = p.name
        out.append(d)
    return out


def test_every_recipe_declares_acceptance():
    """@spec RECIPE-001"""
    missing = [d["_file"] for d in _all_recipes() if not (d.get("acceptance") or [])]
    assert not missing, f"这些配方没有声明 acceptance: {missing}"


def test_prompt_fragment_covers_output_sections():
    """@spec RECIPE-002"""
    bad = []
    for d in _all_recipes():
        frag = str(d.get("prompt_fragment") or "")
        for sec in d.get("output_sections") or []:
            if str(sec) not in frag:
                bad.append(f"{d['id']}: 缺段落「{sec}」")
    assert not bad, f"prompt_fragment 漏了输出段落: {bad[:6]}"


def test_budget_covers_tool_count():
    """@spec RECIPE-003"""
    bad = []
    for d in _all_recipes():
        budget = int((d.get("budget") or {}).get("max_tool_calls") or 0)
        n_tools = len(d.get("tools") or [])
        if budget < n_tools:
            bad.append(f"{d['id']}: budget={budget} < 工具数 {n_tools}（后面的工具永远轮不到）")
    assert not bad, bad


def test_recipe_tools_exist_in_registry():
    """@spec RECIPE-004"""
    from src.agents.chat.registry import CHAT_TOOL_REGISTRY

    known = set(CHAT_TOOL_REGISTRY)
    bad = []
    for d in _all_recipes():
        for tool in d.get("tools") or []:
            if tool not in known:
                bad.append(f"{d['id']}: 工具不存在 {tool}")
    assert not bad, f"配方里写了不存在的工具（只会静默少一个维度）: {bad[:6]}"


def test_when_keywords_are_unique_across_recipes():
    """@spec RECIPE-005"""
    seen: dict[str, str] = {}
    clashes = []
    for d in _all_recipes():
        for kw in d.get("when") or []:
            kw = str(kw)
            if kw in seen and seen[kw] != d["id"]:
                clashes.append(f"「{kw}」被 {seen[kw]} 与 {d['id']} 同时认领")
            seen.setdefault(kw, d["id"])
    assert not clashes, f"关键词冲突会让路由取决于列表顺序: {clashes[:6]}"


def test_left_diagnosis_has_three_tier_tools():
    """@spec RECIPELD-001"""
    d = next(x for x in _all_recipes() if x["id"] == "left_diagnosis")
    tools = set(d.get("tools") or [])
    for need in ("get_sentiment_cycle", "get_event_catalyst"):
        assert need in tools, f"三层滤网缺工具 {need} —— 模型只能写「本轮未授权」"


def test_deep_diagnosis_covers_six_layers():
    """@spec RECIPEDD-001"""
    d = next(x for x in _all_recipes() if x["id"] == "deep_diagnosis")
    tools = set(d.get("tools") or [])
    for need, layer in (
        ("get_trend_profile", "前期走势"),
        ("get_stock_dossier", "消息面"),
        ("get_scenario_matrix", "情景研判"),
    ):
        assert need in tools, f"六层时间轴缺「{layer}」所需工具 {need}"


def test_demon_diagnosis_uses_demon_factor():
    """@spec RECIPEDM-001"""
    d = next(x for x in _all_recipes() if x["id"] == "demon_diagnosis")
    assert "get_demon_factor" in set(d.get("tools") or []), "妖股配方必须带妖股因子"


# ─────────────── devil_advocate（空头司令，2026-10-24 创新 6） ───────────────


def _devil() -> dict:
    return next(x for x in _all_recipes() if x["id"] == "devil_advocate")


def test_devil_advocate_is_symbol_pipeline_recipe():
    """@spec DEVIL-001"""
    d = _devil()
    assert d.get("pipeline") is True, "压力测试必须走确定性取数管线，模型自由选工具就会挑软柿子捏"
    assert d.get("scope") == "symbol", "空头司令是单标的配方，scope 必须显式为 symbol"
    assert int((d.get("budget") or {}).get("max_tool_calls") or 0) >= len(d.get("tools") or [])


def test_devil_advocate_mounts_evidence_chain_tools():
    """@spec DEVIL-002"""
    tools = set(_devil().get("tools") or [])
    for need, why in (
        ("get_main_intent", "逐笔主力意图（吸筹/派发）"),
        ("get_capital_flow", "四档资金结构（大单流出）"),
        ("get_decision_pioneer", "L2 主力净流入（TQ）"),
        ("get_chip_accumulation", "上方套牢筹码峰/获利盘"),
    ):
        assert need in tools, f"证据链缺「{why}」所需工具 {need} —— 死因就只能写空话"


def test_devil_advocate_prompt_requires_falsifiable_causes():
    """@spec DEVIL-003"""
    frag = str(_devil().get("prompt_fragment") or "")
    assert "亏 15%" in frag or "亏15%" in frag, "必须原文声明「亏 15%」情景假设"
    for sec in ("死因一", "死因二", "死因三"):
        assert sec in frag, f"缺死因段落「{sec}」"
    assert "不成立" in frag and ("若" in frag), "每条死因必须要求可证伪的反驳条件（若…则不成立）"


def test_devil_advocate_prompt_forbids_fabrication():
    """@spec DEVIL-004"""
    frag = str(_devil().get("prompt_fragment") or "")
    assert "无数据" in frag, "证据缺失必须显式写「无数据」"
    assert "无数据源" in frag, "无现成工具的维度（商誉/解禁等）必须写「无数据源」"
    assert "编造" in frag and "0 冒充" in frag, "必须声明禁止编造数字、禁止用 0 冒充缺失"


def test_devil_advocate_prompt_declares_caliber_redlines():
    """@spec DEVIL-005"""
    frag = str(_devil().get("prompt_fragment") or "")
    assert "get_main_intent" in frag and "逐笔" in frag, "主力意图必须声明只认逐笔口径"
    assert "get_capital_flow" in frag and "禁" in frag, "必须声明 get_capital_flow 禁做主力意图方向判定"
    assert "get_decision_pioneer" in frag and "TQ" in frag, "L2 主力净流入必须标 TQ 口径"


def test_devil_advocate_output_contract():
    """@spec DEVIL-006"""
    d = _devil()
    sections = list(d.get("output_sections") or [])
    assert sections and sections[0] == "一句话结论", "首段必须是一句话结论（全站参谋输出契约）"
    assert "口径与缺口" in sections, "必须有「口径与缺口」收尾段，无数据源维度要有固定落点"
    for sec in ("死因一", "死因二", "死因三"):
        assert sec in sections, f"输出契约缺「{sec}」"
