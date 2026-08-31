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
- seal_rate: 封板率 — 当前数据源只给已封板池, 落 1.0(数据源不可得时 None)
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
    - seal_rate: 当前数据源只给已封板池, 落 1.0(数据源不可得时 None)
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

    # 封板率 — 数据源只给已封板池, 默认 1.0(数据源不可得时 None)
    seal_rate: float | None = 1.0 if pool else None

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
) -> str:
    """根据 EMA 平滑后的驱动量, 按优先级判定当日 raw 阶段标签。"""
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
    if g2 >= CLIMAX_GE2 or fb >= CLIMAX_FIRST_BOARD:
        return PHASE_CLIMAX
    # 主升 — 三维同高 / 晋级率极强
    if h >= RALLY_HEIGHT and g2 >= RALLY_GE2 and pr >= RALLY_PROMO:
        return PHASE_RALLY
    if pr >= RALLY_PROMO_ALT and g2 >= RALLY_GE2_ALT and h >= RALLY_HEIGHT_ALT:
        return PHASE_RALLY
    # 冰点 — 三维贴地(优先于退潮, 长期死寂不应被标"自高位退潮")
    if h <= ICE_HEIGHT and g2 <= ICE_GE2 and fb <= ICE_FIRST_BOARD:
        return PHASE_ICE
    # 退潮 — 自高位回落 + 晋级率坍塌 / 双弱
    from_high = g2_prev >= EBB_RECENT_GE2 or h_prev >= EBB_RECENT_HEIGHT
    if from_high and pr <= EBB_PROMO and g2 < g2_prev:
        return PHASE_EBB
    if sr is not None and sr > 0 and pr <= EBB_PROMO_STRICT and sr < EBB_SEAL:
        return PHASE_EBB
    # 启动 — 自低位扩张
    if g2 - g2_prev >= IGNITE_GE2_DELTA and g2 >= IGNITE_GE2 and pr >= IGNITE_PROMO:
        return PHASE_IGNITE
    if h - h_prev >= IGNITE_HEIGHT_DELTA and h >= IGNITE_HEIGHT and pr >= IGNITE_PROMO_SOFT:
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
    raw_labels: list[str] = []
    for i in range(n):
        raw = _raw_phase_label(i, h_s, fb_s, g2_s, pr_s, sr_s)
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
