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
