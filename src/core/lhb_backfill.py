"""龙虎榜(东财)回填(2026-09-10, 老板要求: 妖股因子 lhb 维接入)。

数据源: 东财 datacenter RPT_DAILYBILLBOARD_DETAILSNEW —— marketdata Engine
`dragon_tiger(date=...)`(市场级按日过滤, 2026-09-10 实抓校准,
FREE_MARKET_CAP 单位=元)。落库 `dragon_tiger_events`
(`(trade_date, symbol, reason)` 唯一, 重拉幂等, 迁移 v156)。

用途(demon_score 六维此前 lhb 维恒"缺数据计 0"):
- n_lhb = 近一年上榜**天数**(COUNT(DISTINCT trade_date), 同日多原因计 1);
- circ_mv = 该股近一年最新一条 free_market_cap(元), 供市值加分。
榜单未覆盖的股票 n_lhb=0(真实 0, 非缺数据)。

节奏:
- 一次性历史回填 `backfill_history(days=365)`: 交易日列表取自
  limit_up_events/dragon_tiger_events 的 distinct 日期(本地, 无交易日历依赖);
  东财对非交易日返回空, 天然容错。
- 每日增量 `daily_job()`(cron 交易日 17:45, 榜单 ~17:30 发布): 拉近 3 个
  交易日 → 有新行的股票触发 demon_factors 重算。永不抛异常。
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import text

logger = logging.getLogger(__name__)

_CST = ZoneInfo("Asia/Shanghai")
_TABLE = "dragon_tiger_events"
_PAGE_SLEEP = 0.3  # 东财 datacenter 礼貌间隔(秒/交易日)
_COLS = (
    "trade_date", "symbol", "market", "name", "reason", "close", "change_pct",
    "net_buy", "buy_amt", "sell_amt", "deal_amt", "turnover_pct",
    "free_market_cap", "source", "created_at",
)


def _engine():
    from src.db.session import engine

    return engine


def _now_cst() -> datetime:
    return datetime.now(_CST)


def _norm_items(items) -> list[dict]:
    """DragonTigerItem 列表 → 落库行(纯函数可单测)。非 6 位数字代码丢弃;
    ETF/可转债等 6 位代码保留(落库完整榜单, demon 因子按 limit_up_events 股票 join 天然过滤)。"""
    now = _now_cst().isoformat(timespec="seconds")
    out: list[dict] = []
    for it in items:
        sym = str(getattr(it, "symbol", "") or "").strip()
        if len(sym) != 6 or not sym.isdigit():
            continue
        d = str(getattr(it, "trade_date", "") or "")[:10].replace("-", "")
        if len(d) != 8 or not d.isdigit():
            continue
        out.append({
            "trade_date": d,
            "symbol": sym,
            "market": "CN",
            "name": (getattr(it, "name", "") or "").strip() or None,
            "reason": (getattr(it, "reason", "") or "").strip(),
            "close": getattr(it, "close", None),
            "change_pct": getattr(it, "change_pct", None),
            "net_buy": getattr(it, "net_buy", None),
            "buy_amt": getattr(it, "buy_amt", None),
            "sell_amt": getattr(it, "sell_amt", None),
            "deal_amt": getattr(it, "deal_amt", None),
            "turnover_pct": getattr(it, "turnover_pct", None),
            "free_market_cap": getattr(it, "free_market_cap", None),
            "source": "eastmoney",
            "created_at": now,
        })
    return out


def _upsert_rows(rows: list[dict]) -> int:
    """幂等写入((trade_date, symbol, reason) 已存在则跳过), 返回新增行数。失败不抛。"""
    if not rows:
        return 0
    saved = 0
    try:
        with _engine().begin() as conn:
            for r in rows:
                exists = conn.execute(
                    text(
                        f"SELECT 1 FROM {_TABLE} WHERE trade_date = :trade_date"
                        " AND symbol = :symbol AND reason = :reason"
                    ),
                    {"trade_date": r["trade_date"], "symbol": r["symbol"], "reason": r["reason"]},
                ).first()
                if exists:
                    continue
                cols = ",".join(_COLS)
                binds = ",".join(f":{c}" for c in _COLS)
                conn.execute(text(f"INSERT INTO {_TABLE} ({cols}) VALUES ({binds})"), r)
                saved += 1
    except Exception as e:  # noqa: BLE001
        logger.warning("龙虎榜写库失败(%s 行): %s", len(rows), e)
        return 0
    return saved


def _upsert_day(rows: list[dict]) -> int:
    """按日批量: 先查该日已有 (symbol, reason) 键, 只插缺失(省逐行 SELECT)。"""
    if not rows:
        return 0
    try:
        with _engine().begin() as conn:
            existing = {
                (r[0], r[1])
                for r in conn.execute(
                    text(f"SELECT symbol, reason FROM {_TABLE} WHERE trade_date = :d"),
                    {"d": rows[0]["trade_date"]},
                ).fetchall()
            }
            saved = 0
            for r in rows:
                if (r["symbol"], r["reason"]) in existing:
                    continue
                cols = ",".join(_COLS)
                binds = ",".join(f":{c}" for c in _COLS)
                conn.execute(text(f"INSERT INTO {_TABLE} ({cols}) VALUES ({binds})"), r)
                saved += 1
            return saved
    except Exception as e:  # noqa: BLE001
        logger.warning("龙虎榜写库失败(%s@%s): %s", rows[0].get("trade_date"), len(rows), e)
        return 0


def _trading_dates(cutoff: str, limit: int = 0) -> list[str]:
    """YYYYMMDD 交易日列表(本地两表 distinct 日期并集, 升序)。limit>0 取最近 N 日。"""
    sql = (
        f"SELECT DISTINCT trade_date FROM {_TABLE} WHERE trade_date >= :cutoff"
        " UNION SELECT DISTINCT trade_date FROM limit_up_events WHERE trade_date >= :cutoff"
        " ORDER BY trade_date"
    )
    try:
        with _engine().connect() as conn:
            ds = [str(r[0]) for r in conn.execute(text(sql), {"cutoff": cutoff}).fetchall()]
    except Exception as e:  # noqa: BLE001
        logger.warning("交易日列表查询失败: %s", e)
        return []
    if limit > 0:
        ds = ds[-limit:]
    return ds


def _fetch_day(date_yyyymmdd: str) -> list:
    from src.core.marketdata_client import get_market_data

    return get_market_data().dragon_tiger(date=date_yyyymmdd)


def backfill_history(days: int = 365, max_days: int = 0) -> dict:
    """历史回填入口(一次性/手动)。days=回看自然日窗口; max_days>0 限天数(试跑用)。"""
    started = _now_cst().isoformat(timespec="seconds")
    cutoff = (_now_cst() - timedelta(days=days)).strftime("%Y%m%d")
    dates = _trading_dates(cutoff)
    if max_days > 0:
        dates = dates[:max_days]
    saved = 0
    failed = 0
    with_rows = 0
    for i, d in enumerate(dates):
        try:
            rows = _norm_items(_fetch_day(d))
            if rows:
                with_rows += 1
            saved += _upsert_day(rows)
        except Exception as e:  # noqa: BLE001
            failed += 1
            logger.warning("龙虎榜回填 %s 失败: %s", d, e)
        if (i + 1) % 25 == 0:
            logger.info("龙虎榜回填进度: %s/%s 日, 累计 %s 行", i + 1, len(dates), saved)
        time.sleep(_PAGE_SLEEP)
    stats = {
        "mode": "history",
        "started_at": started,
        "finished_at": _now_cst().isoformat(timespec="seconds"),
        "days": len(dates),
        "days_with_rows": with_rows,
        "rows_saved": saved,
        "failed_days": failed,
    }
    logger.info("龙虎榜历史回填完成: %s", stats)
    return stats


def daily_recent(days: int = 3) -> dict:
    """每日增量: 近 N 个交易日榜单落库, 返回有新增行的日期与股票。"""
    dates = _trading_dates("20000101", limit=days)
    if not dates:
        # 空库兜底: 近 N 个工作日(仅多打几次空请求, 不影响正确性)
        dates = []
        d = _now_cst()
        while len(dates) < days:
            d -= timedelta(days=1)
            if d.weekday() < 5:
                dates.append(d.strftime("%Y%m%d"))
        dates.reverse()
    saved = 0
    touched: set[str] = set()
    for d in dates:
        rows = _norm_items(_fetch_day(d))
        n = _upsert_day(rows)
        if n:
            saved += n
            touched.update(r["symbol"] for r in rows)
        time.sleep(_PAGE_SLEEP)
    return {"mode": "daily", "dates": dates, "rows_saved": saved, "touched": sorted(touched)}


def daily_job(days: int = 3, recompute: bool = True) -> dict:
    """cron 入口(交易日 17:45): 增量落库 → 有新行的股票因子重算。永不抛异常。"""
    try:
        inc = daily_recent(days=days)
        rec: dict = {}
        if recompute and inc.get("touched"):
            from src.core.demon_factors import recompute_factors

            rec = recompute_factors(inc["touched"])
        out = {"lhb": inc, "factors": rec}
        logger.info("龙虎榜每日增量完成: rows=%s, 因子重算=%s", inc.get("rows_saved"), rec.get("updated"))
        return out
    except Exception as e:  # noqa: BLE001
        logger.warning("龙虎榜每日增量失败: %s", e)
        return {"error": str(e)}


def lhb_stats(symbols: list[str] | None = None, days: int = 365) -> dict[str, dict]:
    """近一年上榜统计: {symbol: {n_lhb: 上榜天数, circ_mv: 最新流通市值(元)}}。

    单查询全表分组(一年约 2-3 万行, Python 归约), 失败返回 {}(demon_score 退回缺数据口径)。
    """
    cutoff = (_now_cst() - timedelta(days=days)).strftime("%Y%m%d")
    try:
        with _engine().connect() as conn:
            rows = conn.execute(
                text(
                    f"SELECT symbol, trade_date, free_market_cap FROM {_TABLE}"
                    " WHERE trade_date >= :cutoff ORDER BY symbol, trade_date"
                ),
                {"cutoff": cutoff},
            ).fetchall()
    except Exception as e:  # noqa: BLE001
        logger.warning("龙虎榜统计查询失败: %s", e)
        return {}
    want = set(symbols) if symbols else None
    days_set: dict[str, set] = {}
    cap_by_sym: dict[str, tuple[str, float]] = {}
    for sym, d, fmc in rows:
        if want is not None and sym not in want:
            continue
        days_set.setdefault(sym, set()).add(str(d))
        if fmc is not None:
            prev = cap_by_sym.get(sym)
            if prev is None or str(d) >= prev[0]:
                cap_by_sym[sym] = (str(d), float(fmc))
    stats: dict[str, dict] = {}
    for sym, ds in days_set.items():
        st = {"n_lhb": len(ds), "circ_mv": None}
        cap = cap_by_sym.get(sym)
        if cap:
            st["circ_mv"] = cap[1]
        stats[sym] = st
    return stats
