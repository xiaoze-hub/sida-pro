"""威科夫量价结构标注(本土三层滤网 P1: 第三层)。

输入日 K 序列, 输出结构化标注 `{markers, verdict}`, 供
`GET /klines/{symbol}/wyckoff` 与 K 线图层消费(与 `/patterns` 同范式)。

口径红线(见 `src/core/caliber.py`):
- 本模块只描述**日 K 量价结构**(放量/缩量/影线/位置), 文案禁用
  "主力/建仓/出货/吸筹/派发"等方向性词;
- 不做主力意图判定, 不调用 `require_directional`, 不消费逐笔/四档资金数据;
- 方向性判定唯一走 `get_main_intent`(tick 逐笔)。本模块 markers 的
  direction(bullish/bearish/neutral)只是**图上记号配色**, 不是方向结论。

纯函数: 无网络/DB/时钟依赖, 同输入必同输出(可单测、可回放)。
bars 元素支持 dict 或对象(与 `kline_patterns._get` 同鸭子类型)。
"""

from __future__ import annotations

from dataclasses import dataclass


# ── 阈值(即文档; 调参必须同步改 docs/_archive/20261026_design/本土三层滤网_设计_20261016.md §3) ──
VOL_SPIKE = 1.8        # 放量: 量 ≥1.8×20 日均量
VOL_BREAKOUT = 1.5     # 突破放量: 量 ≥1.5×20 日均量
VOL_DRY = 0.6          # 缩量: 量 ≤0.6×20 日均量
FLAT_PCT = 1.0         # 滞涨: |涨幅| < 1.0%
DRY_PCT = 1.5          # 沉寂: |涨幅| < 1.5%
SHADOW_RATIO = 1.5     # 冲高回落: 上影 ≥1.5×实体
AVG_WINDOW = 20        # 均量窗口
SUPPORT_WINDOW = 10    # 弹簧/支撑观察窗口
RESIST_WINDOW = 20     # 突破新高窗口
SPRING_RECOVERY_PCT = 1.0  # 弹簧有效回收: (收盘-最低)/最低 ≥1%(防阴跌碎步刷屏)
MIN_BARS = 25          # 最少 bar 数(均量 20 + 观察余量); 不足 → 不识别
DRAW_DOWN_A = 10.0     # Phase A 粗判: 近 20 日最大跌幅超 10%

DIR_BULL = "bullish"
DIR_BEAR = "bearish"
DIR_NEUTRAL = "neutral"

CAT_STRUCTURE = "量价结构"

DEFINITIONS = {
    "effort_divergence": (
        "放量滞涨: 成交量≥1.8×20日均量但涨幅<1%(或放量冲高回落,"
        "上影≥1.5×实体且收盘在下半部) —— 努力(量)大而结果(价)小,"
        "上方有供应承接, 追高需谨慎。"
    ),
    "spring_probe": (
        "弹簧试探(待确认): 盘中跌破近10日最低但收盘收回其上, 且从最低回收≥1% —— "
        "疑似终极洗盘(Spring), 须次日缩量企稳(LPS)才确认; 未收回/回收不足不打标。"
    ),
    "dry_up": (
        "缩量沉淀: 连续≥2根量≤0.6×均量且波幅<1.5% —— 浮动筹码减少,"
        "Phase B 筹码沉淀特征, 方向待选择。"
    ),
    "markup_break": (
        "放量突破: 收盘创20日新高且量≥1.5×均量 —— 疑似 SOS(强势出现),"
        "须回踩不破才确认趋势, 防假突破(UTAD)。"
    ),
}

VERDICT_NOTE = (
    "仅日K量价结构描述(不含逐笔数据), 不得作为主力意图/买卖结论; "
    "方向判定唯一走 get_main_intent(腾讯逐笔口径)。"
)


def _num(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f:  # NaN
        return None
    return f


def _get(bar, key: str):
    if isinstance(bar, dict):
        return bar.get(key)
    return getattr(bar, key, None)


def _date(bar) -> str | None:
    d = _get(bar, "date")
    if d is None:
        return None
    s = str(d)[:10]
    if len(s) == 10 and s[4] == "-" and s[7] == "-":
        return s
    if len(s) == 8 and s.isdigit():  # YYYYMMDD → ISO
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return None


@dataclass(frozen=True)
class WyckoffMark:
    """一个量价结构标注(证据, 非建议; 线型对齐 PatternMark.to_dict)。"""

    name: str            # 展示名(中文, 与形态名同惯例, 图上直接渲染)
    code: str            # 稳定编码(effort_divergence/spring_probe/dry_up/markup_break)
    direction: str       # bullish / bearish / neutral(仅记号配色, 非方向结论)
    category: str        # 固定"量价结构"
    index: int           # 确认根在输入序列中的绝对索引
    date: str | None
    position: str        # 低位 / 高位 / 趋势中
    basis: tuple[str, ...]
    definition: str
    span: tuple[int, int]

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "code": self.code,
            "direction": self.direction,
            "category": self.category,
            "index": self.index,
            "date": self.date,
            "position": self.position,
            "basis": list(self.basis),
            "definition": self.definition,
            "span": [self.span[0], self.span[1]],
        }


class _B:
    """归一化 bar。usable=True 蕴含 o/h/l/c/v 全非空(构造处保证)。"""

    __slots__ = ("date", "o", "h", "l", "c", "v", "usable", "pct")

    def __init__(self, bar, prev_close: float | None):
        o = _num(_get(bar, "open"))
        h = _num(_get(bar, "high"))
        l = _num(_get(bar, "low"))
        c = _num(_get(bar, "close"))
        v = _num(_get(bar, "volume"))
        ok = (
            o is not None and h is not None and l is not None
            and c is not None and v is not None
            and h >= l and h > 0 and v >= 0
        )
        # 零振幅零量桩线(网关占位行): 不可用, 防误标
        if ok and h == l and v == 0:
            ok = False
        self.date = _date(bar)
        self.o: float | None = o
        self.h: float | None = h
        self.l: float | None = l
        self.c: float | None = c
        self.v: float | None = v
        self.usable = bool(ok)
        self.pct: float | None = (
            (c - prev_close) / prev_close * 100.0
            if (ok and c is not None and prev_close is not None and prev_close != 0)
            else None
        )

    def vals(self) -> tuple[float, float, float, float, float] | None:
        """可用时返回 (o,h,l,c,v), 否则 None(供类型收窄)。"""
        if not self.usable:
            return None
        if self.o is None or self.h is None or self.l is None:
            return None
        if self.c is None or self.v is None:
            return None
        return (self.o, self.h, self.l, self.c, self.v)


def _norm_bars(bars) -> list[_B]:
    """输入升序序列 → 归一化(涨幅需前收; 脏根 pct=None 但仍占位保索引)。"""
    out: list[_B] = []
    prev: float | None = None
    for b in list(bars or []):
        nb = _B(b, prev)
        out.append(nb)
        if nb.usable and nb.c is not None:
            prev = nb.c
    return out


def _avg_vol(bs: list[_B], i: int, window: int = AVG_WINDOW) -> float | None:
    vs = [b.v for b in bs[max(0, i - window):i]
          if b.usable and b.v is not None]
    if len(vs) < window // 2:  # 窗口内有效不足一半 → 均量不可信
        return None
    return sum(vs) / len(vs)


def _m_effort_divergence(bs: list[_B], i: int) -> tuple[str, ...] | None:
    """放量滞涨 / 放量冲高回落(单根, 需均量 + 涨幅)。"""
    b = bs[i]
    vv = b.vals()
    if vv is None or b.pct is None:
        return None
    o, h, l, c, v = vv
    avg = _avg_vol(bs, i)
    if avg is None or avg <= 0 or v < VOL_SPIKE * avg:
        return None
    ratio = v / avg
    body = abs(c - o)
    # case1: 放量小实体
    if abs(b.pct) < FLAT_PCT:
        return (
            f"量 {ratio:.1f}×20日均量(≥{VOL_SPIKE}×)",
            f"涨幅 {b.pct:+.2f}%(幅度<{FLAT_PCT}%)",
        )
    # case2: 放量冲高回落(长上影且收盘在下半部)
    rng = h - l
    upper = h - max(c, o)
    mid = (h + l) / 2.0
    if rng > 0 and upper >= SHADOW_RATIO * max(body, 1e-9) and c <= mid:
        return (
            f"量 {ratio:.1f}×20日均量(≥{VOL_SPIKE}×)",
            f"上影≥{SHADOW_RATIO}×实体且收盘在下半部",
        )
    return None


def _m_spring_probe(bs: list[_B], i: int) -> tuple[str, ...] | None:
    """弹簧试探: 跌破近 N 日最低但收回其上(未收回 → 不打标)。"""
    b = bs[i]
    vv = b.vals()
    if vv is None:
        return None
    o, h, l, c, v = vv
    prior = [x.l for x in bs[max(0, i - SUPPORT_WINDOW):i]
             if x.usable and x.l is not None]
    if len(prior) < SUPPORT_WINDOW // 2:
        return None
    support = min(prior)
    if l < support and c >= support and (c - l) / l * 100.0 >= SPRING_RECOVERY_PCT:
        return (
            f"盘中跌破近{SUPPORT_WINDOW}日最低({support:.2f})",
            f"收盘收回其上({c:.2f}, 从最低回收{(c - l) / l * 100.0:.1f}%)",
        )
    return None


def _m_dry_up(bs: list[_B], i: int) -> tuple[str, ...] | None:
    """缩量沉淀: 连续≥2 根缩量窄波(标最后一根, 避免刷屏)。"""
    b = bs[i]
    vv = b.vals()
    if vv is None or b.pct is None or abs(b.pct) >= DRY_PCT:
        return None
    o, h, l, c, v = vv
    avg = _avg_vol(bs, i)
    if avg is None or avg <= 0 or v > VOL_DRY * avg:
        return None
    prev = bs[i - 1] if i > 0 else None
    if prev is None or not prev.usable or prev.pct is None:
        return None
    pvv = prev.vals()
    if pvv is None:
        return None
    if abs(prev.pct) >= DRY_PCT:
        return None
    pavg = _avg_vol(bs, i - 1)
    if pavg is None or pavg <= 0 or pvv[4] > VOL_DRY * pavg:
        return None
    return (
        f"连续缩量(量≤{VOL_DRY}×均量)",
        f"波幅<{DRY_PCT}%",
    )


def _m_markup_break(bs: list[_B], i: int) -> tuple[str, ...] | None:
    """放量突破: 收盘创 N 日新高 + 放量。"""
    b = bs[i]
    vv = b.vals()
    if vv is None:
        return None
    o, h, l, c, v = vv
    prior = [x.h for x in bs[max(0, i - RESIST_WINDOW):i]
             if x.usable and x.h is not None]
    if len(prior) < RESIST_WINDOW // 2:
        return None
    avg = _avg_vol(bs, i)
    if avg is None or avg <= 0:
        return None
    if c > max(prior) and v >= VOL_BREAKOUT * avg:
        return (
            f"收盘创{RESIST_WINDOW}日新高({c:.2f})",
            f"量 {v / avg:.1f}×均量(≥{VOL_BREAKOUT}×)",
        )
    return None


_DETECTORS = (
    ("effort_divergence", "放量滞涨", DIR_BEAR, "高位", _m_effort_divergence),
    ("spring_probe", "弹簧试探", DIR_NEUTRAL, "低位", _m_spring_probe),
    ("dry_up", "缩量沉淀", DIR_NEUTRAL, "趋势中", _m_dry_up),
    ("markup_break", "放量突破", DIR_BULL, "高位", _m_markup_break),
)


def analyze_wyckoff(bars, lookback: int | None = 60) -> dict:
    """在 K 线序列上识别威科夫量价结构, 返回 `{markers, verdict}`。

    bars: 升序序列(含 open/high/low/close/volume[/date]); lookback 仅返回
    确认根落在最后 `lookback` 根内的标注, None = 全序列。识别不出返回空 markers
    + `stage=unknown` 的 verdict(不硬凑)。
    """
    bs = _norm_bars(bars)
    n = len(bs)
    if n < MIN_BARS:
        return {"markers": [],
                "verdict": {"stage": "unknown",
                            "stage_label": "区间震荡/无法定位",
                            "reason": f"bar 不足({n}<{MIN_BARS})不识别",
                            "note": VERDICT_NOTE}}
    lo = 0 if not lookback else max(0, n - int(lookback))
    marks: list[WyckoffMark] = []
    for i in range(n):
        if i < lo or not bs[i].usable:
            continue
        for code, display, direction, position, fn in _DETECTORS:
            basis = fn(bs, i)
            if not basis:
                continue
            marks.append(WyckoffMark(
                name=display, code=code, direction=direction, category=CAT_STRUCTURE,
                index=i, date=bs[i].date, position=position,
                basis=tuple(basis), definition=DEFINITIONS[code],
                span=(max(0, i - RESIST_WINDOW + 1), i),
            ))
    marks.sort(key=lambda m: (m.index, m.name))
    return {"markers": [m.to_dict() for m in marks],
            "verdict": _verdict(bs, marks)}


def _verdict(bs: list[_B], marks: list[WyckoffMark]) -> dict:
    tail = marks[-4:] if len(marks) >= 4 else marks
    codes_tail = {m.code for m in tail}
    recent = marks[-8:] if len(marks) >= 8 else tail
    codes_recent = {m.code for m in recent}
    if "markup_break" in codes_tail:
        stage, label = "D/E", "突破/趋势(疑似SOS, 须回踩确认防假突破)"
    elif "spring_probe" in codes_recent:
        stage, label = "C", "终极测试(弹簧试探待确认, 须缩量企稳LPS)"
    elif "dry_up" in codes_recent:
        stage, label = "B", "筹码沉淀中(缩量, 方向待选择)"
    else:
        closes = [b.c for b in bs[-AVG_WINDOW:]
                  if b.usable and b.c is not None]
        if len(closes) >= AVG_WINDOW // 2 and max(closes) > 0:
            dd = (min(closes) - max(closes)) / max(closes) * 100.0
            if dd <= -DRAW_DOWN_A:
                return {"stage": "A",
                        "stage_label": "止跌构建中(近20日跌超10%)",
                        "effort_result": ("divergence" if "effort_divergence"
                                          in codes_tail else "unknown"),
                        "markers_count": len(marks), "note": VERDICT_NOTE}
        stage, label = "unknown", "区间震荡/无法定位"
    eff = ("divergence" if "effort_divergence" in codes_tail
           else ("match" if tail else "unknown"))
    return {"stage": stage, "stage_label": label, "effort_result": eff,
            "markers_count": len(marks), "note": VERDICT_NOTE}
