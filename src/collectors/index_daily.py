# -*- coding: utf-8 -*-
"""指数日K落库(2026-10-18 P0-b, 方案 §7.2⑤ / §8 批次 2)。

问题
----
`klines_minute.py` 明确"指数不入 klines(裸码撞号)", 于是 `/api/market/indices` 的
spark(近 20 日收盘)只能靠 60s 进程内 + 30min 缓存, **每 30 分钟重付一轮 6×指数K
(≈4s)**, 多 worker 各付一份、重启即失, 且历史不可查(方案 §5.3 / §4 #3)。

本模块
------
- 写: 交易日 15:30 cron 拉 6 个主要指数的日K(默认回填 800 天), 幂等 upsert 到
  `index_daily`(唯一键 trade_date+code);
- 读: `load_recent_closes(code, days)` 供 spark 直接读库(零跨网), `history()` 供
  大盘择时回测;
- 首次运行自动回填(表空 → 拉 `BACKFILL_DAYS`)。

诚实边界
--------
- 上游无返回/某指数未映射(如美股指) → 不写行, 显式计数; 不补 0;
- `prev_close` 取同批序列的前一根收盘; 序列首根无前值时写 NULL(不猜);
- 指数 K 线本就没有成交额字段 → `amount` 留 NULL, 不从成交量估算。
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.db.models import IndexDaily
from src.db.session import SessionLocal

logger = logging.getLogger(__name__)

#: 回填窗口(自然日)。800 天 ≈ 3 年, 覆盖一轮完整牛熊 + 择时回测起步样本。
BACKFILL_DAYS = 800
JOB_KIND = "index_daily"
JOB_LABEL = "指数日K落库"


#: 与 `src.web.api.market.MARKET_INDICES` 的 symbol/market 保持一致(单测校验)。
#: 这里不 import web 层(核心层不依赖 API), 保持单向依赖。
INDEX_UNIVERSE: tuple[dict, ...] = (
    {"symbol": "000001", "name": "上证指数", "market": "CN"},
    {"symbol": "399001", "name": "深证成指", "market": "CN"},
    {"symbol": "399006", "name": "创业板指", "market": "CN"},
    {"symbol": "HSI", "name": "恒生指数", "market": "HK"},
    {"symbol": "IXIC", "name": "纳斯达克", "market": "US"},
    {"symbol": "DJI", "name": "道琼斯", "market": "US"},
)


def _bar_dict(bar, prev_close: float | None) -> dict | None:
    """KlineData / dict → 入库行。缺日期或收盘 → None(不落半行)。"""
    if isinstance(bar, dict):
        d = bar.get("date") or bar.get("trade_date")
        o, h, low, c = bar.get("open"), bar.get("high"), bar.get("low"), bar.get("close")
        vol = bar.get("volume")
    else:
        d = getattr(bar, "date", None)
        o, h, low, c = (getattr(bar, k, None) for k in ("open", "high", "low", "close"))
        vol = getattr(bar, "volume", None)
    if not d or c is None:
        return None
    day = str(d)[:10]
    if len(day) == 8 and day.isdigit():        # YYYYMMDD → YYYY-MM-DD
        day = f"{day[:4]}-{day[4:6]}-{day[6:8]}"
    try:
        c = float(c)
    except (TypeError, ValueError):
        return None
    return {
        "trade_date": day,
        "open": float(o) if o is not None else None,
        "high": float(h) if h is not None else None,
        "low": float(low) if low is not None else None,
        "close": c,
        "prev_close": prev_close,
        "volume": float(vol) if vol is not None else None,
        "amount": None,
    }


def upsert_bars(
    code: str,
    bars: Iterable,
    *,
    market: str = "CN",
    name: str | None = None,
    source: str = "marketdata",
    session: Session | None = None,
    session_factory=None,
) -> int:
    """幂等写入指数日K(唯一键 trade_date+code)。返回写入行数。"""
    seq: list[dict] = []
    prev: float | None = None
    for bar in bars or []:
        row = _bar_dict(bar, prev)
        if row is None:
            continue
        seq.append(row)
        prev = row["close"]
    if not seq:
        return 0

    def _run(sess: Session) -> int:
        n = 0
        for r in seq:
            values = {
                **r,
                "code": code,
                "market": (market or "CN").upper(),
                "name": name,
                "source": source,
                "captured_at": datetime.now().timestamp(),
            }
            row = sess.scalar(
                select(IndexDaily).where(
                    IndexDaily.trade_date == r["trade_date"],
                    IndexDaily.code == code,
                )
            )
            if row is None:
                sess.add(IndexDaily(**values))
            else:
                old_prev = row.prev_close
                for k, v in values.items():
                    setattr(row, k, v)
                # 已有行的 prev_close 不要被"本次序列首根"的 None 冲掉
                if r["prev_close"] is None and old_prev is not None:
                    row.prev_close = old_prev
            n += 1
        if n:
            sess.commit()
        return n

    if session is not None:
        return _run(session)
    with (session_factory or SessionLocal)() as sess:
        return _run(sess)


def load_recent_closes(
    code: str,
    *,
    days: int = 20,
    market: str | None = None,
    session: Session | None = None,
    session_factory=None,
) -> list[float]:
    """最近 N 个交易日收盘(升序)。无行 → [](调用方回落取数, 不编造)。"""

    def _run(sess: Session) -> list[float]:
        stmt = select(IndexDaily).where(IndexDaily.code == code)
        if market:
            stmt = stmt.where(IndexDaily.market == market.upper())
        rows = sess.scalars(
            stmt.order_by(IndexDaily.trade_date.desc()).limit(max(1, int(days)))
        ).all()
        return [float(r.close) for r in reversed(list(rows)) if r.close is not None]

    if session is not None:
        return _run(session)
    try:
        with (session_factory or SessionLocal)() as sess:
            return _run(sess)
    except Exception as e:  # noqa: BLE001 - 表未迁移/库不可用 → 回落
        logger.warning("index_daily 读库失败[%s]: %s", code, e)
        return []


def history(
    code: str,
    *,
    days: int = 250,
    market: str | None = None,
    session: Session | None = None,
    session_factory=None,
) -> list[dict]:
    """最近 N 根日K(升序), 供大盘择时回测。不回源。"""

    def _run(sess: Session) -> list[dict]:
        stmt = select(IndexDaily).where(IndexDaily.code == code)
        if market:
            stmt = stmt.where(IndexDaily.market == market.upper())
        rows = sess.scalars(
            stmt.order_by(IndexDaily.trade_date.desc()).limit(max(1, int(days)))
        ).all()
        return [
            {
                "trade_date": r.trade_date,
                "open": r.open, "high": r.high, "low": r.low, "close": r.close,
                "prev_close": r.prev_close, "volume": r.volume, "amount": r.amount,
            }
            for r in reversed(list(rows))
        ]

    if session is not None:
        return _run(session)
    with (session_factory or SessionLocal)() as sess:
        return _run(sess)


def count_rows(session: Session) -> int:
    from sqlalchemy import func

    return int(session.scalar(select(func.count()).select_from(IndexDaily)) or 0)


def backfill(
    *,
    days: int = BACKFILL_DAYS,
    incremental_days: int = 10,
    indices: Iterable[dict] | None = None,
    fetcher=None,
    session_factory=None,
) -> dict:
    """拉取指数日K并落库。返回统计(不抛)。`fetcher(code, market, days)` 可注入。

    首次(该指数库里无行)拉满 `days`(默认 800 天); 之后每日只拉
    `incremental_days`(默认 10, 覆盖长假), 避免每晚把 6×800 根 K 线重写一遍。
    """
    if fetcher is None:
        from src.collectors.kline_collector import get_index_klines
        from src.models.market import MarketCode

        def _fetch(code: str, market: str, n: int, _fn=get_index_klines):
            return _fn(code, MarketCode(market), days=n)

        fetcher = _fetch

    universe = list(indices or INDEX_UNIVERSE)
    written = empty = failed = 0
    details: list[dict] = []
    for idx in universe:
        code, market = idx["symbol"], idx.get("market") or "CN"
        want = days
        try:
            if load_recent_closes(
                code, days=1, market=market, session_factory=session_factory
            ):
                want = min(int(days), max(1, int(incremental_days)))
        except Exception:  # noqa: BLE001 - 判断失败按全量拉(宁慢勿缺)
            want = days
        try:
            bars = fetcher(code, market, want)
        except Exception as e:  # noqa: BLE001 - 单指数失败不影响其余
            failed += 1
            details.append({"code": code, "error": f"{type(e).__name__}: {e}"})
            logger.warning("指数日K取数失败 %s: %s", code, e)
            continue
        if not bars:
            empty += 1
            details.append({"code": code, "error": "上游无返回(未映射/停市)"})
            continue
        try:
            n = upsert_bars(
                code, bars, market=market, name=idx.get("name"),
                session_factory=session_factory,
            )
        except Exception as e:  # noqa: BLE001
            failed += 1
            details.append({"code": code, "error": f"落库失败: {e}"})
            continue
        written += n
        details.append({"code": code, "rows": n})

    ok = written > 0
    return {
        "ok": ok,
        "rows": written,
        "indices": len(universe),
        "empty": empty,
        "failed": failed,
        "details": details,
        "reason": None if ok else "全部指数无可用日K(上游不可用或未映射)",
    }


def run_index_daily_job(*, days: int = BACKFILL_DAYS, indices=None) -> dict:
    """cron 入口(交易日 15:30): 指数日K增量/回填落库。"""
    from src.core.jobs import jobs
    from src.core.trade_phase import cn_now
    from src.core.trading_calendar import is_trading_day

    if not is_trading_day(cn_now().date()):
        return {"ok": True, "skipped": "non_trading_day", "rows": 0}

    job_id, is_new = jobs.create(JOB_KIND, JOB_LABEL)
    if not is_new:
        return {"ok": True, "skipped": "in_flight", "job_id": job_id}
    try:
        out = backfill(days=days, indices=indices)
    except Exception as e:  # noqa: BLE001
        jobs.fail(job_id, str(e))
        logger.exception("指数日K作业异常: %s", e)
        return {"ok": False, "error": str(e), "job_id": job_id}
    out["job_id"] = job_id
    jobs.finish(job_id, out, context="指数日K: ")
    return out
