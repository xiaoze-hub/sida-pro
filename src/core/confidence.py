"""双源/多源置信度徽章(批次A A4, 2026-09-06 28号)。

建立在 mainflow_tri.judge_agree(腾讯四档+thSdk DDE+TQ Zjl_HB 三源交叉验证,
已上线)之上, 把一致性判定映射成产品可展示的 A/B/C 置信度等级:
- A: ≥2 源有数且同号+离散度达标(双源一致, 直接信)
- B: 仅 1 源有数(无交叉验证, 可看需谨慎)
- C: ≥2 源有数但符号不一致或离散度超阈(分歧, 标黄并展示各源值)
- None: 0 源有数 → 无数据(不编造)

红线呼应: "不编造"的可视化终形态 —— 每个数字知道自己可信到什么程度。
"""
from __future__ import annotations


def level_of(agree: bool | None, spread_pct: float | None, n_ok: int) -> str | None:
    """judge_agree 三元组 → 等级。n_ok<=0 → None(无数据)。"""
    if n_ok <= 0:
        return None
    if n_ok >= 2:
        return "A" if agree else "C"
    return "B"


def payload(tri: dict) -> dict:
    """triangulate() 输出 → 接口字段。永不抛异常, 缺/坏字段按无数据处理。"""
    try:
        n_ok = int(tri.get("n_ok") or 0)
    except (TypeError, ValueError):
        n_ok = 0
    agree = tri.get("agree")
    spread = tri.get("spread_pct")
    level = level_of(agree if isinstance(agree, bool) else None,
                     spread if isinstance(spread, (int, float)) else None,
                     n_ok)
    return {
        "confidence_level": level,
        "sources_agree": agree if n_ok >= 2 else None,
        "spread_pct": spread,
        "n_ok": n_ok,
        "consensus_wan": tri.get("consensus_wan"),
        "sources": tri.get("sources") or {},
    }
