"""数字对账护栏（2026-10-21）。

线上实例：工具返回三花智控「70% 筹码带 33.62 – 44.52 / 获利盘 9.9%」，
模型写成「筹码峰 87.5、获利盘 39%、成本带 83.12–91.88」——把强瑞技术的量级串到了三花头上。
这类"数字串位"比整段幻觉更难发现，因为句子读起来完全合理。

做法：把回答里的**硬数字**逐个回查工具原文。查得到 = 放行；查不到 = 标注出来给人看。
不删改正文（删改会让人以为数据没问题），只在末尾附一行对账结论。
"""
from __future__ import annotations

import re

# 只抓"硬数字"：带小数点的（价格/百分比）或带量纲的（万/亿/倍/手/元）。
# 纯整数无单位（3 板 / 5 日 / 17 次）不查，否则满屏误报。
_NUM_RE = re.compile(
    r"(?<![\d.])(-?\d{1,3}(?:,\d{3})*(?:\.\d{1,4})?)(?:\s*(万元|亿元|%|万|亿|倍|手|元))?"
)
_UNITS = ("万元", "亿元", "%", "万", "亿", "倍", "手", "元")

_CODE_LIKE = re.compile(r"(?<!\d)\d{6}(?!\d)")
_DATE_LIKE = re.compile(r"\d{4}-\d{2}-\d{2}|\d{2}:\d{2}")


def _norm(token: str) -> float | None:
    t = (token or "").replace(",", "").strip()
    for unit in _UNITS:  # 单位不影响数值比较
        if t.endswith(unit):
            t = t[: -len(unit)].strip()
            break
    try:
        return float(t)
    except ValueError:
        return None


def extract_claims(text: str) -> list[str]:
    """抽出需要核对的硬数字（原样 token，便于在提示里回显）。"""
    if not text:
        return []
    cleaned = _DATE_LIKE.sub(" ", _CODE_LIKE.sub(" ", text))
    seen: list[str] = []
    for m in _NUM_RE.finditer(cleaned):
        num, unit = m.group(1), m.group(2) or ""
        if "." not in num and not unit:  # 纯整数无单位 → 不是硬数字
            continue
        cand = num + unit
        if cand not in seen:
            seen.append(cand)
    return seen


def _evidence_pool(evidence: str) -> tuple[set[float], list[float]]:
    vals: list[float] = []
    for token in extract_claims(evidence):
        v = _norm(token)
        if v is not None:
            vals.append(v)
    return set(vals), vals[:150]


def _derived(pool: list[float]) -> set[float]:
    """把"从两个读数算出来的百分比/差值"也当作合法出处，避免误报。

    模型常写「现价距底仓 -10.13%」这种算出来的数；工具没直接给，但它不是编的。
    """
    out: set[float] = set()
    for i, a in enumerate(pool):
        for b in pool[i + 1:]:
            if b:
                out.add(round((a - b) / b * 100, 2))
                out.add(round((b - a) / b * 100, 2))
            if a:  # 换分母也算合法推算（涨跌幅按现价/按成本两种口径都常见）
                out.add(round((a - b) / a * 100, 2))
                out.add(round((b - a) / a * 100, 2))
            if a:
                out.add(round(b / a * 100, 2))
            out.add(round(abs(a - b), 2))
    return out


def audit(answer: str, evidence: str, question: str = "") -> dict:
    """返回 {checked, unverified, ratio, severe}。"""
    claims = extract_claims(answer or "")
    if not claims:
        return {"checked": 0, "unverified": [], "ratio": 0.0, "severe": False}
    known, pool = _evidence_pool(evidence or "")
    q_vals = {v for v in (_norm(t) for t in extract_claims(question or "")) if v is not None}
    derived = _derived(pool)
    unverified: list[str] = []
    checked = 0
    for token in claims:
        v = _norm(token)
        if v is None:
            continue
        checked += 1
        if v in known or v in q_vals:
            continue
        if abs(v) in {abs(x) for x in known}:  # 方向符号写法差异
            continue
        if any(abs(v - d) <= 0.02 for d in derived):
            continue
        unverified.append(token)
    ratio = len(unverified) / checked if checked else 0.0
    return {
        "checked": checked,
        "unverified": unverified,
        "ratio": round(ratio, 3),
        # 小样本别急着下"未通过"结论：2 个数里错 1 个只是可疑，5 个数里错 3 个才是严重
        "severe": len(unverified) >= 3 or (checked >= 6 and ratio >= 0.34),
    }


def annotate(answer: str, result: dict) -> str:
    """把对账结论附在回答末尾（不改正文）。"""
    bad = result.get("unverified") or []
    if not bad:
        return answer
    shown = "、".join(bad[:6]) + ("…" if len(bad) > 6 else "")
    if result.get("severe"):
        tail = (
            f"\n\n---\n⚠️ **数字对账未通过**：{len(bad)}/{result['checked']} 个数字在本次工具返回里找不到出处"
            f"（{shown}）。这些数可能是模型推算或串位，**请以工具原始读数为准**；"
            "要看准确数字建议只问一只票、单看一个维度。"
        )
    else:
        tail = (
            f"\n\n---\n⚠️ 数字对账：{len(bad)} 个数字（{shown}）未在工具返回中找到出处，"
            "可能是推算值，请以原始读数为准。"
        )
    return (answer or "").rstrip() + tail
