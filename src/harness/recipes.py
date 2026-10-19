"""Harness 配方（内置智能体）加载器。

一个"内置智能体"不是新运行时，而是一份 YAML 配方：
    工具白名单 + 提示词片段 + 输出段落 + 预算

为什么用白名单而不是让模型自由选 55 个工具：模型会绕路（多点探测、重复取数），
既慢又贵；白名单把「这类问题该看哪几眼」固化下来，可审计、可回归。
"""
from __future__ import annotations

import functools
from dataclasses import dataclass, field
from pathlib import Path

import yaml

RECIPE_DIR = Path(__file__).resolve().parent / "recipes"


@dataclass(frozen=True)
class Recipe:
    id: str
    label: str
    icon: str = "🤖"
    desc: str = ""
    when: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    output_sections: tuple[str, ...] = ()
    max_tool_calls: int = 10
    prompt_fragment: str = field(default="")

    def public(self) -> dict:
        """给前端 /api/copilot/agents 的元信息（不含提示词正文，避免泄露与大包体）。"""
        return {
            "id": self.id,
            "label": self.label,
            "icon": self.icon,
            "desc": self.desc,
            "when": list(self.when),
            "tool_count": len(self.tools),
            "output_sections": list(self.output_sections),
        }


def _parse(path: Path) -> Recipe:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    budget = raw.get("budget") or {}
    return Recipe(
        id=str(raw.get("id") or path.stem),
        label=str(raw.get("label") or path.stem),
        icon=str(raw.get("icon") or "🤖"),
        desc=str(raw.get("desc") or ""),
        # YAML 1.1 会把 `9:25` 解析成六十进制整数 565 —— 所有标签一律强制转字符串，
        # 否则 ' '.join(when) 直接 TypeError（`/mentions` 线上 500 就是这么来的）。
        when=tuple(str(x) for x in (raw.get("when") or ())),
        tools=tuple(str(x) for x in (raw.get("tools") or ())),
        output_sections=tuple(str(x) for x in (raw.get("output_sections") or ())),
        max_tool_calls=int(budget.get("max_tool_calls") or 10),
        prompt_fragment=str(raw.get("prompt_fragment") or "").strip(),
    )


@functools.lru_cache(maxsize=1)
def load_recipes() -> dict[str, Recipe]:
    """读目录下全部配方（进程内缓存；改 YAML 后重启生效）。"""
    out: dict[str, Recipe] = {}
    if not RECIPE_DIR.exists():
        return out
    for p in sorted(RECIPE_DIR.glob("*.yaml")):
        try:
            r = _parse(p)
        except Exception:
            continue
        out[r.id] = r
    return out


def get_recipe(recipe_id: str | None) -> Recipe | None:
    if not recipe_id:
        return None
    return load_recipes().get(recipe_id.strip())


def list_recipes() -> list[Recipe]:
    return sorted(load_recipes().values(), key=lambda r: r.id)
