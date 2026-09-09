"""涨停事件回填(批次B, 2026-09-06 28号)。

每日盘后(默认 15:30 cron)对全市场 A 股日 K 批量回填涨停事件到
limit_up_events 表: 昨收 → limit_rules 算涨停价 → 触及/封住/一字板判定。

口径(诚实标注):
- open_count(盘中开板次数)需逐笔, MVP 留空(None)由后续逐笔链路补充;
- one_way 一字板 = 开盘即涨停且最低价未离开涨停价(近似, 除权日涨停价
  用前收盘推算可能与客户端略有出入, 边界由测试覆盖);
- 涨停池(wudao)的 first_time/封单额由 seal_sampler 侧当日数据合并,
  回填历史日无此数据 → 留空, 不编造。
全市场 ~5562 只 × 260 日: 走 marketdata Engine(TQ 优先 ~30ms/只),
失败股票跳过记数, 绝不写半行。表按 (symbol, trade_date) 唯一, 重跑幂等。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import text

logger = logging.getLogger(__name__)

_CST = ZoneInfo("Asia/Shanghai")
_TABLE = "limit_up_events"
_WINDOW_DAYS = 260  # 近一年交易日(约 250 + 余量)
_EPS = 1e-6


def _extract_events_from_bars(symbol: str, name: str, bars: list[dict], days: int = _WINDOW_DAYS) -> list[dict]:
    """日 K → 涨停事件行(纯函数可单测)。bars 按 date 升序。"""
    from src.core.limit_rules import limit_up_price

    out = []
    for i in range(1, len(bars)):
        prev = bars[i - 1]
        cur = bars[i]
        prev_close = _f(prev.get("close"))
        lp = limit_up_price(symbol, prev_close, is_st="ST" in (name or "").upper())
        if lp is None:
            continue  # 新股首日/停牌复牌无昨收, 显式跳过
        o, h, l, c = (_f(cur.get("open")), _f(cur.get("high")), _f(cur.get("low")), _f(cur.get("close")))
        if h is None or c is None:
            continue
        touched = h >= lp - _EPS
        if not touched:
            continue  # 非涨停日不入表(只存涨停事件)
        sealed = c >= lp - _EPS
        one_way = int(bool(o is not None and o >= lp - _EPS and l is not None and l >= lp - _EPS and sealed))
        d = str(cur.get("date") or "").replace("-", "")
        out.append({
            "trade_date": d,
            "symbol": symbol,
            "market": "CN",
            "name": name or None,
            "prev_close": prev_close,
            "limit_price": lp,
            "open_price": o,
            "high_price": h,
            "low_price": l,
            "close_price": c,
            "touched": 1,
            "is_sealed_close": int(sealed),
            "one_way": one_way,
            "open_count": None,
            "first_time": None,
            "max_seal_amount": None,
            "limit_days": None,
            "circ_mv": None,
            "source": "tdx_backfill",
            "extra": json.dumps({"note": "open_count/封单额 MVP 留空待逐笔/涨停池补充"}, ensure_ascii=False),
        })
    return out[-_WINDOW_DAYS:]


def _f(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def backfill_symbol(symbol: str, name: str = "") -> int:
    """单只股票回填近一年涨停事件, 返回写入行数(重跑幂等)。"""
    try:
        from src.core.decision_pioneer import fetch_bars

        bars = fetch_bars(symbol, "CN", days=_WINDOW_DAYS)
        if not bars or len(bars) < 2:
            return 0
        rows = _extract_events_from_bars(symbol, name, bars)
        return _upsert_rows(rows)
    except Exception as e:  # noqa: BLE001
        logger.warning("涨停回填 %s 失败: %s", symbol, e)
        return 0


def _upsert_rows(rows: list[dict]) -> int:
    if not rows:
        return 0
    from src.db.session import SessionLocal

    saved = 0
    db = SessionLocal()
    try:
        for r in rows:
            try:
                exists = db.execute(
                    text(f"SELECT 1 FROM {_TABLE} WHERE symbol = :symbol AND trade_date = :trade_date"),
                    {"symbol": r["symbol"], "trade_date": r["trade_date"]},
                ).first()
                if exists:
                    continue
                cols = ",".join(r.keys())
                binds = ",".join(f":{k}" for k in r.keys())
                db.execute(text(f"INSERT INTO {_TABLE} ({cols}) VALUES ({binds})"), r)
                saved += 1
            except Exception as e:  # noqa: BLE001
                logger.warning("涨停事件写库失败 %s@%s: %s", r.get("symbol"), r.get("trade_date"), e)
                db.rollback()
        db.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("涨停事件写库整体失败: %s", e)
        db.rollback()
    finally:
        db.close()
    return saved


def backfill_all(symbols: list[str] | None = None, max_stocks: int = 0) -> dict:
    """全市场回填入口(盘后 cron / 手动)。返回统计。symbols 给定时只回填这些。"""
    started = datetime.now(_CST)
    try:
        if symbols is None:
            from src.collectors.stock_list import get_stock_list

            stocks = [
                {"symbol": s["symbol"], "name": s.get("name") or ""}
                for s in get_stock_list()
                if s.get("symbol") and len(str(s["symbol"])) == 6
            ]
        else:
            stocks = [{"symbol": s, "name": ""} for s in symbols]
        if max_stocks > 0:
            stocks = stocks[:max_stocks]
        saved = 0
        failed = 0
        for st in stocks:
            n = backfill_symbol(st["symbol"], st["name"])
            if n < 0:
                failed += 1
            saved += max(n, 0)
        stats = {
            "started_at": started.isoformat(timespec="seconds"),
            "finished_at": datetime.now(_CST).isoformat(timespec="seconds"),
            "stocks": len(stocks),
            "events_saved": saved,
        }
        logger.info("涨停事件回填完成: %s", stats)
        return stats
    except Exception as e:  # noqa: BLE001
        logger.warning("涨停事件回填整体失败: %s", e)
        return {"error": str(e)}


def get_events_window(symbol: str, days: int = 250) -> list[dict]:
    """取某股近 N 自然日涨停事件(ts 升序), 供 demon_score。"""
    from src.db.session import SessionLocal

    cutoff = datetime.now(_CST).strftime("%Y%m%d")
    # 简化: 近一年 = 日期字符串前 6 位在近 12 个月内(YYYYMMDD 字典序可用)
    year_ago = _shift_year(cutoff, -1)
    db = SessionLocal()
    try:
        rows = db.execute(
            text(
                f"SELECT trade_date, symbol, name, limit_price, open_price, high_price, low_price,"
                " close_price, touched, is_sealed_close, one_way, open_count, first_time,"
                f" max_seal_amount, limit_days, circ_mv FROM {_TABLE}"
                " WHERE symbol = :symbol AND trade_date >= :cutoff ORDER BY trade_date"
            ),
            {"symbol": symbol, "cutoff": year_ago},
        ).fetchall()
    finally:
        db.close()
    return [dict(r._mapping) for r in rows]


def _shift_year(yyyymmdd: str, delta: int) -> str:
    try:
        d = datetime.strptime(yyyymmdd, "%Y%m%d")
        return d.replace(year=d.year + delta).strftime("%Y%m%d")
    except ValueError:
        return yyyymmdd
