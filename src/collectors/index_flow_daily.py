"""指数盘面资金落库（2026-11-03）。

盘面分析要的不是"某只票的主力净额"，而是**八个宽基指数的涨跌幅 + 成交额 + 四档资金结构 +
涨跌家数**在同一张表里可横向比较、且能看历史分位。`get_exday_data` 对指数只给当日 1 行，
所以必须自己按日累积（同 `exday_daily` 的理由）。

调度（`src/bootstrap/startup.py`）：
- 盘中：每 2 分钟（9:30–11:35、12:55–15:05），把当日累计值刷新进库；
- 收盘：15:42 定稿一次（此时 exday 已是全天累计）。

写库键 `(trade_date, symbol)`，幂等 upsert —— 盘中反复刷同一天只更新不新增。
"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime, time as dtime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from src.core.index_codes import BOARD_ORDER

logger = logging.getLogger(__name__)

TZ = ZoneInfo("Asia/Shanghai")
MARKET = "CN"
SOURCE = "tq"

# 交易时段（含尾盘留 5 分钟给 TQ 定稿）
_WINDOWS = ((dtime(9, 30), dtime(11, 35)), (dtime(12, 55), dtime(15, 5)))


def now_cn() -> datetime:
    return datetime.now(TZ)


def _today() -> str:
    return now_cn().strftime("%Y%m%d")


def in_session(ts: datetime | None = None) -> bool:
    t = (ts or now_cn()).time()
    return any(a <= t <= b for a, b in _WINDOWS)


def is_market_day(d: date | None = None) -> bool:
    """交易日判定走统一日历（禁 weekday 近似）。"""
    try:
        from src.core.trading_calendar import is_trading_day

        return bool(is_trading_day(d or now_cn().date()))
    except Exception as exc:  # noqa: BLE001
        logger.warning("index_flow: 交易日判定异常，按非交易日处理: %r", exc)
        return False


def targets() -> list[str]:
    return list(BOARD_ORDER)


def upsert_rows(db: Session, rows: list[dict[str, Any]]) -> int:
    """按 (trade_date, symbol) 幂等写；返回写入条数。"""
    if not rows:
        return 0
    from src.db.models import IndexFlowDaily

    days = {r["trade_date"] for r in rows}
    existing: dict[tuple[str, str], Any] = {}
    try:
        for obj in db.query(IndexFlowDaily).filter(IndexFlowDaily.trade_date.in_(tuple(days))).all():
            existing[(obj.trade_date, obj.symbol)] = obj
    except Exception as exc:  # noqa: BLE001 - 迁移没跑时整批跳过而不是炸
        logger.warning("index_flow_daily 读取失败(迁移未执行?): %s", exc)
        return 0

    written = 0
    for r in rows:
        key = (r["trade_date"], r["symbol"])
        obj = existing.get(key)
        try:
            if obj is None:
                db.add(IndexFlowDaily(**r))
                existing[key] = r
            else:
                for k, v in r.items():
                    setattr(obj, k, v)
            written += 1
        except Exception as exc:  # noqa: BLE001 - 单条失败不毁整批
            logger.debug("index_flow 写入失败 %s: %s", r.get("symbol"), exc)
    db.commit()
    return written


_ROW_FIELDS = (
    "trade_date", "symbol", "name", "close", "pct_chg", "amount", "volume_lots",
    "open", "high", "low", "up_home", "down_home", "outside", "inside",
    "xl_net", "lg_net", "md_net", "sm_net", "main_net", "net_all",
    "active_buy", "active_sell", "caliber", "quality", "reason", "source",
)


def to_row(item: dict[str, Any], trade_date: str) -> dict[str, Any] | None:
    """fetch 结果 → 落库行；不可用返回 None（不补 0）。"""
    if not item.get("available"):
        return None
    td = str(item.get("trade_date") or trade_date or "").strip()
    if len(td) != 8 or not td.isdigit():
        td = trade_date
    row = {k: item.get(k) for k in _ROW_FIELDS}
    row["trade_date"] = td
    row["symbol"] = item["symbol"]
    row["source"] = SOURCE
    return row


def collect_once(db: Session | None = None, symbols: list[str] | None = None) -> dict[str, Any]:
    """取一次全部指数并落库；返回统计（供日志/接口复用）。"""
    from src.core import index_flow

    syms = symbols or targets()
    t0 = time.time()
    items = index_flow.fetch_many(syms)
    rows = [r for r in (to_row(it, _today()) for it in items) if r]
    written = 0
    if db is not None and rows:
        written = upsert_rows(db, rows)
    failed = [it["symbol"] for it in items if not it.get("available")]
    suspect = [it["symbol"] for it in items if it.get("available") and it.get("quality") != "ok"]
    stats = {
        "scanned": len(syms),
        "written": written,
        "failed": failed,
        "suspect": suspect,
        "elapsed_s": round(time.time() - t0, 2),
        "items": items,
    }
    if failed:
        logger.warning("index_flow: %d/%d 个指数无数据: %s", len(failed), len(syms), failed)
    return stats


def run_intraday() -> dict[str, Any]:
    """盘中刷新（每 2 分钟）：非交易日/非交易时段直接跳过。"""
    if not is_market_day() or not in_session():
        return {"skipped": "非交易时段"}
    from src.db.session import SessionLocal

    db = SessionLocal()
    try:
        return collect_once(db)
    finally:
        db.close()


def run_eod() -> dict[str, Any]:
    """收盘定稿（15:42）。"""
    if not is_market_day():
        return {"skipped": "非交易日"}
    from src.db.session import SessionLocal

    db = SessionLocal()
    try:
        stats = collect_once(db)
        logger.info(
            "index_flow 收盘定稿: 写入 %s 条, 耗时 %ss", stats["written"], stats["elapsed_s"]
        )
        return stats
    finally:
        db.close()
