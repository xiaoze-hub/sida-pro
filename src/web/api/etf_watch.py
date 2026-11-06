"""国家队 ETF 看板 API（TDX/TQ 口径，2026-11-05）。

三个数据面，全部**读库**（采集在 `src/collectors/tq_etf_watch.py`，不进请求路径）：

GET /api/etf-watch/basket              20 只重点 ETF + 12 类分组 + 数据源声明
GET /api/etf-watch/intraday?date=      分钟级聚合：20 只合计 + 上证综指 + 20 日同期均值
GET /api/etf-watch/support             市场级 ETF 规模/净申购（SC8）+ 托底日样本 + 今日分位
GET /api/etf-watch/daily?days=30       日度：总成交额 / 分类成交额 / 净申购

口径纪律（用户对幻觉敏感，这里逐条写死）：

1. **净申购只有市场级是真的**。单只 ETF 的 `get_gb_info` 份额是「当前值回填到每一天」
   （实测 6 只主流 ETF × 63 个交易日零变化），拿它做单只净申购必然恒为 0。
   所以响应里 `single_share_supported=false` + `single_share_note`，前端必须显式标注，
   **禁止用别的算法"凑"一个单只净申购出来**。
2. 数据缺失一律 `null` + `note`，不写 0。分钟行不全（覆盖率 <80%）时 `state="insufficient"`
   而不是拿半场数据算活跃度。
3. 单位：成交额对外统一 **亿元**（库内 `amount_wan` 是万元，出口 ÷10000）；净申购本来就是亿元。
4. 时段：`phase` 区分 `pre / live / closed / non_trading_day`，盘中均值只和**同日已发生
   的分钟**比（拿今天累计比过去 20 日全天累计是错的）。
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Query
from sqlalchemy import text

from src.collectors import tq_etf_watch as ew
from src.web.database import engine

logger = logging.getLogger(__name__)

router = APIRouter()

_CN = ZoneInfo("Asia/Shanghai")

#: 覆盖率闸门：不足 80% 的交易日判定「数据不全」，不参与活跃度结论。
_COVERAGE_MIN = 0.80

#: 20 日基线窗口（交易日，不含当日）。
BASELINE_DAYS = 20


# ── 工具 ─────────────────────────────────────────────────────────────────

def _now_cn() -> datetime:
    return datetime.now(timezone.utc).astimezone(_CN)


def _phase_of(d: date) -> str:
    """pre / live / closed / non_trading_day。"""
    from src.core.trading_calendar import is_trading_day

    if d.weekday() >= 5 or not is_trading_day(d):
        return "non_trading_day"
    hm = _now_cn().strftime("%H%M")
    if d != _now_cn().date():
        return "closed" if d < _now_cn().date() else "pre"
    if "0930" <= hm <= "1130" or "1300" <= hm <= "1500":
        return "live"
    if hm < "0930":
        return "pre"
    return "closed"  # 午休 / 收盘后


def _q(sql: str, params: dict | None = None) -> list[dict]:
    with engine.connect() as conn:
        rows = conn.execute(text(sql), params or {}).mappings().all()
    return [dict(r) for r in rows]


def _int_param(v: object, default: int) -> int:
    """FastAPI 注入时是 int；直接函数调用时可能是 `Query(...)` 对象。"""
    return int(getattr(v, "default", v) or default)


def _round(v: float | None, n: int = 2) -> float | None:
    return None if v is None else round(float(v), n)


def _in_clause(prefix: str, values: list[str]) -> tuple[str, dict[str, str]]:
    """跨方言 IN 占位符（PG 的 `= ANY(:list)` 在 SQLite 上不成立）。"""
    params = {f"{prefix}{i}": v for i, v in enumerate(values)}
    placeholders = ", ".join(f":{k}" for k in params)
    return placeholders, params


# ── basket ───────────────────────────────────────────────────────────────

@router.get("/basket")
def get_basket() -> dict:
    """20 只重点 ETF 与分类。前端据此渲染图例与分类表，不在前端硬编码名单。"""
    return {
        "items": ew.basket_payload(),
        "categories": ew.categories(),
        "benchmark": {"code": ew.BENCHMARK_INDEX, "name": ew.BENCHMARK_NAME},
        "source": "tdx_tq",
        "single_share_supported": ew.single_share_source is not None,
        "single_share_note": (
            "单只 ETF 净申购不可用：通达信 ETF 份额为当前值回填（实测 63 个交易日零变化），"
            "净申购一律采用市场级 SC8 口径。"
            if ew.single_share_source is None
            else f"单只份额来源：{ew.single_share_source}"
        ),
    }


# ── intraday ─────────────────────────────────────────────────────────────

def _minute_rows(trade_date: str) -> list[dict]:
    """单日分钟聚合：每分钟 20 只合计成交额 + 已采到的 ETF 数。"""
    return _q(
        """
        SELECT minute,
               SUM(amount_wan) AS amount_wan,
               COUNT(DISTINCT etf_code) AS etfs
        FROM etf_minute_flow
        WHERE trade_date = :d
        GROUP BY minute
        ORDER BY minute
        """,
        {"d": trade_date},
    )


def _baseline_dates(trade_date: str, n: int) -> list[str]:
    """当日之前最近的 n 个**有数据**的交易日。"""
    rows = _q(
        """
        SELECT DISTINCT trade_date FROM etf_minute_flow
        WHERE trade_date < :d
        ORDER BY trade_date DESC
        LIMIT :n
        """,
        {"d": trade_date, "n": n},
    )
    return [r["trade_date"] for r in rows]


def _baseline_cum(dates: list[str]) -> dict[str, float]:
    """过去 N 日「同一分钟」的累计成交额均值（亿元）。只统计 20 只齐全的分钟。"""
    if not dates:
        return {}
    ph, in_params = _in_clause("d", dates)
    rows = _q(
        f"""
        SELECT minute,
               trade_date,
               SUM(amount_wan) AS amount_wan,
               COUNT(DISTINCT etf_code) AS etfs
        FROM etf_minute_flow
        WHERE trade_date IN ({ph})
        GROUP BY minute, trade_date
        """,
        in_params,
    )
    expected = len(ew.ETF_BASKET)
    per_day: dict[str, dict[str, float]] = {}
    for r in rows:
        if int(r["etfs"] or 0) < expected * _COVERAGE_MIN:
            continue
        per_day.setdefault(r["trade_date"], {})[r["minute"]] = float(r["amount_wan"] or 0.0)
    if not per_day:
        return {}

    # 每个分钟在多少个交易日出现（缺失日不计 0，避免把基线拉低）
    acc: dict[str, float] = {}
    cnt: dict[str, int] = {}
    for mins in per_day.values():
        cum = 0.0
        for m in sorted(mins):
            cum += mins[m]
            acc[m] = acc.get(m, 0.0) + cum
            cnt[m] = cnt.get(m, 0) + 1
    return {m: (acc[m] / cnt[m]) / 1e4 for m in acc if cnt[m] > 0}


@router.get("/intraday")
def get_intraday(date_str: str | None = Query(default=None, alias="date")) -> dict:
    """盘中分钟观测：20 只 ETF 合计成交额 + 上证综指 + 20 日同期均值。"""
    target = date_str or _now_cn().strftime("%Y%m%d")
    try:
        d_obj = datetime.strptime(target, "%Y%m%d").date()
    except ValueError:
        return {"error": "date 需为 YYYYMMDD", "date": target}

    expected = len(ew.ETF_BASKET)
    mins = _minute_rows(target)
    # 当日还没有分钟数据时，回退到最近一个有数据的交易日并**显式标注**。
    # 为什么必须回退：盘中/收盘后短期内通达信的当日分钟线尚未就绪（实测 15:21 仍取不到，
    # `refresh_kline` 也无效），若直接显示"无数据"，页面在交易日大部分时间都是空白。
    # 但**不能**把旧数据伪装成今天 —— 所以 `fallback_from` / `note` 必须一起返回。
    fallback_from: str | None = None
    if not mins and date_str is None:
        latest_rows = _q("SELECT MAX(trade_date) AS d FROM etf_minute_flow")
        latest = (latest_rows[0]["d"] if latest_rows else None)
        if latest and latest != target:
            mins = _minute_rows(latest)
            fallback_from = target
            target = latest
            try:
                d_obj = datetime.strptime(target, "%Y%m%d").date()
            except ValueError:
                pass
    if not mins:
        return {
            "date": target,
            "phase": _phase_of(d_obj),
            "state": "no_data",
            "fallback_from": fallback_from,
            "note": "当日无分钟数据（未采集或非交易日）；盘中采集每分钟一次，收盘后 15:35 补采。",
            "series": [],
            "totals": None,
            "source": "tdx_tq",
        }

    base_dates = _baseline_dates(target, BASELINE_DAYS)
    baseline = _baseline_cum(base_dates)

    series: list[dict] = []
    cum = 0.0
    full_minutes = 0
    for r in mins:
        amt_yi = (float(r["amount_wan"] or 0.0)) / 1e4
        cum += amt_yi
        complete = int(r["etfs"] or 0) >= expected * _COVERAGE_MIN
        if complete:
            full_minutes += 1
        series.append({
            "minute": r["minute"],
            "amount_yi": _round(amt_yi, 3),
            "cum_yi": _round(cum, 2),
            "baseline_cum_yi": _round(baseline.get(r["minute"]), 2),
            "complete": complete,
        })

    last_minute = series[-1]["minute"] if series else None
    base_at = baseline.get(last_minute) if last_minute else None
    ratio = (cum / base_at) if (base_at and base_at > 0) else None

    coverage_ok = full_minutes >= max(1, len(series)) * _COVERAGE_MIN
    if not coverage_ok:
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

    # 上证综指同分钟点位（拿不到不影响主结论，独立标注）
    points: list[dict] = []
    try:
        for hm, close, _amt in ew.fetch_index_minute(target):
            if close is not None:
                points.append({"minute": hm, "close": _round(close, 2)})
    except Exception as e:  # noqa: BLE001 - 指数是旁证, 失败不拖累主图
        logger.info("[etf-watch] 指数分钟线不可用: %s", e)

    return {
        "date": target,
        "phase": _phase_of(d_obj),
        "state": state,
        "fallback_from": fallback_from,
        "benchmark": {"code": ew.BENCHMARK_INDEX, "name": ew.BENCHMARK_NAME, "points": points},
        "series": series,
        "totals": {
            "cum_yi": _round(cum, 2),
            "baseline_avg_yi": _round(base_at),
            "ratio_vs_baseline": _round(ratio, 3),
            "baseline_days": len(base_dates),
            "last_minute": last_minute,
            "etfs_expected": expected,
            "full_minutes": full_minutes,
            "minutes": len(series),
        },
        "note": (
            "成交额为 20 只重点宽基 ETF 合计（通达信分钟线，单位亿元）；"
            "基线取过去 %d 个交易日同一分钟的累计均值，口径为「同日同期」，不是全天对比。"
            % BASELINE_DAYS
        ),
        "source": "tdx_tq",
    }


# ── support（托底信号） ──────────────────────────────────────────────────

@router.get("/support")
def get_support(days: int = Query(default=750, ge=30, le=1500)) -> dict:
    """市场级 ETF 规模/净申购 + 托底日样本 + 今日分位。"""
    days = _int_param(days, 750)
    rows = _q(
        """
        SELECT trade_date, scale_yi AS etf_scale, net_sub_yi AS etf_net_sub
        FROM etf_flow_daily
        WHERE net_sub_yi IS NOT NULL
        ORDER BY trade_date DESC
        LIMIT :n
        """,
        {"n": days},
    )
    rows.reverse()
    if not rows:
        return {
            "state": "no_data",
            "note": "市场级 ETF 规模/净申购（SC8）尚无落库数据；采集器回补后可用。",
            "single_share_supported": ew.single_share_source is not None,
            "source": "tdx_tq",
        }

    series = [
        {
            "date": r["trade_date"],
            "scale_yi": _round(r["etf_scale"]),
            "net_sub_yi": _round(r["etf_net_sub"]),
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
        "support_days": support[-80:],      # 只回最近 80 个托底日给图，统计用全量
        "support_count": len(support),
        "sample_count": len(series),
        "latest": latest,
        "latest_percentile_in_support_days": pct,
        "max_net_sub_yi": values[-1] if values else None,
        "min_net_sub_yi": values[0] if values else None,
        "single_share_supported": ew.single_share_source is not None,
        "single_share_note": (
            "单只 ETF 净申购不可用（通达信 ETF 份额为当前值回填，实测零变化）；"
            "此处为全市场 ETF 口径（SC8），不能拆到单只。"
            if ew.single_share_source is None
            else f"单只份额来源：{ew.single_share_source}"
        ),
        "note": (
            "净申购 = 当日 ETF 份额变动 × 净值（通达信 SC8 市场级口径，亿元）。"
            "单日净申购 ≥%g 亿元视为托底日样本。" % ew.SUPPORT_FLOW_THRESHOLD_YI
        ),
        "source": "tdx_tq",
    }


# ── daily（日度资金全景） ────────────────────────────────────────────────

@router.get("/daily")
def get_daily(days: int = Query(default=30, ge=5, le=120)) -> dict:
    """日度：总成交额 / 分类成交额 / 净申购（净申购仅市场级）。"""
    days = _int_param(days, 30)
    dates_rows = _q(
        """
        SELECT DISTINCT trade_date FROM etf_minute_flow
        ORDER BY trade_date DESC
        LIMIT :n
        """,
        {"n": days},
    )
    dates = sorted(r["trade_date"] for r in dates_rows)
    if not dates:
        return {"state": "no_data", "note": "尚无分钟数据落库。", "source": "tdx_tq"}

    ph, in_params = _in_clause("d", dates)
    rows = _q(
        f"""
        SELECT trade_date, etf_code, SUM(amount_wan) AS amount_wan
        FROM etf_minute_flow
        WHERE trade_date IN ({ph})
        GROUP BY trade_date, etf_code
        """,
        in_params,
    )
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

    net_rows = _q(
        f"""
        SELECT trade_date, scale_yi AS etf_scale, net_sub_yi AS etf_net_sub
        FROM etf_flow_daily
        WHERE trade_date IN ({ph})
        """,
        in_params,
    )
    net = {r["trade_date"]: r for r in net_rows}

    return {
        "state": "ok",
        "dates": dates,
        "total_amount_yi": [_round(total.get(d)) for d in dates],
        "net_sub_yi": [
            _round(net[d]["etf_net_sub"]) if d in net else None for d in dates
        ],
        "scale_yi": [
            _round(net[d]["etf_scale"]) if d in net else None for d in dates
        ],
        "by_category": [
            {
                "category": cat,
                "amount_yi": [_round(m.get(d)) for d in dates],
            }
            for cat, m in by_cat.items()
            if m
        ],
        "single_share_supported": ew.single_share_source is not None,
        "note": "成交额为 20 只 ETF 合计（亿元）；净申购为市场级 SC8 口径，只到全市场一层。",
        "source": "tdx_tq",
    }
