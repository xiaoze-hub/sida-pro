"""快照行情 1 分钟桶落库(批次2 2/2, 2026-09-10)。

范围: 自选(stocks)∪启用账户持仓(positions join accounts)∪大盘指数 —— 与用户
无关的公共市场数据(设计文档 §4 落库矩阵 #3)。写入者为**后台采集 job**(交易时段
每分钟一次, scheduler 注册), API 层只读。

口径:
- 每分钟一桶: ts = 当分钟 floor('YYYY-MM-DD HH:MM:00', CST), 同桶重拉覆盖(幂等)。
- 个股走 md_quote_rows(与 WS 聚合器同源), 指数走 index_quotes 腾讯原始符号路径
  (sh000001 等, market='IDX', 避免与同号个股撞键)。
- 交易时段守卫: 交易日(交易日历) ∩ 09:15-11:30 / 12:55-15:05; 非时段直接跳过
  (不打源不写库)。失败不抛, 返回 error。
- 保留: 1 年 → agg 日线(治理脚本后续挂; 表按 trade_date 裁剪)。
"""
from __future__ import annotations

import logging
from datetime import datetime, time as _time
from zoneinfo import ZoneInfo

from sqlalchemy import text

logger = logging.getLogger(__name__)

_CST = ZoneInfo("Asia/Shanghai")
_TABLE = "quote_snapshots"
_COLS = (
    "trade_date", "ts", "symbol", "market", "name", "last", "open", "high", "low",
    "prev_close", "change_pct", "volume", "turnover", "turnover_rate",
    "volume_ratio", "circ_mv", "source",
)
# 大盘指数(腾讯原始符号; 既有 report_generator 3 个 + 沪深300/中证500/科创50)
INDEX_SYMBOLS = (
    "sh000001", "sz399001", "sz399006", "sh000300", "sh000905", "sh000688",
)
# 交易时段(CST): 盘前集合竞价起, 收盘后 5 分钟余量
_SESSIONS = (
    (_time(9, 15), _time(11, 30)),
    (_time(12, 55), _time(15, 5)),
)


def _engine():
    from src.db.session import engine

    return engine


def _in_session(now: datetime) -> bool:
    t = now.time()
    return any(lo <= t <= hi for lo, hi in _SESSIONS)


def in_trading_window(now: datetime | None = None) -> bool:
    """交易日(交易日历) ∩ A 股交易时段。日历不可用时退回 weekday 判定(采集器宁多跑不漏跑)。"""
    from datetime import date as _date

    now = now or datetime.now(_CST)
    if not _in_session(now):
        return False
    try:
        from src.core.trading_calendar import is_trading_day

        return is_trading_day(now.date())
    except Exception:  # noqa: BLE001
        return now.weekday() < 5


def collect_market_symbols() -> dict[str, list[str]]:
    """自选∪启用账户持仓(按市场分组)。镜像 quote_stream._collect_watchlist_symbols 的
    标的集合(不 import src/web, KI-039 棘轮: core 禁反向依赖), 但不维护 per-user 缓存。"""
    try:
        from src.db.models import Account, Position, Stock
        from src.db.session import SessionLocal

        db = SessionLocal()
        try:
            groups: dict[str, list[str]] = {}
            for sym, mkt in db.query(Stock.symbol, Stock.market).all():
                mkt = mkt or "CN"
                lst = groups.setdefault(mkt, [])
                if sym and sym not in lst:
                    lst.append(sym)
            for sym, mkt in (
                db.query(Stock.symbol, Stock.market)
                .join(Position, Position.stock_id == Stock.id)
                .join(Account, Account.id == Position.account_id)
                .filter(Account.enabled == True)  # noqa: E712
                .all()
            ):
                mkt = mkt or "CN"
                lst = groups.setdefault(mkt, [])
                if sym and sym not in lst:
                    lst.append(sym)
            return groups
        finally:
            db.close()
    except Exception as e:  # noqa: BLE001
        logger.warning("快照落库标的收集失败: %s", e)
        return {}


def _stock_rows(groups: dict[str, list[str]], now: datetime) -> list[dict]:
    from src.core.marketdata_client import md_quote_rows

    trade_date = now.strftime("%Y%m%d")
    ts = now.replace(second=0, microsecond=0).strftime("%Y-%m-%d %H:%M:00")
    out: list[dict] = []
    for market, symbols in groups.items():
        try:
            rows = md_quote_rows(symbols, market)
        except Exception as e:  # noqa: BLE001
            logger.warning("快照落库批量行情 %s 失败: %s", market, e)
            continue
        for r in rows:
            sym = r.get("symbol")
            if not sym:
                continue
            out.append({
                "trade_date": trade_date,
                "ts": ts,
                "symbol": str(sym),
                "market": market or "CN",
                "name": r.get("name"),
                "last": r.get("current_price"),
                "open": r.get("open_price"),
                "high": r.get("high_price"),
                "low": r.get("low_price"),
                "prev_close": r.get("prev_close"),
                "change_pct": r.get("change_pct"),
                "volume": r.get("volume"),
                "turnover": r.get("turnover"),
                "turnover_rate": r.get("turnover_rate"),
                "volume_ratio": r.get("volume_ratio"),
                "circ_mv": r.get("circulating_market_value"),
                "source": r.get("source") or None,
            })
    return out


def _index_rows(now: datetime) -> list[dict]:
    from src.core.marketdata_client import get_market_data

    trade_date = now.strftime("%Y%m%d")
    ts = now.replace(second=0, microsecond=0).strftime("%Y-%m-%d %H:%M:00")
    try:
        rows = get_market_data().index_quotes(list(INDEX_SYMBOLS))
    except Exception as e:  # noqa: BLE001
        logger.warning("快照落库指数行情失败: %s", e)
        return []
    # index_quotes 返回 symbol=裸码(000001), 无法与个股区分 → 用 tencent 原始符号对位
    by_code = {str(r.get("symbol")): r for r in rows if r.get("symbol")}
    out: list[dict] = []
    for tsym in INDEX_SYMBOLS:
        code = tsym[2:]
        r = by_code.get(code)
        if not r:
            continue
        out.append({
            "trade_date": trade_date,
            "ts": ts,
            "symbol": tsym,
            "market": "IDX",
            "name": r.get("name"),
            "last": r.get("current_price"),
            "open": None,
            "high": None,
            "low": None,
            "prev_close": r.get("prev_close"),
            "change_pct": r.get("change_pct"),
            "volume": r.get("volume"),
            "turnover": r.get("turnover"),
            "turnover_rate": None,
            "volume_ratio": None,
            "circ_mv": None,
            "source": "tencent",
        })
    return out


def persist_quote_snapshots(rows: list[dict]) -> int:
    """幂等写入((trade_date, market, symbol, ts) 已存在则跳过), 返回新增行数。失败不抛。"""
    if not rows:
        return 0
    saved = 0
    try:
        with _engine().begin() as conn:
            key = (rows[0]["trade_date"], rows[0]["ts"])
            existing = {
                (r[0], r[1])
                for r in conn.execute(
                    text(
                        f"SELECT market, symbol FROM {_TABLE}"
                        " WHERE trade_date = :d AND ts = :ts"
                    ),
                    {"d": key[0], "ts": key[1]},
                ).fetchall()
            }
            for r in rows:
                if (r["market"], r["symbol"]) in existing:
                    continue
                cols = ",".join(_COLS)
                binds = ",".join(f":{c}" for c in _COLS)
                conn.execute(text(f"INSERT INTO {_TABLE} ({cols}) VALUES ({binds})"), r)
                saved += 1
    except Exception as e:  # noqa: BLE001
        logger.warning("快照落库写库失败(%s 行): %s", len(rows), e)
        return 0
    return saved


def collect_once(now: datetime | None = None) -> dict:
    """采集入口(交易时段每分钟由 scheduler 调): 标的收集 → 批量行情 → 落库。永不抛。"""
    try:
        now = now or datetime.now(_CST)
        if not in_trading_window(now):
            return {"skipped": "non_trading_time"}
        groups = collect_market_symbols()
        if not groups and not INDEX_SYMBOLS:
            return {"skipped": "no_symbols"}
        rows = _stock_rows(groups, now) + _index_rows(now)
        n = persist_quote_snapshots(rows)
        return {"trade_date": now.strftime("%Y%m%d"), "rows_fetched": len(rows), "rows_saved": n}
    except Exception as e:  # noqa: BLE001
        logger.warning("快照落库采集失败: %s", e)
        return {"error": str(e)}
