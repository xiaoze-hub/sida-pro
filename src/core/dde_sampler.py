"""逐 N 分钟 DDE 大单净流入采样落库(P3 补差 A, 2026-10-10)。

现有链(thsdk `get_dde_flow` 批量 / `get_main_flow_official` + TQ `get_more_info`)只给
**当日快照**, `minute_breakthrough.fetch_dde_series` 只能构造单点序列 → 分时突破「突」的
『DDE大单持续流入』条件无法判定(生产恒显式降级)。本模块在**盘中**按 N 分钟把全市场
DDE 主力净流入落成**序列表** `dde_minute_flow`, `fetch_dde_series` 改读库构造真序列。

诚实口径(硬约束):
  - **非交易时段**(非交易日 / 非连续竞价时段) **不落任何行**, 返回显式无数据
    (`ok=True, skipped=..., note=...`), 绝不写占位/0;
  - 采样**幂等**: (trade_date, market, symbol, sample_ts) upsert, 重跑同一样本不重复、不双算;
  - 采样本身失败(数据源全空 / 异常) → `ok=False`(作业框架据此判 failed, 不假装成功)。

采样范围 = 全市场沪深 A(与 `dark_fund_scan` 同一 thsdk DDE 批量链); 节拍来自
`thresholds.value("minute_dde_sample_min")`(默认 5 分钟, env `SIDA_THRESHOLD_MINUTE_DDE_SAMPLE_MIN`)。
"""

from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_CST = ZoneInfo("Asia/Shanghai")

JOB_KIND = "dde_intraday_sample"
JOB_LABEL = "DDE大单逐分钟采样"


def _now_cst(now: datetime | None = None) -> datetime:
    if now is None:
        return datetime.now(_CST)
    if now.tzinfo is None:
        return now.replace(tzinfo=_CST)
    return now.astimezone(_CST)


def sample_cadence_min() -> int:
    """采样节拍(分钟, 至少 1)。走 thresholds 配置层(env 可覆盖)。"""
    from src.core import thresholds

    try:
        return max(1, int(thresholds.value("minute_dde_sample_min")))
    except Exception:  # noqa: BLE001 — 配置层异常不拖垮调度
        return 5


def _session_ok(now: datetime | None = None) -> tuple[bool, str]:
    """(是否可采样, 跳过原因)。交易日 + 连续竞价时段统一走 trading_calendar。"""
    from src.core.trading_calendar import (
        TradingCalendarError,
        is_trading_day,
        is_trading_session,
    )

    n = _now_cst(now)
    try:
        if not is_trading_day(n.date()):
            return False, "non_trading_day"
    except TradingCalendarError:
        # 日历未覆盖年份 → 保守视为不可采样(显式记原因, 不推测)
        return False, "calendar_uncovered"
    if not is_trading_session(n):
        return False, "non_trading_time"
    return True, ""


def _prev_cum_map(db, trade_date: str, market: str, sample_ts: str) -> dict[str, float]:
    """上一采样(严格早于 sample_ts)的 symbol → 当日累计主力净流入(万元)。

    幂等关键: 排除当前 sample_ts 自身, 重跑同一样本时 delta 重算结果一致(不双算)。
    """
    from sqlalchemy import text

    out: dict[str, float] = {}
    try:
        rows = db.execute(
            text(
                "SELECT symbol, sample_ts, cum_net_wan FROM dde_minute_flow "
                "WHERE trade_date = :d AND market = :m AND sample_ts < :ts "
                "ORDER BY sample_ts"
            ),
            {"d": trade_date, "m": market, "ts": sample_ts},
        ).fetchall()
    except Exception as e:  # noqa: BLE001 — 表未建/查询失败 → 无上一样本(首个采样语义)
        logger.debug("dde_sampler 读上一样本失败: %s", e)
        return out
    for r in rows:
        sym, cum = r[0], r[2]
        if cum is None:
            continue
        out[str(sym)] = float(cum)  # ORDER BY sample_ts 升序 → 保留最新
    return out


def sample_dde_once(
    *,
    db=None,
    l2=None,
    now: datetime | None = None,
    markets=None,
    batch_size=None,
    sample_ts: str | None = None,
) -> dict:
    """落一次全市场 DDE 采样 → 序列表。返回摘要(失败不抛, 以 ok=False 表达)。

    db 缺省自开 SessionLocal(供 cron); 测试可注入。
    """
    from src.core.dark_fund_scan import DEFAULT_BATCH, DEFAULT_MARKETS, scan_dde_universe

    markets = markets or DEFAULT_MARKETS
    batch_size = batch_size or DEFAULT_BATCH

    uni = scan_dde_universe(markets=tuple(markets), batch_size=batch_size, l2=l2)
    rows = uni.get("rows") or []
    if not rows:
        return {
            "ok": False,
            "reason": "DDE 全市场扫描返回空(数据源不可用或未登录)",
            "universe": uni.get("universe", 0),
        }

    n = _now_cst(now)
    trade_date = n.strftime("%Y-%m-%d")
    ts = sample_ts or n.strftime("%H:%M")

    owns_db = db is None
    if owns_db:
        from src.db.session import SessionLocal

        db = SessionLocal()
    try:
        prev = _prev_cum_map(db, trade_date, "CN", ts)
        from sqlalchemy import text

        from src.db.dialect import upsert_sql

        stmt = text(
            upsert_sql(
                "dde_minute_flow",
                ["trade_date", "market", "symbol", "sample_ts",
                 "cum_net_wan", "delta_net_wan", "main_net_vol",
                 "total_amount_wan", "source"],
                ["trade_date", "market", "symbol", "sample_ts"],
                ["cum_net_wan", "delta_net_wan", "main_net_vol",
                 "total_amount_wan", "source"],
            )
        )
        written = 0
        for r in rows:
            cum = r.get("main_net_wan")
            if cum is None:
                continue
            base = prev.get(str(r.get("symbol")))
            delta = float(cum) - base if base is not None else float(cum)
            db.execute(
                stmt,
                {
                    "trade_date": trade_date,
                    "market": "CN",
                    "symbol": str(r.get("symbol")),
                    "sample_ts": ts,
                    "cum_net_wan": float(cum),
                    "delta_net_wan": round(delta, 2),
                    "main_net_vol": r.get("main_net_vol"),
                    "total_amount_wan": r.get("total_amount_wan"),
                    "source": "thsdk_dde",
                },
            )
            written += 1
        db.commit()
    except Exception as e:  # noqa: BLE001
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        logger.exception("DDE 采样落库失败: %s", e)
        return {"ok": False, "reason": f"落库失败: {e}"}
    finally:
        if owns_db:
            db.close()

    return {
        "ok": True,
        "trade_date": trade_date,
        "sample_ts": ts,
        "universe": uni.get("universe", 0),
        "computed": uni.get("computed", 0),
        "written": written,
        "sample_min": sample_cadence_min(),
    }


def run_dde_sample_job(*, now: datetime | None = None, l2=None) -> dict:
    """盘中 cron 入口: 采样 + 落库, 走作业框架诚实性(ok=False → failed)。

    - 非交易时段: 显式无数据, **不建作业行**(避免节假日/盘外污染 app_jobs);
    - 交易时段: 建 `dde_intraday_sample` 作业 → `jobs.finish` 按返回体诚实落终态
      (数据源全空/异常 → failed, 不假装成功)。
    """
    ok, reason = _session_ok(now)
    if not ok:
        return {
            "ok": True,
            "skipped": reason,
            "note": "非交易时段, DDE 大单采样显式无数据(不落任何行)",
        }

    from src.core.jobs import jobs

    job_id, is_new = jobs.create(JOB_KIND, JOB_LABEL)
    if not is_new:
        return {"ok": True, "skipped": "in_flight", "job_id": job_id,
                "note": "上一轮采样仍在跑, 跳过本轮"}

    try:
        out = sample_dde_once(now=now, l2=l2)
    except Exception as e:  # noqa: BLE001
        jobs.fail(job_id, str(e))
        logger.exception("DDE 采样作业异常: %s", e)
        return {"ok": False, "error": str(e), "job_id": job_id}

    out["job_id"] = job_id
    jobs.finish(job_id, out, context="DDE采样: ")
    return out
