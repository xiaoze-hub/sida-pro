"""题材情绪分(2026-09-12 老板口径; 设计见 docs/research/题材情绪分_设计方案_20260912.md)。

每个通达信题材(行业 128 + 概念 457)每个交易日一个 0-100 综合情绪分:
  情绪分 = 0.28×涨停结构 + 0.24×题材扩散 + 0.20×核心强度 + 0.20×接力反馈 + 0.08×连续性
- 各维固定锚点映射 → 跨交易日可比(不做当日截面归一, 窗口/题材数量不影响排名);
- 缺失维度 → 50 中性收缩(权重不转移); 置信度独立(有效维度覆盖×成分覆盖×新鲜度);
- 涨停事实由日线 + limit_rules 自算(与 limit_up_events 同口径), 当日不依赖外部涨停池。
"""
from __future__ import annotations

WEIGHTS = {"s1": 0.28, "s2": 0.24, "s3": 0.20, "s4": 0.20, "s5": 0.08}
NEUTRAL = 50.0

# 属性/情绪类"板块"不是题材: 昨日涨停/连板系、持仓系、股本属性系等 —— 参与评分会误导
# (如"昨日涨停"必然恒居榜首)。名称命中即整板跳过(不评分不落库), 计入 scan() 返回的 skipped_meta。
META_KEYWORDS = (
    "昨日", "MSCI", "QFII", "重仓", "新进", "微盘", "微小盘", "大盘股", "次新", "低价股",
    "高价股", "破净", "送转", "分红", "解禁", "减持", "增持", "回购", "户数", "融资",
    "活跃小盘", "个人持股", "金股", "ST板块",
)


def is_meta_board(name: str) -> bool:
    """属性/情绪类板块(非题材)判定(纯函数)。"""
    n = str(name or "")
    return any(k in n for k in META_KEYWORDS)


def anchor_map(value, anchors) -> float:
    """分段线性夹逼 → 0-100; None/脏值 → 50 中性。anchors 按 x 升序。"""
    if value is None:
        return NEUTRAL
    try:
        v = float(value)
    except (TypeError, ValueError):
        return NEUTRAL
    if v <= anchors[0][0]:
        return float(anchors[0][1])
    if v >= anchors[-1][0]:
        return float(anchors[-1][1])
    for (x0, y0), (x1, y1) in zip(anchors, anchors[1:]):
        if x0 <= v <= x1:
            if x1 == x0:
                return float(y1)
            return float(y0 + (y1 - y0) * (v - x0) / (x1 - x0))
    return float(anchors[-1][1])


def percentile_rank(values, v) -> float | None:
    """v 在 values 中的百分位(0-100); 空样本/v 为 None → None(调用方按缺失处理)。"""
    xs = [float(x) for x in values if x is not None]
    if not xs or v is None:
        return None
    return 100.0 * sum(1 for x in xs if x <= float(v)) / len(xs)


_ANCH_LADDER_HEIGHT = [(1, 20), (2, 45), (3, 65), (4, 80), (6, 100)]
_ANCH_LADDER_GE2 = [(0, 0), (1, 40), (2, 60), (3, 75), (5, 100)]
_ANCH_SCALE = [(1, 35), (2, 55), (3, 68), (5, 82), (8, 92), (12, 100)]
_ANCH_SCARCE = [(0.0, 0), (0.02, 40), (0.05, 60), (0.10, 80), (0.20, 100)]
_ANCH_EXCESS_PCT = [(-3, 0), (-1, 25), (0, 50), (1, 75), (3, 100)]
_ANCH_STRONG_PP = [(-15, 0), (-8, 10), (-3, 30), (0, 50), (3, 70), (8, 90), (15, 100)]


def dim_structure(*, sealed: int, touched: int, max_boards: int, ge2: int,
                  hist_sealed: list[int], market_sealed: int) -> tuple[float | None, dict]:
    """S1 涨停结构(封住口径): 梯队40 + 规模25 + 自身历史分位20 + 同日稀缺度15。"""
    if not sealed:
        return None, {"missing": "当日无封住"}
    ladder = 0.6 * anchor_map(max_boards, _ANCH_LADDER_HEIGHT) + 0.4 * anchor_map(ge2, _ANCH_LADDER_GE2)
    scale = anchor_map(sealed, _ANCH_SCALE)
    pct = percentile_rank(hist_sealed, sealed)
    share = (sealed / market_sealed) if market_sealed else None
    scarce = anchor_map(share, _ANCH_SCARCE) if share is not None else None
    score = (
        0.40 * ladder
        + 0.25 * scale
        + 0.20 * (NEUTRAL if pct is None else pct)
        + 0.15 * (NEUTRAL if scarce is None else scarce)
    )
    detail = {
        "sealed": sealed, "touched": touched, "max_boards": max_boards, "ge2": ge2,
        "ladder": round(ladder, 2), "scale": round(scale, 2),
        "hist_pct": None if pct is None else round(pct, 2),
        "scarce": None if scarce is None else round(scarce, 2),
    }
    return score, detail


def dim_diffusion(*, pcts: list[float], market: dict) -> tuple[float | None, dict]:
    """S2 题材扩散: 中位/平均涨幅与强涨占比, 全部减全市场同口径(扣普涨)。"""
    sample = [float(p) for p in pcts if p is not None]
    if not sample:
        return None, {"missing": "无成分行情"}
    sample.sort()
    n = len(sample)
    med = sample[n // 2] if n % 2 else (sample[n // 2 - 1] + sample[n // 2]) / 2.0
    mean = sum(sample) / n
    strong_share = sum(1 for p in sample if p >= 5.0) / n * 100.0
    med_ex = med - float(market.get("pct_median") or 0.0)
    mean_ex = mean - float(market.get("pct_mean") or 0.0)
    strong_ex = strong_share - float(market.get("strong_share") or 0.0)
    score = (
        0.45 * anchor_map(med_ex, _ANCH_EXCESS_PCT)
        + 0.30 * anchor_map(mean_ex, _ANCH_EXCESS_PCT)
        + 0.25 * anchor_map(strong_ex, _ANCH_STRONG_PP)
    )
    detail = {
        "median_pct": round(med, 2), "mean_pct": round(mean, 2), "strong_share": round(strong_share, 2),
        "median_excess": round(med_ex, 2), "mean_excess": round(mean_ex, 2),
        "strong_excess_pp": round(strong_ex, 2), "sample": n,
    }
    return score, detail


_ANCH_BOARDS = [(1, 25), (2, 55), (3, 75), (4, 88), (6, 100)]
_ANCH_MOM5 = [(-10, 0), (0, 45), (5, 65), (15, 85), (30, 100)]
SEAL_QUALITY = {"一字": 1.0, "全天封死": 0.8, "开过板": 0.45}


def core_stock_score(*, boards: int, amount_pct: float | None, momentum5: float | None) -> float:
    """个股核心分 = 0.45×连板高度 + 0.30×题材内成交额分位 + 0.25×近5日动量。"""
    return (
        0.45 * anchor_map(boards, _ANCH_BOARDS)
        + 0.30 * (NEUTRAL if amount_pct is None else float(amount_pct))
        + 0.25 * anchor_map(momentum5, _ANCH_MOM5)
    )


def continuation_prob(*, boards: int, seal_quality: str, amount_pct: float | None) -> float:
    """连续概率(0-1, 展示用, 不参与总分): 连板高度 + 封板质量 + 量能分位。"""
    q = SEAL_QUALITY.get(seal_quality, 0.45)
    h = min(1.0, 0.25 * max(0, int(boards) - 1))
    a = (float(amount_pct) / 100.0) if amount_pct is not None else 0.5
    return round(max(0.05, min(0.95, 0.35 * q + 0.30 * h + 0.20 * a + 0.15)), 2)


def dim_core(candidates: list[dict]) -> tuple[float | None, dict]:
    """S3 核心强度 = 0.7×最高核心分 + 0.3×次高(仅 1 只则权重 1.0)。"""
    if not candidates:
        return None, {"missing": "无核心候选"}
    scores = sorted((float(c["core_score"]) for c in candidates), reverse=True)
    score = scores[0] if len(scores) == 1 else 0.7 * scores[0] + 0.3 * scores[1]
    detail = {"core_count": len(scores), "top": round(scores[0], 2),
              "second": round(scores[1], 2) if len(scores) > 1 else None}
    return score, detail


_ANCH_PROMOTE = [(0, 10), (20, 35), (33, 50), (50, 70), (80, 90), (100, 100)]
_ANCH_SEAL = [(0, 10), (30, 35), (50, 50), (70, 65), (90, 85), (100, 100)]
_ANCH_CARRY = [(0, 10), (30, 40), (50, 55), (70, 70), (90, 85), (100, 100)]
_ANCH_D1 = [(-10, 0), (-5, 25), (0, 50), (5, 75), (10, 100)]


def dim_relay(*, prev_sealed: int, promoted: int, touched_today: int, sealed_today: int,
              prev_failed: int, failed_up: int, highest_pct: float | None) -> tuple[float | None, dict]:
    """S4 接力反馈: 晋级率35 + 封板率30 + 断板承接20 + 昨日最高板D+1 15; 子项缺失→50。"""
    promote = anchor_map(promoted / prev_sealed * 100.0, _ANCH_PROMOTE) if prev_sealed else None
    seal = anchor_map(sealed_today / touched_today * 100.0, _ANCH_SEAL) if touched_today else None
    carry = anchor_map(failed_up / prev_failed * 100.0, _ANCH_CARRY) if prev_failed else None
    d1 = anchor_map(highest_pct, _ANCH_D1) if highest_pct is not None else None
    if promote is None and seal is None and carry is None and d1 is None:
        return None, {"missing": "无昨日样本"}
    score = (
        0.35 * (NEUTRAL if promote is None else promote)
        + 0.30 * (NEUTRAL if seal is None else seal)
        + 0.20 * (NEUTRAL if carry is None else carry)
        + 0.15 * (NEUTRAL if d1 is None else d1)
    )
    detail = {
        "promote_rate": None if promote is None else round(promoted / prev_sealed * 100.0, 1),
        "seal_rate": None if seal is None else round(sealed_today / touched_today * 100.0, 1),
        "carry_rate": None if carry is None else round(failed_up / prev_failed * 100.0, 1),
        "highest_d1_pct": highest_pct,
        "prev_sealed": prev_sealed, "prev_failed": prev_failed,
    }
    return score, detail


def dim_continuity(recent_s1: list[float]) -> tuple[float | None, dict]:
    """S5 连续性: 此前 3 个交易日结构分的均值 × 稳定度(100 − 2×标准差)。"""
    xs = [float(x) for x in recent_s1 if x is not None][-3:]
    if not xs:
        return None, {"missing": "无历史结构分"}
    mean = sum(xs) / len(xs)
    if len(xs) >= 2:
        var = sum((x - mean) ** 2 for x in xs) / len(xs)
        stab = max(0.0, min(100.0, 100.0 - 2.0 * (var ** 0.5)))
    else:
        stab = NEUTRAL
    return 0.6 * mean + 0.4 * stab, {"s1_mean3": round(mean, 2), "stability": round(stab, 2), "days": len(xs)}


MAX_CORE_STOCKS = 2


def total_score(parts: dict[str, float | None]) -> float:
    """情绪分: 缺失维度按 50 参与(权重不转移)。"""
    return round(sum(WEIGHTS[k] * (NEUTRAL if parts.get(k) is None else float(parts[k])) for k in WEIGHTS), 1)


def confidence_of(*, parts: dict[str, float | None], coverage: float | None, fresh: float = 1.0) -> int:
    """置信度(0-100): 0.5×有效维度权重覆盖 + 0.3×成分覆盖率 + 0.2×数据新鲜度。"""
    valid_w = sum(WEIGHTS[k] for k in WEIGHTS if parts.get(k) is not None)
    cov = 0.5 if coverage is None else max(0.0, min(1.0, float(coverage)))
    fresh = max(0.0, min(1.0, float(fresh)))
    return int(round(100 * (0.5 * valid_w + 0.3 * cov + 0.2 * fresh)))


def is_core(*, score: float, confidence: int, recent_scores: list, recent_sealed: list,
            sealed_today: int, relay: float | None) -> bool:
    """核心题材判定: 分≥60 且 置信≥70 且 近3日≥2日≥55 且其中至少一日封住≥2 且 当日封住≥2 且 接力≥50。"""
    if score < 60 or confidence < 70 or sealed_today < 2:
        return False
    if relay is None or relay < 50:
        return False
    pairs = [(s, n) for s, n in zip(list(recent_scores)[-3:], list(recent_sealed)[-3:]) if s is not None]
    days55 = [p for p in pairs if p[0] >= 55]
    if len(days55) < 2:
        return False
    return any(n is not None and int(n) >= 2 for _, n in days55)


def rank_items(items: list[dict]) -> list[dict]:
    """排序: 情绪分 → 近3日均分 → 置信度 → 当日封住家数 → 板块代码(稳定序)。"""
    def key(it: dict):
        return (
            -float(it.get("score") or 0.0),
            -float(it.get("score3_avg") or 0.0),
            -int(it.get("confidence") or 0),
            -int(it.get("limit_up_cnt") or 0),
            str(it.get("block_code") or ""),
        )
    return sorted(items, key=key)


def align_cells(cells: list[dict] | None, dates: list[str]) -> list[dict]:
    """按共享日期轴对齐 cells(缺该交易日的题材补空位), 保证矩阵列与时间轴严格对位。"""
    by_date = {c.get("date"): c for c in (cells or [])}
    out = []
    for d in dates:
        c = by_date.get(d) or {}
        out.append({"date": d, "score": c.get("score"), "limit_up_cnt": c.get("limit_up_cnt")})
    return out


def market_series(rows: list[dict], dates: list[str], top_n: int = 20) -> list[dict]:
    """强势情绪走势(周期表顶部曲线): 每个交易日情绪分前 top_n 名的均值。

    不用全体题材均值 —— 521 个题材里多数长期休眠(贴 50 中性), 均值被压成 49~53 的直线;
    前 top_n 是"领先端", 才有周期形态(实测 67.9 → 83.8 → 71.7)。缺该交易日 → score=None。
    """
    by_date: dict[str, list[float]] = {}
    for r in rows:
        d, s = r.get("trade_date"), r.get("score")
        if d is None or s is None:
            continue
        by_date.setdefault(d, []).append(float(s))
    out = []
    for d in dates:
        vals = sorted(by_date.get(d) or [], reverse=True)[: int(top_n)]
        out.append({"date": d, "score": round(sum(vals) / len(vals), 1) if vals else None})
    return out


def compute_theme_day(*, date: str, today: dict, pcts: list, market: dict, hist_sealed: list,
                      s1_history: list, sealed_history: list, prev: dict, core_candidates: list) -> dict:
    """单题材单日装配(纯函数): 五维 → 总分/置信度/核心/明细/广度。"""
    s1, d1 = dim_structure(sealed=int(today["sealed"]), touched=int(today["touched"]),
                           max_boards=int(today["max_boards"]), ge2=int(today["ge2"]),
                           hist_sealed=hist_sealed, market_sealed=int(market.get("sealed") or 0))
    s2, d2 = dim_diffusion(pcts=pcts, market=market)
    s3, d3 = dim_core(core_candidates)
    s4, d4 = dim_relay(**prev)
    s5, d5 = dim_continuity(s1_history)
    parts = {"s1": s1, "s2": s2, "s3": s3, "s4": s4, "s5": s5}
    score = total_score(parts)
    sample = [p for p in pcts if p is not None]
    members = int(today.get("members") or 0)
    coverage = (len(sample) / members) if members else None
    conf = confidence_of(parts=parts, coverage=coverage)
    recent_scores = [*list(s1_history)[-2:], s1] if s1 is not None else list(s1_history)[-3:]
    recent_sealed = [*list(sealed_history)[-2:], int(today["sealed"])]
    core = is_core(score=score, confidence=conf, recent_scores=recent_scores, recent_sealed=recent_sealed,
                   sealed_today=int(today["sealed"]), relay=s4)
    stocks = sorted(core_candidates, key=lambda c: -float(c.get("core_score") or 0))[:MAX_CORE_STOCKS]
    return {
        "trade_date": date,
        "score": score,
        "s1": None if s1 is None else round(s1, 1),
        "s2": None if s2 is None else round(s2, 1),
        "s3": None if s3 is None else round(s3, 1),
        "s4": None if s4 is None else round(s4, 1),
        "s5": None if s5 is None else round(s5, 1),
        "confidence": conf,
        "core": core,
        "limit_up_cnt": int(today["sealed"]),
        "touched_cnt": int(today["touched"]),
        "max_boards": int(today["max_boards"]),
        "ge2_cnt": int(today["ge2"]),
        "core_stocks": [
            {"symbol": c.get("symbol"), "name": c.get("name"), "boards": c.get("boards"),
             "pct": c.get("pct"), "score": round(float(c.get("core_score") or 0), 1), "prob": c.get("prob")}
            for c in stocks
        ],
        "detail": {"s1": d1, "s2": d2, "s3": d3, "s4": d4, "s5": d5},
        "breadth": {"coverage": None if coverage is None else round(coverage, 3),
                    "sample": len(sample), "members": members,
                    "median_pct": d2.get("median_pct"), "strong_share": d2.get("strong_share")},
        "source": "close",
    }


# ── IO 段: 取数(通达信) → 逐日计算 → 幂等落库 ─────────────────────────────────
import json  # noqa: E402
import logging  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402

from src.core.tdx_boards import constituents, name_map, sector_items  # noqa: E402

logger = logging.getLogger(__name__)
_CST = timezone(timedelta(hours=8))
TABLE = "theme_mood_daily"
KLINE_WINDOW = 60          # 取数窗口(交易日), 提供 60 日自身分位
_CHUNK = 100               # TDX get_market_data 单次代码上限


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _tdx_code(sym: str) -> str:
    s = str(sym or "").strip().upper()
    if "." in s:
        return s
    if s.startswith(("6", "9")):
        return f"{s}.SH"
    if s.startswith(("4", "8")):
        return f"{s}.BJ"
    return f"{s}.SZ"


def _fetch_bars(codes: list[str]) -> dict[str, list[dict]]:
    """通达信批量日线(前复权) → {code: bars}; 分片失败跳过。"""
    from marketdata.vendors.tq import tq_rpc

    out: dict[str, list[dict]] = {}
    for i in range(0, len(codes), _CHUNK):
        part = codes[i:i + _CHUNK]
        try:
            v = tq_rpc("get_market_data", {"stock_list": part, "period": "1d",
                                           "count": KLINE_WINDOW + 5, "dividend_type": "front"}, timeout=120)
        except Exception as e:  # noqa: BLE001
            logger.warning("题材情绪: 日线批量失败(%d 码): %s", len(part), e)
            continue
        for code, rows in (v or {}).items():
            if not isinstance(rows, dict):
                continue
            ds, op, cl = rows.get("Date") or [], rows.get("Open") or [], rows.get("Close") or []
            hi, lo = rows.get("High") or [], rows.get("Low") or []
            vo, am = rows.get("Volume") or [], rows.get("Amount") or []
            n = min(len(ds), len(op), len(cl), len(hi), len(lo))
            bars = []
            for j in range(n):
                bars.append({
                    "date": str(ds[j]).replace("-", ""),
                    "open": _f(op[j]), "close": _f(cl[j]), "high": _f(hi[j]), "low": _f(lo[j]),
                    "volume": _f(vo[j]) if j < len(vo) else None,
                    "amount": _f(am[j]) if j < len(am) else None,
                })
            if bars:
                out[code] = bars
    return out


def _stock_series(symbol: str, name: str, bars: list[dict]) -> dict:
    """个股窗口序列: {pct, amount, events, streak}(events 与 limit_up_events 同口径)。

    注意: limit_rules 只认 6 位纯数字代码(带 .SH/.SZ 后缀会返 None), 故此处剥离后缀。
    """
    from src.core.limit_up_backfill import _extract_events_from_bars

    code6 = str(symbol).split(".")[0]
    pct: dict[str, float] = {}
    amount: dict[str, float] = {}
    for i, b in enumerate(bars):
        if b.get("amount") is not None:
            amount[b["date"]] = float(b["amount"])
        if i == 0:
            continue
        prev_c, cur_c = bars[i - 1].get("close"), b.get("close")
        if prev_c and cur_c:
            pct[b["date"]] = (float(cur_c) / float(prev_c) - 1.0) * 100.0
    events = {e["trade_date"]: e for e in _extract_events_from_bars(code6, name, bars)}
    streak: dict[str, int] = {}
    run = 0
    for b in bars:
        d = b["date"]
        ev = events.get(d)
        run = run + 1 if (ev and ev.get("is_sealed_close")) else 0
        if run:
            streak[d] = run
    return {"pct": pct, "amount": amount, "events": events, "streak": streak}


def _market_ctx(series: dict[str, dict], date: str) -> dict:
    """全市场当日基准(等权): 中位/平均涨幅、≥5% 占比、封住家数。"""
    market_pcts = [s["pct"].get(date) for s in series.values()]
    market_pcts = [p for p in market_pcts if p is not None]
    market_pcts.sort()
    n = len(market_pcts)
    med = None
    if n:
        med = market_pcts[n // 2] if n % 2 else (market_pcts[n // 2 - 1] + market_pcts[n // 2]) / 2.0
    mean = (sum(market_pcts) / n) if n else None
    strong = (sum(1 for p in market_pcts if p >= 5.0) / n * 100.0) if n else None
    sealed = sum(1 for s in series.values() if (s["events"].get(date) or {}).get("is_sealed_close"))
    return {"pct_median": med, "pct_mean": mean, "strong_share": strong, "sealed": sealed}


def _theme_today(code: str, members: list[str], series: dict, date: str) -> dict:
    sealed_syms, failed_syms, touched = [], [], 0
    ge2, sealed, best_board, highest_sym = 0, 0, 0, None
    for s in members:
        ser = series.get(s)
        if not ser:
            continue
        ev = ser["events"].get(date)
        if not ev:
            continue
        touched += 1
        st = int(ser["streak"].get(date) or 0)
        if ev.get("is_sealed_close"):
            sealed += 1
            sealed_syms.append(s)
            if st >= 2:
                ge2 += 1
            if st > best_board:
                best_board, highest_sym = st, s
        else:
            failed_syms.append(s)
    return {"members": len(members), "sealed": sealed, "touched": touched, "max_boards": best_board,
            "ge2": ge2, "sealed_syms": sealed_syms, "failed_syms": failed_syms, "highest_sym": highest_sym}


def _theme_prev(prev_state: dict, today: dict, series: dict, date: str) -> dict:
    """接力反馈输入: 昨日封住→今日晋级 / 昨日炸板→今日承接 / 昨日最高板→今日涨跌幅。"""
    prev_sealed = list(prev_state.get("sealed_syms") or [])
    promoted = sum(1 for s in prev_sealed
                   if (series.get(s, {}).get("events", {}).get(date) or {}).get("is_sealed_close"))
    prev_failed = list(prev_state.get("failed_syms") or [])
    failed_up = sum(1 for s in prev_failed if (series.get(s, {}).get("pct", {}).get(date) or 0) > 0)
    hs = prev_state.get("highest_sym")
    highest_pct = (series.get(hs, {}).get("pct", {}).get(date)) if hs else None
    return {"prev_sealed": len(prev_sealed), "promoted": promoted,
            "touched_today": today["touched"], "sealed_today": today["sealed"],
            "prev_failed": len(prev_failed), "failed_up": failed_up, "highest_pct": highest_pct}


def _theme_cores(code: str, members: list[str], series: dict, date: str) -> list[dict]:
    """核心候选: 该题材今日封住股, 按 连板高度/题材内成交额分位/近5日动量 打分。"""
    ev_syms = [(s, series[s]) for s in members
               if series.get(s) and (series[s]["events"].get(date) or {}).get("is_sealed_close")]
    if not ev_syms:
        return []
    amounts = sorted((ser["amount"].get(date) or 0.0) for _, ser in ev_syms)
    out = []
    for s, ser in ev_syms:
        ev = ser["events"][date]
        boards = int(ser["streak"].get(date) or 1)
        amt = ser["amount"].get(date) or 0.0
        rank = (sum(1 for a in amounts if a <= amt) / len(amounts) * 100.0) if amounts else None
        mom5 = None
        pcts = [ser["pct"][d] for d in sorted(ser["pct"])]
        if len(pcts) >= 5:
            base = 100.0
            for p in pcts[-5:]:
                base *= (1 + p / 100.0)
            mom5 = base - 100.0
        if ev.get("one_way"):
            quality = "一字"
        elif ev.get("low_price") is not None and ev.get("limit_price") is not None \
                and float(ev["low_price"]) >= float(ev["limit_price"]) - 1e-6:
            quality = "全天封死"
        else:
            quality = "开过板"
        out.append({
            "symbol": s, "name": ev.get("name"), "boards": boards, "pct": ser["pct"].get(date),
            "core_score": core_stock_score(boards=boards, amount_pct=rank, momentum5=mom5),
            "prob": continuation_prob(boards=boards, seal_quality=quality, amount_pct=rank),
        })
    return out


def _upsert(rows: list[dict]) -> int:
    from sqlalchemy import text as _text

    from src.db.session import engine

    cols = ("trade_date", "block_code", "block_name", "block_type", "score", "s1", "s2", "s3", "s4", "s5",
            "confidence", "limit_up_cnt", "touched_cnt", "max_boards", "ge2_cnt", "source")
    with engine.begin() as conn:
        for r in rows:
            conn.execute(
                _text(
                    """
                    INSERT INTO theme_mood_daily
                        (trade_date, block_code, block_name, block_type, score, s1, s2, s3, s4, s5,
                         confidence, core, limit_up_cnt, touched_cnt, max_boards, ge2_cnt,
                         core_stocks, detail, breadth, source)
                    VALUES (:trade_date, :block_code, :block_name, :block_type, :score, :s1, :s2, :s3, :s4, :s5,
                            :confidence, :core, :limit_up_cnt, :touched_cnt, :max_boards, :ge2_cnt,
                            :core_stocks, :detail, :breadth, :source)
                    ON CONFLICT(trade_date, block_code) DO UPDATE SET
                        block_name=excluded.block_name, block_type=excluded.block_type, score=excluded.score,
                        s1=excluded.s1, s2=excluded.s2, s3=excluded.s3, s4=excluded.s4, s5=excluded.s5,
                        confidence=excluded.confidence, core=excluded.core, limit_up_cnt=excluded.limit_up_cnt,
                        touched_cnt=excluded.touched_cnt, max_boards=excluded.max_boards, ge2_cnt=excluded.ge2_cnt,
                        core_stocks=excluded.core_stocks, detail=excluded.detail, breadth=excluded.breadth,
                        source=excluded.source, updated_at=CURRENT_TIMESTAMP
                    """
                ),
                {
                    **{k: r.get(k) for k in cols},
                    "core": bool(r.get("core")),
                    "core_stocks": json.dumps(r.get("core_stocks") or [], ensure_ascii=False),
                    "detail": json.dumps(r.get("detail") or {}, ensure_ascii=False),
                    "breadth": json.dumps(r.get("breadth") or {}, ensure_ascii=False),
                },
            )
    return len(rows)


def _purge_codes(dates: list[str], codes: list[str]) -> int:
    """删除指定日期下这些板块的落库行(属性板块规则变更/收紧后不留脏行)。"""
    if not dates or not codes:
        return 0
    from sqlalchemy import text as _text

    from src.db.session import engine

    n = 0
    with engine.begin() as conn:
        for d in dates:
            for i in range(0, len(codes), 100):
                part = codes[i:i + 100]
                keys = {f"c{j}": c for j, c in enumerate(part)}
                ph = ", ".join(f":{k}" for k in keys)
                r = conn.execute(
                    _text(f"DELETE FROM theme_mood_daily WHERE trade_date = :d AND block_code IN ({ph})"),
                    {"d": d, **keys},
                )
                n += r.rowcount or 0
    return n


def scan(*, write_days: int = 1, day: str | None = None) -> dict:
    """全量扫描: 585 题材 × 最近 write_days 个交易日 → 幂等落库。永不抛异常。"""
    try:
        themes_all = sector_items()
        if not themes_all:
            return {"ok": False, "reason": "板块目录为空"}
        themes = [t for t in themes_all if not is_meta_board(t.get("name"))]
        skipped_meta = len(themes_all) - len(themes)
        const: dict[str, list[str]] = {}
        for t in themes:
            syms = constituents(t["code"]) or []
            if syms:
                const[t["code"]] = [_tdx_code(s) for s in syms]
        all_syms = sorted({s for syms in const.values() for s in syms})
        if not all_syms:
            return {"ok": False, "reason": "无成分股"}
        bars_by = _fetch_bars(all_syms)
        # 个股名(供 ST 涨停幅度 5% 判定: _extract_events_from_bars 按 "ST" 关键字识别)
        names = name_map()
        series = {s: _stock_series(s, names.get(s, ""), b) for s, b in bars_by.items()}
        dates = sorted({d for s in series.values() for d in s["pct"]})
        if not dates:
            return {"ok": False, "reason": "无日线数据"}
        if day:
            dates = [d for d in dates if d <= day.replace("-", "")]
        write = dates[-max(1, int(write_days)):]
        sealed_hist: dict[str, list[int]] = {c: [] for c in const}
        s1_hist: dict[str, list[float]] = {c: [] for c in const}
        prev_state: dict[str, dict] = {c: {} for c in const}
        rows: list[dict] = []
        for d in dates:
            market = _market_ctx(series, d)
            for t in themes:
                code = t["code"]
                members = const.get(code) or []
                if not members:
                    continue
                today = _theme_today(code, members, series, d)
                prev = _theme_prev(prev_state[code], today, series, d)
                cores = _theme_cores(code, members, series, d)
                if d in write:
                    row = compute_theme_day(
                        date=d, today=today,
                        pcts=[(series.get(s) or {}).get("pct", {}).get(d) for s in members],
                        market=market, hist_sealed=sealed_hist[code][-60:], s1_history=s1_hist[code][-3:],
                        sealed_history=sealed_hist[code][-2:], prev=prev, core_candidates=cores,
                    )
                    row["block_code"] = code
                    row["block_name"] = t.get("name") or code
                    row["block_type"] = t.get("type") or ("industry" if code.startswith("881") else "concept")
                    rows.append(row)
                s1_valid = None
                if today["sealed"]:
                    s1_valid, _ = dim_structure(sealed=today["sealed"], touched=today["touched"],
                                                max_boards=today["max_boards"], ge2=today["ge2"],
                                                hist_sealed=sealed_hist[code][-60:],
                                                market_sealed=int(market.get("sealed") or 0))
                sealed_hist[code].append(today["sealed"])
                s1_hist[code].append(NEUTRAL if s1_valid is None else s1_valid)
                prev_state[code] = {
                    "sealed_syms": today["sealed_syms"], "failed_syms": today["failed_syms"],
                    "highest_sym": today["highest_sym"],
                }
        if not rows:
            return {"ok": False, "reason": "无可写行"}
        _upsert(rows)
        meta_codes = [t["code"] for t in themes_all if is_meta_board(t.get("name"))]
        purged = _purge_codes(write, meta_codes)
        return {"ok": True, "rows": len(rows), "dates": write, "themes": len(const),
                "symbols": len(all_syms), "skipped_meta": skipped_meta, "purged": purged}
    except Exception as e:  # noqa: BLE001
        logger.warning("题材情绪扫描异常: %s", e)
        return {"ok": False, "reason": str(e)}


def daily_job() -> dict:
    """cron 入口(交易日 15:45): 扫描当日。永不抛异常。"""
    try:
        return scan(write_days=1)
    except Exception as e:  # noqa: BLE001
        logger.warning("题材情绪每日任务异常: %s", e)
        return {"ok": False, "reason": str(e)}
