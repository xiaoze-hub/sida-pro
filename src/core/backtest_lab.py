"""量化因子多维胜率回测与持久化实验室（2026-10-23）。

## 设计原则（对应用户铁律）

1. **所有回测结果必须落库**：计算结果幂等写入 `factor_winrate_daily`（Migration 221），
   数智参谋工具 `get_factor_winrate` 读库毫秒级响应，不靠临时口算。
2. **强制同池无条件基准对照**：任何因子分桶都同时算同一股票池、同一时间窗的
   `baseline_win_rate_pct` 与 `baseline_avg_return_pct`，并给出 `excess_win_rate_pp`
   （超额胜率百分点）与 `excess_return_pct`（超额均值收益），防止把大盘贝塔当成因子阿尔法。
3. **零未来函数（No Look-Ahead Bias）**：
   - `G信号_原始` 以交叉当日 `T` 收盘价为锚点，统计 `T -> T+h`；
   - `G信号_次日确认` 在 `T-1` 发生上穿且 `T` 日维持 `A0 > BB0` 时，**以确认日 `T` 的收盘价为锚点**
     统计 `T -> T+h`（绝不拿 `T-1` 的价格算确认后的收益）。
4. **可交易性过滤**：若 `T+1` 为一字涨停（`open == high == low` 且涨幅 ≥ 4.8%），现实中无法买入，
   计入 `untradable_skipped` 并从多头信号样本中剔除。
5. **样本不足不给胜率**：`sample_count < MIN_SAMPLE`（默认 20）时，`win_rate_pct` 等指标一律写
   `None`，诚实标注样本不足。
"""
from __future__ import annotations

import logging
import statistics
from collections import defaultdict
from datetime import datetime
from typing import Any, Sequence
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

MIN_SAMPLE = 20
MIN_WARMUP_BARS = 35
DEFAULT_HORIZONS: tuple[int, ...] = (1, 3, 5)
SOURCE = "backtest_lab_v1"

FAMILY_LABELS: dict[str, str] = {
    "gs_state": "数智决策·GS趋势状态与信号",
    "ai_activity": "数智决策·AI机构活跃度分档",
    "gs_x_activity": "数智决策·GS×活跃度共振组合",
    "wyckoff": "威科夫·日K量价结构四标",
    "limit_up_quality": "涨停板·身位与封板质量",
}


def _today_compact() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d")


def _sma_at(vals: list[float], n: int, idx: int) -> float | None:
    if idx + 1 < n or n <= 0:
        return None
    return sum(vals[idx + 1 - n : idx + 1]) / n


def _is_untradable_next_bar(cur_close: float, next_bar: dict[str, float]) -> bool:
    """T+1 是否为一字涨停（开=高=低 且较前收涨幅 ≥ 4.8%，现实中买不进）。"""
    if cur_close <= 0:
        return True
    o, h, l = next_bar["open"], next_bar["high"], next_bar["low"]
    if h <= 0 or l <= 0:
        return True
    if abs(h - l) < 1e-6 and abs(o - h) < 1e-6 and (o - cur_close) / cur_close >= 0.048:
        return True
    return False


def _forward_outcome(
    bars: list[dict[str, float]], idx: int, horizon: int
) -> tuple[float, float] | None:
    """计算第 idx 根到 idx+horizon 根的 (收盘收益率%, 期内最高涨幅%)。"""
    if idx + horizon >= len(bars):
        return None
    c0 = bars[idx]["close"]
    c_end = bars[idx + horizon]["close"]
    if c0 <= 0 or c_end <= 0:
        return None
    highs = [bars[k]["high"] for k in range(idx + 1, idx + 1 + horizon) if bars[k]["high"] > 0]
    if not highs:
        return None
    close_ret = (c_end - c0) / c0 * 100.0
    max_gain = (max(highs) - c0) / c0 * 100.0
    return close_ret, max_gain


def _agg_outcomes(
    outcomes: list[tuple[float, float]],
    skipped: int,
    *,
    baseline_wr: float | None = None,
    baseline_avg: float | None = None,
    min_sample: int = MIN_SAMPLE,
) -> dict[str, Any]:
    """将 [(close_ret_pct, max_gain_pct), ...] 聚合为统计字典。"""
    n = len(outcomes)
    wins = [r for r, _ in outcomes if r > 0]
    losses = [abs(r) for r, _ in outcomes if r < 0]
    win_cnt = len(wins)
    if n < min_sample:
        return {
            "sample_count": n,
            "win_count": win_cnt,
            "untradable_skipped": skipped,
            "win_rate_pct": None,
            "hit3_rate_pct": None,
            "avg_return_pct": None,
            "median_return_pct": None,
            "profit_loss_ratio": None,
            "baseline_win_rate_pct": baseline_wr,
            "baseline_avg_return_pct": baseline_avg,
            "excess_win_rate_pp": None,
            "excess_return_pct": None,
        }
    rets = [r for r, _ in outcomes]
    hit3 = sum(1 for _, mg in outcomes if mg >= 3.0)
    wr = round(win_cnt / n * 100.0, 2)
    hit3_pct = round(hit3 / n * 100.0, 2)
    avg_ret = round(statistics.mean(rets), 4)
    med_ret = round(statistics.median(rets), 4)
    avg_win = statistics.mean(wins) if wins else None
    avg_loss = statistics.mean(losses) if losses else None
    pl_ratio = round(avg_win / avg_loss, 2) if (avg_win and avg_loss and avg_loss > 0) else None
    ex_wr = round(wr - baseline_wr, 2) if baseline_wr is not None else None
    ex_ret = round(avg_ret - baseline_avg, 4) if baseline_avg is not None else None
    return {
        "sample_count": n,
        "win_count": win_cnt,
        "untradable_skipped": skipped,
        "win_rate_pct": wr,
        "hit3_rate_pct": hit3_pct,
        "avg_return_pct": avg_ret,
        "median_return_pct": med_ret,
        "profit_loss_ratio": pl_ratio,
        "baseline_win_rate_pct": baseline_wr,
        "baseline_avg_return_pct": baseline_avg,
        "excess_win_rate_pp": ex_wr,
        "excess_return_pct": ex_ret,
    }


def _norm_bar(b: Any) -> dict[str, Any] | None:
    """将 dict 或 Bar 对象归一为 {date, open, high, low, close, volume}。"""
    def g(k: str) -> Any:
        return b.get(k) if isinstance(b, dict) else getattr(b, k, None)

    try:
        o = float(g("open"))
        h = float(g("high"))
        l = float(g("low"))
        c = float(g("close"))
    except (TypeError, ValueError):
        return None
    if o <= 0 or h <= 0 or l <= 0 or c <= 0 or h < l:
        return None
    v_raw = g("volume")
    try:
        v = float(v_raw) if v_raw not in (None, "") else 0.0
    except (TypeError, ValueError):
        v = 0.0
    if h == l and v <= 0:
        return None
    d = str(g("date") or "")[:10].replace("-", "")
    return {"date": d, "open": o, "high": h, "low": l, "close": c, "volume": v}


def evaluate_bars_universe(
    symbol_bars: dict[str, Sequence[Any]],
    *,
    horizons: Sequence[int] = DEFAULT_HORIZONS,
    as_of_date: str | None = None,
    min_sample: int = MIN_SAMPLE,
) -> list[dict[str, Any]]:
    """对给定的 `{symbol: bars}` 全序列执行五大因子族滚动回测（纯函数，无 DB 依赖）。

    返回可直接写入 `factor_winrate_daily` 的扁平字典列表。
    """
    from src.core.thresholds import bull_line, life_line, strong_line

    life = life_line()
    strong = strong_line()
    bull = bull_line()
    as_of = (as_of_date or _today_compact()).replace("-", "")[:8]
    max_h = max(horizons) if horizons else 5

    # (family, bucket_key, bucket_label, horizon) -> list[(close_ret, max_gain)]
    buckets: dict[tuple[str, str, str, int], list[tuple[float, float]]] = defaultdict(list)
    skipped_map: dict[tuple[str, str, str, int], int] = defaultdict(int)
    base_outcomes: dict[int, list[tuple[float, float]]] = defaultdict(list)
    base_skipped: dict[int, int] = defaultdict(int)

    valid_symbols = 0
    max_bars_seen = 0

    def add_hit(family: str, bkey: str, blabel: str, bars: list[dict[str, float]], idx: int) -> None:
        untradable = _is_untradable_next_bar(bars[idx]["close"], bars[idx + 1])
        for h in horizons:
            key = (family, bkey, blabel, int(h))
            if untradable:
                skipped_map[key] += 1
                continue
            oc = _forward_outcome(bars, idx, int(h))
            if oc is not None:
                buckets[key].append(oc)

    for _sym, raw_bars in (symbol_bars or {}).items():
        bars = [nb for b in (raw_bars or []) if (nb := _norm_bar(b)) is not None]
        n = len(bars)
        if n < MIN_WARMUP_BARS + max_h + 1:
            continue
        valid_symbols += 1
        if n > max_bars_seen:
            max_bars_seen = n

        closes = [b["close"] for b in bars]
        highs = [b["high"] for b in bars]
        lows = [b["low"] for b in bars]
        opens = [b["open"] for b in bars]
        vols = [b["volume"] for b in bars]

        # 预计算 A0, BB0, AI 机构活跃度, 涨跌幅
        a0: list[float] = []
        bb0: list[float | None] = []
        acts: list[float | None] = [None]
        pcts: list[float | None] = [None]
        for i in range(n):
            o, h, l, c = opens[i], highs[i], lows[i], closes[i]
            a0.append((h + l + 2.0 * o + 6.0 * c) / 10.0)
            mas = [_sma_at(closes, k, i) for k in (3, 7, 13, 27)]
            bb0.append(None if any(m is None for m in mas) else sum(m for m in mas if m is not None) / 4.0)
            if i > 0:
                pc = closes[i - 1]
                pct = (c - pc) / pc * 100.0 if pc > 0 else None
                pcts.append(pct)
                if pc > 0 and o > 0 and l > 0:
                    up_sh = (h - max(c, o)) / max(c, o) * 100.0
                    lo_sh = (min(c, o) - l) / l * 100.0
                    body = (c - o) / o * 100.0
                    rise = (c - pc) / pc * 100.0
                    gap = (o - pc) / pc * 100.0
                    acts.append(
                        max(
                            up_sh,
                            lo_sh,
                            body + up_sh,
                            body + lo_sh,
                            up_sh + lo_sh,
                            rise,
                            gap,
                        )
                        * 1.2
                    )
                else:
                    acts.append(None)

        # 连板计数（收盘涨停：涨幅 >= 9.5% 且最高==收盘）
        streak = 0
        streaks: list[int] = [0] * n
        for i in range(1, n):
            p = pcts[i]
            if p is not None and p >= 9.5 and abs(highs[i] - closes[i]) / closes[i] <= 0.002:
                streak += 1
            else:
                streak = 0
            streaks[i] = streak

        for i in range(MIN_WARMUP_BARS, n - max_h):
            if bb0[i] is None or bb0[i - 1] is None:
                continue
            cur_dif = a0[i] - bb0[i]  # type: ignore[operator]
            prev_dif = a0[i - 1] - bb0[i - 1]  # type: ignore[operator]
            if cur_dif == 0:
                continue

            # 0) 无条件基准
            untrad_base = _is_untradable_next_bar(closes[i], bars[i + 1])
            for h in horizons:
                if untrad_base:
                    base_skipped[int(h)] += 1
                else:
                    oc = _forward_outcome(bars, i, int(h))
                    if oc is not None:
                        base_outcomes[int(h)].append(oc)

            # 1) GS 状态与信号（原始 vs 次日确认）
            is_g_zone = cur_dif > 0
            if is_g_zone:
                add_hit("gs_state", "g_zone", "G区间(趋势多头)", bars, i)
            else:
                add_hit("gs_state", "s_zone", "S区间(趋势空头)", bars, i)

            is_g_cross_raw = prev_dif <= 0 < cur_dif
            is_s_cross_raw = prev_dif >= 0 > cur_dif
            if is_g_cross_raw:
                add_hit("gs_state", "g_cross_raw", "G信号(原始上穿当日)", bars, i)
            if is_s_cross_raw:
                add_hit("gs_state", "s_cross_raw", "S信号(原始下穿当日)", bars, i)

            # 次日确认：i-1 日发生上穿/下穿，且 i 日收盘仍维持同侧（锚点在确认日 i，零未来函数）
            if i >= 2 and bb0[i - 2] is not None:
                dif_2 = a0[i - 2] - bb0[i - 2]  # type: ignore[operator]
                if dif_2 <= 0 < prev_dif and cur_dif > 0:
                    add_hit("gs_state", "g_cross_confirmed", "G信号(次日收盘确认)", bars, i)
                elif dif_2 >= 0 > prev_dif and cur_dif < 0:
                    add_hit("gs_state", "s_cross_confirmed", "S信号(次日收盘确认)", bars, i)

            # 2) AI 机构活跃度四档 + 3) GS×活跃度组合共振
            act = acts[i]
            if act is not None:
                if act >= bull:
                    add_hit("ai_activity", "bull", "大牛线(≥6.0)", bars, i)
                elif act >= strong:
                    add_hit("ai_activity", "strong", "强势线(3.0~6.0)", bars, i)
                elif act >= life:
                    add_hit("ai_activity", "life", "生命线(1.56~3.0)", bars, i)
                else:
                    add_hit("ai_activity", "weak", "弱势区(<1.56)", bars, i)

                if is_g_cross_raw and act >= strong:
                    add_hit("gs_x_activity", "g_cross_strong", "G信号+强势以上(首次共振)", bars, i)
                if is_g_zone and act >= bull:
                    add_hit("gs_x_activity", "g_zone_bull", "G区间+大牛线(≥6.0)", bars, i)
                elif is_g_zone and act >= strong:
                    add_hit("gs_x_activity", "g_zone_strong", "G区间+强势线(3.0~6.0)", bars, i)
                elif is_g_zone and act < life:
                    add_hit("gs_x_activity", "g_zone_weak", "G区间+弱势(<1.56背离)", bars, i)
                elif (not is_g_zone) and act < life:
                    add_hit("gs_x_activity", "s_zone_weak", "S区间+弱势(<1.56走坏)", bars, i)

            # 4) 威科夫日 K 量价四标
            v_win = [v for v in vols[i - 20 : i] if v > 0]
            avg_v20 = (sum(v_win) / len(v_win)) if len(v_win) >= 10 else None
            pct_i = pcts[i]
            if avg_v20 and avg_v20 > 0 and vols[i] > 0 and pct_i is not None:
                v_ratio = vols[i] / avg_v20
                body_abs = abs(closes[i] - opens[i])
                upper_sh = highs[i] - max(closes[i], opens[i])
                mid_hl = (highs[i] + lows[i]) / 2.0
                # 放量滞涨
                if v_ratio >= 1.8 and (
                    abs(pct_i) < 1.0 or (upper_sh >= 1.5 * max(body_abs, 1e-6) and closes[i] <= mid_hl)
                ):
                    add_hit("wyckoff", "effort_divergence", "放量滞涨(≥1.8倍量滞涨/长上影)", bars, i)
                # 放量突破 20 日高
                prior_h20 = max(highs[i - 20 : i])
                if closes[i] > prior_h20 and v_ratio >= 1.5:
                    add_hit("wyckoff", "markup_break", "放量突破(创20日新高+≥1.5倍量)", bars, i)
                # 连续缩量沉淀
                if (
                    v_ratio <= 0.6
                    and abs(pct_i) < 1.5
                    and pcts[i - 1] is not None
                    and abs(pcts[i - 1]) < 1.5
                    and vols[i - 1] <= 0.6 * avg_v20
                ):
                    add_hit("wyckoff", "dry_up", "缩量沉淀(连续2日≤0.6倍量窄幅)", bars, i)

            prior_l10 = min(lows[i - 10 : i])
            if (
                lows[i] < prior_l10
                and closes[i] >= prior_l10
                and (closes[i] - lows[i]) / lows[i] * 100.0 >= 1.0
            ):
                add_hit("wyckoff", "spring_probe", "弹簧试探(破10日低收回≥1%)", bars, i)

            # 5) 涨停身位与封板质量
            st = streaks[i]
            if st >= 1:
                if st == 1:
                    add_hit("limit_up_quality", "first_board", "首板涨停(1板)", bars, i)
                elif st in (2, 3):
                    add_hit("limit_up_quality", "streak_2_3", "连板接力(2~3板)", bars, i)
                else:
                    add_hit("limit_up_quality", "streak_4p", "高标连板(≥4板)", bars, i)

                amp = (highs[i] - lows[i]) / closes[i - 1] * 100.0 if closes[i - 1] > 0 else 99.0
                if amp <= 3.0:
                    add_hit("limit_up_quality", "sealed_solid", "一字/T字硬板(振幅≤3%)", bars, i)
                elif avg_v20 and vols[i] >= 1.8 * avg_v20:
                    add_hit("limit_up_quality", "turnover_board", "放量换手板(≥1.8倍均量)", bars, i)

    # 先算各 horizon 的无条件基准
    baseline_stats: dict[int, dict[str, Any]] = {}
    for h in horizons:
        ih = int(h)
        baseline_stats[ih] = _agg_outcomes(
            base_outcomes.get(ih, []), base_skipped.get(ih, 0), min_sample=min_sample
        )

    rows: list[dict[str, Any]] = []
    # 把基准自身也存进 baseline 桶，便于单表自包含查询
    for h in horizons:
        ih = int(h)
        bs = baseline_stats[ih]
        rows.append(
            {
                "as_of_date": as_of,
                "factor_family": "baseline",
                "bucket_key": "unconditional",
                "bucket_label": "同池无条件基准(剔除一字涨停不可买)",
                "horizon": ih,
                **bs,
                "baseline_win_rate_pct": bs["win_rate_pct"],
                "baseline_avg_return_pct": bs["avg_return_pct"],
                "excess_win_rate_pp": 0.0 if bs["win_rate_pct"] is not None else None,
                "excess_return_pct": 0.0 if bs["avg_return_pct"] is not None else None,
                "universe_size": valid_symbols,
                "lookback_bars": max_bars_seen,
                "source": SOURCE,
            }
        )

    all_keys = sorted(set(buckets.keys()) | set(skipped_map.keys()))
    for family, bkey, blabel, ih in all_keys:
        bs = baseline_stats.get(ih, {})
        agg = _agg_outcomes(
            buckets.get((family, bkey, blabel, ih), []),
            skipped_map.get((family, bkey, blabel, ih), 0),
            baseline_wr=bs.get("win_rate_pct"),
            baseline_avg=bs.get("avg_return_pct"),
            min_sample=min_sample,
        )
        rows.append(
            {
                "as_of_date": as_of,
                "factor_family": family,
                "bucket_key": bkey,
                "bucket_label": blabel,
                "horizon": ih,
                **agg,
                "universe_size": valid_symbols,
                "lookback_bars": max_bars_seen,
                "source": SOURCE,
            }
        )
    return rows


def upsert_winrate_rows(db: Session, rows: Sequence[dict[str, Any]]) -> int:
    """按 `(as_of_date, factor_family, bucket_key, horizon)` 幂等落库 `factor_winrate_daily`。"""
    if not rows:
        return 0
    from src.core.db_safe import safe_rollback
    from src.db.models import FactorWinrateDaily

    dates = tuple({str(r["as_of_date"]) for r in rows})
    existing: dict[tuple[str, str, str, int], Any] = {}
    try:
        for obj in (
            db.query(FactorWinrateDaily)
            .filter(FactorWinrateDaily.as_of_date.in_(dates))
            .all()
        ):
            existing[(obj.as_of_date, obj.factor_family, obj.bucket_key, int(obj.horizon))] = obj
    except Exception as exc:  # noqa: BLE001
        safe_rollback(db)
        logger.warning("factor_winrate_daily 查询失败(表未迁移?): %s", exc)
        return 0

    written = 0
    for r in rows:
        key = (str(r["as_of_date"]), str(r["factor_family"]), str(r["bucket_key"]), int(r["horizon"]))
        obj = existing.get(key)
        if obj is None:
            obj = FactorWinrateDaily(**r)
            db.add(obj)
            existing[key] = obj
        else:
            for k, v in r.items():
                setattr(obj, k, v)
        written += 1
    try:
        db.commit()
    except Exception as exc:  # noqa: BLE001
        safe_rollback(db)
        logger.warning("factor_winrate_daily 提交失败: %s", exc)
        return 0
    return written


def load_bars_from_pg(db: Session, *, limit_symbols: int = 400, lookback_days: int = 450) -> dict[str, list[dict]]:
    """从 PG `klines` 批量读取前复权日线供回测使用（PG 走 `ANY(:syms)` + 分区裁剪，约 1.5s）。"""
    from datetime import timedelta
    from sqlalchemy import text
    from src.core.db_safe import safe_rollback

    out: dict[str, list[dict]] = defaultdict(list)
    cal_days = max(180, int(lookback_days * 1.55))
    min_ts = datetime.utcnow() - timedelta(days=cal_days)
    try:
        conn = db.get_bind()
        is_pg = getattr(getattr(conn, "dialect", None), "name", "") == "postgresql"
        with conn.connect() as c:
            if is_pg:
                try:
                    c.execute(text("SET statement_timeout = '30000'"))
                except Exception:  # noqa: BLE001
                    pass
            syms: list[str] = []
            try:
                sym_rows = c.execute(
                    text(
                        "SELECT DISTINCT symbol FROM stock_factor_daily "
                        "WHERE trade_date = (SELECT MAX(trade_date) FROM stock_factor_daily) "
                        "ORDER BY symbol LIMIT :n"
                    ),
                    {"n": int(limit_symbols)},
                ).fetchall()
                syms = [str(r[0]).strip() for r in sym_rows if r and r[0]]
            except Exception:  # noqa: BLE001
                syms = []
            if not syms:
                sym_rows = c.execute(
                    text(
                        "SELECT DISTINCT symbol FROM klines "
                        "WHERE period='1d' AND adjust='qfq' AND market='CN' "
                        "AND ts >= :min_ts "
                        "ORDER BY symbol LIMIT :n"
                    ),
                    {"min_ts": min_ts, "n": int(limit_symbols)},
                ).fetchall()
                syms = [str(r[0]).strip() for r in sym_rows if r and r[0]]
            if not syms:
                return {}
            if is_pg:
                bar_rows = c.execute(
                    text(
                        f"SELECT symbol, ts, open, high, low, close, volume FROM klines "
                        f"WHERE period='1d' AND adjust='qfq' AND market='CN' "
                        f"AND symbol = ANY(:syms) "
                        f"AND ts >= now() - interval '{int(cal_days)} days' "
                        f"ORDER BY symbol, ts ASC"
                    ),
                    {"syms": syms},
                ).fetchall()
                for sym, ts, o, h, l, cl, vol in bar_rows:
                    out[str(sym)].append(
                        {
                            "date": str(ts)[:10].replace("-", ""),
                            "open": o,
                            "high": h,
                            "low": l,
                            "close": cl,
                            "volume": vol,
                        }
                    )
            else:
                in_clause = ", ".join(f":s{i}" for i in range(len(syms)))
                params: dict[str, Any] = {"min_ts": min_ts, **{f"s{i}": s for i, s in enumerate(syms)}}
                bar_rows = c.execute(
                    text(
                        f"SELECT symbol, ts, open, high, low, close, volume FROM klines "
                        f"WHERE period='1d' AND adjust='qfq' AND market='CN' "
                        f"AND symbol IN ({in_clause}) "
                        f"ORDER BY symbol, ts ASC"
                    ),
                    params,
                ).fetchall()
                for sym, ts, o, h, l, cl, vol in bar_rows:
                    out[str(sym)].append(
                        {
                            "date": str(ts)[:10].replace("-", ""),
                            "open": o,
                            "high": h,
                            "low": l,
                            "close": cl,
                            "volume": vol,
                        }
                    )
    except Exception as exc:  # noqa: BLE001
        safe_rollback(db)
        logger.warning("backtest_lab 批量读日线失败: %s", exc)
        return {}
    return {k: v[-lookback_days:] for k, v in out.items() if len(v) >= MIN_WARMUP_BARS + 6}


def run_and_persist(
    db: Session,
    *,
    limit_symbols: int = 400,
    lookback_days: int = 450,
    horizons: Sequence[int] = DEFAULT_HORIZONS,
    as_of_date: str | None = None,
) -> dict[str, Any]:
    """从库内拉日线 → 跑五大因子族回测 → 幂等落库 `factor_winrate_daily`。"""
    symbol_bars = load_bars_from_pg(db, limit_symbols=limit_symbols, lookback_days=lookback_days)
    if not symbol_bars:
        return {"ok": False, "written": 0, "reason": "库内无足够前复权日线样本"}
    rows = evaluate_bars_universe(symbol_bars, horizons=horizons, as_of_date=as_of_date)
    written = upsert_winrate_rows(db, rows)
    return {
        "ok": written > 0,
        "written": written,
        "universe_size": len(symbol_bars),
        "as_of_date": rows[0]["as_of_date"] if rows else (as_of_date or _today_compact()),
    }


def load_winrates(
    db: Session,
    *,
    factor_family: str = "all",
    horizon: int | None = None,
) -> dict[str, Any]:
    """从 `factor_winrate_daily` 读取最新一期因子胜率快照（供 API 与数智参谋工具消费）。"""
    from sqlalchemy import func
    from src.core.db_safe import safe_rollback
    from src.db.models import FactorWinrateDaily

    try:
        latest_date = db.query(func.max(FactorWinrateDaily.as_of_date)).scalar()
    except Exception as exc:  # noqa: BLE001
        safe_rollback(db)
        return {"available": False, "reason": f"因子胜率表不可用: {type(exc).__name__}"}

    if not latest_date:
        return {"available": False, "reason": "factor_winrate_daily 尚未生成回测快照"}

    q = db.query(FactorWinrateDaily).filter(FactorWinrateDaily.as_of_date == latest_date)
    fam = (factor_family or "all").strip().lower()
    if fam not in ("", "all", "*"):
        q = q.filter(FactorWinrateDaily.factor_family.in_([fam, "baseline"]))
    if horizon in (1, 3, 5):
        q = q.filter(FactorWinrateDaily.horizon == int(horizon))

    rows = q.order_by(
        FactorWinrateDaily.factor_family.asc(),
        FactorWinrateDaily.bucket_key.asc(),
        FactorWinrateDaily.horizon.asc(),
    ).all()
    if not rows:
        return {"available": False, "reason": f"未找到 factor_family={fam} 的回测记录"}

    items = [
        {
            "factor_family": r.factor_family,
            "bucket_key": r.bucket_key,
            "bucket_label": r.bucket_label,
            "horizon": r.horizon,
            "sample_count": r.sample_count,
            "win_count": r.win_count,
            "untradable_skipped": r.untradable_skipped,
            "win_rate_pct": r.win_rate_pct,
            "hit3_rate_pct": r.hit3_rate_pct,
            "avg_return_pct": r.avg_return_pct,
            "median_return_pct": r.median_return_pct,
            "profit_loss_ratio": r.profit_loss_ratio,
            "baseline_win_rate_pct": r.baseline_win_rate_pct,
            "baseline_avg_return_pct": r.baseline_avg_return_pct,
            "excess_win_rate_pp": r.excess_win_rate_pp,
            "excess_return_pct": r.excess_return_pct,
        }
        for r in rows
    ]
    return {
        "available": True,
        "as_of_date": latest_date,
        "universe_size": rows[0].universe_size,
        "lookback_bars": rows[0].lookback_bars,
        "source": rows[0].source,
        "items": items,
    }


def render(data: dict[str, Any]) -> str:
    """将 `load_winrates` 的输出渲染成数智参谋可读的证据文本。"""
    if not data.get("available"):
        return f"[因子胜率回测库] {data.get('reason', '暂无数据')}——不做主观胜率推测。"
    items = data.get("items") or []
    lines = [
        f"[数据源: factor_winrate_daily · 快照日 {data.get('as_of_date')} · "
        f"样本池 {data.get('universe_size')} 只 × {data.get('lookback_bars')} 根前复权日线 · "
        f"已剔除 T+1 一字涨停不可买样本]"
    ]
    by_fam: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for it in items:
        by_fam[it["factor_family"]].append(it)

    if "baseline" in by_fam:
        b_parts = []
        for b in by_fam["baseline"]:
            if b.get("win_rate_pct") is not None:
                b_parts.append(
                    f"T+{b['horizon']} 胜率 {b['win_rate_pct']:.2f}% / 均值 {b['avg_return_pct']:+.3f}% "
                    f"(3%达标 {b['hit3_rate_pct']:.1f}%, n={b['sample_count']})"
                )
        if b_parts:
            lines.append("【同池无条件基准】" + " ｜ ".join(b_parts))

    for fam, rows in by_fam.items():
        if fam == "baseline":
            continue
        title = FAMILY_LABELS.get(fam, fam)
        lines.append(f"\n### {title} ({fam})")
        for r in rows:
            n = r["sample_count"]
            h = r["horizon"]
            lbl = r["bucket_label"]
            if r["win_rate_pct"] is None:
                lines.append(f"- {lbl} [T+{h}]：样本不足（n={n} < {MIN_SAMPLE}，不输出胜率）")
                continue
            ex_wr = r.get("excess_win_rate_pp")
            ex_ret = r.get("excess_return_pct")
            pl = r.get("profit_loss_ratio")
            ex_txt = (
                f"，超额胜率 {ex_wr:+.2f}pp / 超额收益 {ex_ret:+.3f}%"
                if (ex_wr is not None and ex_ret is not None)
                else ""
            )
            pl_txt = f"，盈亏比 {pl:.2f}" if pl is not None else ""
            lines.append(
                f"- {lbl} [T+{h}]：胜率 {r['win_rate_pct']:.2f}%｜3%达标 {r['hit3_rate_pct']:.1f}%｜"
                f"均值 {r['avg_return_pct']:+.3f}%（中位 {r['median_return_pct']:+.3f}%）"
                f"{pl_txt}{ex_txt}（n={n}，一字剔除={r['untradable_skipped']}）"
            )
    lines.append(
        "口径说明：胜率=收盘对收盘收益>0占比；3%达标=期内最高涨幅≥3%占比（同花顺官方口径）；"
        "历史统计频率不代表未来单次必然重复，仅作条件化赔率证据。"
    )
    return "\n".join(lines)


def run_scheduled() -> dict[str, Any]:
    """定时任务入口（交易日 16:15）：更新 `factor_winrate_daily`。"""
    from src.db.session import SessionLocal

    db = SessionLocal()
    try:
        return run_and_persist(db, limit_symbols=500, lookback_days=450)
    except Exception as exc:  # noqa: BLE001
        logger.error("factor_winrate_daily 定时任务失败: %s", exc)
        return {"ok": False, "reason": str(exc)[:200]}
    finally:
        db.close()
