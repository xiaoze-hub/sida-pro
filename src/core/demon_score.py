"""妖股评分/股性雷达(批次B, 2026-09-06 28号)。

老板假设: 部分股票近一年涨停数十次(妖股记忆效应), 板块轮到时涨得最快最多。
妖股池 = 把"股性"量化成六维评分, 题材启动时作为"先锋名单"。

六维(权重合计 100, 全权重制——某维缺数据计 0 分并在 flags 标注, 不编造):
- freq 30      近 250 日涨停(封板收)次数分档: ≥20 极妖/10-19 妖/5-9 活跃
- lianban 20   最高连板高度 + 断板再连(反复激活)
- seal 15      封板成功率(封板收/盘中触及)
- theme 15     涨停归因题材广度(wencai, MVP 未接 → flags 标"缺数据")
- lhb 10       近一年龙虎榜上榜天数(东财 LHB, 2026-09-10 接入; 榜单未覆盖=真实 0)
- stamina 10   近 60 日仍活跃(涨停/触及出现次数)
调整项: 流通市值 20-120 亿 +5(妖股温床; 市值取自 LHB FREE_MARKET_CAP, 无则不加分);
最新事件一字板 → 排板口径标注。
MVP 满分 85+5(题材维 wencai 接入后 100+5), 前端展示时注明口径。

分档阈值与权重为初版经验值, 由"题材启动日妖股领先效应"回测校准
(scripts/backtest_demon_leading.py), 结果入 docs。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

_CST = ZoneInfo("Asia/Shanghai")

WEIGHTS = {"freq": 30, "lianban": 20, "seal": 15, "theme": 15, "lhb": 10, "stamina": 10}

_BUCKETS = (
    (20, 100),
    (10, 80),
    (5, 60),
    (3, 40),
    (1, 20),
)


def _bucket_score(count: int | None) -> int | None:
    if count is None:
        return None
    for th, sc in _BUCKETS:
        if count >= th:
            return sc
    return 0


def _streak_stats(events: list[dict]) -> tuple[int, int]:
    """封板收日序列 → (最高连板, 连板≥2 的段数)。事件按日期升序。

    连续性近似: 相邻封板日自然日间隔 ≤5 天视为连板(覆盖周末+短假期;
    长假期可能合并两段, 由 is_sealed_close 逐日核对, 近似口径标注)。
    """
    dates = sorted({str(e.get("trade_date")) for e in events if e.get("is_sealed_close")})
    if not dates:
        return 0, 0
    from datetime import date as _d

    def _parse(s: str) -> _d | None:
        try:
            return datetime.strptime(s, "%Y%m%d").date()
        except ValueError:
            try:
                return datetime.strptime(s, "%Y-%m-%d").date()
            except ValueError:
                return None

    best = cur = 1
    runs2 = 0
    prev = None
    for ds in dates:
        d = _parse(ds)
        if d is None:
            continue
        if prev is not None and (d - prev).days <= 5:
            cur += 1
        else:
            if prev is not None and cur >= 2:
                runs2 += 1
            cur = 1
        best = max(best, cur)
        prev = d
    if cur >= 2:
        runs2 += 1
    return best, runs2


def lianban_score(max_streak: int | None, runs2: int | None) -> int | None:
    """连板能力: 高度为主, 反复激活加成。"""
    if max_streak is None:
        return None
    base = 0
    if max_streak >= 5:
        base = 100
    elif max_streak >= 3:
        base = 80
    elif max_streak == 2:
        base = 60
    elif max_streak == 1:
        base = 30
    if runs2 and runs2 >= 2:  # 反复激活(妖股记忆效应)
        base = min(100, base + 10)
    return base


def seal_quality_score(events: list[dict]) -> int | None:
    """封板成功率: 封板收 / 盘中触及。"""
    touched = sum(1 for e in events if e.get("touched"))
    sealed = sum(1 for e in events if e.get("is_sealed_close"))
    if touched == 0:
        return None
    return round(sealed / touched * 100)


def recent_activity(events: list[dict], days: int = 60) -> int | None:
    """近 N 自然日涨停/触及次数。"""
    if not events:
        return 0
    cutoff = (datetime.now(_CST) - timedelta(days=days)).strftime("%Y%m%d")
    return sum(1 for e in events if str(e.get("trade_date")) >= cutoff)


def demon_score_from_events(
    events: list[dict],
    circ_mv: float | None = None,
    n_themes: int | None = None,
    n_lhb: int | None = None,
) -> dict:
    """六维评分主入口(纯函数)。events: 近一年涨停事件(升序)。

    返回 {total, grade, dims:{...}, flags:[...], participation}。
    满分口径: MVP 75+5(theme/lhb 未接入), 接入后 100+5。
    """
    flags: list[str] = []
    if not events:
        return {"total": None, "grade": "无数据", "dims": {}, "flags": ["无涨停事件"], "participation": None}

    n_freq = sum(1 for e in events if e.get("is_sealed_close"))
    max_streak, runs2 = _streak_stats(events)
    s_freq = _bucket_score(n_freq)
    s_lianban = lianban_score(max_streak, runs2)
    s_seal = seal_quality_score(events)
    if n_themes is None:
        flags.append("题材广度缺数据(wencai 未接入)")
    s_theme = _bucket_score(n_themes) if n_themes is not None else None
    if n_lhb is None:
        flags.append("龙虎榜频次缺数据(东财 LHB 未覆盖)")
    s_lhb = _bucket_score(n_lhb) if n_lhb is not None else None
    n_recent = recent_activity(events)
    s_stamina = _bucket_score(n_recent) if n_recent is not None else None

    dims_raw = {
        "freq": (s_freq, n_freq, "次封板"),
        "lianban": (s_lianban, max_streak, "最高连板"),
        "seal": (s_seal, s_seal, "%封板成功率"),
        "theme": (s_theme, n_themes, "题材数"),
        "lhb": (s_lhb, n_lhb, "次上榜"),
        "stamina": (s_stamina, n_recent, "近60日触及"),
    }
    dims = {}
    total = 0.0
    for name, w in WEIGHTS.items():
        s, val, unit = dims_raw[name]
        if s is None:
            flags.append(f"{name} 缺数据计 0")
            s = 0
        dims[name] = {"score": s, "weight": w, "value": val, "unit": unit}
        total += w * s / 100.0

    adjustment = 0.0
    participation = "换手板可参与"
    if circ_mv is not None:
        # Hermes 复批(2026-09-06): 市值加分拆两档, 小盘弹性更大加权更高; 回测校准
        if 20e8 <= circ_mv < 50e8:
            adjustment += 8
            flags.append("小盘 20-50 亿(弹性最大) +8")
        elif 50e8 <= circ_mv <= 120e8:
            adjustment += 5
            flags.append("中盘 50-120 亿 +5")
    latest = events[-1]
    if latest.get("one_way"):
        # Hermes 复批: 龙头战法正是一字板买入, 不宜写死"不可参与" → 改参与方式标注
        participation = "一字板：排板可参与，非低吸埋伏标的"
        flags.append("最新事件为一字板(打板/排板口径可参与; 埋伏低吸口径不适配)")

    total = round(total + adjustment, 1)
    grade = "极妖" if n_freq >= 20 else ("妖" if n_freq >= 10 else ("活跃" if n_freq >= 5 else "普通"))
    return {
        "total": total,
        "grade": grade,
        "dims": dims,
        "flags": flags,
        "participation": participation,
        "n_events": len(events),
        "n_sealed": n_freq,
        "max_streak": max_streak,
    }
