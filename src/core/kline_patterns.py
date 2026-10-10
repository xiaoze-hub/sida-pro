"""K线形态识别(严格规则版) —— 供 **K 线图层标注** 使用。

与 `src/core/kline_pattern.py` 的分工(两者并存, 不要混用):
- `kline_pattern.py`: 启发式, 单/多形态宽口径, 输出**相对尾部**的 bars 索引, 供 AI 助手文本参考;
- **本模块**: 每种形态给**严格数学定义**(窗口大小 / 影线比 / 实体比 / 量能), 不做模糊阈值,
  输出**绝对索引**位置 + 逐条置信依据, 供前端在工作台 K 线图上打标注。

形态口径来源: 同花顺《K线经典形态》《K线形态大全》(skill `kline-pattern-recognition`)。
**本模块只做技术形态识别, 不构成投资建议** —— 标注文案只客观描述形态, 不出"买入/卖出"字样。

设计红线:
- 识别不出 → 显式空列表, **绝不硬凑**(宁缺勿造);
- 边界显式不识别: 窗口不足 / 一字板(range<=0) / 停牌缺口(相邻日期跨度过大);
- 只用 OHLCV, 不做预测, 不编造数字。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date as _date

EPS = 1e-9
_NAN = float("nan")
#: 低位/高位判定窗口: 形态确认根往前看多少根, 判断是否创窗口新低/新高
POSITION_LOOKBACK = 20
#: 位置类形态要求的最小前置历史根数(不够则**不识别**, 而不是放宽条件)
MIN_HISTORY = 5
#: 停牌缺口语义: 相邻 K 线日期跨度超过该天数 → 视为序列不连续(春节最长休市 < 15 天)
MAX_DATE_GAP_DAYS = 15
#: 实体参照窗口: 算"大实体/小实体"用的历史均值根数
BODY_REF_WINDOW = 10

_DIR_BULL = "看涨"
_DIR_BEAR = "看跌"
_CAT_REVERSAL = "反转"
_CAT_CONT = "持续"


def _num(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _get(bar, key):
    """兼容 dict 与对象两种 bar 表示(KlineData dataclass / PG row / 测试 FakeBar)。"""
    if isinstance(bar, dict):
        return bar.get(key)
    return getattr(bar, key, None)


@dataclass(frozen=True)
class Candle:
    """归一化 K 线。缺字段以 NaN 表示; 只有 `usable` 为真的根才参与判定。"""

    index: int
    date: str | None
    open: float
    high: float
    low: float
    close: float
    volume: float

    @property
    def valid(self) -> bool:
        return all(math.isfinite(v) for v in (self.open, self.high, self.low, self.close))

    @property
    def body(self) -> float:
        return 0.0 if not self.valid else abs(self.close - self.open)

    @property
    def upper(self) -> float:
        return 0.0 if not self.valid else self.high - max(self.open, self.close)

    @property
    def lower(self) -> float:
        return 0.0 if not self.valid else min(self.open, self.close) - self.low

    @property
    def range(self) -> float:
        return 0.0 if not self.valid else self.high - self.low

    @property
    def is_yang(self) -> bool:
        return self.valid and self.close > self.open + EPS

    @property
    def is_yin(self) -> bool:
        return self.valid and self.close < self.open - EPS

    @property
    def usable(self) -> bool:
        """可参与形态判定: 字段齐全 + 振幅>0(一字板剔除) + OHLC 内部自洽。"""
        if not self.valid or self.range <= EPS:
            return False
        return self.high + EPS >= max(self.open, self.close) and self.low - EPS <= min(
            self.open, self.close
        )


@dataclass(frozen=True)
class PatternMark:
    """识别到的一个形态标注(证据, 非建议)。"""

    name: str
    direction: str          # 看涨 / 看跌
    category: str           # 反转 / 持续
    index: int              # 形态"确认根"在输入序列中的绝对索引(0-based)
    date: str | None        # 确认根日期(有则给)
    position: str           # 低位 / 高位 / 趋势中
    basis: tuple[str, ...]  # 置信依据: 满足的严格条件清单
    definition: str         # 客观定义(标注悬停/图例用)
    span: tuple[int, int]   # 形态涉及的首末绝对索引(闭区间)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "direction": self.direction,
            "category": self.category,
            "index": self.index,
            "date": self.date,
            "position": self.position,
            "basis": list(self.basis),
            "definition": self.definition,
            "span": [self.span[0], self.span[1]],
        }


# ── 形态目录(标注图例 / 文档共用; 与下方识别函数一一对应) ────────────────────
PATTERN_CATALOG: list[dict] = [
    {
        "name": "金针探底", "category": _CAT_REVERSAL, "direction": _DIR_BULL, "window": 1,
        "definition": "单根长下影线: 下影≥2×实体、上影≤0.5×实体、实体≤0.35×振幅, 且创20日新低。"
                      "量能无硬约束(放量更可靠)。",
    },
    {
        "name": "早晨之星", "category": _CAT_REVERSAL, "direction": _DIR_BULL, "window": 3,
        "definition": "下跌末端 长阴→小实体星线(≤首阴实体0.3)→长阳(收回首阴实体中点上方), 创20日新低, 收阳放量。",
    },
    {
        "name": "看涨吞没", "category": _CAT_REVERSAL, "direction": _DIR_BULL, "window": 2,
        "definition": "阳线实体完全吞没前一根阴线实体(阳实体≥阴实体1.2倍), 且创20日新低。",
    },
    {
        "name": "黄昏之星", "category": _CAT_REVERSAL, "direction": _DIR_BEAR, "window": 3,
        "definition": "上涨末端 长阳→小实体星线(≤首阳实体0.3)→长阴(收回首阳实体中点下方), 创20日新高, 收阴放量。",
    },
    {
        "name": "看跌吞没", "category": _CAT_REVERSAL, "direction": _DIR_BEAR, "window": 2,
        "definition": "阴线实体完全吞没前一根阳线实体(阴实体≥阳实体1.2倍), 且创20日新高。",
    },
    {
        "name": "红三兵", "category": _CAT_CONT, "direction": _DIR_BULL, "window": 3,
        "definition": "三连阳且收盘递增; 后两根在前一根实体内开盘; 每根上影≤实体; 收盘根量≥首根量。",
    },
    {
        "name": "上升三法", "category": _CAT_CONT, "direction": _DIR_BULL, "window": 5,
        "definition": "大阳(实体≥历史均值1.3倍)→三根小实体整理(不破首阳低点)→再拉大阳收盘破首阳收盘, 突破根放量。",
    },
    {
        "name": "黑三兵", "category": _CAT_CONT, "direction": _DIR_BEAR, "window": 3,
        "definition": "三连阴且收盘递减; 后两根在前一根实体内开盘; 每根实体≤历史均值0.6倍(小阴)。量能无硬约束。",
    },
    {
        "name": "下降三法", "category": _CAT_CONT, "direction": _DIR_BEAR, "window": 5,
        "definition": "大阴(实体≥历史均值1.3倍)→三根小实体整理(不破首阴高点)→再落大阴收盘破首阴收盘, 破位根放量。",
    },
    {
        "name": "空方炮", "category": _CAT_CONT, "direction": _DIR_BEAR, "window": 3,
        "definition": "阴-阳-阴序列: 第三根阴线收盘低于第一根阴线收盘, 中间阳线未创新高, 第三根量≥第二根量。",
    },
]

_CATALOG_BY_NAME = {p["name"]: p for p in PATTERN_CATALOG}


def pattern_catalog() -> list[dict]:
    """形态目录(供 API / 前端图例展示定义)。"""
    return [dict(p) for p in PATTERN_CATALOG]


def _definition(name: str) -> str:
    return _CATALOG_BY_NAME.get(name, {}).get("definition", "")


# ── 构造与基础工具 ────────────────────────────────────────────────────────────


def _build_candles(bars) -> list[Candle]:
    out: list[Candle] = []

    def _f(bar, key) -> float:
        v = _num(_get(bar, key))
        return v if v is not None else _NAN

    for i, b in enumerate(bars or []):
        d = _get(b, "date")
        out.append(
            Candle(
                index=i,
                date=(str(d)[:10] if d else None),
                open=_f(b, "open"),
                high=_f(b, "high"),
                low=_f(b, "low"),
                close=_f(b, "close"),
                volume=_f(b, "volume") if _num(_get(b, "volume")) is not None else 0.0,
            )
        )
    return out


def _avg_body(lo: int, hi: int, candles: list[Candle]) -> float:
    """[lo, hi) 区间内可用实体的均值(0 表示无参照)。"""
    vals = [c.body for c in candles[max(0, lo):hi] if c.usable and c.body > EPS]
    return sum(vals) / len(vals) if vals else 0.0


def _window_ok(candles: list[Candle], lo: int, hi: int) -> bool:
    """[lo, hi] 闭区间内所有根可用 + 日期连续(停牌缺口 → 不识别)。"""
    if lo < 0 or hi >= len(candles):
        return False
    for c in candles[lo : hi + 1]:
        if not c.usable:
            return False
    # 连续性检查带上**形态前一根**(缺口可能出现在进入形态处, 如停牌后复牌)
    return _dates_continuous(candles, max(0, lo - 1), hi)


def _dates_continuous(candles: list[Candle], lo: int, hi: int) -> bool:
    """相邻日期跨度 > MAX_DATE_GAP_DAYS 视为不连续(停牌/长期缺口)。

    日期缺失 → 无法判断, 按连续处理(不因缺日期就否决)。
    """
    prev: _date | None = None
    for c in candles[lo : hi + 1]:
        if not c.date:
            prev = None
            continue
        try:
            d = _date.fromisoformat(c.date)
        except ValueError:
            prev = None
            continue
        if prev is not None and (d - prev).days > MAX_DATE_GAP_DAYS:
            return False
        prev = d
    return True


def _preceding_low(candles: list[Candle], start: int) -> float | None:
    """形态首根之前 POSITION_LOOKBACK 根的最低点; 历史不足 MIN_HISTORY → None(不识别)。"""
    lo = max(0, start - POSITION_LOOKBACK)
    seg = candles[lo:start]
    if len(seg) < MIN_HISTORY:
        return None
    return min(c.low for c in seg)


def _preceding_high(candles: list[Candle], start: int) -> float | None:
    lo = max(0, start - POSITION_LOOKBACK)
    seg = candles[lo:start]
    if len(seg) < MIN_HISTORY:
        return None
    return max(c.high for c in seg)


def _is_new_low(candles: list[Candle], lo: int, hi: int) -> bool:
    """形态涉及根的最低点是否创前置窗口新低(低位)。"""
    ref = _preceding_low(candles, lo)
    if ref is None:
        return False
    pat_low = min(c.low for c in candles[lo : hi + 1])
    return pat_low <= ref + EPS


def _is_new_high(candles: list[Candle], lo: int, hi: int) -> bool:
    ref = _preceding_high(candles, lo)
    if ref is None:
        return False
    pat_high = max(c.high for c in candles[lo : hi + 1])
    return pat_high >= ref - EPS


# ── 各形态判定(返回满足的条件清单; None = 不成立) ────────────────────────────


def _m_bullish_hammer(candles: list[Candle], i: int) -> tuple[str, ...] | None:
    """金针探底: 单根 i。"""
    c = candles[i]
    if not _window_ok(candles, i, i) or c.body <= EPS:
        return None
    if c.lower < 2.0 * c.body - EPS:
        return None
    if c.upper > 0.5 * c.body + EPS:
        return None
    if c.body > 0.35 * c.range + EPS:
        return None
    if not _is_new_low(candles, i, i):
        return None
    return (
        "下影≥2×实体",
        "上影≤0.5×实体",
        "实体≤0.35×振幅",
        f"创{POSITION_LOOKBACK}日新低(低位)",
    )


def _m_morning_star(candles: list[Candle], i: int) -> tuple[str, ...] | None:
    """早晨之星: i-2(长阴) i-1(星线) i(长阳)。"""
    if i < 3:
        return None
    lo = i - 2
    if not _window_ok(candles, lo, i):
        return None
    a, b, c = candles[i - 2], candles[i - 1], candles[i]
    if candles[i - 3].close is None:
        return None
    if not (a.is_yin and a.body > EPS and c.is_yang and c.body > EPS):
        return None
    if not (candles[i - 3].close > a.close):  # 形态前处于下跌
        return None
    if b.body > 0.3 * a.body + EPS:
        return None
    if c.close < (a.open + a.close) / 2 - EPS:  # 收回首阴实体中点上方
        return None
    if c.volume <= b.volume:  # 收阳放量
        return None
    if not _is_new_low(candles, lo, i):
        return None
    return (
        "首根长阴(下跌末端)",
        "次根星线(实体≤首阴0.3)",
        "末根长阳收回首阴实体中点上方",
        "末根量>星线量(放量)",
        f"创{POSITION_LOOKBACK}日新低(低位)",
    )


def _m_bullish_engulfing(candles: list[Candle], i: int) -> tuple[str, ...] | None:
    """看涨吞没: i-1(阴) i(阳)。"""
    if i < 1:
        return None
    p, c = candles[i - 1], candles[i]
    if not _window_ok(candles, i - 1, i):
        return None
    if not (p.is_yin and p.body > EPS and c.is_yang and c.body > EPS):
        return None
    if not (c.open <= p.close + EPS and c.close >= p.open - EPS):
        return None
    if c.body < 1.2 * p.body - EPS:
        return None
    if not _is_new_low(candles, i - 1, i):
        return None
    return (
        "阳线实体完全吞没前阴实体",
        "阳实体≥阴实体1.2倍",
        f"创{POSITION_LOOKBACK}日新低(低位)",
    )


def _m_evening_star(candles: list[Candle], i: int) -> tuple[str, ...] | None:
    """黄昏之星: i-2(长阳) i-1(星线) i(长阴)。"""
    if i < 3:
        return None
    lo = i - 2
    if not _window_ok(candles, lo, i):
        return None
    a, b, c = candles[i - 2], candles[i - 1], candles[i]
    if candles[i - 3].close is None:
        return None
    if not (a.is_yang and a.body > EPS and c.is_yin and c.body > EPS):
        return None
    if not (candles[i - 3].close < a.close):  # 形态前处于上涨
        return None
    if b.body > 0.3 * a.body + EPS:
        return None
    if c.close > (a.open + a.close) / 2 + EPS:  # 收回首阳实体中点下方
        return None
    if c.volume <= b.volume:  # 收阴放量
        return None
    if not _is_new_high(candles, lo, i):
        return None
    return (
        "首根长阳(上涨末端)",
        "次根星线(实体≤首阳0.3)",
        "末根长阴收回首阳实体中点下方",
        "末根量>星线量(放量)",
        f"创{POSITION_LOOKBACK}日新高(高位)",
    )


def _m_bearish_engulfing(candles: list[Candle], i: int) -> tuple[str, ...] | None:
    """看跌吞没: i-1(阳) i(阴)。"""
    if i < 1:
        return None
    p, c = candles[i - 1], candles[i]
    if not _window_ok(candles, i - 1, i):
        return None
    if not (p.is_yang and p.body > EPS and c.is_yin and c.body > EPS):
        return None
    if not (c.open >= p.close - EPS and c.close <= p.open + EPS):
        return None
    if c.body < 1.2 * p.body - EPS:
        return None
    if not _is_new_high(candles, i - 1, i):
        return None
    return (
        "阴线实体完全吞没前阳实体",
        "阴实体≥阳实体1.2倍",
        f"创{POSITION_LOOKBACK}日新高(高位)",
    )


def _m_three_white_soldiers(candles: list[Candle], i: int) -> tuple[str, ...] | None:
    """红三兵: i-2 i-1 i 三连阳收盘递增。"""
    if i < 2:
        return None
    if not _window_ok(candles, i - 2, i):
        return None
    c0, c1, c2 = candles[i - 2], candles[i - 1], candles[i]
    if not (c0.is_yang and c1.is_yang and c2.is_yang):
        return None
    if not (c0.close < c1.close < c2.close):
        return None
    # 后两根在前一根实体内开盘(经典红三兵口径)
    if not (c0.open <= c1.open <= c0.close and c1.open <= c2.open <= c1.close):
        return None
    if any(cc.upper > cc.body + EPS for cc in (c0, c1, c2)):
        return None
    if c2.volume < c0.volume - EPS:  # 收盘根量不小于首根量
        return None
    return (
        "三连阳且收盘递增",
        "后两根在前一根实体内开盘",
        "每根上影≤实体",
        "收盘根量≥首根量",
    )


def _m_rising_three(candles: list[Candle], i: int) -> tuple[str, ...] | None:
    """上升三法: i-4(大阳) i-3..i-1(小实体整理) i(大阳突破)。"""
    if i < 5:
        return None
    lo = i - 4
    if not _window_ok(candles, lo, i):
        return None
    f = candles[i - 4]
    mids = candles[i - 3 : i]
    z = candles[i]
    ref = _avg_body(max(0, lo - BODY_REF_WINDOW), lo, candles)
    if ref <= EPS:
        return None
    if not (f.is_yang and f.body >= 1.3 * ref - EPS):
        return None
    for m in mids:
        if m.body > 0.6 * f.body + EPS:
            return None
        if m.low < f.low - EPS or m.high > f.high + EPS:  # 不破首阳区间
            return None
    if not (z.is_yang and z.body >= 1.0 * ref - EPS and z.close > f.close + EPS):
        return None
    if z.volume < mids[-1].volume - EPS:  # 突破根放量
        return None
    return (
        "首根大阳(实体≥历史均值1.3倍)",
        "中间三根小实体且不破首阳区间",
        "末根大阳收盘破首阳收盘",
        "突破根量≥整理根量",
    )


def _m_three_black_crows(candles: list[Candle], i: int) -> tuple[str, ...] | None:
    """黑三兵: i-2 i-1 i 三连小阴收盘递减。"""
    if i < 2:
        return None
    if not _window_ok(candles, i - 2, i):
        return None
    c0, c1, c2 = candles[i - 2], candles[i - 1], candles[i]
    if not (c0.is_yin and c1.is_yin and c2.is_yin):
        return None
    if not (c0.close > c1.close > c2.close):
        return None
    ref = _avg_body(max(0, i - 2 - BODY_REF_WINDOW), i - 2, candles)
    if ref <= EPS:
        return None
    if any(cc.body > 0.6 * ref + EPS for cc in (c0, c1, c2)):
        return None
    # 后两根在前一根实体内开盘(阴线实体区间为 [收盘, 开盘])
    if not (c0.close <= c1.open <= c0.open and c1.close <= c2.open <= c1.open):
        return None
    return (
        "三连阴且收盘递减",
        "后两根在前一根实体内开盘",
        "每根实体≤历史均值0.6倍(小阴)",
    )


def _m_falling_three(candles: list[Candle], i: int) -> tuple[str, ...] | None:
    """下降三法: i-4(大阴) i-3..i-1(小实体整理) i(大阴破位)。"""
    if i < 5:
        return None
    lo = i - 4
    if not _window_ok(candles, lo, i):
        return None
    f = candles[i - 4]
    mids = candles[i - 3 : i]
    z = candles[i]
    ref = _avg_body(max(0, lo - BODY_REF_WINDOW), lo, candles)
    if ref <= EPS:
        return None
    if not (f.is_yin and f.body >= 1.3 * ref - EPS):
        return None
    for m in mids:
        if m.body > 0.6 * f.body + EPS:
            return None
        if m.high > f.high + EPS or m.low < f.low - EPS:  # 不破首阴区间
            return None
    if not (z.is_yin and z.body >= 1.0 * ref - EPS and z.close < f.close - EPS):
        return None
    if z.volume < mids[-1].volume - EPS:
        return None
    return (
        "首根大阴(实体≥历史均值1.3倍)",
        "中间三根小实体且不破首阴区间",
        "末根大阴收盘破首阴收盘",
        "破位根量≥整理根量",
    )


def _m_bearish_cannon(candles: list[Candle], i: int) -> tuple[str, ...] | None:
    """空方炮: i-2(阴) i-1(阳) i(阴), 第三根收盘低于第一根。"""
    if i < 2:
        return None
    if not _window_ok(candles, i - 2, i):
        return None
    c0, c1, c2 = candles[i - 2], candles[i - 1], candles[i]
    if not (c0.is_yin and c1.is_yang and c2.is_yin):
        return None
    if c2.close >= c0.close - EPS:  # 第三根必须收在首根下方
        return None
    if c1.high > c0.high + EPS:  # 中间反弹未创新高
        return None
    if c2.volume < c1.volume - EPS:  # 下砸有量
        return None
    return (
        "阴-阳-阴序列",
        "第三根收盘低于首根收盘",
        "中间阳线未创新高",
        "第三根量≥第二根量",
    )


#: (形态名, 方向, category, 判定函数) —— 顺序即输出稳定序
_DETECTORS = [
    ("金针探底", _DIR_BULL, _CAT_REVERSAL, _m_bullish_hammer),
    ("早晨之星", _DIR_BULL, _CAT_REVERSAL, _m_morning_star),
    ("看涨吞没", _DIR_BULL, _CAT_REVERSAL, _m_bullish_engulfing),
    ("黄昏之星", _DIR_BEAR, _CAT_REVERSAL, _m_evening_star),
    ("看跌吞没", _DIR_BEAR, _CAT_REVERSAL, _m_bearish_engulfing),
    ("红三兵", _DIR_BULL, _CAT_CONT, _m_three_white_soldiers),
    ("上升三法", _DIR_BULL, _CAT_CONT, _m_rising_three),
    ("黑三兵", _DIR_BEAR, _CAT_CONT, _m_three_black_crows),
    ("下降三法", _DIR_BEAR, _CAT_CONT, _m_falling_three),
    ("空方炮", _DIR_BEAR, _CAT_CONT, _m_bearish_cannon),
]

_WINDOW_BY_NAME = {p["name"]: p["window"] for p in PATTERN_CATALOG}


def _position_label(name: str, direction: str) -> str:
    cat = _CATALOG_BY_NAME.get(name, {}).get("category")
    if cat == _CAT_REVERSAL:
        return "低位" if direction == _DIR_BULL else "高位"
    return "趋势中"


def detect_pattern_marks(bars, lookback: int | None = None) -> list[PatternMark]:
    """在 K 线序列上识别严格形态, 返回标注列表(按确认根索引升序)。

    bars: 升序 K 线序列(含 open/high/low/close/volume[/date])。
    lookback: 仅返回**确认根**落在最后 `lookback` 根内的标注; None = 全序列扫描。

    识别不出返回空列表(不硬凑)。
    """
    candles = _build_candles(bars)
    n = len(candles)
    if n < 3:
        return []
    lo_bound = 0 if not lookback else max(0, n - int(lookback))

    marks: list[PatternMark] = []
    for i in range(n):
        if i < lo_bound:
            continue
        for name, direction, category, fn in _DETECTORS:
            window = _WINDOW_BY_NAME[name]
            span_lo = i - (window - 1)
            if span_lo < 0:
                continue  # 窗口不足 → 不识别
            basis = fn(candles, i)
            if not basis:
                continue
            marks.append(
                PatternMark(
                    name=name,
                    direction=direction,
                    category=category,
                    index=i,
                    date=candles[i].date,
                    position=_position_label(name, direction),
                    basis=tuple(basis),
                    definition=_definition(name),
                    span=(span_lo, i),
                )
            )
    marks.sort(key=lambda m: (m.index, m.name))
    return marks


def format_marks(marks: list[PatternMark]) -> str:
    """格式化为客观文本(证据, 不含买卖建议)。"""
    if not marks:
        return "近期未识别到符合严格定义的K线形态。"
    lines = [f"识别到 {len(marks)} 个K线形态(证据标注, 非投资建议):"]
    for m in marks:
        lines.append(f"- 【{m.name}】{m.direction} · {m.category} · {m.position} @ {m.date or m.index}")
        lines.append(f"  依据: {'; '.join(m.basis)}")
    return "\n".join(lines)
