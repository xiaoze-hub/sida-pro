# -*- coding: utf-8 -*-
"""TQ `more_info` 104 字段收盘定稿落库(2026-10-18 P0-b, 方案 §7.2①)。

问题
----
`get_more_info` 是**实时会话值**: 盘后/周末一律回 0。此前只有 8 个 L2 字段收盘落
`l2_fund_snapshots`, 而封单额/封成比/委比/连板天数/竞价买入额这些**复盘核心字段**
只活在会话里 —— 既不能回测, 盘后请求也没有历史可回退(方案 §4 #5, 判定 C/D)。

本模块
------
- 写: 交易日 15:10 对全市场**分批**取数(每批 `BATCH` 只), 幂等 upsert 到
  `more_info_daily`(唯一键 trade_date+symbol+market); 整行 104 字段原样进 `raw`;
- 读: `latest()` 取最近交易日行, 供 `/api/quotes/{symbol}/more-info` 在非交易时段
  回退(TQ 回 0 时不再只能显式"无数据");
- 保留: 永久(5400 行/日, 压缩后 ≈0.35 GB/年)。

诚实边界
--------
- 全部字段为 0/None 的标的**不落库** —— 那不是"收盘值", 是"会话已结束";
- 单批失败不拖垮全轮, 但整轮 0 行 → `ok=False`(作业判 failed, 断链可见);
- 派生列只增不改; 需要重算口径时按 `source` 过滤 + `raw` 重放, 不改历史行。
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Callable, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.db.models import MoreInfoDaily
from src.db.session import SessionLocal

logger = logging.getLogger(__name__)

#: 每批取多少只(md_more_info 单次 RPC 的 symbol 列表)。200 是实测不触发超时的量级。
BATCH = 200
#: 批间小睡(秒), 防止 15:10 集中打爆 TQ。
BATCH_SLEEP_S = 0.05
JOB_KIND = "more_info_daily"
JOB_LABEL = "TQ more_info 收盘定稿"


#: 落库列 ← md_more_info 返回键(逐条对齐 marketdata.types.MoreInfo)。
FIELD_MAP: dict[str, str] = {
    "turnover_rate": "turnover_rate",
    "volume_ratio": "volume_ratio",
    "commission_ratio": "commission_ratio",
    "total_mv": "total_market_value",
    "circ_mv": "circulating_market_value",
    "chg_pct": "change_pct",
    "chg_pct_5d": "change_pct_5d",
    "chg_pct_20d": "change_pct_20d",
    "chg_pct_ytd": "change_pct_ytd",
    "limit_up_amount": "limit_up_amount",
    "limit_up_ratio": "limit_up_ratio",
    "open_amount": "open_amount",
    "open_limit_buy": "open_limit_buy",
    "consecutive_limit_days": "consecutive_limit_days",
    "consecutive_up_days": "consecutive_up_days",
    "pe_dynamic": "pe_dynamic",
    "pe_ttm": "pe_ttm",
    "pb": "pb",
    "dividend_yield": "dividend_yield",
    "beta": "beta",
    "l2_tick_num": "l2_tick_num",
    "l2_order_num": "l2_order_num",
    "total_buy_vol": "total_buy_vol",
    "total_sell_vol": "total_sell_vol",
    "cancel_buy": "cancel_buy",
    "cancel_sell": "cancel_sell",
    "zjl": "zjl",
    "zjl_hb": "zjl_hb",
}
_INT_COLS = ("consecutive_limit_days", "consecutive_up_days")


def _num(v) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _int(v) -> int | None:
    f = _num(v)
    return int(f) if f is not None else None


def has_live_values(row: dict) -> bool:
    """任一映射字段为非 0 有限数值 → True。

    为什么以"非 0"为判据: 收盘后 TQ 会话结束, **所有**字段一起回 0(含总市值/PE
    这些本不可能为 0 的静态量)。它们同时为 0 只能是"不是数据", 不是"收盘值"。
    """
    for col in FIELD_MAP:
        n = _num(row.get(FIELD_MAP[col]))
        if n is not None and n != 0.0:
            return True
    # 兜底: raw 里只要有一个非 0 数值也算(103/104 字段未来新增时的保险)
    raw = row.get("raw")
    if isinstance(raw, dict):
        for v in raw.values():
            n = _num(v)
            if n is not None and n != 0.0:
                return True
    return False


def row_to_columns(row: dict) -> dict:
    """一条 md_more_info 返回 → `more_info_daily` 列(不含 trade_date/symbol)。"""
    cols: dict = {}
    for col, key in FIELD_MAP.items():
        val = row.get(key)
        cols[col] = _int(val) if col in _INT_COLS else _num(val)
    raw = row.get("raw") if isinstance(row.get("raw"), dict) else {}
    # OpenZAF 只在 raw(104 字段)里; 取不到就 NULL, 不从涨幅推
    cols["open_pct"] = _num(raw.get("OpenZAF")) if raw else None
    cols["raw"] = raw or None
    return cols


# --------------------------------------------------------------------------- #
# 写
# --------------------------------------------------------------------------- #
def upsert_rows(
    rows: Iterable[dict],
    *,
    trade_date: str,
    market: str = "CN",
    session: Session | None = None,
    session_factory=None,
    captured_at: float | None = None,
    allow_zero: bool = False,
) -> int:
    """幂等写入(唯一键 trade_date+symbol+market), 返回写入行数。

    `allow_zero=False`(默认)时跳过全 0/全空行 —— 零值不冒充收盘值。
    """
    ts = float(captured_at if captured_at is not None else time.time())
    items = []
    for r in rows or []:
        sym = str(r.get("symbol") or "").strip()
        if not sym:
            continue
        if not allow_zero and not has_live_values(r):
            continue
        items.append((sym, r))
    if not items:
        return 0

    def _run(sess: Session) -> int:
        n = 0
        for sym, r in items:
            values = {
                "trade_date": trade_date,
                "symbol": sym,
                "market": (r.get("market") or market or "CN").upper(),
                **row_to_columns(r),
                "source": "tq_more_info",
                "captured_at": ts,
            }
            row = sess.scalar(
                select(MoreInfoDaily).where(
                    MoreInfoDaily.trade_date == trade_date,
                    MoreInfoDaily.symbol == sym,
                    MoreInfoDaily.market == values["market"],
                )
            )
            if row is None:
                sess.add(MoreInfoDaily(**values))
            else:
                for k, v in values.items():
                    setattr(row, k, v)
            n += 1
        if n:
            sess.commit()
        return n

    if session is not None:
        return _run(session)
    with (session_factory or SessionLocal)() as sess:
        return _run(sess)


# --------------------------------------------------------------------------- #
# 读
# --------------------------------------------------------------------------- #
def _row_to_dict(r: MoreInfoDaily) -> dict:
    out = {col: getattr(r, col) for col in FIELD_MAP}
    out.update({
        "trade_date": r.trade_date,
        "symbol": r.symbol,
        "market": r.market,
        "open_pct": r.open_pct,
        "source": r.source,
        "captured_at": r.captured_at,
        "raw": r.raw or {},
    })
    return out


def latest(
    symbol: str,
    market: str = "CN",
    *,
    session: Session | None = None,
    session_factory=None,
) -> dict | None:
    """该标的最新中国交易日定稿行; 无行 → None(调用方显式 available:false)。"""

    def _run(sess: Session) -> dict | None:
        r = sess.scalar(
            select(MoreInfoDaily)
            .where(
                MoreInfoDaily.symbol == symbol,
                MoreInfoDaily.market == (market or "CN").upper(),
            )
            .order_by(MoreInfoDaily.trade_date.desc())
            .limit(1)
        )
        if r is None:
            return None
        return _row_to_dict(r)

    if session is not None:
        return _run(session)
    try:
        with (session_factory or SessionLocal)() as sess:
            return _run(sess)
    except Exception as e:  # noqa: BLE001 - 表未迁移/库不可用 → 交由调用方降级
        logger.warning("more_info_daily 读库失败[%s]: %s", symbol, e)
        return None


def history(
    symbol: str,
    market: str = "CN",
    *,
    days: int = 60,
    session: Session | None = None,
    session_factory=None,
) -> list[dict]:
    """最近 N 个交易日的定稿行(升序), 供回测/趋势。不回源。"""

    def _run(sess: Session) -> list[dict]:
        rows = sess.scalars(
            select(MoreInfoDaily)
            .where(
                MoreInfoDaily.symbol == symbol,
                MoreInfoDaily.market == (market or "CN").upper(),
            )
            .order_by(MoreInfoDaily.trade_date.desc())
            .limit(max(1, int(days)))
        ).all()
        return [_row_to_dict(r) for r in reversed(list(rows))]

    if session is not None:
        return _run(session)
    with (session_factory or SessionLocal)() as sess:
        return _run(sess)


# --------------------------------------------------------------------------- #
# 采集 + 作业
# --------------------------------------------------------------------------- #
def capture_universe(
    symbols: list[str] | None = None,
    *,
    market: str = "CN",
    max_stocks: int = 0,
    now: datetime | None = None,
    fetcher: Callable[..., list[dict]] | None = None,
    batch: int = BATCH,
    session_factory=None,
) -> dict:
    """全市场(或指定池)分批取 more_info → 落库。返回统计(不抛)。"""
    from src.core.trade_phase import cn_now

    trade_date = cn_now(now).date().isoformat()
    if symbols is None:
        try:
            from src.collectors.stock_list import get_stock_list

            symbols = [
                s["symbol"] for s in get_stock_list()
                if s.get("symbol") and len(str(s["symbol"])) == 6
            ]
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "reason": f"股票名单加载失败: {e}", "rows": 0}
    symbols = [s for s in symbols if s]
    if max_stocks > 0:
        symbols = symbols[:max_stocks]

    if fetcher is None:
        from src.core.marketdata_client import md_more_info as fetcher  # type: ignore[assignment]

    captured = skipped = failed = 0
    errors: list[str] = []
    for i in range(0, len(symbols), max(1, int(batch))):
        part = symbols[i : i + max(1, int(batch))]
        try:
            rows = fetcher(part, market) or []
        except Exception as e:  # noqa: BLE001 - 单批失败不拖垮全轮
            failed += len(part)
            errors.append(f"批次 {i // batch + 1}: {type(e).__name__}: {e}")
            logger.warning("more_info 批次失败(%d 只): %s", len(part), e)
            continue
        live = [r for r in rows if r.get("symbol") and has_live_values(r)]
        skipped += max(0, len(rows) - len(live)) + max(0, len(part) - len(rows))
        if not live:
            continue
        try:
            captured += upsert_rows(
                live, trade_date=trade_date, market=market,
                session_factory=session_factory,
            )
        except Exception as e:  # noqa: BLE001
            failed += len(live)
            errors.append(f"批次 {i // batch + 1} 落库: {type(e).__name__}: {e}")
            logger.warning("more_info 批次落库失败: %s", e)
        if BATCH_SLEEP_S > 0:
            time.sleep(BATCH_SLEEP_S)

    ok = captured > 0
    return {
        "ok": ok,
        "trade_date": trade_date,
        "symbols": len(symbols),
        "captured": captured,
        "skipped_zero": skipped,
        "failed": failed,
        "errors": errors[:5],
        "reason": None if ok else (
            errors[0] if errors else "全部标的会话值全 0(非交易时段或 TQ 未连接), 不落库"
        ),
    }


def run_more_info_daily_job(
    *,
    now: datetime | None = None,
    symbols: list[str] | None = None,
    fetcher: Callable[..., list[dict]] | None = None,
) -> dict:
    """cron 入口(交易日 15:10): 全市场 more_info 收盘定稿。"""
    from src.core.jobs import jobs
    from src.core.trade_phase import cn_now
    from src.core.trading_calendar import is_trading_day

    if not is_trading_day(cn_now(now).date()):
        return {"ok": True, "skipped": "non_trading_day", "captured": 0}

    job_id, is_new = jobs.create(JOB_KIND, JOB_LABEL)
    if not is_new:
        return {"ok": True, "skipped": "in_flight", "job_id": job_id}
    try:
        out = capture_universe(symbols, now=now, fetcher=fetcher)
    except Exception as e:  # noqa: BLE001
        jobs.fail(job_id, str(e))
        logger.exception("more_info 收盘定稿作业异常: %s", e)
        return {"ok": False, "error": str(e), "job_id": job_id}
    out["job_id"] = job_id
    jobs.finish(job_id, out, context="more_info收盘定稿: ")
    return out
