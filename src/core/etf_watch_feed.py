"""ETF 看板只读数据面（2026-11-07）—— `/api/etf-watch/*` 与数智参谋 `etf.flow` 共用。

为什么单独抽一层：页面和参谋如果各写一套 SQL，同一个「20 只篮子成交额」会算出两个数。
本模块是唯一的取数/聚合实现，Web API 只做 HTTP 收尾，`query_data_domain` 只做渲染。

纪律（与 `src/web/api/etf_watch.py` 原有口径逐条一致，改动必须同步这里）：
1. 只读：不 INSERT/UPDATE，不调采集器，不联网。
2. 数据缺失一律 `None` + note，不写 0；分钟覆盖不足 80% 判 `insufficient`。
3. 净申购只有市场级 SC8 是真的；20 只篮子的净申购**不存在**，不能拆。
4. 单位：库内 `amount_wan` 是万元，出口 ÷10000 转亿元。

调用方传入 `run(sql, params) -> list[dict]`，因此本模块不依赖 Web 层也不绑定 engine。
"""

from __future__ import annotations

import logging
import math
from datetime import date, datetime
from typing import Any, Callable

from src.collectors import tq_etf_watch as ew

logger = logging.getLogger(__name__)

Runner = Callable[..., list[dict[str, Any]]]
#: `run(key, params, fmt)` —— key 对应 `SQL`，fmt 用于 IN 占位符展开。
#: 调用方可以用 `_read(db, key, params)`（键已登记在 tools_catalog._SQL）或自行执行 SQL。

#: 覆盖率闸门：不足 80% 的交易日判「数据不全」，不参与活跃度结论。
COVERAGE_MIN = 0.80

#: 20 日基线窗口（交易日，不含当日）。
BASELINE_DAYS = 20

#: 单日分钟上限（含 11:30 / 13:00 边界与盘后残留行；SQL 的 LIMIT 按它取上界）。
MAX_MINUTES_PER_DAY = 1440


def min_etfs_for_complete(expected: int | None = None) -> int:
    """一个分钟「可用」的标的数门槛：**≥80%，不是全部**（20 只篮子 → 16 只）。

    以前出口文案把过闸门的分钟说成「20 只齐全」，把 16 只也说成 20 只，
    属于把覆盖口径说满；现在门槛与文案都从本函数取，只有一个真值。
    """
    n = len(ew.ETF_BASKET) if expected is None else int(expected)
    return max(1, math.ceil(n * COVERAGE_MIN - 1e-9))


#: 单域只读语句（全部带 LIMIT；`tools_catalog._SQL` 直接复用这批字符串）。
SQL: dict[str, str] = {
    "etf_minute_rows": (
        "SELECT minute, SUM(amount_wan) AS amount_wan, COUNT(DISTINCT etf_code) AS etfs "
        "FROM etf_minute_flow WHERE trade_date = :d "
        "GROUP BY minute ORDER BY minute LIMIT 1440"
    ),
    # 回退基准必须**早于目标日**：表里若有未来日期的行，MAX() 会把未来日当「最近交易日」。
    "etf_latest_date": (
        "SELECT MAX(trade_date) AS d FROM etf_minute_flow "
        "WHERE trade_date < :d LIMIT 1"
    ),
    "etf_baseline_dates": (
        "SELECT DISTINCT trade_date FROM etf_minute_flow "
        "WHERE trade_date < :d ORDER BY trade_date DESC LIMIT :n"
    ),
    "etf_baseline_rows": (
        "SELECT minute, trade_date, SUM(amount_wan) AS amount_wan, "
        "COUNT(DISTINCT etf_code) AS etfs FROM etf_minute_flow "
        # 上界 = 窗口交易日 × 单日分钟上限（20 × 1440 = 28800）。写死 6000 会把
        # 20 个完整交易日的分组截断，基线被静默算低 —— 原实现没有 LIMIT，这里只是护栏。
        "WHERE trade_date IN ({ph}) GROUP BY minute, trade_date LIMIT 30000"
    ),
    "etf_support": (
        "SELECT trade_date, scale_yi AS etf_scale, net_sub_yi AS etf_net_sub "
        "FROM etf_flow_daily WHERE net_sub_yi IS NOT NULL "
        "ORDER BY trade_date DESC LIMIT :n"
    ),
    "etf_daily_dates": (
        "SELECT DISTINCT trade_date FROM etf_minute_flow "
        "ORDER BY trade_date DESC LIMIT :n"
    ),
    "etf_daily_minute": (
        "SELECT trade_date, etf_code, SUM(amount_wan) AS amount_wan "
        "FROM etf_minute_flow WHERE trade_date IN ({ph}) "
        "GROUP BY trade_date, etf_code LIMIT 4000"
    ),
    "etf_daily_net": (
        "SELECT trade_date, scale_yi AS etf_scale, net_sub_yi AS etf_net_sub "
        "FROM etf_flow_daily WHERE trade_date IN ({ph}) LIMIT 400"
    ),
}


# ── 工具 ─────────────────────────────────────────────────────────────────

def round_or_none(v: Any, n: int = 2) -> float | None:
    return None if v is None else round(float(v), n)


def in_clause(prefix: str, values: list[str]) -> tuple[str, dict[str, str]]:
    """跨方言 IN 占位符（PG 的 `= ANY(:list)` 在 SQLite 上不成立）。"""
    params = {f"{prefix}{i}": v for i, v in enumerate(values)}
    ph = ", ".join(f":{k}" for k in params)
    return ph, params


def phase_of(d: date, now: datetime) -> str:
    """pre / live / closed / non_trading_day（`now` 由调用方给，便于测试注入）。"""
    from src.core.trading_calendar import is_trading_day

    if d.weekday() >= 5 or not is_trading_day(d):
        return "non_trading_day"
    hm = now.strftime("%H%M")
    today = now.date()
    if d != today:
        return "closed" if d < today else "pre"
    if "0930" <= hm <= "1130" or "1300" <= hm <= "1500":
        return "live"
    if hm < "0930":
        return "pre"
    return "closed"


# ── basket（无库依赖） ───────────────────────────────────────────────────

def basket_payload() -> dict[str, Any]:
    """20 只重点 ETF + 分类 + 单只份额可用性声明。"""
    supported = ew.single_share_source is not None
    return {
        "items": ew.basket_payload(),
        "categories": ew.categories(),
        "benchmark": {"code": ew.BENCHMARK_INDEX, "name": ew.BENCHMARK_NAME},
        "source": "tdx_tq",
        "single_share_supported": supported,
        "single_share_note": (
            "单只 ETF 净申购不可用：通达信 ETF 份额为当前值回填（实测 63 个交易日零变化），"
            "净申购一律采用市场级 SC8 口径。"
            if not supported
            else f"单只份额来源：{ew.single_share_source}"
        ),
    }


# ── intraday ─────────────────────────────────────────────────────────────

def baseline_curve(run: Runner, dates: list[str]) -> dict[str, Any]:
    """过去 N 日「同一分钟」的累计成交额均值 + **真实样本数**。

    返回 `cum`（分钟 → 均值亿元）、`days`（分钟 → 实际参与该分钟的交易日数）、
    `days_used`（有合规分钟的交易日数）、`days_requested`、`gate_etfs`。

    为什么要把样本数一起带出来：原来只回 `cum`，出口就拿「请求了 20 日」当「有 20 日基线」，
    窗口里只有 2 天也会写「过去 20 个交易日」。分母缺失（缺失日是未采集还是非交易日）
    无法从分钟表反推，所以只报**能算出的实际样本数**，剩余标「未确认」。
    """
    out: dict[str, Any] = {
        "cum": {},
        "days": {},
        "days_used": 0,
        "days_requested": len(dates),
        "gate_etfs": min_etfs_for_complete(),
    }
    if not dates:
        return out
    ph, in_params = in_clause("d", dates)
    rows = run("etf_baseline_rows", in_params, {"ph": ph})
    gate = int(out["gate_etfs"])
    per_day: dict[str, dict[str, float]] = {}
    for r in rows:
        if int(r["etfs"] or 0) < gate:
            continue
        per_day.setdefault(r["trade_date"], {})[r["minute"]] = float(r["amount_wan"] or 0.0)
    out["days_used"] = len(per_day)
    if not per_day:
        return out

    acc: dict[str, float] = {}
    cnt: dict[str, int] = {}
    for mins in per_day.values():
        cum = 0.0
        for m in sorted(mins):
            cum += mins[m]
            acc[m] = acc.get(m, 0.0) + cum
            cnt[m] = cnt.get(m, 0) + 1
    out["cum"] = {m: (acc[m] / cnt[m]) / 1e4 for m in acc if cnt[m] > 0}
    out["days"] = {m: cnt[m] for m in cnt if cnt[m] > 0}
    return out


def baseline_cum(run: Runner, dates: list[str]) -> dict[str, float]:
    """`baseline_curve` 的均值部分（保留旧签名，调用方只关心曲线时用）。"""
    return baseline_curve(run, dates)["cum"]


def intraday_payload(
    run: Runner,
    date_str: str | None,
    *,
    now: datetime,
    index_points: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """盘中分钟观测：20 只 ETF 合计成交额 + 近 N 个交易日同期均值（+ 可选指数旁证）。

    `N` 只报实际可用样本（`totals.baseline_days`），窗口请求数是 20；不足时不写「过去 20 日」。
    `phase` 是**数据日**的相位，`session_phase` 才是当前时段 —— 回退到历史日时两者不同。
    """
    target = date_str or now.strftime("%Y%m%d")
    try:
        d_obj = datetime.strptime(target, "%Y%m%d").date()
    except ValueError:
        return {"error": "date 需为 YYYYMMDD", "date": target}

    expected = len(ew.ETF_BASKET)
    gate = min_etfs_for_complete(expected)
    session_phase = phase_of(now.date(), now)
    mins = run("etf_minute_rows", {"d": target})
    # 当日还没有分钟数据时，回退到最近一个有数据的交易日并**显式标注**。
    # 盘中/收盘后通达信当日分钟线未就绪（实测 15:21 仍取不到），不回退页面会长时间空白；
    # 但不能把旧数据伪装成今天 —— `fallback_from` 必须一起返回。
    fallback_from: str | None = None
    if not mins and date_str is None:
        # 只取 < 今日的日期：表里若有未来行，MAX() 会把未来日当成「最近的交易日」。
        latest_rows = run("etf_latest_date", {"d": target})
        latest = latest_rows[0]["d"] if latest_rows else None
        if latest and str(latest) < target:
            mins = run("etf_minute_rows", {"d": latest})
            fallback_from = target
            target = latest
            try:
                d_obj = datetime.strptime(target, "%Y%m%d").date()
            except ValueError:
                pass
    if not mins:
        return {
            "date": target,
            "phase": phase_of(d_obj, now),
            "session_phase": session_phase,
            "session_time": now.strftime("%H:%M"),
            "state": "no_data",
            "fallback_from": fallback_from,
            "stale": fallback_from is not None,
            "note": "当日无分钟数据（未采集或非交易日）；盘中采集每分钟一次，收盘后 15:35 补采。",
            "series": [],
            "totals": None,
            "source": "tdx_tq",
        }

    base_dates = [
        r["trade_date"]
        for r in run("etf_baseline_dates", {"d": target, "n": BASELINE_DAYS})
    ]
    base = baseline_curve(run, base_dates)
    baseline: dict[str, float] = base["cum"]

    series: list[dict[str, Any]] = []
    cum = 0.0
    full_minutes = 0
    strict_minutes = 0
    for r in mins:
        amt_yi = float(r["amount_wan"] or 0.0) / 1e4
        cum += amt_yi
        got = int(r["etfs"] or 0)
        complete = got >= gate
        if complete:
            full_minutes += 1
        if got >= expected:
            strict_minutes += 1
        series.append({
            "minute": r["minute"],
            "amount_yi": round_or_none(amt_yi, 3),
            "cum_yi": round_or_none(cum, 2),
            "baseline_cum_yi": round_or_none(baseline.get(r["minute"]), 2),
            "complete": complete,
        })

    last_minute = series[-1]["minute"] if series else None
    base_at = baseline.get(last_minute) if last_minute else None
    ratio = (cum / base_at) if (base_at and base_at > 0) else None

    coverage = (full_minutes / len(series)) if series else None
    if not series or not coverage or coverage < COVERAGE_MIN:
        state = "insufficient"
    elif ratio is None:
        state = "no_baseline"
    elif ratio >= 1.5:
        state = "surge"
    elif ratio >= 1.15:
        state = "active"
    elif ratio <= 0.7:
        state = "quiet"
    else:
        state = "normal"

    points = index_points or []
    days_requested = BASELINE_DAYS
    days_available = len(base_dates)          # 窗口内真的有行情的交易日
    days_used = int(base["days_used"])        # 其中至少有 1 个合规分钟、真进均值的天数
    missing_days = max(days_requested - days_available, 0)
    dropped_days = max(days_available - days_used, 0)
    sample_days = int((base["days"] or {}).get(last_minute, 0)) if last_minute else 0
    confirmed = missing_days == 0 and dropped_days == 0

    if confirmed:
        base_note = (
            f"基线取过去 {days_requested} 个交易日同一分钟的累计均值，口径为「同日同期」，不是全天对比"
        )
    else:
        base_note = (
            f"基线仅 {days_used} 个交易日可用（请求 {days_requested} 日；"
            f"窗口内缺 {missing_days} 个交易日样本"
        )
        if dropped_days:
            base_note += f"，另有 {dropped_days} 个交易日无达标分钟被剔除"
        base_note += "；缺的日期是未采集还是非交易日无法从分钟表确认，标未确认）"

    return {
        "date": target,
        "phase": phase_of(d_obj, now),
        "session_phase": session_phase,
        "session_time": now.strftime("%H:%M"),
        "state": state,
        "fallback_from": fallback_from,
        "stale": fallback_from is not None,
        "benchmark": {"code": ew.BENCHMARK_INDEX, "name": ew.BENCHMARK_NAME, "points": points},
        "series": series,
        "totals": {
            "cum_yi": round_or_none(cum, 2),
            "baseline_avg_yi": round_or_none(base_at),
            "ratio_vs_baseline": round_or_none(ratio, 3),
            "baseline_days": days_used,
            "baseline_days_requested": days_requested,
            "baseline_days_in_window": days_available,
            "baseline_missing_days": missing_days,
            "baseline_days_dropped": dropped_days,
            "baseline_sample_days": sample_days,
            "baseline_confirmed": confirmed,
            "last_minute": last_minute,
            "etfs_expected": expected,
            "min_etfs_for_complete": gate,
            "full_minutes": full_minutes,
            "strict_minutes": strict_minutes,
            "minutes": len(series),
            "coverage": round_or_none(coverage, 3),
        },
        "note": (
            f"成交额为 {expected} 只重点宽基 ETF 合计（通达信分钟线，单位亿元）；"
            + base_note
            + "。"
        ),
        "source": "tdx_tq",
    }


# ── support（市场级 SC8） ────────────────────────────────────────────────

def support_payload(run: Runner, days: int) -> dict[str, Any]:
    rows = run("etf_support", {"n": days})
    rows.reverse()
    supported = ew.single_share_source is not None
    if not rows:
        return {
            "state": "no_data",
            "note": "市场级 ETF 规模/净申购（SC8）尚无落库数据；采集器回补后可用。",
            "single_share_supported": supported,
            "source": "tdx_tq",
        }

    series = [
        {
            "date": r["trade_date"],
            "scale_yi": round_or_none(r["etf_scale"]),
            "net_sub_yi": round_or_none(r["etf_net_sub"]),
        }
        for r in rows
    ]
    support = [x for x in series if (x["net_sub_yi"] or 0) >= ew.SUPPORT_FLOW_THRESHOLD_YI]
    values = sorted(x["net_sub_yi"] for x in series if x["net_sub_yi"] is not None)

    latest = series[-1]
    latest_val = latest.get("net_sub_yi")
    pct = None
    if latest_val is not None and support:
        below = sum(1 for x in support if (x["net_sub_yi"] or 0) < latest_val)
        pct = round(below / len(support) * 100, 1)

    return {
        "state": "ok",
        "threshold_yi": ew.SUPPORT_FLOW_THRESHOLD_YI,
        "series": series,
        "support_days": support[-80:],
        "support_count": len(support),
        "sample_count": len(series),
        "latest": latest,
        "latest_percentile_in_support_days": pct,
        "max_net_sub_yi": values[-1] if values else None,
        "min_net_sub_yi": values[0] if values else None,
        "single_share_supported": supported,
        "single_share_note": (
            "单只 ETF 净申购不可用（通达信 ETF 份额为当前值回填，实测零变化）；"
            "此处为全市场 ETF 口径（SC8），不能拆到单只。"
            if not supported
            else f"单只份额来源：{ew.single_share_source}"
        ),
        "note": (
            "净申购 = 当日 ETF 份额变动 × 净值（通达信 SC8 市场级口径，亿元）。"
            "单日净申购 ≥%g 亿元视为托底日样本。" % ew.SUPPORT_FLOW_THRESHOLD_YI
        ),
        "source": "tdx_tq",
    }


# ── daily（日度资金全景） ────────────────────────────────────────────────

def daily_payload(run: Runner, days: int) -> dict[str, Any]:
    dates = sorted(
        r["trade_date"] for r in run("etf_daily_dates", {"n": days})
    )
    if not dates:
        return {"state": "no_data", "note": "尚无分钟数据落库。", "source": "tdx_tq"}

    ph, in_params = in_clause("d", dates)
    rows = run("etf_daily_minute", in_params, {"ph": ph})
    cat_of = {c: cat for c, _n, cat in ew.ETF_BASKET}
    by_cat: dict[str, dict[str, float]] = {cat: {} for cat in ew.categories()}
    total: dict[str, float] = {}
    for r in rows:
        cat = cat_of.get(r["etf_code"])
        if not cat:
            continue
        yi = float(r["amount_wan"] or 0.0) / 1e4
        by_cat[cat][r["trade_date"]] = by_cat[cat].get(r["trade_date"], 0.0) + yi
        total[r["trade_date"]] = total.get(r["trade_date"], 0.0) + yi

    net = {
        r["trade_date"]: r
        for r in run("etf_daily_net", in_params, {"ph": ph})
    }

    return {
        "state": "ok",
        "dates": dates,
        "total_amount_yi": [round_or_none(total.get(d)) for d in dates],
        "net_sub_yi": [
            round_or_none(net[d]["etf_net_sub"]) if d in net else None for d in dates
        ],
        "scale_yi": [
            round_or_none(net[d]["etf_scale"]) if d in net else None for d in dates
        ],
        "by_category": [
            {"category": cat, "amount_yi": [round_or_none(m.get(d)) for d in dates]}
            for cat, m in by_cat.items()
            if m
        ],
        "single_share_supported": ew.single_share_source is not None,
        "note": "成交额为 20 只 ETF 合计（亿元）；净申购为市场级 SC8 口径，只到全市场一层。",
        "source": "tdx_tq",
    }


__all__ = [
    "BASELINE_DAYS",
    "COVERAGE_MIN",
    "MAX_MINUTES_PER_DAY",
    "SQL",
    "basket_payload",
    "baseline_cum",
    "baseline_curve",
    "daily_payload",
    "in_clause",
    "intraday_payload",
    "min_etfs_for_complete",
    "phase_of",
    "round_or_none",
    "support_payload",
]
