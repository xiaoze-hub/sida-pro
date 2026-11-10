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

覆盖纪律（2026-11-10 复审：80% 闸门曾把 16/20 的基线当成「可用」，把 20/20 的今日
算成「偏活跃」）：
- **齐备（complete）只有一个口径：全篮子 20/20 只且金额非空。** 16/20 只的分钟合计
  天然少 4 只的钱；拿它当「可用」会让基线被系统性压低（同一只 ETF 的同样成交额，
  20 只今日 ÷ 16 只基线 ≈ 1.25 → 假 active）。80%（`COVERAGE_MIN`）只用于
  「齐备分钟占已观测分钟的比例」这一件事，不再用来判某分钟是否可用。
- **基线只收「同日全篮子 + 同一批分钟」的交易日**：基线日必须在本次比较的分钟集合上
  逐分钟齐备、且没有缺口；否则整日剔除并记原因（`incomplete_basket` / `minute_gap`），
  绝不进均值。基线均值是「同一分钟集合的累计」的日均，不是「有行的分钟各自均值」。
- **缺分钟/缺金额不许单边漏算**：今日和基线都只累加同一批分钟（`minutes_compared`），
  任何一侧有缺口或残缺分钟就不做比较 —— 缺一根分钟既会让累计少算一截，也会让比值
  凭空放大或缩小。
- **部分数据只能无结论**：缺只 / 金额全 NULL / 分钟缺口 / 齐备分钟占比不足 80% →
  `state="insufficient"`、`conclusive=False`、`ratio_vs_baseline=None`，只报已采集累计
  与覆盖明细（哪些分钟缺只、缺几根、缺哪几分钟）。
- **真实零值保留**：`amount_wan = 0.0` 是「这一分钟确实成交 0」，照常进累计与齐备计数；
  只有 NULL 才是缺失，且缺失不补 0。
- **盘中实时可用**：盘中「时钟当前那一分钟」往往还在成形（部分标的尚未回报），单列
  `in_progress` 并不计入累计/结论，但不会让整张看板在盘中永远「数据不全」；更早的分钟
  缺只/缺额就是真的数据残缺，不给结论。

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
from datetime import date, datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo

from src.collectors import tq_etf_watch as ew

logger = logging.getLogger(__name__)

Runner = Callable[..., list[dict[str, Any]]]
#: `run(key, params, fmt)` —— key 对应 `SQL`，fmt 用于 IN 占位符展开。
#: 调用方可以用 `_read(db, key, params)`（键已登记在 tools_catalog._SQL）或自行执行 SQL。

#: 齐备（complete）的唯一口径：全篮子齐只。`min_etfs_for_complete` 见函数说明。
#: 80% 只用于「齐备分钟 / 已观测分钟」这一条覆盖率闸门，**不再**用来把 16/20 只当成可用分钟。
COVERAGE_MIN = 0.80

#: 20 日基线窗口（交易日，不含当日）。
BASELINE_DAYS = 20

#: 单日分钟上限（含 11:30 / 13:00 边界与盘后残留行；SQL 的 LIMIT 按它取上界）。
MAX_MINUTES_PER_DAY = 1440

#: 交易时段的分钟网格（当日分钟数，闭区间）：09:30-11:30、13:00-15:00。
#: 用来判「已观测分钟之间有没有洞」—— 缺一根分钟会让累计少一截，比值随之失真。
SESSION_WINDOWS: tuple[tuple[int, int], ...] = (
    (9 * 60 + 30, 11 * 60 + 30),
    (13 * 60, 15 * 60),
)

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

#: 「不给结论」的原因 → 人话。出口与测试都照这张表，避免各写一套说法。
WITHHELD_LABELS = {
    "no_minutes": "没有可用于比较的分钟（无累计可算）",
    "minutes_without_amount": "有分钟金额全是 NULL（未落库，不补 0）",
    "minutes_short_basket": "有分钟篮子缺只（未达全篮子 20/20）",
    "minute_gaps": "已采集分钟之间有缺口（缺分钟会让累计少算一截）",
    "coverage_below_min": "齐备分钟占已观测分钟的比例不足 80%",
    "baseline_no_sample": "基线没有「同日全篮子 + 同一批分钟」的可比交易日",
    "baseline_zero": "基线同期累计为 0（除不出比值）",
}


def min_etfs_for_complete(expected: int | None = None) -> int:
    """一个分钟「齐备」所需的标的数：**全篮子**（20 只篮子 → 20 只），不是 80%。

    旧实现返回 `ceil(n * 0.8)`（20 只 → 16），于是 16/20 只的分钟被当成「可用」并入
    基线与结论：16 只的合计天然只有 20 只的 80%，同一笔成交额会算出 1.25 倍 → 假「偏活跃」。
    与采集侧（`etf_minute_gapfill` 逐只对账：20 里差一只即 `insufficient_etfs`）对齐后，
    「齐备」只有一个含义：全篮子且金额非空。

    80%（`COVERAGE_MIN`）仍保留，但只用于「齐备分钟 / 已观测分钟」的覆盖率闸门，
    不再参与「这一分钟算不算数」的判定。
    """
    n = len(ew.ETF_BASKET) if expected is None else int(expected)
    return max(1, n)


def minute_of_day(minute: Any) -> int | None:
    """`"0930"` → 570；脏值返回 None（不猜）。"""
    m = str(minute or "").strip()
    if len(m) != 4 or not m.isdigit():
        return None
    hh, mm = int(m[:2]), int(m[2:])
    if hh > 23 or mm > 59:
        return None
    return hh * 60 + mm


def hhmm_of(value: int) -> str:
    return f"{value // 60:02d}{value % 60:02d}"


def session_minute_grid(first: str, last: str) -> list[str]:
    """`first..last` 之间的交易分钟网格（跳过午休）；任一端非法返回空。

    只用来找「已观测分钟之间的洞」：网格里没有、但区间内本应有行的那几分钟就是缺口。
    """
    a, b = minute_of_day(first), minute_of_day(last)
    if a is None or b is None or b < a:
        return []
    out: list[str] = []
    for lo, hi in SESSION_WINDOWS:
        out.extend(hhmm_of(m) for m in range(max(lo, a), min(hi, b) + 1))
    return out


def analyze_minutes(rows: list[dict[str, Any]], expected: int) -> dict[str, Any]:
    """把「按分钟聚合的分钟行」分成 齐备 / 缺只 / 金额全 NULL / 缺分钟（洞）。

    `rows` 来自 `SQL["etf_minute_rows"]` / `SQL["etf_baseline_rows"]`：
    `minute, [trade_date,] amount_wan, etfs`。`amount_wan` 为 None 表示这一分钟
    没有任何非空金额（`etfs` 也会是 0），必须与「真实 0.0」区分开。
    """
    amount: dict[str, float | None] = {}
    counts: dict[str, int] = {}
    for r in rows or []:
        m = str(r.get("minute") or "")
        if minute_of_day(m) is None:
            continue
        raw = r.get("amount_wan")
        amount[m] = None if raw is None else float(raw)
        counts[m] = int(r.get("etfs") or 0)

    observed = sorted(amount)
    without_amount = [m for m in observed if amount[m] is None]
    short_basket = [m for m in observed if amount[m] is not None and counts[m] < expected]
    complete = [
        m for m in observed if amount[m] is not None and counts[m] >= expected
    ]
    gaps = (
        [m for m in session_minute_grid(observed[0], observed[-1]) if m not in amount]
        if observed
        else []
    )
    return {
        "observed": observed,
        "amount": amount,
        "etfs": counts,
        "complete": complete,
        "complete_set": set(complete),
        "without_amount": without_amount,
        "short_basket": short_basket,
        #: 会让「这一分钟的合计」残缺的分钟：金额全 NULL 或篮子缺只。
        "incomplete": sorted(set(without_amount) | set(short_basket)),
        "gaps": gaps,
    }


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

def baseline_curve(
    run: Runner,
    dates: list[str],
    *,
    minutes: list[str] | None = None,
    expected: int | None = None,
) -> dict[str, Any]:
    """过去 N 日「同一分钟集合」的累计成交额均值 + **真实样本数与剔除原因**。

    返回 `cum`（分钟 → 日均累计亿元）、`days`（分钟 → 参与均值的交易日数）、
    `days_used`、`days_requested`、`minutes_compared`、`days_dropped`、`drop_reasons`。

    两条硬口径（2026-11-10 复审修）：
    1. **只收同日全篮子**：一天在比较的分钟里只要有一分钟缺只（<`expected`）或金额全
       NULL，这天就整日剔除 —— 它的「累计」是残缺的，混进均值就是把基线压低。
    2. **同一分钟集合累计**：`minutes` 由调用方给（今日已观测分钟）；基线日必须在这批
       分钟上逐分钟有行，少一根就整日剔除（`minute_gap`）。插值、补 0、只加「有行的
       分钟」都会让某一天的累计少算一截，比值随之失真。

    没给 `minutes` 时（`baseline_cum` 旧入口）：取各全篮子日分钟集合的**交集**当比较集，
    保证「同一分钟集合」这条纪律在旧调用路径上同样成立。

    分母缺失（缺失日是未采集还是非交易日）无法从分钟表反推，所以只报**能算出的实际
    样本数**，剩余标「未确认」。
    """
    expected_etfs = len(ew.ETF_BASKET) if expected is None else int(expected)
    out: dict[str, Any] = {
        "cum": {},
        "days": {},
        "days_used": 0,
        "days_requested": len(dates),
        "minutes_compared": 0,
        "expected_etfs": expected_etfs,
        # 兼容旧字段名：齐备门槛现在就是全篮子，不再有 16/20 这种「闸门」。
        "gate_etfs": expected_etfs,
        "days_dropped": 0,
        "drop_reasons": {},
        "days_incomplete_basket": 0,
        "days_with_minute_gap": 0,
    }
    if not dates:
        return out

    ph, in_params = in_clause("d", dates)
    rows = run("etf_baseline_rows", in_params, {"ph": ph})
    by_day: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        day = str(r.get("trade_date") or "")
        if not day:
            continue
        by_day.setdefault(day, []).append(r)
    per_day = {d: analyze_minutes(rs, expected_etfs) for d, rs in by_day.items()}

    # 比较用的分钟集合：调用方给的（今日已观测）优先；否则取各全篮子日分钟集合的交集。
    caller_minutes = minutes is not None
    if minutes is None:
        clean = [a for a in per_day.values() if not a["incomplete"]]
        common = set(clean[0]["observed"]) if clean else set()
        for a in clean[1:]:
            common &= set(a["observed"])
        minutes = sorted(common)
    target = [m for m in minutes if minute_of_day(m) is not None]
    out["minutes_compared"] = len(target)
    if not target:
        # 没有可比分钟 → 没有一天能算「同一分钟集合的累计」，不拿它当 0 基线。
        out["days_dropped"] = len(per_day)
        out["drop_reasons"] = {"no_aligned_minutes": len(per_day)} if per_day else {}
        return out

    reasons = {"incomplete_basket": 0, "minute_gap": 0}
    acc: dict[str, float] = {}
    used = 0
    for day in sorted(per_day):
        info = per_day[day]
        if caller_minutes:
            # 只看比较集内的分钟：集外的残缺分钟不影响同一分钟集合的累计。
            bad = [
                m for m in target
                if m in info["amount"]
                and (info["amount"][m] is None or info["etfs"][m] < expected_etfs)
            ]
        else:
            # 旧入口没有「今日分钟集」可对齐：整日必须齐备，否则剔除。
            bad = list(info["incomplete"])
        if bad:
            reasons["incomplete_basket"] += 1
            continue
        missing = [m for m in target if m not in info["amount"]]
        if missing:
            reasons["minute_gap"] += 1
            continue
        used += 1
        running = 0.0
        for m in target:
            running += float(info["amount"][m] or 0.0)
            acc[m] = acc.get(m, 0.0) + running

    out["days_used"] = used
    out["days_dropped"] = sum(reasons.values())
    out["drop_reasons"] = {k: v for k, v in reasons.items() if v}
    out["days_incomplete_basket"] = reasons["incomplete_basket"]
    out["days_with_minute_gap"] = reasons["minute_gap"]
    if used and acc:
        out["cum"] = {m: (acc[m] / used) / 1e4 for m in target}
        out["days"] = {m: used for m in target}
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

    analysis = analyze_minutes(mins, expected)
    used_observed = list(analysis["observed"])

    # 盘中「当前这一分钟」往往还在成形（部分标的尚未回报）。它既不能算进累计（会少算），
    # 也不该让整张看板在盘中永远「数据不全」：单列 `in_progress`，不进累计、不进结论。
    # 只认**时钟当前那一分钟**（`now` 的 HH:MM）：更早的分钟缺只/缺额就是真的数据残缺，
    # 不能拿「可能还在成形」当免罪符。
    current_minute = now.strftime("%H%M")
    in_progress: dict[str, Any] | None = None
    if (
        used_observed
        and session_phase == "live"
        and d_obj == now.date()
        and used_observed[-1] == current_minute
        and used_observed[-1] in set(analysis["incomplete"])
    ):
        tail = used_observed[-1]
        tail_etfs = int(analysis["etfs"].get(tail) or 0)
        in_progress = {
            "minute": tail,
            "etfs": tail_etfs,
            "etfs_expected": expected,
            "note": (
                f"{tail} 这根分钟仍在成形（{tail_etfs}/{expected} 只已回报），"
                "未计入累计与活跃度结论"
            ),
        }
        used_observed = used_observed[:-1]

    used_set = set(used_observed)
    complete_used = [m for m in used_observed if m in analysis["complete_set"]]
    missing_amount_used = [m for m in analysis["without_amount"] if m in used_set]
    short_basket_used = [m for m in analysis["short_basket"] if m in used_set]

    base_dates = [
        r["trade_date"]
        for r in run("etf_baseline_dates", {"d": target, "n": BASELINE_DAYS})
    ]
    # 比较集 = 今日已观测的那批分钟（不是「基线的分钟」也不是「有行的分钟」）：
    # 两侧都只累加这一批，缺一分钟都不会单边漏算。
    base = baseline_curve(run, base_dates, minutes=used_observed, expected=expected)
    baseline: dict[str, float] = base["cum"]
    base_src = minute_sources(run, [str(d) for d in base_dates])
    # 基线里只要出现非 TQ 或未知来源，这个「同期比值」就不是同源同比，必须显式提示。
    base_foreign = [
        s for s in base_src["sources"] if s not in (SOURCE_TQ,)
    ]

    series: list[dict[str, Any]] = []
    cum = 0.0
    #: 真正累进过金额的分钟数：为 0 时 `cum` 不是 0.00，而是「未知」。
    amount_minutes = 0
    for m in used_observed:
        amt = analysis["amount"][m]
        if amt is None:
            series.append({
                "minute": m,
                "amount_yi": None,
                "cum_yi": round_or_none(cum, 2),
                "baseline_cum_yi": round_or_none(baseline.get(m), 2),
                "complete": False,
            })
            continue
        amt_yi = amt / 1e4
        cum += amt_yi
        amount_minutes += 1
        series.append({
            "minute": m,
            "amount_yi": round_or_none(amt_yi, 3),
            "cum_yi": round_or_none(cum, 2),
            "baseline_cum_yi": round_or_none(baseline.get(m), 2),
            "complete": m in analysis["complete_set"],
        })

    last_minute = complete_used[-1] if complete_used else None
    # `last_minute` = 参与累计/结论的最后一根齐备分钟；`last_observed_minute` = 库里真有的
    # 最后一根（含仍在成形、被排除的那根），出口把「比较到哪」与「采到哪」分开说。
    last_observed_minute = analysis["observed"][-1] if analysis["observed"] else None
    coverage = (len(complete_used) / len(used_observed)) if used_observed else None
    base_at = baseline.get(last_minute) if last_minute else None

    # 部分数据只能无结论：任一条件不满足就不出比值，也不出活跃度结论。
    data_withheld: list[str] = []
    if not used_observed:
        data_withheld.append("no_minutes")
    if missing_amount_used:
        data_withheld.append("minutes_without_amount")
    if short_basket_used:
        data_withheld.append("minutes_short_basket")
    if analysis["gaps"]:
        data_withheld.append("minute_gaps")
    if coverage is not None and coverage < COVERAGE_MIN:
        data_withheld.append("coverage_below_min")

    baseline_ok = (
        base["days_used"] > 0 and base_at is not None and base_at > 0 and amount_minutes > 0
    )
    conclusive = not data_withheld and baseline_ok
    ratio = (cum / base_at) if conclusive and base_at else None
    if data_withheld:
        state = "insufficient"
        withheld = data_withheld
    elif not baseline_ok:
        # 基线 0 日可用 / 基线本身就是 0 → 没有可比对象，不是「常态」。
        state = "no_baseline"
        withheld = ["baseline_no_sample" if base["days_used"] == 0 else "baseline_zero"]
    else:
        withheld = []
        if ratio is not None and ratio >= 1.5:
            state = "surge"
        elif ratio is not None and ratio >= 1.15:
            state = "active"
        elif ratio is not None and ratio <= 0.7:
            state = "quiet"
        else:
            state = "normal"

    points = index_points or []
    days_requested = BASELINE_DAYS
    days_available = len(base_dates)          # 窗口内真的有行情的交易日
    days_used = int(base["days_used"])        # 其中「同日全篮子 + 同一批分钟」齐备的天数
    days_dropped = int(base["days_dropped"])
    missing_days = max(days_requested - days_available, 0)
    sample_days = int((base["days"] or {}).get(last_minute, 0)) if last_minute else 0
    confirmed = missing_days == 0 and days_dropped == 0
    minutes_compared = int(base["minutes_compared"])
    has_compared_minutes = minutes_compared > 0
    basket_note = (
        f"基线只收「同日 {expected}/{expected} 只齐备 + 覆盖本次 {minutes_compared} 根分钟」的交易日，"
        "按「同一分钟集合的累计」取日均，不是全天对比"
    )

    if confirmed:
        base_note = f"基线取过去 {days_requested} 个交易日；{basket_note}"
    else:
        base_note = (
            f"基线本次仅 {days_used} 个交易日可用（请求 {days_requested} 日；"
            f"窗口内缺 {missing_days} 个交易日样本"
        )
        if days_dropped:
            bits = []
            if base["days_incomplete_basket"]:
                bits.append(f"{base['days_incomplete_basket']} 日篮子缺只")
            if base["days_with_minute_gap"]:
                bits.append(f"{base['days_with_minute_gap']} 日缺分钟")
            base_note += f"，另有 {days_dropped} 个交易日被剔除（{'、'.join(bits)}）"
        base_note += f"；{basket_note}"
        base_note += "；缺的日期是未采集还是非交易日无法从分钟表确认，标未确认"
    if not has_compared_minutes:
        base_note = f"本次没有可比分钟；{basket_note}"
    if base_dates and base_foreign:
        # 混源基线不是同源同比：只提示，不改数、不剔除（剔除也是无依据的取舍）。
        base_note += (
            f"；⚠️ 基线窗口含非 TQ／未标注来源（{'/'.join(base_src['labels']) or '来源未知'}），"
            "基线均值与当前数据可能不同源，比值只作参考"
        )

    withheld_note = ""
    if withheld:
        withheld_note = "；本次不给活跃度结论：" + "、".join(
            WITHHELD_LABELS.get(k, k) for k in withheld
        )

    coverage_note = ""
    if used_observed:
        coverage_note = (
            f"；已观测 {len(used_observed)} 根分钟，其中 {len(complete_used)} 根 {expected}/{expected} 只齐备"
            f"（{round_or_none(coverage, 3)}），{len(short_basket_used)} 根篮子缺只、"
            f"{len(missing_amount_used)} 根金额全是 NULL、{len(analysis['gaps'])} 根分钟缺口"
        )
        if in_progress:
            coverage_note += f"；{in_progress['minute']} 这根仍在成形，未计入"

    return {
        "date": target,
        "phase": phase_of(d_obj, now),
        "session_phase": session_phase,
        "session_time": now.strftime("%H:%M"),
        "state": state,
        "conclusive": conclusive,
        "fallback_from": fallback_from,
        "stale": fallback_from is not None,
        "benchmark": {"code": ew.BENCHMARK_INDEX, "name": ew.BENCHMARK_NAME, "points": points},
        "series": series,
        "in_progress": in_progress,
        "totals": {
            "cum_yi": round_or_none(cum, 2) if amount_minutes else None,
            # 没有结论就不给比较数字：半个口径的基线均值只会被拿去心算一个假比值。
            "baseline_avg_yi": round_or_none(base_at) if conclusive else None,
            "ratio_vs_baseline": round_or_none(ratio, 3) if conclusive else None,
            "conclusive": conclusive,
            "withheld_reasons": withheld,
            "baseline_days": days_used,
            "baseline_days_requested": days_requested,
            "baseline_days_in_window": days_available,
            "baseline_missing_days": missing_days,
            "baseline_days_dropped": days_dropped,
            "baseline_drop_reasons": base["drop_reasons"],
            "baseline_days_incomplete_basket": int(base["days_incomplete_basket"]),
            "baseline_days_minute_gap": int(base["days_with_minute_gap"]),
            "baseline_minutes_compared": minutes_compared,
            "baseline_expected_etfs": expected,
            "baseline_sample_days": sample_days,
            "baseline_confirmed": confirmed,
            "last_minute": last_minute,
            "last_observed_minute": last_observed_minute,
            "in_progress_minute": (in_progress or {}).get("minute"),
            "etfs_expected": expected,
            "min_etfs_for_complete": gate,
            "complete_minutes": len(complete_used),
            "full_minutes": len(complete_used),
            #: 旧字段名兼容：`strict_minutes` 过去指「真 20 只全齐」，现在与 `full_minutes` 同义。
            "strict_minutes": len(complete_used),
            "minutes_short_basket": len(short_basket_used),
            "minutes_without_amount": len(missing_amount_used),
            "minute_gaps": len(analysis["gaps"]),
            "minute_gaps_detail": analysis["gaps"][:10],
            "minutes": len(series),
            "coverage": round_or_none(coverage, 3),
            "coverage_min": COVERAGE_MIN,
        },
        "note": (
            f"成交额为 {expected} 只重点宽基 ETF 已采集合计（单位亿元）；"
            + base_note
            + coverage_note
            + withheld_note
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
    "SESSION_WINDOWS",
    "SOURCE_LABELS",
    "SOURCE_UNKNOWN",
    "SQL",
    "STALE_MAX_LAG_TRADING_DAYS",
    "WITHHELD_LABELS",
    "analyze_minutes",
    "basket_payload",
    "baseline_cum",
    "baseline_curve",
    "daily_payload",
    "hhmm_of",
    "in_clause",
    "intraday_payload",
    "min_etfs_for_complete",
    "minute_of_day",
    "minute_sources",
    "phase_of",
    "round_or_none",
    "session_minute_grid",
    "summarize_minute_sources",
    "sc8_freshness",
    "support_payload",
]
