# -*- coding: utf-8 -*-
"""连板梯队数据新鲜度自检 + 自动补数(2026-10-11 P1 数据断档修复)。

背景
----
`limit_up_events` 是连板梯队**收盘态真值源**(`/api/theme-mood/ladder` 在 15:05 后
由盘中态切回 finalized 读它)。写入方 = 妖股因子 15:35 增量管线
(`src.core.demon_factors.update_pipeline`)。

TQ 断链期(2026-10-08~09)该管线取数失败、以 events_saved=0 静默收场 —— 表停在
20260930, 连板梯队页面几天不更新, 无人发现。

本模块(纯核心层, 不引 src.web —— B4.1)
------------------------------------
- ``check()``              : 比对 limit_up_events 最新交易日 vs 期望最新交易日, 缺则
                            ``stale=True`` 并列出缺失交易日;
- ``backfill_missing()``   : TQ 直连**只补缺失交易日**(存档层幂等 upsert);
- ``guard()``              : stale → 补数 → 复检, 返回诚实作业态;
- ``run_ladder_freshness_job()``: cron 入口, 走作业框架(``ok=False → failed``)。

口径(与全仓一致, 缺数据显式不编造)
--------------------------------
- 「真实交易日」= ``trading_calendar.is_trading_day`` **且非周末**。A 股周末恒不开市
  (实测 2026 各"调休补班周六"在 klines / market_breadth_daily / limit_up_events
  三类数据表里**均无任何行**), 而交易日历把补班周末也记为交易日属已知偏差; 本模块
  显式剔除该偏差, 否则节假日后的第一个工作日会永久假告警并反复触发全市场重补。
- ``limit_up_events`` 为空(无任何事件期号)时,**不**以"空表"冒充"无缺口",
  显式 ``ok=False``(不下结论)。
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_CST = ZoneInfo("Asia/Shanghai")
_EVENTS = "limit_up_events"

JOB_KIND = "ladder_freshness"
JOB_LABEL = "连板梯队数据自检"


# ── 交易日历口径 ────────────────────────────────────────────────────────────

def _is_real_trading_day(d: date) -> bool:
    """真实交易日 = 交易日历判为交易日 **且非周末**(A 股周末恒不开市, 见模块头)。"""
    from src.core.trading_calendar import is_trading_day

    if d.weekday() >= 5:
        return False
    try:
        return bool(is_trading_day(d))
    except Exception as e:  # noqa: BLE001 日历表未覆盖年份 → 显式不猜
        logger.warning("ladder-freshness: 交易日历查询失败(%s), 该日不判为交易日", e)
        return False


def expected_latest_trading_date(now: datetime | None = None) -> str:
    """期望的 limit_up_events 最新交易日(YYYYMMDD)。

    = 最近一个 **真实交易日**(<= now 当日); 周末/节假日回退到上一交易日,
    并剔除日历把"补班周末"记为交易日的偏差。
    """
    from src.core.trading_calendar import prev_trading_day, trading_day_anchor

    dt = now or datetime.now(_CST)
    d = dt.date() if isinstance(dt, datetime) else dt
    d = trading_day_anchor(d)          # 当日非交易日 → 回退上一交易日
    # 日历把补班周末记为交易日 → 逐级回退到真正的工作日
    guard = 0
    while d.weekday() >= 5 and guard < 20:
        d = prev_trading_day(d)
        guard += 1
    return d.strftime("%Y%m%d")


def missing_trading_days(latest: str | None, expected: str | None) -> list[str]:
    """(latest, expected] 区间内的真实交易日(升序)。latest/expected 为 YYYYMMDD。"""
    try:
        lo = datetime.strptime(str(latest), "%Y%m%d").date()
        hi = datetime.strptime(str(expected), "%Y%m%d").date()
    except (TypeError, ValueError):
        return []
    if hi <= lo:
        return []
    out: list[str] = []
    cur = lo + timedelta(days=1)
    while cur <= hi:
        if _is_real_trading_day(cur):
            out.append(cur.strftime("%Y%m%d"))
        cur += timedelta(days=1)
    return out


# ── 数据读 ──────────────────────────────────────────────────────────────────

def latest_event_date(db=None) -> str | None:
    """limit_up_events 全表最新交易日(YYYYMMDD); 表空/异常 → None(不猜)。"""
    from sqlalchemy import text

    owns = db is None
    if owns:
        from src.db.session import SessionLocal

        db = SessionLocal()
    try:
        row = db.execute(text(f"SELECT MAX(trade_date) FROM {_EVENTS}")).fetchone()
        return str(row[0]) if row and row[0] is not None else None
    finally:
        if owns:
            db.close()


def check(now: datetime | None = None, *, latest_fn=latest_event_date) -> dict:
    """新鲜度自检。返回 {ok, latest, expected, missing, stale}。"""
    expected = expected_latest_trading_date(now)
    latest = latest_fn()
    if latest is None:
        return {
            "ok": False,
            "latest": None,
            "expected": expected,
            "missing": [],
            "stale": True,
            "reason": "limit_up_events 无任何事件(空表不下结论, 需全量回填)",
        }
    missing = missing_trading_days(latest, expected)
    ok = not missing and str(latest) >= expected
    return {"ok": ok, "latest": str(latest), "expected": expected,
            "missing": missing, "stale": not ok}


# ── 补数 ────────────────────────────────────────────────────────────────────

def backfill_missing(days: list[str], symbols: list[str] | None = None,
                     names: dict[str, str] | None = None) -> dict:
    """TQ 直连**只补缺失交易日**到 limit_up_events(幂等, 重跑安全)。

    - 取数窗口按最早缺失日往前回看, 保证缺失日的 prev_close 可算;
    - 单股 TQ 取数失败显式计入 failed(不静默); 全市场略过非涨停日(只存涨停事件)。
    """
    days = sorted({str(d) for d in (days or []) if d})
    if not days:
        return {"ok": True, "days": [], "saved": 0, "stocks": 0, "failed": 0}

    from src.core.demon_factors import _stock_names, _tq_bars_direct
    from src.core.limit_up_backfill import _extract_events_from_bars, _upsert_rows

    if symbols is None:
        names = names or _stock_names()
        symbols = list(names.keys())
    else:
        names = names or {}

    from src.core.trading_calendar import prev_trading_day

    try:
        start = prev_trading_day(datetime.strptime(days[0], "%Y%m%d").date())
    except Exception:  # noqa: BLE001
        start = datetime.now(_CST).date() - timedelta(days=30)
    lookback = max(20, (date.today() - start).days + 10)

    want = set(days)
    saved = failed = 0
    for sym in symbols:
        try:
            bars = _tq_bars_direct(sym, lookback, raise_on_error=True)
        except Exception as e:  # noqa: BLE001
            failed += 1
            logger.warning("ladder-freshness 补数 %s TQ 取数失败: %s", sym, e)
            continue
        if not bars or len(bars) < 2:
            continue
        try:
            rows = [
                r for r in _extract_events_from_bars(sym, names.get(sym, ""), bars)
                if str(r["trade_date"]) in want
            ]
            if rows:
                saved += _upsert_rows(rows)
        except Exception as e:  # noqa: BLE001
            failed += 1
            logger.warning("ladder-freshness 补数 %s 写入失败: %s", sym, e)

    return {"ok": failed == 0, "days": days, "saved": saved,
            "stocks": len(symbols), "failed": failed}


# ── 自检 + 自愈 ─────────────────────────────────────────────────────────────

def guard(now: datetime | None = None, *, latest_fn=latest_event_date,
          backfill_fn=None) -> dict:
    """自检 → 缺则补数 → 复检。返回诚实作业态(``ok`` False = 仍缺, 判 failed)。

    幂等: 无缺口时不触发任何补数(纯读)。
    """
    chk = check(now, latest_fn=latest_fn)
    if chk["ok"]:
        return {**chk, "latest_before": chk["latest"], "backfilled": 0,
                "backfill_failed": 0, "recovered": True}

    bf = backfill_fn or backfill_missing
    try:
        res = bf(chk["missing"]) if chk["missing"] else {"ok": False, "saved": 0, "failed": 0}
    except Exception as e:  # noqa: BLE001
        logger.exception("ladder-freshness 补数异常: %s", e)
        res = {"ok": False, "saved": 0, "failed": 0, "error": str(e)}

    chk2 = check(now, latest_fn=latest_fn)
    return {
        **chk2,
        "latest_before": chk["latest"],
        "backfilled": int(res.get("saved") or 0),
        "backfill_failed": int(res.get("failed") or 0),
        "recovered": chk2["ok"],
        "reason": None if chk2["ok"] else (
            f"连板梯队仍缺 {len(chk2['missing'])} 个交易日 "
            f"(最新 {chk2['latest']} < 期望 {chk2['expected']}), 补数 {'成功' if res.get('ok') else '未完成'}"
        ),
    }


def run_ladder_freshness_job(now: datetime | None = None) -> dict:
    """cron 入口(每日): 自检 + 自愈, 走作业框架诚实性(``ok=False → failed``)。

    非交易日也会跑 —— 这正是"周末发现断档立即自愈"的路径(期望日 = 上一交易日)。
    """
    from src.core.jobs import jobs

    job_id, is_new = jobs.create(JOB_KIND, JOB_LABEL)
    if not is_new:
        return {"ok": True, "skipped": "in_flight", "job_id": job_id,
                "note": "上一轮自检仍在跑, 跳过本轮"}
    try:
        out = guard(now)
    except Exception as e:  # noqa: BLE001
        jobs.fail(job_id, str(e))
        logger.exception("连板梯队自检作业异常: %s", e)
        return {"ok": False, "error": str(e), "job_id": job_id}
    out["job_id"] = job_id
    jobs.finish(job_id, out, context="连板梯队自检: ")
    return out
