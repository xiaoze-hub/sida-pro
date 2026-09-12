"""A 股市场情绪周期 6 阶段体系 — 冰点 / 启动 / 主升 / 高潮 / 退潮 / 修复 (2026-08-24)

业务定位:
- 回答"当前 A 股处于情绪周期什么位置"。与 5 档 state 体系并存(本模块不改 state),
  供市场环境页分析使用。
- 不生成交易信号; 不做挖掘/回测过滤; 不参与任何旧消费方。

驱动量(全部可由 MarketSentimentCollector().get_limit_up_pool() + 上证指数派生):
- first_board: 首板(1 连板)家数
- ge2_count / ge3_count / ge5_count: N 板及以上家数(梯队宽度)
- max_height: 当日最高连板数(高度)
- promo_rate: 晋级率 — 昨日连板池中今日续封的比例(昨日池 < PROMO_MIN_POOL 记 None)
- seal_rate: 封板率 — 数据源只给已封板池、无炸板分母, 算不出真值, 落 None(前端“--”)
- sh_index_pct: 上证指数当日涨跌幅, 用于弱档否决

阈值(集中本模块顶部常量, 调整只改这里, 标定自全 A 股历史分位数):
- 高潮 climax:    ge2 >= 50 或 first_board >= 220(<2% 天数)
- 主升 rally:     height >= 7 + ge2 >= 15 + promo >= 0.23, 或 promo >= 0.30 + height >= 5 + ge2 >= 12
- 退潮 ebb:       晋级率崩至 < 0.15 且宽度自 5 日前高位回落; 或 promo < 0.13 + seal < 0.57 双弱
- 启动 ignite:    宽度/高度自低位扩张(ge2 较 5 日前 +3 且 >= 8, 或 height 抬升且 >= 5) + promo >= 0.19~0.20
- 冰点 ice:       height <= 4 + ge2 <= 6 + first_board <= 24 同时贴地
- 修复 repair:    兜底

持续性(降低日频噪声):
- 驱动量先做 EMA(alpha=1/3)平滑, 缺失值 ffill 沿用上一平滑值
- 阶段切换需连续 CONFIRM_DAYS=2 日同标签才生效(平均段长 ~9.7 天 vs state 1.1~1.5 天)

弱档否决:
- 当日上证指数跌幅 < -2% 时, 正向阶段(climax/rally/ignite)降为 repair
- 修复场景: 连板梯队强但大盘崩(如 2024-01 微盘流动性危机)
  — 涨停生态与大盘背离时, 以大盘为准

历史不足 ACCUMULATING_MIN_DAYS=5 天 → 阶段 = 'accumulating'(不可作交易信号)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from statistics import mean
from typing import Iterable, Sequence

logger = logging.getLogger(__name__)


# ───────────────────────────── 阶段词汇 ─────────────────────────────
PHASE_ICE = "ice"
PHASE_IGNITE = "ignite"
PHASE_RALLY = "rally"
PHASE_CLIMAX = "climax"
PHASE_EBB = "ebb"
PHASE_REPAIR = "repair"
PHASE_ACCUMULATING = "accumulating"

PHASE_LABELS: dict[str, str] = {
    PHASE_ICE: "冰点",
    PHASE_IGNITE: "启动",
    PHASE_RALLY: "主升",
    PHASE_CLIMAX: "高潮",
    PHASE_EBB: "退潮",
    PHASE_REPAIR: "修复",
    PHASE_ACCUMULATING: "积累中",
}

# 判定优先级: 高潮 > 主升 > 退潮 > 启动 > 冰点 > 修复(兜底)
# 冰点排在退潮前 — 长期死寂应被标冰点而非"自高位退潮"。
PHASE_PRIORITY: tuple[str, ...] = (
    PHASE_CLIMAX,
    PHASE_RALLY,
    PHASE_EBB,
    PHASE_IGNITE,
    PHASE_ICE,
)

# 正向阶段(被弱档否决降级为 repair)
POSITIVE_PHASES: frozenset[str] = frozenset({PHASE_CLIMAX, PHASE_RALLY, PHASE_IGNITE})


# ───────────────────────────── 阈值常量 ─────────────────────────────
# 高潮 — 极端宣泄(<2% 历史天数)
CLIMAX_GE2: float = 50.0           # p90 的 2 倍
CLIMAX_FIRST_BOARD: float = 220.0  # p90 的 2.5 倍

# 主升 — 高度/宽度/晋级率同时高于中位, 或晋级率极强
RALLY_HEIGHT: float = 7.0
RALLY_GE2: float = 15.0
RALLY_PROMO: float = 0.23
RALLY_PROMO_ALT: float = 0.30
RALLY_GE2_ALT: float = 12.0
RALLY_HEIGHT_ALT: float = 5.0

# 退潮 — 自高位回落 + 晋级率坍塌, 或双弱
EBB_PROMO: float = 0.15
EBB_PROMO_STRICT: float = 0.13
EBB_SEAL: float = 0.57
EBB_RECENT_GE2: float = 12.0
EBB_RECENT_HEIGHT: float = 6.0

# 启动 — 宽度/高度自低位扩张
IGNITE_GE2_DELTA: float = 3.0
IGNITE_GE2: float = 8.0
IGNITE_PROMO: float = 0.20
IGNITE_HEIGHT_DELTA: float = 1.0
IGNITE_HEIGHT: float = 5.0
IGNITE_PROMO_SOFT: float = 0.19

# 冰点 — 高度/宽度/首板同时贴地
ICE_HEIGHT: float = 4.0
ICE_GE2: float = 6.0
ICE_FIRST_BOARD: float = 24.0

# 持续性 — EMA + 2 日确认
EMA_ALPHA: float = 1.0 / 3.0
CONFIRM_DAYS: int = 2

# 弱档否决 — 上证当日跌幅阈值(%)
WEAK_VETO_SH_PCT: float = -2.0

# 历史不足 → 'accumulating'
ACCUMULATING_MIN_DAYS: int = 5

# 晋级率最小池(< 此值记 None, 小样本噪声)
PROMO_MIN_POOL: int = 10

# 5 日前对比(平滑后)
RECENT_LOOKBACK: int = 5


# ───────────────────────────── 数据结构 ─────────────────────────────
@dataclass
class DailyMetrics:
    """单日梯队指标(EMA 平滑前原始值)。

    字段全部由 compute_daily_metrics 从涨停池派生。sh_index_pct 由调用方
    (API 层)注入 — 该模块不直连指数源, 保持纯函数特性。
    """
    first_board: int = 0
    ge2_count: int = 0
    ge3_count: int = 0
    ge5_count: int = 0
    max_height: int = 0
    promo_rate: float | None = None  # 昨日连板池今日续封比例
    seal_rate: float | None = None   # 数据源不可得时 None
    sh_index_pct: float | None = None  # 上证当日涨跌幅, 由 API 层注入

    def to_row_dict(self, d):
        """转 dict, 供 classify_phase_series 入参使用。"""
        return {
            "date": d,
            "first_board": self.first_board,
            "ge2_count": self.ge2_count,
            "ge3_count": self.ge3_count,
            "ge5_count": self.ge5_count,
            "max_height": self.max_height,
            "promo_rate": self.promo_rate,
            "seal_rate": self.seal_rate,
            "sh_index_pct": self.sh_index_pct,
        }


# ───────────────────────────── 核心计算 ─────────────────────────────
def compute_daily_metrics(
    pool: list[dict],
    prev_pool: list[dict] | None,
) -> DailyMetrics:
    """从涨停池推算当日梯队指标。

    pool:       [{code, name, days, ...}, ...] 当日所有涨停股(可空)
    prev_pool:  昨日涨停池(用于计算晋级率); None/[] 时 promo_rate=None

    字段定义:
    - days: 连板数(MarketSentimentCollector 统一返回, 默认 1)
    - first_board: days == 1 的家数
    - ge2/3/5_count: days >= N 的家数
    - max_height: max(days) — 当日最高板数, 池空时 0
    - promo_rate: 昨日连板池(>=2 板)中今日续封的比例, 池不足 PROMO_MIN_POOL 时 None
    - seal_rate: 数据源只给已封板池、无炸板分母, 算不出真值, 落 None(前端“--”)
    """
    first_board = sum(1 for p in pool if int(p.get("days") or 1) == 1)
    ge2_count = sum(1 for p in pool if int(p.get("days") or 1) >= 2)
    ge3_count = sum(1 for p in pool if int(p.get("days") or 1) >= 3)
    ge5_count = sum(1 for p in pool if int(p.get("days") or 1) >= 5)
    max_height = max((int(p.get("days") or 1) for p in pool), default=0)

    # 晋级率 — 昨日连板池中今日续封的比例
    promo_rate: float | None = None
    if prev_pool:
        prev_ge2_codes = {
            str(p.get("code") or "").strip()
            for p in prev_pool
            if int(p.get("days") or 1) >= 2 and str(p.get("code") or "").strip()
        }
        if len(prev_ge2_codes) >= PROMO_MIN_POOL:
            today_codes = {
                str(p.get("code") or "").strip()
                for p in pool
                if str(p.get("code") or "").strip()
            }
            continued = sum(1 for c in prev_ge2_codes if c in today_codes)
            promo_rate = round(continued / len(prev_ge2_codes), 4)

    # 封板率 — 数据源只给已封板池、无炸板分母，算不出真封板率。
    # 2026-09-05: 落 None（前端显“--”），此前硬编码 1.0 会误导成“100%封板”。
    seal_rate: float | None = None

    return DailyMetrics(
        first_board=first_board,
        ge2_count=ge2_count,
        ge3_count=ge3_count,
        ge5_count=ge5_count,
        max_height=max_height,
        promo_rate=promo_rate,
        seal_rate=seal_rate,
        sh_index_pct=None,
    )


def _ema(values: Sequence[float | None], alpha: float = EMA_ALPHA) -> list[float]:
    """EMA 平滑。None/NaN 沿用上一平滑值(ffill), 起始缺失用首个有效值回填。

    返回长度 == len(values)。所有 None 输入 → 全 0.0(完全退化)。
    """
    out: list[float] = []
    cur: float | None = None
    for v in values:
        if v is None or (isinstance(v, float) and v != v):  # NaN 视为缺失
            if cur is None:
                out.append(0.0)  # 起始无前置 → 占位 0, 等首个有效值回填
            else:
                out.append(cur)  # ffill
            continue
        cur = float(v) if cur is None else cur + alpha * (float(v) - cur)
        out.append(cur)

    # 起始占位回填为第一个有效值
    first_valid = next((i for i, x in enumerate(out) if x > 0), None)
    if first_valid is not None and first_valid > 0:
        for i in range(first_valid):
            out[i] = out[first_valid]
    return out


def _raw_phase_label(
    i: int,
    height_s: Sequence[float],
    first_s: Sequence[float],
    ge2_s: Sequence[float],
    promo_s: Sequence[float],
    seal_s: Sequence[float],
    thr: dict[str, float] | None = None,
) -> str:
    """根据 EMA 平滑后的驱动量, 按优先级判定当日 raw 阶段标签。"""
    t = thr or DEFAULT_THR
    h = height_s[i]
    fb = first_s[i]
    g2 = ge2_s[i]
    pr = promo_s[i]
    sr = seal_s[i]
    # 5 日前对比 — 不足 5 日取最早可用
    prev_idx = max(0, i - RECENT_LOOKBACK)
    g2_prev = ge2_s[prev_idx]
    h_prev = height_s[prev_idx]

    # 高潮 — 极端宣泄
    if g2 >= t["climax_ge2"] or fb >= t["climax_fb"]:
        return PHASE_CLIMAX
    # 主升 — 三维同高 / 晋级率极强
    if h >= t["rally_h"] and g2 >= t["rally_ge2"] and pr >= t["rally_promo"]:
        return PHASE_RALLY
    if pr >= t["rally_promo_alt"] and g2 >= t["rally_ge2_alt"] and h >= t["rally_h_alt"]:
        return PHASE_RALLY
    # 冰点 — 三维贴地(优先于退潮, 长期死寂不应被标"自高位退潮")
    if h <= t["ice_h"] and g2 <= t["ice_ge2"] and fb <= t["ice_fb"]:
        return PHASE_ICE
    # 退潮 — 自高位回落 + 晋级率坍塌 / 双弱
    from_high = g2_prev >= t["ebb_recent_ge2"] or h_prev >= t["ebb_recent_h"]
    if from_high and pr <= t["ebb_promo"] and g2 < g2_prev:
        return PHASE_EBB
    if sr is not None and sr > 0 and pr <= t["ebb_promo_strict"] and sr < t["ebb_seal"]:
        return PHASE_EBB
    # 启动 — 自低位扩张
    if g2 - g2_prev >= t["ignite_ge2_delta"] and g2 >= t["ignite_ge2"] and pr >= t["ignite_promo"]:
        return PHASE_IGNITE
    if h - h_prev >= t["ignite_h_delta"] and h >= t["ignite_h"] and pr >= t["ignite_promo_soft"]:
        return PHASE_IGNITE
    return PHASE_REPAIR


def classify_phase_series(rows: Sequence[dict]) -> list[str]:
    """对完整日序打阶段标签。

    输入 rows 形如:
      [{
        "date": date/datetime/str,
        "first_board", "ge2_count", "ge3_count", "ge5_count",
        "max_height", "promo_rate", "seal_rate", "sh_index_pct",
      }, ...]

    返回与 rows 等长的阶段标签列表(按入参顺序)。

    流水线:
    1) 历史不足 ACCUMULATING_MIN_DAYS 天 → 全部 'accumulating'
    2) 驱动量 EMA(alpha=1/3)平滑, None ffill
    3) 按优先级逐日判定 raw 阶段
    4) 弱档否决: sh_index_pct < -2% 时, 正向阶段降为 repair
    5) 连续 CONFIRM_DAYS=2 日同 raw 阶段才切换(防止日频噪声)
    """
    n = len(rows)
    if n == 0:
        return []

    # 历史不足 → 积累中(不可作交易信号)
    if n < ACCUMULATING_MIN_DAYS:
        return [PHASE_ACCUMULATING] * n

    # 提取各驱动量序列(None 透传, _ema 内做 ffill)
    height = [_safe_num(r.get("max_height")) for r in rows]
    first = [_safe_num(r.get("first_board")) for r in rows]
    ge2 = [_safe_num(r.get("ge2_count")) for r in rows]
    promo = [_safe_num(r.get("promo_rate")) for r in rows]
    seal = [_safe_num(r.get("seal_rate")) for r in rows]
    sh = [_safe_num(r.get("sh_index_pct")) for r in rows]

    h_s = _ema(height)
    fb_s = _ema(first)
    g2_s = _ema(ge2)
    pr_s = _ema(promo)
    sr_s = _ema(seal)

    # 逐日 raw 阶段 + 弱档否决
    thr, _calibrated = calibrate(rows)
    raw_labels: list[str] = []
    for i in range(n):
        raw = _raw_phase_label(i, h_s, fb_s, g2_s, pr_s, sr_s, thr)
        sh_pct = sh[i]
        if raw in POSITIVE_PHASES and sh_pct is not None and sh_pct < WEAK_VETO_SH_PCT:
            raw = PHASE_REPAIR
        raw_labels.append(raw)

    # 连续 CONFIRM_DAYS 日同 raw 阶段才切换
    final: list[str] = [raw_labels[0]]
    current = raw_labels[0]
    pending: str | None = None
    pending_run = 0
    for i in range(1, n):
        raw = raw_labels[i]
        if raw == current:
            final.append(current)
            pending = None
            pending_run = 0
            continue
        # 新标签 — 累计连续日数
        if raw == pending:
            pending_run += 1
        else:
            pending = raw
            pending_run = 1
        if pending_run >= CONFIRM_DAYS:
            current = raw
            final.append(current)
            pending = None
            pending_run = 0
        else:
            final.append(current)
    return final


def classify_phase_series_full(rows: Sequence[dict]) -> list[dict]:
    """同 classify_phase_series, 但返回逐日 {phase, phase_raw}(回填/审计用)。

    phase_raw = 确认前的原始标签; 两者差异即"被 2 日确认拦下的抖动"。
    """
    labels = classify_phase_series(rows)
    if not labels:
        return []
    # raw 标签重算一次(与 classify 内部同参数), 供落库对照
    height = [_safe_num(r.get("max_height")) for r in rows]
    first = [_safe_num(r.get("first_board")) for r in rows]
    ge2 = [_safe_num(r.get("ge2_count")) for r in rows]
    promo = [_safe_num(r.get("promo_rate")) for r in rows]
    seal = [_safe_num(r.get("seal_rate")) for r in rows]
    sh = [_safe_num(r.get("sh_index_pct")) for r in rows]
    h_s, fb_s, g2_s, pr_s, sr_s = _ema(height), _ema(first), _ema(ge2), _ema(promo), _ema(seal)
    thr, _ = calibrate(rows)
    out = []
    for i, lab in enumerate(labels):
        raw = _raw_phase_label(i, h_s, fb_s, g2_s, pr_s, sr_s, thr)
        if raw in POSITIVE_PHASES and sh[i] is not None and sh[i] < WEAK_VETO_SH_PCT:
            raw = PHASE_REPAIR
        out.append({"phase": lab, "phase_raw": raw})
    return out


def phase_distribution(phases: Iterable[str]) -> dict[str, int]:
    """阶段 → 天数 分布统计(不排序, 由调用方按 PHASE_PRIORITY 排)。"""
    out: dict[str, int] = {}
    for p in phases:
        if not p:
            continue
        out[p] = out.get(p, 0) + 1
    return out


def ordered_distribution(dist: dict[str, int]) -> list[tuple[str, int, str]]:
    """按业务优先级排序的分布: (phase_key, days, label)。"""
    order = list(PHASE_PRIORITY) + [PHASE_REPAIR, PHASE_ACCUMULATING]
    seen: set[str] = set()
    result: list[tuple[str, int, str]] = []
    for k in order:
        if k in dist and k not in seen:
            result.append((k, dist[k], PHASE_LABELS.get(k, k)))
            seen.add(k)
    # 未在优先级列表中的(防御性)
    for k, v in dist.items():
        if k not in seen:
            result.append((k, v, PHASE_LABELS.get(k, k)))
    return result


# ───────────────────────────── 内部辅助 ─────────────────────────────
def _safe_num(v) -> float | None:
    """安全转 float — None / 非数值返回 None。"""
    if v is None:
        return None
    try:
        f = float(v)
        if f != f:  # NaN
            return None
        return f
    except (TypeError, ValueError):
        return None


# ─────────────── 阈值标定(2026-09-12, 借鉴 TSP: 用自有历史分位, 不抄常数) ───────────────
# 默认值 = 模块顶部常量(TSP 标定自其 6 年历史); 自有历史足够时 calibrate() 覆盖。
DEFAULT_THR: dict[str, float] = {
    "climax_ge2": CLIMAX_GE2, "climax_fb": CLIMAX_FIRST_BOARD,
    "rally_h": RALLY_HEIGHT, "rally_ge2": RALLY_GE2, "rally_promo": RALLY_PROMO,
    "rally_promo_alt": RALLY_PROMO_ALT, "rally_ge2_alt": RALLY_GE2_ALT,
    "rally_h_alt": RALLY_HEIGHT_ALT,
    "ebb_promo": EBB_PROMO, "ebb_promo_strict": EBB_PROMO_STRICT, "ebb_seal": EBB_SEAL,
    "ebb_recent_ge2": EBB_RECENT_GE2, "ebb_recent_h": EBB_RECENT_HEIGHT,
    "ignite_ge2_delta": IGNITE_GE2_DELTA, "ignite_ge2": IGNITE_GE2,
    "ignite_promo": IGNITE_PROMO, "ignite_h_delta": IGNITE_HEIGHT_DELTA,
    "ignite_h": IGNITE_HEIGHT, "ignite_promo_soft": IGNITE_PROMO_SOFT,
    "ice_h": ICE_HEIGHT, "ice_ge2": ICE_GE2, "ice_fb": ICE_FIRST_BOARD,
}
CALIBRATE_MIN_DAYS = 120   # 自有历史不足此天数 → 沿用默认阈值(并在 API note 里说明)


def _pct(samples: list[float], q: float) -> float:
    if not samples:
        return 0.0
    s = sorted(samples)
    k = (len(s) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def calibrate(rows: Sequence[dict]) -> tuple[dict[str, float], bool]:
    """用自有历史分位标定阈值。返回 (thr, calibrated)。

    标定规则沿用 TSP 的倍率(高潮=p90 的 2/2.5 倍; 主升/退潮/启动/冰点取 p60/p20/p10),
    但分位数取自**我们自己的** limit_up_events 历史, 不抄别人的常数。
    历史不足 CALIBRATE_MIN_DAYS 天 → 返回默认阈值 + calibrated=False。
    """
    if len(rows) < CALIBRATE_MIN_DAYS:
        return dict(DEFAULT_THR), False
    fb = [float(r["first_board"]) for r in rows]
    ge2 = [float(r["ge2_count"]) for r in rows]
    ht = [float(r["max_height"]) for r in rows if r.get("max_height")]
    promo = [float(r["promo_rate"]) for r in rows if r.get("promo_rate") is not None]
    seal = [float(r["seal_rate"]) for r in rows if r.get("seal_rate") is not None]
    thr = dict(DEFAULT_THR)
    thr.update({
        "climax_ge2": _pct(ge2, 0.90) * 2, "climax_fb": _pct(fb, 0.90) * 2.5,
        "rally_h": max(_pct(ht, 0.60), 4), "rally_ge2": _pct(ge2, 0.60),
        "rally_promo": _pct(promo, 0.60), "rally_promo_alt": _pct(promo, 0.85),
        "rally_ge2_alt": _pct(ge2, 0.60) * 0.8, "rally_h_alt": max(_pct(ht, 0.50), 3),
        "ebb_promo": _pct(promo, 0.20), "ebb_promo_strict": _pct(promo, 0.15),
        "ebb_seal": _pct(seal, 0.20) if seal else DEFAULT_THR["ebb_seal"],
        "ebb_recent_ge2": max(_pct(ge2, 0.60), 10), "ebb_recent_h": max(_pct(ht, 0.70), 5),
        "ignite_ge2": max(_pct(ge2, 0.35), 6), "ignite_promo": _pct(promo, 0.40),
        "ignite_promo_soft": _pct(promo, 0.35),
        "ice_h": max(_pct(ht, 0.10), 2), "ice_ge2": max(_pct(ge2, 0.10), 3),
        "ice_fb": max(_pct(fb, 0.10), 8),
    })
    return thr, True


# ─────────────── 从已落库涨停事件派生历史指标(回填用, 不依赖 vendor) ───────────────
def consecutive_boards(dates: Sequence[str], sealed: dict[tuple[str, str], bool]) -> dict[tuple[str, str], int]:
    """按交易日历连续 run 算每只票当日连板数(仅封板日有值)。

    与 TSP 的 `consec.shift(1).over("symbol")` 同义: 昨日封板且今日封板 → +1, 否则 1。
    """
    out: dict[tuple[str, str], int] = {}
    prev: str | None = None
    for d in dates:
        for (dd, sym), ok in sealed.items():
            if dd != d or not ok:
                continue
            prior = out.get((prev, sym)) if prev else None
            out[(d, sym)] = (prior or 0) + 1
        prev = d
    return out


def ladder_completeness(runs: Sequence[int], height: int) -> float | None:
    """梯队完整度: 2..height 中非空档位占比; height<3 记 None(样本无意义)。"""
    if height < 3:
        return None
    present = {n for n in runs if n >= 2}
    return round(len(present) / (height - 1), 4)


def metrics_rows_from_events(dates: Sequence[str], events: Sequence[dict]) -> list[dict]:
    """events: [{trade_date, symbol, touched, sealed}] → classify_phase_series 入参行。

    封板率这里能算真值(sealed/touched), 优于 vendor 池的 None。
    """
    sealed = {(e["trade_date"], e["symbol"]): bool(e["sealed"]) for e in events}
    touched_cnt: dict[str, int] = {}
    for e in events:
        if e.get("touched"):
            touched_cnt[e["trade_date"]] = touched_cnt.get(e["trade_date"], 0) + 1
    boards = consecutive_boards(dates, sealed)
    by_date: dict[str, list[int]] = {}
    for (d, _s), n in boards.items():
        by_date.setdefault(d, []).append(n)

    rows: list[dict] = []
    for i, d in enumerate(dates):
        runs = by_date.get(d, [])
        height = max(runs) if runs else 0
        sealed_n = len(runs)
        pool = by_date.get(dates[i - 1], []) if i > 0 else []
        promoted = sum(1 for n in runs if n >= 2)
        touched = touched_cnt.get(d, 0)
        rows.append({
            "date": d,
            "first_board": sum(1 for n in runs if n == 1),
            "ge2_count": sum(1 for n in runs if n >= 2),
            "ge3_count": sum(1 for n in runs if n >= 3),
            "ge5_count": sum(1 for n in runs if n >= 5),
            "max_height": height,
            "promo_rate": round(promoted / len(pool), 4) if len(pool) >= PROMO_MIN_POOL else None,
            "promo_pool": len(pool),
            "seal_rate": round(sealed_n / touched, 4) if touched else None,
            "sh_index_pct": None,
            "completeness": ladder_completeness(runs, height),
        })
    return rows


# ─────────────── 分段 / 转移概率 / 分位数(阶段规律面板用) ───────────────
def segmentize(rows: Sequence[dict]) -> list[dict]:
    """连续同阶段合并成段: {phase,label,start,end,days,avg_*}。"""
    out: list[dict] = []
    for r in rows:
        ph = r.get("phase") or ""
        if not ph:
            continue
        if out and out[-1]["phase"] == ph:
            out[-1]["end"] = r["date"]
            out[-1]["days"] += 1
            out[-1]["_rows"].append(r)
        else:
            out.append({"phase": ph, "label": PHASE_LABELS.get(ph, ph),
                        "start": r["date"], "end": r["date"], "days": 1, "_rows": [r]})
    for s in out:
        rs = s.pop("_rows")
        s["avg_height"] = round(mean([r.get("max_height") or 0 for r in rs]), 1)
        s["avg_first_board"] = round(mean([r.get("first_board") or 0 for r in rs]), 1)
        s["avg_ge2"] = round(mean([r.get("ge2_count") or 0 for r in rs]), 1)
        pr = [r["promo_rate"] for r in rs if r.get("promo_rate") is not None]
        sr = [r["seal_rate"] for r in rs if r.get("seal_rate") is not None]
        s["avg_promo"] = round(mean(pr), 3) if pr else None
        s["avg_seal_rate"] = round(mean(sr), 3) if sr else None
    return out


def transition_stats(segments: Sequence[dict]) -> dict:
    """每阶段: 历史段数/平均时长/最长 + 去向概率(段→段转移, 按占比降序)。"""
    stats: dict[str, dict] = {}
    for s in segments:
        st = stats.setdefault(s["phase"], {"count": 0, "total_days": 0, "max_days": 0, "next": {}})
        st["count"] += 1
        st["total_days"] += s["days"]
        st["max_days"] = max(st["max_days"], s["days"])
    for a, b in zip(segments, segments[1:]):
        nxt = stats[a["phase"]]["next"]
        nxt[b["phase"]] = nxt.get(b["phase"], 0) + 1
    for ph, st in stats.items():
        tot = sum(st["next"].values()) or 1
        st["next"] = {k: round(v / tot, 2) for k, v in sorted(st["next"].items(), key=lambda kv: -kv[1])}
        st["next_labels"] = {PHASE_LABELS.get(k, k): v for k, v in st["next"].items()}
        st["avg_days"] = round(st["total_days"] / st["count"], 1)
        st["label"] = PHASE_LABELS.get(ph, ph)
    return stats


def percentile_of(value: float | None, samples: Sequence[float]) -> int | None:
    """value 在历史样本中的百分位(0-100); 缺值/空样本 → None(前端显 '--')。"""
    if value is None or not samples:
        return None
    below = sum(1 for s in samples if s < value)
    return int(round(100 * below / len(samples)))


# ─────────────────── IO: 从已落库涨停事件回填(不依赖 vendor) ───────────────────
def _read_events(start: str | None = None) -> list[dict]:
    from sqlalchemy import text

    from src.db.session import engine

    sql = ("SELECT trade_date, symbol, touched, is_sealed_close AS sealed FROM limit_up_events"
           + (" WHERE trade_date >= :s" if start else ""))
    with engine.begin() as conn:
        rows = conn.execute(text(sql), {"s": start} if start else {}).fetchall()
    # SQLAlchemy 2.0 的 Row 不支持 row["col"] 字符串下标, 必须走 _mapping(与 theme_mood 同法)
    return [{"trade_date": r["trade_date"], "symbol": r["symbol"],
             "touched": bool(r["touched"]), "sealed": bool(r["sealed"])}
            for r in (dict(x._mapping) for x in rows)]


_UPSERT = """
INSERT INTO market_phase_daily (date, first_board, ge2_count, ge3_count, ge5_count, max_height,
                                promo_rate, seal_rate, completeness, phase, phase_raw)
VALUES (:date, :first_board, :ge2_count, :ge3_count, :ge5_count, :max_height,
        :promo_rate, :seal_rate, :completeness, :phase, :phase_raw)
ON CONFLICT(date) DO UPDATE SET
  first_board = EXCLUDED.first_board, ge2_count = EXCLUDED.ge2_count,
  ge3_count = EXCLUDED.ge3_count, ge5_count = EXCLUDED.ge5_count,
  max_height = EXCLUDED.max_height, promo_rate = EXCLUDED.promo_rate,
  seal_rate = EXCLUDED.seal_rate, completeness = EXCLUDED.completeness,
  phase = EXCLUDED.phase, phase_raw = EXCLUDED.phase_raw,
  updated_at = CURRENT_TIMESTAMP
"""


def scan_from_events(*, start: str | None = None, write: bool = True) -> dict:
    """回填市场情绪周期: 读 limit_up_events → 派生梯队 → 全量重标 → upsert。

    为什么全量重标而不是增量: EMA 平滑与 2 日确认都依赖完整序列(与 TSP 的
    refresh_phase_labels 同一取舍), 而全量只有几百行, 开销可忽略。
    不动 `sh_index_pct`(那是 vendor 同步写的, 回填没有当日指数数据)。
    """
    from sqlalchemy import text

    from src.db.session import engine

    try:
        events = _read_events(start)
        dates = sorted({e["trade_date"] for e in events})
        if not dates:
            return {"ok": False, "reason": "无涨停事件可回填"}
        rows = metrics_rows_from_events(dates, events)
        labelled = classify_phase_series_full(rows)
        thr, calibrated = calibrate(rows)
        payload = []
        for r, lab in zip(rows, labelled):
            payload.append({
                "date": f"{r['date'][:4]}-{r['date'][4:6]}-{r['date'][6:]}",
                "first_board": r["first_board"], "ge2_count": r["ge2_count"],
                "ge3_count": r["ge3_count"], "ge5_count": r["ge5_count"],
                "max_height": r["max_height"], "promo_rate": r["promo_rate"],
                "seal_rate": r["seal_rate"], "completeness": r["completeness"],
                "phase": lab["phase"], "phase_raw": lab["phase_raw"],
            })
        if write:
            with engine.begin() as conn:
                for p in payload:
                    conn.execute(text(_UPSERT), p)
        segs = segmentize([dict(p, phase=p["phase"]) for p in payload])
        return {
            "ok": True, "days": len(payload), "segments": len(segs), "calibrated": calibrated,
            "latest_phase": payload[-1]["phase"] if payload else None,
            "note": (f"回填 {len(payload)} 天({dates[0]}~{dates[-1]}); 阈值"
                     + ("已用自有历史分位标定" if calibrated else "沿用默认(历史<120天)")
                     + f"; 共 {len(segs)} 段; 阈值集 {sorted(thr)[:2]}…"),
        }
    except Exception as e:  # noqa: BLE001
        logger.warning("市场情绪周期回填异常: %s", e)
        return {"ok": False, "reason": str(e)}
