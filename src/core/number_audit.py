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
    r"(?<![\d.])(-?\d+(?:,\d{3})*(?:\.\d{1,4})?)(?:\s*(万元|亿元|%|万|亿|倍|手|元))?"
)
_UNITS = ("万元", "亿元", "%", "万", "亿", "倍", "手", "元")
_SCALE = {"万": 1e4, "万元": 1e4, "亿": 1e8, "亿元": 1e8}

# 只抹掉"带市场标记"的代码（002050.SZ / CN:002050 / sh600519）。
# 裸 6 位数不能当代码抹掉 —— 125628 手 也是 6 位数，抹了会让证据池缺数、把真数判成编造。
_CODE_LIKE = re.compile(
    r"(?:CN:|SH|SZ|BJ|sh|sz|bj)\d{6}(?!\d)|(?<!\d)\d{6}\.(?:SH|SZ|HK|BJ|sh|sz|hk|bj)(?!\w)"
)
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
    # 证据池上限 150 → 400: system 上下文里还有持仓成本/画像, 池子太小会把真数挤出窗口
    return set(vals), vals[:400]


def _diff_set(pool: list[float]) -> set[float]:
    """两两价差（2 位小数）集合 —— 用来反查「金额 = 价差 × 股数」。

    2026-10-22：模型写「-0.75 元/股 × 4600 股 ≈ -3,450 元」，4600 与 0.752 都在证据里，
    但 0.752 本身是**算出来的**，不在池子里；于是乘积查不到就被判成编造。
    """
    out: set[float] = set()
    for i, a in enumerate(pool):
        for b in pool[i + 1:]:
            out.add(round(abs(a - b), 2))
    return out


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
            # 金额 = 价差 × 股数（「浮亏 -3,450 元」就是这么算的），
            # 只收量级合理的一对，避免小数相乘产生一堆垃圾候选
            if abs(a) < 1e6 and abs(b) < 1e6:
                out.add(round(a * b, 2))
                out.add(round(abs(a * b), 2))
    return out


def _close(v: float, x: float, dp: int = 2) -> bool:
    """读数与回答里的数是否"同一个数"。

    按**回答里那个数字的小数位**做四舍五入比较，而不是拍一个百分比容差：
    - 证据 -0.3066 / 回答 0.31  → round(0.3066, 2) == 0.31 ✓
    - 证据 88.07  / 回答 87.5   → round(88.07, 1) == 88.1 ≠ 87.5 ✗（跨标的串位照样抓住）
    - 金额类（|x| >= 1000）允许 1%，因为模型常写「≈ -3,450 元」
    """
    a, b = abs(v), abs(x)
    if b >= 1000:
        return abs(a - b) <= b * 0.01
    return abs(round(b, dp) - a) <= (10 ** -dp) / 2 + 1e-9


def audit(answer: str, evidence: str, question: str = "") -> dict:
    """返回 {checked, unverified, ratio, severe}。"""
    claims = extract_claims(answer or "")
    if not claims:
        return {"checked": 0, "unverified": [], "ratio": 0.0, "severe": False}
    known, pool = _evidence_pool(evidence or "")
    pool_all = pool
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
        dot = token.find(".")
        dp = 0
        if dot >= 0:
            tail = re.match(r"\.(\d+)", token[dot:])
            dp = len(tail.group(1)) if tail else 0
        if any(_close(v, d, dp) for d in derived):
            continue
        # 金额 = 价差 × 股数（-3,450 元 = 0.752 × 4600）
        if token.endswith(("元", "万元", "亿元")):
            _hit = False
            # 股数/手数不在 extract_claims 的硬数字口径里（纯整数无单位不抓），单独抽出来当乘数
            _shares = [float(m.group(1)) for m in
                       re.finditer(r"(?<![\d.])(\d{3,9})\s*(?:股|手)", evidence or "")]
            for c in _shares + [x for x in pool if abs(x) >= 100]:
                    _t = abs(v) / abs(c)
                    if any(abs(round(_t, 2) - d) <= 0.011 for d in _diff_set(pool)):
                        _hit = True
                        break
            if _hit:
                continue
        # 四舍五入/精度差异（-0.3066% 写成 0.31%）
        if any(_close(v, x, dp) for x in known):
            continue
        # 带万/亿的量：允许四舍五入折算（115,000 手写成 11.5 万手不是编造），容差 1.5%
        unit = next((u for u in _UNITS if token.endswith(u)), "")
        scale = _SCALE.get(unit)
        if scale and any(abs(v * scale - x) <= abs(x) * 0.015 for x in pool_all if x):
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
