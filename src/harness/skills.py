"""技能手册(2026-10-20, P2)。

和 Hermes 的 skill 同一个思路, 但只有一件事: 把「这类问题该怎么读」写成可注入的
程序性知识, 按配方加载, 用完即走。

为什么不用提示词堆在 SYSTEM_PROMPT 里: 6 个配方 × 各自手册 = 全塞进去会挤掉行情数据,
而且互相干扰(竞价手册的"9:25 前不定型"对盘后复盘毫无意义)。
"""

from __future__ import annotations

import functools
from pathlib import Path

_SKILL_DIR = Path(__file__).with_name("skills")
MAX_CHARS = 1200


@functools.lru_cache(maxsize=16)
def load_skill(name: str) -> str:
    """读一篇手册; 不存在返回空串(不抛, 配方可渐进加手册)。"""
    if not name or "/" in name or ".." in name:
        return ""
    path = _SKILL_DIR / f"{name}.md"
    try:
        text = path.read_text(encoding="utf-8").strip()
    except Exception:
        return ""
    return text[:MAX_CHARS]


def skills_block(names: tuple[str, ...] | list[str] | None) -> str:
    """把多篇手册拼成一段可注入文本。"""
    if not names:
        return ""
    parts = [load_skill(n) for n in names]
    parts = [p for p in parts if p]
    return "\n\n".join(parts)
