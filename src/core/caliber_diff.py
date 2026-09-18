"""口径成对差异解释器(P2-1, 2026-09-18)。

## 为什么需要
三源并排(明盘 L2 / 暗盘逐笔 / 东财四档)已经能让人"看到差", 但**看到差之后呢**?
用户还得自己判断: 这个差是"正常的口径差", 还是"哪里不对"。本模块把这一步自动化 ——
给**每一对**口径算差, 并按**已知机制**判断它落在"预期带"还是"值得看一眼"。

## 三条不越界的原则
1. **不合成单一权威数字**: 输出的是"差的解释", 不是"校准后的值";
2. **不猜**: 任一源缺数 → 该对直接标 `unknown` + "缺数据无法比较", 不用 0 或跨基准日凑数;
3. **方向冲突优先采信逐笔**(仓库硬约束): 两口径方向相反时, 结论必须说明差异并**以逐笔口径为准**,
   并在返回里显式标 `prefer = 'tick'`, 不许含糊。

## 预期带从哪来
不是拍的, 来自**实测**(2026-09-18, 002361 同日三源留痕):
  · 明盘 L2 vs 东财四档 ≈ **1.2%**(两家"主力"定义最接近, 都是按单金额分档, 只差分档细节);
  · 暗盘 vs 明盘 ≈ **2.75 倍**(暗盘含拆单还原 + 竞价段, 系统性偏大);
  · 暗盘 vs 东财 ≈ 两者叠加(约 2.7 倍)。
所以: 明盘/东财之间差 40% 是**异常**, 而暗盘/明盘差 2.5 倍是**预期**。
阈值与带宽写死在这里并写明依据, 不随页面口径漂。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# ── 预期带(实测依据见模块 docstring) ────────────────────────────────────────────
# 每对: (下界, 上界) 的**比值带**; 落在带内 = 预期(ok), 出带 = 需要看一眼。
EXPECTED_RATIO_BAND: dict[tuple[str, str], tuple[float, float]] = {
    ("tencent_dark", "thsdk_l2"): (1.5, 4.5),
    ("eastmoney_flow", "thsdk_l2"): (0.5, 2.0),
    ("tencent_dark", "eastmoney_flow"): (1.5, 5.0),
}

# 每对的"为什么会有差"—— 机器可读的 key → 人话。依据 = 各源的**口径定义**, 不是事后解释。
PAIR_REASON: dict[tuple[str, str], str] = {
    ("tencent_dark", "thsdk_l2"):
        "暗盘按逐笔主动成交并含拆单还原与竞价段, 明盘 L2 只按单笔金额分档 —— 暗盘系统性偏大(实测约 2~3 倍)。",
    ("eastmoney_flow", "thsdk_l2"):
        "两家都按单金额四档归类, 只是分档细节与数据面不同(东财是公开 L1 派生) —— 实测差约 1% 量级。",
    ("tencent_dark", "eastmoney_flow"):
        "逐笔(含拆单/竞价)对比 L1 派生四档 —— 差主要来自覆盖范围, 预期与「暗盘 vs 明盘」同量级。",
}

SOURCE_LABEL = {
    "thsdk_l2": "明盘 L2(TQ)",
    "tencent_dark": "暗盘(腾讯逐笔)",
    "eastmoney_flow": "东财四档",
    "ths": "同花顺 DDE",
}


@dataclass
class PairDiff:
    """一对口径的差异结论。"""

    a: str
    b: str
    label_a: str
    label_b: str
    value_a: float | None
    value_b: float | None
    abs_diff: float | None
    rel_diff: float | None      # 相对差(以较大绝对值为分母), 0.1 = 10%
    ratio: float | None         # a / b(带符号会失真, 故仅同号时给)
    level: str                  # ok | warn | alert | unknown
    expected: bool              # 是否落在实测预期带内
    sign_conflict: bool         # 方向是否相反
    prefer: str                 # 方向冲突时的采信顺序(逐笔优先)
    note: str                   # 一句解释(人话)

    def as_dict(self) -> dict[str, Any]:
        return {
            "a": self.a, "b": self.b, "label_a": self.label_a, "label_b": self.label_b,
            "value_a": self.value_a, "value_b": self.value_b,
            "abs_diff": self.abs_diff, "rel_diff": self.rel_diff, "ratio": self.ratio,
            "level": self.level, "expected": self.expected, "sign_conflict": self.sign_conflict,
            "prefer": self.prefer, "note": self.note,
        }


def _band(a: str, b: str) -> tuple[float, float] | None:
    return EXPECTED_RATIO_BAND.get((a, b)) or EXPECTED_RATIO_BAND.get((b, a))


def _reason(a: str, b: str) -> str:
    return PAIR_REASON.get((a, b)) or PAIR_REASON.get((b, a)) or "两口径定义不同, 数字不等是正常的。"


def compare_pair(a: str, b: str, value_a: float | None, value_b: float | None) -> PairDiff:
    """比一对口径, 并给出**可执行**的结论(预期 / 值得看 / 方向冲突)。"""
    la, lb = SOURCE_LABEL.get(a, a), SOURCE_LABEL.get(b, b)
    reason = _reason(a, b)

    # 不猜: 任一源缺数 → unknown, 明确说"无法比较", 不拿 0 顶上
    if value_a is None or value_b is None:
        return PairDiff(a, b, la, lb, value_a, value_b, None, None, None,
                        "unknown", False, False, "", f"缺数据无法比较({la} 或 {lb} 未取到)。{reason}")

    abs_diff = abs(value_a - value_b)
    base = max(abs(value_a), abs(value_b))
    rel_diff = (abs_diff / base) if base else 0.0
    sign_conflict = (value_a > 0 > value_b) or (value_b > 0 > value_a)
    ratio = (value_a / value_b) if value_b else None

    band = _band(a, b)
    expected = bool(band and ratio is not None and band[0] <= ratio <= band[1])

    if sign_conflict:
        # 仓库硬约束: 方向冲突必须说明差异, 且一律优先采信逐笔
        prefer = "tick" if "tencent_dark" in (a, b) else ""
        note = (f"方向冲突({la} 与 {lb} 一正一负) —— 多为「按单金额分档」与「逐笔主动买卖」的归类差异; "
                f"按仓库口径规则**一律优先采信逐笔**。{reason}")
        return PairDiff(a, b, la, lb, value_a, value_b, round(abs_diff, 2), round(rel_diff, 4),
                        None, "alert", False, True, prefer, note)  # 冲突不给 ratio: 带符号比值会失真

    if expected:
        note = f"落在实测预期带({band[0]}~{band[1]}×): 属正常口径差, 不需要处理。{reason}"
        level = "ok"
    elif band and ratio is not None and (ratio > band[1] * 1.5 or ratio < band[0] / 1.5):
        # 偏离预期带 50% 以上: 已经不是"分档细节", 先怀疑时间窗/基准日
        note = (f"比值 {ratio:.2f}× 远离预期带({band[0]}~{band[1]}×): "
                f"先查时间窗/基准日是否一致(跨基准日等于拿两天比一天)。{reason}")
        level = "alert"
    else:
        note = (f"相对差 {rel_diff * 100:.0f}%(略出预期带): 多为分档细节差异, "
                f"建议结合方向是否一致判断。{reason}")
        level = "warn"

    return PairDiff(a, b, la, lb, value_a, value_b, round(abs_diff, 2), round(rel_diff, 4),
                    None if ratio is None else round(ratio, 4), level, expected, False, "", note)


#: 对照三源的主力净流入字段名(与 caliber_compare 的 sources[].key 对齐)
DEFAULT_PAIRS: tuple[tuple[str, str], ...] = (
    ("tencent_dark", "thsdk_l2"),
    ("eastmoney_flow", "thsdk_l2"),
    ("tencent_dark", "eastmoney_flow"),
)


def build_pair_diffs(values: dict[str, float | None]) -> list[dict[str, Any]]:
    """按默认配对(三对)产出差异结论; 缺的源自动走 unknown 分支。"""
    out: list[dict[str, Any]] = []
    for a, b in DEFAULT_PAIRS:
        out.append(compare_pair(a, b, values.get(a), values.get(b)).as_dict())
    return out


def conclusion_level(diffs: list[dict[str, Any]]) -> str:
    """整页一句结论: 有 alert → alert; 否则有 warn → warn; 全 ok → ok; 全无数据 → unknown。"""
    levels = [d.get("level") for d in diffs]
    if "alert" in levels:
        return "alert"
    if "warn" in levels:
        return "warn"
    if levels and all(x == "ok" for x in levels):
        return "ok"
    return "unknown"
