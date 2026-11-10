"""ETF 看板只读数据面（2026-11-07）—— `/api/etf-watch/*` 与数智参谋 `etf.flow` 共用。

为什么单独抽一层：页面和参谋如果各写一套 SQL，同一个「20 只篮子成交额」会算出两个数。
本模块是唯一的取数/聚合实现，Web API 只做 HTTP 收尾，`query_data_domain` 只做渲染。

纪律（与 `src/web/api/etf_watch.py` 原有口径逐条一致，改动必须同步这里）：
1. 只读：不 INSERT/UPDATE，不调采集器，不联网。
2. 数据缺失一律 `None` + note，不写 0；分钟覆盖不足 80% 判 `insufficient`。
3. 份额变动只有市场级 SC8 有（且单位是推断）；20 只篮子的净申购**不存在**，不能拆。
4. 单位：库内 `amount_wan` 是万元，出口 ÷10000 转亿元。

来源纪律（2026-11-10 读侧安全修复）：
- `etf_minute_flow.source`（迁移 245）是**每分钟行**的真实写入来源：TQ 主源 `tdx_tq`、
  东财 trends2 兜底 `em_trends2`。读侧必须按该列回报实际来源，**不许**在出口写死
  「tdx_tq」：兜底日、混源日、或 source 为 NULL 的历史行（来源未知）说成 TQ 就是虚构。
- 有分钟行但 source 为 NULL → 出口 `unknown`；同一份数据出现多种来源 → `mixed` 并逐日列出。
  基线窗口若含非 TQ / 未标注来源，必须显式提示「与当前数据可能不同源，比值只作参考」。

SC8 单位与新鲜度（2026-11-10 核查；**单位是推断，不是已确认事实**）：
- `get_scjy_value(SC8)` 两列**更可能是份额（亿份）**而非金额：2026-10-16 上交所 924 只
  （TOT_VOL 万份）+ 深交所 761 只（份）合计 32863.16 亿份，SC8 列0 = 32886.32（差 -0.07%）；
  列1 ≈ Δ列0（914 天中 557 天精确相等）。
- 但反证同样存在：同口径「Σ份额 × 净值」= 46548 亿元，是列0 的 1.42 倍；且没有交易所/
  通达信官方字段定义可引用。所以**只能标为推断**（`unit_confidence="inferred"`），
  出口措辞一律「推断为份额」，不得写成「已确认 / 钉死」，也不得据此换算金额。
- 因此**不提供任何「托底日」判定**：`SUPPORT_FLOW_THRESHOLD_YI`（100，采集器里是
  「单日净流入 100 **亿元**」的金额阈值）与份额单位不可换算，拿它当「100 亿份」筛样本
  是无依据的推断，已从读侧移除（含旧兼容字段 `threshold_yi`）。
- TQ SC8 全历史止于 2026-10-16（915 行，上游市场统计序列停更）。读源直接取
  `series[-1]` 当最新会让 10-16 长期冒充今天，故 `support_payload` 必须按交易日历算滞后并
  给出 `stale/as_of`；前端与参谋一律带真实日期与滞后提示，**不改库、不改历史行**。

调用方传入 `run(sql, params) -> list[dict]`，因此本模块不依赖 Web 层也不绑定 engine。
"""

from __future__ import annotations

import logging
import math
from datetime import date, datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo

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

#: SC8 两列的单位：**推断为份额（亿份）**，不是金额。落库列名 `scale_yi` / `net_sub_yi` 保留
#: （历史表与 API 契约），但出口必须带这个显式单位，禁止再渲染成「亿元」。
#: 证据只能支持「更可能是份额」，无法唯一确认 —— 见 `SC8_UNIT_CAVEAT` 与 `unit_confidence`。
SC8_UNIT = "亿份"

#: 单位置信度：`inferred` = 推断（不是已确认）。出口与测试都照这个值断言。
SC8_UNIT_CONFIDENCE = "inferred"

#: 单位推断的证据与反证（原样外发，别只留结论）：正证是份额合计对拍 -0.07%，
#: 反证是「Σ份额×净值」为列0 的 1.42 倍。两条都在，所以只能叫推断。
SC8_UNIT_CAVEAT = (
    "推断依据：2026-10-16 沪深交易所份额合计 32863.16 亿份 vs SC8 列0 32886.32（差 -0.07%），"
    "且列1≈Δ列0；反证：同口径 Σ份额×净值 = 46548 亿元，是列0 的 1.42 倍，"
    "且无交易所/通达信官方字段定义可引用 —— 故单位为推断，不是已确认事实。"
)

#: `etf_minute_flow.source`：主源与未知。兜底源标识见 `src/collectors/em_etf_minute.py`。
SOURCE_TQ = "tdx_tq"
SOURCE_EM_TRENDS2 = "em_trends2"
#: source 为 NULL（迁移前手工构造行/未标注）→ 未知。**不许**当成 tdx_tq。
SOURCE_UNKNOWN = "unknown"
SOURCE_LABELS = {
    SOURCE_TQ: "通达信 TQ",
    SOURCE_EM_TRENDS2: "东财 trends2",
    SOURCE_UNKNOWN: "来源未知",
}

#: SC8 新鲜度容差（交易日）。T+1 序列在交易日当天的最新日 = 上一交易日（滞后 0），
#: 早间采集窗口内允许滞后 1；再多就是上游停更/采集失败，必须标 stale。
STALE_MAX_LAG_TRADING_DAYS = 1


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
        # etfs 只数**有金额**的标的：分钟行存在但 amount_wan 为 NULL 时，它既不能算
        # 成交额（补 0 就是造数），也不能算覆盖（那一分钟的合计是残缺的）。
        "SELECT minute, SUM(amount_wan) AS amount_wan, "
        "COUNT(DISTINCT CASE WHEN amount_wan IS NOT NULL THEN etf_code END) AS etfs "
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
        "COUNT(DISTINCT CASE WHEN amount_wan IS NOT NULL THEN etf_code END) AS etfs "
        "FROM etf_minute_flow "
        # 上界 = 窗口交易日 × 单日分钟上限（20 × 1440 = 28800）。写死 6000 会把
        # 20 个完整交易日的分组截断，基线被静默算低 —— 原实现没有 LIMIT，这里只是护栏。
        "WHERE trade_date IN ({ph}) GROUP BY minute, trade_date LIMIT 30000"
    ),
    "etf_support": (
        "SELECT trade_date, scale_yi AS etf_scale, net_sub_yi AS etf_net_sub "
        "FROM etf_flow_daily WHERE net_sub_yi IS NOT NULL AND trade_date <= :as_of "
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
    #: 每日真实写入来源（迁移 245 的 `source` 列）。**不聚合掉 NULL**：source 为 NULL 的
    #: 行会以 source=NULL 分组回来，读侧据此报「未知」，而不是默认成 TQ。
    "etf_minute_sources": (
        "SELECT trade_date, source, COUNT(*) AS n FROM etf_minute_flow "
        "WHERE trade_date IN ({ph}) GROUP BY trade_date, source LIMIT 400"
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


def _source_word(raw: str) -> str:
    """来源标识 → 人话；未登记标识按原值显示（不猜、不映射成 TQ）。"""
    return SOURCE_LABELS.get(raw, raw)


def summarize_minute_sources(
    rows: list[dict[str, Any]],
    *,
    has_data: bool,
) -> dict[str, Any]:
    """分钟表真实来源汇总：单源 / 混源 / 未知，逐日展开，绝不再写死 `tdx_tq`。

    `rows` 来自 `SQL["etf_minute_sources"]`（trade_date, source, n）。source 为 NULL
    的行按 `unknown` 计 —— 迁移 245 之前的存量行虽被回填为 `tdx_tq`，但手工构造/未标注
    的行仍可能是 NULL，读侧只能照实说「未知」，不能替它认领来源。
    """
    by_date: dict[str, list[str]] = {}
    for r in rows or []:
        day = str(r.get("trade_date") or "")
        raw = r.get("source")
        raw = str(raw) if raw not in (None, "") else SOURCE_UNKNOWN
        bucket = by_date.setdefault(day, [])
        if raw not in bucket:
            bucket.append(raw)

    raw_all = sorted({s for v in by_date.values() for s in v})
    if not has_data:
        state = "no_data"
    elif not raw_all or raw_all == [SOURCE_UNKNOWN]:
        state = "unknown"
    elif len(raw_all) == 1:
        state = raw_all[0]
    else:
        state = "mixed"

    labels = [_source_word(s) for s in raw_all]
    # 逐日来源固定按标识排序（同日多条时 SQL 的 GROUP BY 顺序不保证），出口才可复现。
    by_date_labels = {d: [_source_word(s) for s in sorted(v)] for d, v in sorted(by_date.items())}

    if state == "no_data":
        note = "无分钟行，来源无从判断"
    elif state == "unknown":
        note = (
            "分钟表 source 为 NULL 或来源列未取到：该数据来源未知（unknown），"
            "不默认当成通达信 TQ"
        )
    elif state == "mixed":
        parts = []
        for d, vs in by_date_labels.items():
            parts.append(f"{d}={'/'.join(vs)}")
        note = (
            "⚠️ 分钟表来源混合（" + "；".join(parts) + "）：混源数据不能当成单一来源口径，"
            "与基线比较只作参考"
        )
    elif state == SOURCE_EM_TRENDS2:
        note = "该数据由东财 trends2 兜底写入（em_trends2），不是 TQ 原生分钟线"
    elif state == SOURCE_TQ:
        note = "分钟表来源为通达信 TQ（tdx_tq），单一来源"
    else:
        note = f"分钟表来源为未登记标识 {state}（按原值展示，不映射成 TQ）"

    return {
        "state": state,
        "sources": raw_all,
        "labels": labels,
        "by_date": by_date_labels,
        "note": note,
    }


def minute_sources(run: Runner, dates: list[str]) -> dict[str, Any]:
    """按交易日集合查真实来源；调用方 dates 为空时明确 `no_data`。

    来源列读不到（老库缺列/单表不可读）时按 **unknown** 出口，不猜成 TQ：来源只是
    解释性元数据，不该让整张看板 500，但也不能因此编一个来源出来。
    """
    if not dates:
        return summarize_minute_sources([], has_data=False)
    ph, in_params = in_clause("s", dates)
    try:
        rows = run("etf_minute_sources", in_params, {"ph": ph})
    except Exception as exc:  # noqa: BLE001 — 来源是旁证, 读不到只能标未知
        # 只记异常类型：驱动异常的 str() 可能带 DSN，来源本身也不是结论依据。
        logger.warning("[etf-feed] 分钟来源列读取失败，按未知处理: %s", type(exc).__name__)
        out = summarize_minute_sources([], has_data=True)
        out["note"] = "分钟来源列读取失败，来源未知（unknown），不默认当成通达信 TQ"
        return out
    return summarize_minute_sources(rows, has_data=True)


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
        "source": "config",
        # 篮子名单是代码里的静态配置，不是从分钟表读出的数据源；分钟数据的真实来源
        # 由 intraday/daily 按 `etf_minute_flow.source` 回报（tdx_tq / em_trends2 / unknown）。
        "source_note": "20 只名单与分类来自静态配置；分钟数据来源见 intraday/daily 的 source 字段。",
        "single_share_supported": supported,
        "single_share_note": (
            "单只 ETF 净申购不可用：通达信 ETF 历史份额尚未通过独立数据验证，"
            "此处只展示市场级 SC8 口径。"
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
            # 没有行就没有来源；不写 tdx_tq（那是虚构）。
            "source": "no_data",
            "sources": [],
            "source_labels": [],
            "source_by_date": {},
            "source_note": "无分钟行，来源无从判断",
        }

    # 真实来源：按 `etf_minute_flow.source` 逐日查，单源/混源/未知都在出口照实说。
    src = minute_sources(run, [target])

    base_dates = [
        r["trade_date"]
        for r in run("etf_baseline_dates", {"d": target, "n": BASELINE_DAYS})
    ]
    base = baseline_curve(run, base_dates)
    baseline: dict[str, float] = base["cum"]
    base_src = minute_sources(run, [str(d) for d in base_dates])
    # 基线里只要出现非 TQ 或未知来源，这个「同期比值」就不是同源同比，必须显式提示。
    base_foreign = [
        s for s in base_src["sources"] if s not in (SOURCE_TQ,)
    ]

    series: list[dict[str, Any]] = []
    cum = 0.0
    full_minutes = 0
    strict_minutes = 0
    #: 有分钟行但金额全为 NULL 的分钟数：这些分钟的累计**不加 0**，也不许算达标。
    amount_missing_minutes = 0
    #: 真正累进过金额的分钟数：为 0 时 `cum` 不是 0.00，而是「未知」。
    amount_minutes = 0
    for r in mins:
        got = int(r["etfs"] or 0)
        if r["amount_wan"] is None:
            amount_missing_minutes += 1
            series.append({
                "minute": r["minute"],
                "amount_yi": None,
                "cum_yi": round_or_none(cum, 2),
                "baseline_cum_yi": round_or_none(baseline.get(r["minute"]), 2),
                "complete": False,
            })
            continue
        amt_yi = float(r["amount_wan"]) / 1e4
        cum += amt_yi
        amount_minutes += 1
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
    ratio = (cum / base_at) if (amount_minutes and base_at and base_at > 0) else None

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
    if base_dates and base_foreign:
        # 混源基线不是同源同比：只提示，不改数、不剔除（剔除也是无依据的取舍）。
        base_note += (
            f"；⚠️ 基线窗口含非 TQ／未标注来源（{'/'.join(base_src['labels']) or '来源未知'}），"
            "基线均值与当前数据可能不同源，比值只作参考"
        )

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
            "cum_yi": round_or_none(cum, 2) if amount_minutes else None,
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
            "minutes_without_amount": amount_missing_minutes,
            "minutes": len(series),
            "coverage": round_or_none(coverage, 3),
        },
        "note": (
            f"成交额为 {expected} 只重点宽基 ETF 合计（单位亿元）；"
            + base_note
            + "。"
            + src["note"]
        ),
        # 分钟数据真实来源：`tdx_tq` / `em_trends2` / `mixed` / `unknown`，绝不写死。
        "source": src["state"],
        "sources": src["sources"],
        "source_labels": src["labels"],
        "source_by_date": src["by_date"],
        "source_note": src["note"],
        "baseline_sources": base_src["sources"],
        "baseline_source_labels": base_src["labels"],
        "baseline_source_note": base_src["note"],
    }


# ── support（市场级 SC8） ────────────────────────────────────────────────

def sc8_freshness(
    as_of: str | None,
    now: datetime,
    *,
    max_lag: int = STALE_MAX_LAG_TRADING_DAYS,
) -> dict[str, Any]:
    """SC8 最新日相对 `now` 的滞后（**交易日**，开区间）。

    `lag_trading_days` = (as_of, now.date()) 之间的交易日数：T+1 序列在交易日当天应为 0
    （最新日 = 上一交易日），1 是早间采集窗口的容差，≥2 即上游停更/采集失败。
    日历未覆盖该年份时**不给 0**：`verified=False` 且判 stale —— 无法确认就不能当当前。

    单独拎出来是因为 Web API、参谋出口、测试都要同一个滞后数，不能各算一套。
    """
    today = now.date()
    out: dict[str, Any] = {
        "as_of": as_of,
        "checked_at": now.strftime("%Y-%m-%d"),
        "lag_trading_days": None,
        "max_lag_trading_days": int(max_lag),
        "verified": False,
        "stale": True,
        "reason": "calendar_unavailable",
    }
    if not as_of:
        return out
    from src.core.trading_calendar import TradingCalendarError, trading_days_between

    try:
        d = datetime.strptime(str(as_of), "%Y%m%d").date()
        lag = trading_days_between(d, today, inclusive=False)
    except (ValueError, TradingCalendarError):
        return out
    if d > today:
        # 未来日期只可能来自时钟错乱或脏行：既不新鲜也不能当最新日。
        out["reason"] = "future_date"
        return out
    out["lag_trading_days"] = lag
    out["verified"] = True
    out["stale"] = lag > int(max_lag)
    out["reason"] = "lag" if out["stale"] else None
    return out


def _support_note(fresh: dict[str, Any]) -> str:
    """SC8 口径说明：**推断**为份额（亿份）+ 真实日期 + 滞后。不做任何托底判定。"""
    base = (
        f"SC8 两列推断为全市场 ETF 总份额与当日份额变动（单位推断为 {SC8_UNIT}，不是金额；"
        "推断依据与反证见 unit_evidence）；不做金额换算，也不按单一阈值筛「托底日」。"
    )
    if not fresh.get("stale"):
        return base
    lag = fresh.get("lag_trading_days")
    if lag is None:
        if fresh.get("reason") == "future_date":
            return base + (
                f"⚠️ 最新日 {fresh.get('as_of')} 晚于今日（时钟或数据异常），"
                "不能确认新鲜度；下列数据照实展示，不得当作当前读数。"
            )
        return base + (
            f"⚠️ 最新日为 {fresh.get('as_of')}，交易日历不可用，无法确认是否仍是最新；"
            "下列历史份额照实展示，不得当作当前读数。"
        )
    return base + (
        f"⚠️ 最新日为 {fresh.get('as_of')}，距 {fresh.get('checked_at')} 已 {lag} 个交易日未更新"
        "（上游市场统计序列停更）；下列历史份额照实展示，不是当前读数，"
        "不得当作今日份额/份额变动。"
    )


def support_payload(
    run: Runner,
    days: int,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """市场级 SC8 原始序列 + 新鲜度闸门（**不做托底日筛选，不提供阈值判定**）。

    `now` 由调用方注入（测试可固定）；默认取上海时区当前时间。
    输出里 `as_of` 永远是库内真实最新日，滞后只加标注，不改日期、不删行、不写库。

    为什么删掉 `support_days/support_count/threshold_yi`：那套统计把采集器里
    「单日净流入 100 **亿元**」的金额阈值直接当成「100 亿份」份额阈值用，单位不可换算，
    是没有任何依据的托底推断；旧的 `threshold_yi` 兼容字段也在继续误导调用方，一并移除。
    现在只给原始样本、样本内分位（不加任何阈值）与单位推断说明。
    """
    now = now or datetime.now(ZoneInfo("Asia/Shanghai"))
    rows = run("etf_support", {"n": days, "as_of": now.strftime("%Y%m%d")})
    # 读源是 ORDER BY trade_date DESC；这里显式按日期升序，最新日不依赖 SQL 返回顺序。
    rows = sorted(rows, key=lambda r: str(r["trade_date"]))
    supported = ew.single_share_source is not None
    if not rows:
        return {
            "state": "no_data",
            "note": "市场级 ETF 份额（SC8）尚无落库数据；采集器回补后可用。",
            "single_share_supported": supported,
            "source": SOURCE_TQ,
            "source_table": "etf_flow_daily（TDX SC8 序列）",
        }

    series = [
        {
            "date": r["trade_date"],
            "scale_yi": round_or_none(r["etf_scale"]),
            "net_sub_yi": round_or_none(r["etf_net_sub"]),
        }
        for r in rows
    ]
    values = sorted(x["net_sub_yi"] for x in series if x["net_sub_yi"] is not None)

    latest = series[-1]
    latest_val = latest.get("net_sub_yi")
    # 分位只描述「最新值在本样本窗口内排第几」，样本就是读源原样：不做阈值筛选，
    # 因此它不是「托底日分位」，文案也必须这么说。
    pct = None
    if latest_val is not None and values:
        pct = round(sum(1 for v in values if v < latest_val) / len(values) * 100, 1)

    fresh = sc8_freshness(latest["date"], now)

    return {
        "state": "stale" if fresh["stale"] else "ok",
        "stale": bool(fresh["stale"]),
        "as_of": latest["date"],
        "freshness": fresh,
        # 显式单位：前端/参谋照它渲染，别再按字段名的 `_yi` 猜成亿元。
        "unit": SC8_UNIT,
        "unit_confidence": SC8_UNIT_CONFIDENCE,
        "unit_evidence": SC8_UNIT_CAVEAT,
        "scale_unit": SC8_UNIT,
        "net_sub_unit": SC8_UNIT,
        "unit_basis": "推断为全市场 ETF 份额（非金额；份额变动 ≠ 金额净申购）",
        "series": series,
        "sample_count": len(series),
        "latest": latest,
        "latest_percentile_in_samples": pct,
        "max_net_sub_yi": values[-1] if values else None,
        "min_net_sub_yi": values[0] if values else None,
        "single_share_supported": supported,
        "single_share_note": (
            "单只 ETF 净申购不可用（通达信 ETF 历史份额尚未通过独立数据验证）；"
            "此处为全市场 ETF 份额口径（SC8），不能拆到单只。"
            if not supported
            else f"单只份额来源：{ew.single_share_source}"
        ),
        "note": _support_note(fresh),
        "source": SOURCE_TQ,
        # SC8 不在分钟表里：它的来源是 etf_flow_daily（TQ SC8 序列），不能套用分钟来源。
        "source_table": "etf_flow_daily（TDX SC8 序列）",
    }


# ── daily（日度资金全景） ────────────────────────────────────────────────

def daily_payload(run: Runner, days: int) -> dict[str, Any]:
    """日度资金全景（Web API 列契约：所有数组都对齐 `dates`）。

    这里只做日度 API 的取数与聚合，**不判定某日是否采满全天**：分钟表里
    「某日有行」既不等于 240 根分钟采齐，也不等于标的齐 20 只，任何 complete
    标记都是新口径的虚报。参谋出口（`etf.flow`）因此不引用本函数的多日合计，
    只报分钟级已采集累计 + 市场级 SC8 原始最新日/样本。
    """
    dates = sorted(
        r["trade_date"] for r in run("etf_daily_dates", {"n": days})
    )
    if not dates:
        return {
            "state": "no_data",
            "note": "尚无分钟数据落库。",
            "dates": [],
            "days_requested": int(days),
            "source": "no_data",
            "sources": [],
            "source_labels": [],
            "source_by_date": {},
            "source_note": "无分钟行，来源无从判断",
        }

    ph, in_params = in_clause("d", dates)
    src = minute_sources(run, [str(d) for d in dates])
    rows = run("etf_daily_minute", in_params, {"ph": ph})
    cat_of = {c: cat for c, _n, cat in ew.ETF_BASKET}
    by_cat: dict[str, dict[str, float]] = {cat: {} for cat in ew.categories()}
    total: dict[str, float] = {}
    #: 当天至少有一个非 NULL 金额的天数：全是 NULL 的那天出口是 None，不是 0.00。
    has_amount: set[str] = set()
    for r in rows:
        cat = cat_of.get(r["etf_code"])
        if not cat:
            continue
        if r["amount_wan"] is None:
            continue
        yi = float(r["amount_wan"]) / 1e4
        by_cat[cat][r["trade_date"]] = by_cat[cat].get(r["trade_date"], 0.0) + yi
        total[r["trade_date"]] = total.get(r["trade_date"], 0.0) + yi
        has_amount.add(r["trade_date"])

    net = {
        r["trade_date"]: r
        for r in run("etf_daily_net", in_params, {"ph": ph})
    }

    return {
        "state": "ok",
        "dates": dates,
        "days_requested": int(days),
        # 单位分开写：成交额是金额（亿元），SC8 两列是份额（亿份）——混用一个「单位」必错。
        "units": {"total_amount": "亿元", "net_sub": SC8_UNIT, "scale": SC8_UNIT},
        "total_amount_yi": [
            round_or_none(total.get(d)) if d in has_amount else None for d in dates
        ],
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
        "note": (
            "成交额为 20 只 ETF 合计（亿元）；份额变动/总份额列（字段名 net_sub_yi/scale_yi）"
            "是市场级 SC8 的**推断份额**口径"
            f"（单位推断为{SC8_UNIT}，是份额不是金额，依据见 support_payload.unit_evidence），"
            "且只到全市场一层；"
            + src["note"]
        ),
        # 分钟成交额的真实来源（逐日）：混合窗口会被标成 mixed，未知标 unknown。
        "source": src["state"],
        "sources": src["sources"],
        "source_labels": src["labels"],
        "source_by_date": src["by_date"],
        "source_note": src["note"],
    }


__all__ = [
    "BASELINE_DAYS",
    "COVERAGE_MIN",
    "MAX_MINUTES_PER_DAY",
    "SC8_UNIT",
    "SC8_UNIT_CAVEAT",
    "SC8_UNIT_CONFIDENCE",
    "SOURCE_LABELS",
    "SOURCE_UNKNOWN",
    "SQL",
    "STALE_MAX_LAG_TRADING_DAYS",
    "basket_payload",
    "baseline_cum",
    "baseline_curve",
    "daily_payload",
    "in_clause",
    "intraday_payload",
    "min_etfs_for_complete",
    "minute_sources",
    "phase_of",
    "round_or_none",
    "summarize_minute_sources",
    "sc8_freshness",
    "support_payload",
]
