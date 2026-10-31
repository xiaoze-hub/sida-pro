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


def _evidence_pool(evidence: str) -> tuple[set[float], list[float], list[tuple[str, float]]]:
    """返回 (数值集合, 数值列表, [(原文 token, 数值)])。

    第三项给「量级不一致」用：只报「差 100 倍」而不给出原文，用户没法自己判断谁对。
    """
    vals: list[float] = []
    pairs: list[tuple[str, float]] = []
    for token in extract_claims(evidence):
        v = _norm(token)
        if v is not None:
            vals.append(v)
            pairs.append((token, v))
    # 证据池上限 150 → 400: system 上下文里还有持仓成本/画像, 池子太小会把真数挤出窗口
    return set(vals), vals[:400], pairs[:400]


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


# ── 量级不一致（2026-10-31）────────────────────────────────────────────────
# 线上实例：同一标的同一天，主力净流出被写成 -61.93 万（取数原文 -6193 万）——
# 差 100 倍。原有的「查得到出处吗」比对抓不住这种错：两个数都不在池子里，
# 它只会被归成「可能是推算值」，语气太软，用户看不出这是单位换算错了。
# 这里单独识别「恰好差 100 / 10000 倍」：万↔百万元级、亿↔万元级，几乎只可能是换算错。
_MAGNITUDE_FACTORS = (100.0, 10000.0)
_MAGNITUDE_TOL = 0.015


# 非货币单位：`12562 手`、`3 倍`、`88.0%` 折成"元"会造出假的 100/10000 倍关系
# （例：6193 手 × ¥100 ≈ 6193 万元 → 与成交额同量级，被误判成单位写错）。
_NON_CURRENCY_UNITS = ("%", "手", "倍")


def _scale_of_token(token: str) -> float | None:
    """token 的量纲系数；非货币单位返回 None（调用方必须跳过，不能当 1.0 用）。"""
    unit = next((u for u in _UNITS if (token or "").endswith(u)), "")
    if unit in _NON_CURRENCY_UNITS:
        return None
    return _SCALE.get(unit, 1.0)


def _to_yuan(token: str, value: float) -> float | None:
    """证据 token 折成元：带万/亿的按单位放大，裸数（含「元」）按元算；非货币单位返回 None。"""
    scale = _scale_of_token(token)
    return None if scale is None else value * scale


def _magnitude_mismatch(claim_value: float, claim_scale: float,
                        pool: list[tuple[str, float]]) -> tuple[str, float] | None:
    """回答里的金额与取数原文是否恰好差 100 / 10000 倍（单位换算错）。

    两边都先折成元再比：证据 `-6193万` = -6.193e7 元，回答 `-61.93万` = -6.193e5 元，
    比值 100 —— 这就是线上那条「同一天同一标的差 100 倍」。
    只认 100 / 10000：2 倍、3 倍这种普通算错仍走「未找到出处」，不冒充量级错误。
    """
    if not claim_scale or claim_value == 0:
        return None
    yen = claim_value * claim_scale
    for token, x in pool:
        x_yen = _to_yuan(token, x)
        if x_yen is None:
            continue  # 手/倍/% 不是金额，不参与量级比对
        if not x_yen or abs(x_yen) < 1e4:  # 太小的读数多是价格/比例
            continue
        ratio = abs(yen / x_yen)
        if ratio == 0:
            continue
        factor = ratio if ratio >= 1 else 1 / ratio
        for target in _MAGNITUDE_FACTORS:
            if abs(factor / target - 1.0) <= _MAGNITUDE_TOL:
                return token, target
    return None


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
    known, pool, pool_pairs = _evidence_pool(evidence or "")
    pool_all = pool
    q_vals = {v for v in (_norm(t) for t in extract_claims(question or "")) if v is not None}
    derived = _derived(pool)
    unverified: list[str] = []
    magnitude: list[dict] = []
    checked = 0
    for token in claims:
        v = _norm(token)
        if v is None:
            continue
        checked += 1
        # 先查「数字一模一样、单位差 100/10000 倍」：证据池把单位剥掉了，`6193元` 与 `6193万`
        # 会撞成同一个数 → 被当成命中放行。这里按**各自单位**再折一次才认命中。
        _unit0 = next((u for u in _UNITS if token.endswith(u)), "")
        _scale0 = _SCALE.get(_unit0) or (1.0 if _unit0 == "元" else 0.0)
        if _scale0 and v in known:
            _unit_mismatch = None
            for _tok, _x in pool_pairs:
                if abs(abs(_x) - abs(v)) > abs(v) * 0.005 or _x == 0:
                    continue
                _s2 = _scale_of_token(_tok)
                if _s2 is None:
                    # 同数字但单位不是钱（`6193 手` vs 回答 `6193 万元`）：不能据此判错，跳过
                    continue
                _r = _scale0 / _s2
                if _r < 1:
                    _r = 1 / _r
                for _target in _MAGNITUDE_FACTORS:
                    if abs(_r / _target - 1.0) <= _MAGNITUDE_TOL:
                        _unit_mismatch = (_tok, _target)
                        break
                # 已经找到同数字的**货币**出处：无论是否命中都停 —— 存在同量纲出处就是有据，
                # 继续往后扫只会被别的同数字 token（另一个单位）带偏。
                break
            if _unit_mismatch:
                magnitude.append({"claim": token, "evidence": _unit_mismatch[0],
                                  "factor": _unit_mismatch[1]})
                unverified.append(token)
                continue
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
        # 单位量级（差 100/10000 倍）比"查不到出处"更严重：这是把万当成了百万元。
        unit_for_mag = next((u for u in _UNITS if token.endswith(u)), "")
        # 「元」也在比对范围内（把 6193万 写成 6193元 = 差 10000 倍）；手/倍/% 不是金额，排除
        scale_for_mag = _SCALE.get(unit_for_mag) or (1.0 if unit_for_mag == "元" else 0.0)
        if scale_for_mag:
            hit = _magnitude_mismatch(v, scale_for_mag, pool_pairs)
            if hit is not None:
                evidence_token, factor = hit
                magnitude.append({
                    "claim": token,
                    "evidence": evidence_token,
                    "factor": round(abs(factor), 4),
                })
                unverified.append(token)
                continue
        unverified.append(token)
    ratio = len(unverified) / checked if checked else 0.0
    return {
        "checked": checked,
        "unverified": unverified,
        "magnitude": magnitude,
        "ratio": round(ratio, 3),
        # 小样本别急着下"未通过"结论：2 个数里错 1 个只是可疑，5 个数里错 3 个才是严重
        # 量级错误一条就够严重 —— 它几乎不可能是"推算值"，只可能是换算/串位。
        "severe": bool(magnitude) or len(unverified) >= 3 or (checked >= 6 and ratio >= 0.34),
    }


def _magnitude_line(result: dict) -> str:
    """把量级错误写成一句人话：谁的数、取数原文是多少、差多少倍。"""
    items = result.get("magnitude") or []
    if not items:
        return ""
    shown = "；".join(
        f"回答写 {it['claim']}，取数原文 {it['evidence']}（差 {int(it['factor'])} 倍）"
        for it in items[:3]
    )
    more = "" if len(items) <= 3 else f"，另有 {len(items) - 3} 处"
    return (
        f"⚠️ **单位量级不一致**：{shown}{more}。"
        "这类差异一般是万/亿换算错误（把万元当成百万元），**请以取数原文为准**。"
    )


def annotate(answer: str, result: dict) -> str:
    """把对账结论附在回答末尾（不改正文）。"""
    bad = result.get("unverified") or []
    mag_line = _magnitude_line(result)
    if not bad:
        return answer
    if mag_line:
        shown = "、".join(bad[:6]) + ("…" if len(bad) > 6 else "")
        return (
            (answer or "").rstrip()
            + f"\n\n---\n{mag_line}"
            + f"\n\n其余 {len(bad)}/{result['checked']} 个数字（{shown}）未在工具返回中找到出处，请以原始读数为准。"
        )
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
