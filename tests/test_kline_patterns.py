"""严格规则 K 线形态识别(src.core.kline_patterns)测试。

覆盖: 10 种形态各 1 正例 + 1 反例; 边界(窗口不足/一字板/停牌缺口)显式不识别;
输出契约(绝对索引 / 方向 / 置信依据); 目录完整性。

**全部合成 K 线序列, 不发网络请求。**
"""

from __future__ import annotations

import pytest

from src.core import kline_patterns as P


def B(o, h, l, c, v=100.0, d=None):
    """一根 K 线(dict 形式, 与 API 序列化后一致)。"""
    bar = {"open": o, "high": h, "low": l, "close": c, "volume": v}
    if d:
        bar["date"] = d
    return bar


def _days(n, start=1):
    return [f"2026-03-{start + i:02d}" for i in range(n)]


def _with_dates(bars, start=1):
    return [dict(b, date=d) for b, d in zip(bars, _days(len(bars), start))]


def names(marks):
    return [m.name for m in marks]


# ── 反转类 ────────────────────────────────────────────────────────────────────

_HEADS = [  # 6 根温和下跌的前置背景(低点 9.8..9.55, 均 > 9.0)
    B(10.0, 10.1, 9.8, 9.9),
    B(9.9, 10.0, 9.7, 9.8),
    B(9.8, 9.9, 9.6, 9.7),
    B(9.7, 9.8, 9.6, 9.7),
    B(9.7, 9.8, 9.5, 9.6),
    B(9.6, 9.7, 9.55, 9.65),
]


def test_金针探底_正例():
    # 实体 0.10, 下影 0.80(8×), 上影 0.05(0.5×), 实体 0.10<=0.35×振幅(0.95), 创窗口新低
    bars = _HEADS + [B(9.80, 9.95, 9.0, 9.90, 300)]
    marks = P.detect_pattern_marks(bars)
    assert "金针探底" in names(marks)
    hit = next(m for m in marks if m.name == "金针探底")
    assert hit.direction == "看涨" and hit.category == "反转" and hit.position == "低位"
    assert hit.index == len(bars) - 1
    assert any("下影≥2×实体" in b for b in hit.basis)


def test_金针探底_反例_下影不足():
    # 下影仅 0.10(1×实体), 不满足 ≥2×
    bars = _HEADS + [B(9.60, 9.72, 9.50, 9.70, 300)]
    assert "金针探底" not in names(P.detect_pattern_marks(bars))


def test_早晨之星_正例():
    bars = _HEADS + [
        B(10.2, 10.25, 9.0, 9.1, 150),   # 长阴(收盘 9.1 < 前收 9.65)
        B(9.00, 9.15, 8.95, 9.05, 80),   # 星线(实体 0.05 <= 0.3×1.1)
        B(9.10, 10.30, 9.05, 10.20, 200),  # 长阳收回首阴中点(9.65)上方, 放量
    ]
    marks = P.detect_pattern_marks(bars)
    assert "早晨之星" in names(marks)
    hit = next(m for m in marks if m.name == "早晨之星")
    assert hit.direction == "看涨" and hit.position == "低位"
    assert hit.span == (len(bars) - 3, len(bars) - 1)


def test_早晨之星_反例_星线实体过大():
    bars = _HEADS + [
        B(10.2, 10.25, 9.0, 9.1, 150),
        B(9.00, 9.60, 8.95, 9.50, 80),   # 星线实体 0.50 > 0.3×1.1=0.33
        B(9.60, 10.30, 9.55, 10.20, 200),
    ]
    assert "早晨之星" not in names(P.detect_pattern_marks(bars))


def test_看涨吞没_正例():
    bars = _HEADS + [
        B(9.90, 10.00, 9.40, 9.50, 100),   # 阴线实体 0.40
        B(9.45, 9.95, 9.35, 9.95, 200),    # 阳线实体 0.50(≥1.2×), 吞没前阴, 创新低
    ]
    marks = P.detect_pattern_marks(bars)
    assert "看涨吞没" in names(marks)
    hit = next(m for m in marks if m.name == "看涨吞没")
    assert hit.direction == "看涨" and hit.index == len(bars) - 1


def test_看涨吞没_反例_未吞没():
    bars = _HEADS + [
        B(9.90, 10.00, 9.40, 9.50, 100),
        B(9.60, 9.95, 9.35, 9.90, 200),    # 开盘 9.60 > 前收 9.50 → 未吞没
    ]
    assert "看涨吞没" not in names(P.detect_pattern_marks(bars))


_RISING_HEADS = [  # 前置温和上涨(高点 9.6..10.05)
    B(9.5, 9.6, 9.45, 9.55),
    B(9.55, 9.7, 9.5, 9.65),
    B(9.65, 9.8, 9.6, 9.75),
    B(9.75, 9.9, 9.7, 9.85),
    B(9.85, 10.0, 9.8, 9.95),
    B(9.95, 10.05, 9.9, 10.0),
]


def test_黄昏之星_正例():
    bars = _RISING_HEADS + [
        B(10.0, 11.2, 9.95, 11.1, 150),   # 长阳(收盘 11.1 > 前收 10.0)
        B(11.05, 11.15, 11.0, 11.05, 80),  # 星线(实体 0.05)
        B(11.05, 11.10, 9.95, 10.05, 200),  # 长阴收回首阳中点(10.55)下方, 放量
    ]
    marks = P.detect_pattern_marks(bars)
    assert "黄昏之星" in names(marks)
    hit = next(m for m in marks if m.name == "黄昏之星")
    assert hit.direction == "看跌" and hit.position == "高位"


def test_黄昏之星_反例_末根未收回中点():
    bars = _RISING_HEADS + [
        B(10.0, 11.2, 9.95, 11.1, 150),
        B(11.05, 11.15, 11.0, 11.05, 80),
        B(11.05, 11.10, 10.6, 10.7, 200),  # 收盘 10.7 > 中点 10.55
    ]
    assert "黄昏之星" not in names(P.detect_pattern_marks(bars))


def test_看跌吞没_正例():
    bars = _RISING_HEADS + [
        B(9.5, 10.2, 9.45, 9.9, 100),   # 阳线实体 0.40, 创窗口新高
        B(9.95, 10.25, 9.4, 9.45, 200),  # 阴线实体 0.50, 吞没前阳
    ]
    marks = P.detect_pattern_marks(bars)
    assert "看跌吞没" in names(marks)
    hit = next(m for m in marks if m.name == "看跌吞没")
    assert hit.direction == "看跌" and hit.position == "高位"


def test_看跌吞没_反例_实体不够大():
    bars = _RISING_HEADS + [
        B(9.5, 10.0, 9.45, 9.9, 100),
        B(9.95, 10.0, 9.4, 9.5, 200),   # 阴实体 0.45 < 1.2×0.40=0.48
    ]
    assert "看跌吞没" not in names(P.detect_pattern_marks(bars))


# ── 持续类 ────────────────────────────────────────────────────────────────────

def test_红三兵_正例():
    bars = [
        B(10.0, 10.45, 9.9, 10.4, 100),   # 实体 0.40, 上影 0.05
        B(10.2, 10.75, 10.1, 10.7, 120),  # 10.2 在 [10.0,10.4] 内开盘
        B(10.5, 11.05, 10.4, 11.0, 130),  # 10.5 在 [10.2,10.7] 内开盘
    ]
    marks = P.detect_pattern_marks(bars)
    assert "红三兵" in names(marks)
    hit = next(m for m in marks if m.name == "红三兵")
    assert hit.direction == "看涨" and hit.category == "持续" and hit.position == "趋势中"


def test_红三兵_反例_收盘不递增():
    bars = [
        B(10.0, 10.45, 9.9, 10.4, 100),
        B(10.2, 10.75, 10.1, 10.7, 120),
        B(10.5, 10.6, 10.3, 10.35, 130),  # 收盘 10.35 < 10.7
    ]
    assert "红三兵" not in names(P.detect_pattern_marks(bars))


def _flat_body_heads(n=10, body=0.2, base=10.0):
    # 历史均值实体 0.2(参照)
    return [B(base, base + body + 0.1, base - 0.05, base + body, 100) for _ in range(n)]


def test_上升三法_正例():
    bars = _flat_body_heads() + [
        B(10.0, 11.1, 9.95, 11.0, 200),   # 大阳 实体 1.0 ≥ 1.3×0.2
        B(10.9, 10.95, 10.5, 10.7, 100),  # 小实体整理
        B(10.7, 10.95, 10.6, 10.85, 90),
        B(10.85, 11.0, 10.6, 10.75, 80),
        B(10.8, 11.6, 10.75, 11.5, 150),  # 大阳突破, 收盘 11.5 > 首阳 11.0
    ]
    marks = P.detect_pattern_marks(bars)
    assert "上升三法" in names(marks)
    hit = next(m for m in marks if m.name == "上升三法")
    assert hit.direction == "看涨" and hit.span[1] == len(bars) - 1


def test_上升三法_反例_末根未破首阳():
    bars = _flat_body_heads() + [
        B(10.0, 11.1, 9.95, 11.0, 200),
        B(10.9, 10.95, 10.5, 10.7, 100),
        B(10.7, 10.95, 10.6, 10.85, 90),
        B(10.85, 11.0, 10.6, 10.75, 80),
        B(10.8, 11.0, 10.7, 10.9, 150),   # 收盘 10.9 < 首阳收盘 11.0
    ]
    assert "上升三法" not in names(P.detect_pattern_marks(bars))


def test_黑三兵_正例():
    # 参照实体均值 0.5 → 每根实体须 ≤ 0.3
    bars = _flat_body_heads(body=0.5) + [
        B(11.0, 11.05, 10.8, 10.85, 100),  # 实体 0.15
        B(10.9, 10.95, 10.65, 10.7, 90),   # 实体 0.20, 10.9 在 [10.85,11.0] 内开盘
        B(10.75, 10.8, 10.55, 10.6, 80),   # 实体 0.15, 10.75 在 [10.7,10.9] 内开盘
    ]
    marks = P.detect_pattern_marks(bars)
    assert "黑三兵" in names(marks)
    hit = next(m for m in marks if m.name == "黑三兵")
    assert hit.direction == "看跌" and hit.category == "持续"


def test_黑三兵_反例_实体过大():
    bars = _flat_body_heads(body=0.5) + [
        B(11.0, 11.05, 9.9, 10.0, 100),   # 实体 1.0 > 0.3
        B(10.9, 10.95, 9.8, 9.9, 90),
        B(10.75, 10.85, 9.7, 9.8, 80),
    ]
    assert "黑三兵" not in names(P.detect_pattern_marks(bars))


def test_下降三法_正例():
    bars = _flat_body_heads() + [
        B(11.0, 11.05, 10.0, 10.05, 200),  # 大阴 实体 0.95 ≥ 0.26
        B(10.1, 10.5, 10.05, 10.3, 100),   # 小实体整理
        B(10.3, 10.5, 10.15, 10.25, 90),
        B(10.25, 10.45, 10.05, 10.15, 80),
        B(10.2, 10.25, 9.5, 9.6, 150),     # 大阴破位, 收盘 9.6 < 首阴 10.05
    ]
    marks = P.detect_pattern_marks(bars)
    assert "下降三法" in names(marks)
    hit = next(m for m in marks if m.name == "下降三法")
    assert hit.direction == "看跌"


def test_下降三法_反例_末根未破首阴():
    bars = _flat_body_heads() + [
        B(11.0, 11.05, 10.0, 10.05, 200),
        B(10.1, 10.5, 10.05, 10.3, 100),
        B(10.3, 10.5, 10.15, 10.25, 90),
        B(10.25, 10.45, 10.05, 10.15, 80),
        B(10.2, 10.25, 10.0, 10.1, 150),   # 收盘 10.1 > 首阴收盘 10.05
    ]
    assert "下降三法" not in names(P.detect_pattern_marks(bars))


def test_空方炮_正例():
    bars = [
        B(11.0, 11.05, 10.55, 10.6, 100),  # 阴
        B(10.6, 10.95, 10.55, 10.8, 80),   # 阳, 高点 10.95 ≤ 首根高点 11.05
        B(10.8, 10.85, 10.35, 10.4, 120),  # 阴, 收盘 10.4 < 首根收盘 10.6, 量放大
    ]
    marks = P.detect_pattern_marks(bars)
    assert "空方炮" in names(marks)
    hit = next(m for m in marks if m.name == "空方炮")
    assert hit.direction == "看跌" and hit.index == 2


def test_空方炮_反例_第三根未破首根():
    bars = [
        B(11.0, 11.05, 10.55, 10.6, 100),
        B(10.6, 10.95, 10.55, 10.8, 80),
        B(10.8, 10.85, 10.6, 10.7, 120),   # 收盘 10.7 > 首根收盘 10.6
    ]
    assert "空方炮" not in names(P.detect_pattern_marks(bars))


# ── 边界: 显式不识别 ──────────────────────────────────────────────────────────

def test_窗口不足_不识别():
    # 只有 2 根 → 任何形态都不识别
    assert P.detect_pattern_marks([B(10, 10.1, 9.9, 10), B(10, 10.1, 9.9, 10)]) == []


def test_前置历史不足_位置类形态不识别():
    # 金针形态成立, 但前置仅 4 根(< MIN_HISTORY=5) → 位置无法判定 → 不识别
    bars = [
        B(10.0, 10.1, 9.8, 9.9),
        B(9.9, 10.0, 9.7, 9.8),
        B(9.8, 9.9, 9.6, 9.7),
        B(9.7, 9.8, 9.6, 9.7),
        B(9.80, 9.95, 9.0, 9.90, 300),   # 金针形态本身满足
    ]
    assert "金针探底" not in names(P.detect_pattern_marks(bars))
    assert P.MIN_HISTORY == 5


def test_一字板_形态内出现则不识别():
    # 早晨之星结构中, 星线为一字板(o=h=l=c, 振幅 0) → 整窗不可用 → 不识别
    bars = _HEADS + [
        B(10.2, 10.25, 9.0, 9.1, 150),
        B(9.05, 9.05, 9.05, 9.05, 0),    # 一字板
        B(9.10, 10.30, 9.05, 10.20, 200),
    ]
    assert "早晨之星" not in names(P.detect_pattern_marks(bars))


def test_全一字板序列_无形态():
    bars = [B(10, 10, 10, 10, 0) for _ in range(30)]
    assert P.detect_pattern_marks(bars) == []


def test_停牌缺口_不识别():
    # 形态跨越 20 天缺口 → 视为序列不连续 → 不识别
    bars = _with_dates(_HEADS + [
        B(10.2, 10.25, 9.0, 9.1, 150),
        B(9.00, 9.15, 8.95, 9.05, 80),
        B(9.10, 10.30, 9.05, 10.20, 200),
    ], start=1)
    # 把最后三根日期整体后移 30 天, 制造 > MAX_DATE_GAP_DAYS 的缺口
    bars[-3]["date"] = "2026-04-15"
    bars[-2]["date"] = "2026-04-16"
    bars[-1]["date"] = "2026-04-17"
    assert "早晨之星" not in names(P.detect_pattern_marks(bars))
    assert P.MAX_DATE_GAP_DAYS == 15


def test_无形态序列_空数组不硬凑():
    # 纯横盘震荡(无严格形态) → 空列表
    bars = [B(10 + (i % 2) * 0.1, 10.2 + (i % 2) * 0.1, 9.9, 10 + (i % 2) * 0.1, 100) for i in range(30)]
    assert P.detect_pattern_marks(bars) == []


# ── 输出契约 ──────────────────────────────────────────────────────────────────

def test_输出契约_字段齐全():
    bars = _HEADS + [B(9.80, 9.95, 9.0, 9.90, 300)]
    hit = next(m for m in P.detect_pattern_marks(bars) if m.name == "金针探底")
    d = hit.to_dict()
    for key in ("name", "direction", "category", "index", "date", "position", "basis", "definition", "span"):
        assert key in d, f"缺字段 {key}"
    assert isinstance(d["index"], int) and d["index"] >= 0
    assert isinstance(d["basis"], list) and d["basis"]
    assert d["direction"] in ("看涨", "看跌")
    assert d["definition"]  # 客观定义非空


def test_对象形式_bar_同样可识别():
    """兼容 KlineData dataclass / 测试 FakeBar(getattr) 与 dict 两种 bar。"""

    class FakeBar:
        def __init__(self, o, h, l, c, v):
            self.open, self.high, self.low, self.close, self.volume = o, h, l, c, v

    bars = [FakeBar(b["open"], b["high"], b["low"], b["close"], b["volume"]) for b in _HEADS]
    bars.append(FakeBar(9.80, 9.95, 9.0, 9.90, 300))
    assert "金针探底" in names(P.detect_pattern_marks(bars))


def test_lookback_只返回尾部窗口内的确认根():
    bars = _HEADS + [B(9.80, 9.95, 9.0, 9.90, 300)]
    assert "金针探底" in names(P.detect_pattern_marks(bars, lookback=3))
    # 确认根在最后 1 根内; 用 lookback=0 之外的小窗口仍应返回(索引=末根)
    marks = P.detect_pattern_marks(bars, lookback=1)
    assert all(m.index >= len(bars) - 1 for m in marks)


def test_目录_十种形态字段齐全():
    cat = P.pattern_catalog()
    assert len(cat) == 10
    assert {c["category"] for c in cat} == {"反转", "持续"}
    assert sum(1 for c in cat if c["category"] == "反转") == 5
    assert sum(1 for c in cat if c["category"] == "持续") == 5
    for c in cat:
        assert c["direction"] in ("看涨", "看跌")
        assert isinstance(c["window"], int) and c["window"] >= 1
        assert c["definition"]


def test_不含投资建议字样():
    """标注是证据不是建议: 定义/依据文案不得出现买卖建议。"""
    banned = ("建议买入", "建议卖出", "推荐", "抄底", "必买", "立即买入")
    bars = _HEADS + [
        B(10.2, 10.25, 9.0, 9.1, 150),
        B(9.00, 9.15, 8.95, 9.05, 80),
        B(9.10, 10.30, 9.05, 10.20, 200),
    ]
    for m in P.detect_pattern_marks(bars):
        text = m.definition + " ".join(m.basis)
        for w in banned:
            assert w not in text, f"标注含建议字样: {w}"


def test_format_marks_空():
    assert "未识别到" in P.format_marks([])


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-q"])
