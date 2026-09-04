"""逐笔日存档(#3 回测底座 + #4 跨日结论序列, 2026-09-04)。

只存被查过的股(compute_dark_flow 触发), 收盘后(≥15:05)快照一次:
全天逐笔 JSON + 当日结论(main_net/分档/signal/data_status)。
一行一日(code, trade_date), upsert 幂等。失败永不影响主链路。

序列查询 read_series() 即"主力连续流入 N 天"的数据底座。
"""
import json
import logging

logger = logging.getLogger(__name__)

_ARCHIVED: set[tuple[str, str]] = set()  # 进程内 guard: 同日同股只存一次
_ENG = None


def _engine():
    global _ENG
    if _ENG is None:
        from sqlalchemy import create_engine

        from src.web.database import DB_URL
        _ENG = create_engine(DB_URL, pool_pre_ping=True, pool_size=2, max_overflow=5)
    return _ENG


def _past_close(now=None) -> bool:
    """收盘后(15:05 后)才存档: 白天逐笔还在长, 存了也是半截。now 可注入单测。"""
    import datetime as _dt
    now = now or _dt.datetime.now()
    try:
        return now.strftime("%H:%M:%S") >= "15:05:00"
    except Exception:  # noqa: BLE001
        return False


def upsert_day(conn, tcode: str, symbol6: str, day: str, ticks: list, result: dict) -> None:
    """写一行(调用方保证 conn)。ON CONFLICT 幂等, PG/SQLite 通用。"""
    from sqlalchemy import text
    conn.execute(
        text(
            """
            INSERT INTO tick_archive
              (trade_date, code, symbol, tick_count, last_tick_t, tick_pages,
               main_net, big_net, mid_net, retail_net, data_status, signal, ticks_json)
            VALUES
              (:day, :code, :sym, :n, :last_t, :pages,
               :main_net, :big_net, :mid_net, :retail_net, :ds, :signal, :ticks)
            ON CONFLICT (code, trade_date) DO UPDATE SET
              tick_count = excluded.tick_count,
              last_tick_t = excluded.last_tick_t,
              tick_pages = excluded.tick_pages,
              main_net = excluded.main_net,
              big_net = excluded.big_net,
              mid_net = excluded.mid_net,
              retail_net = excluded.retail_net,
              data_status = excluded.data_status,
              signal = excluded.signal,
              ticks_json = excluded.ticks_json
            """
        ),
        {
            "day": day, "code": tcode, "sym": symbol6,
            "n": len(ticks), "last_t": result.get("last_tick_t"),
            "pages": result.get("tick_pages"),
            "main_net": result.get("main_net"), "big_net": result.get("big_net"),
            "mid_net": result.get("mid_net"), "retail_net": result.get("small_net"),
            "ds": result.get("data_status"), "signal": result.get("signal"),
            "ticks": json.dumps(ticks, ensure_ascii=False),
        },
    )


def maybe_archive_day(tcode: str, symbol6: str, ticks: list, result: dict) -> bool:
    """收盘后快照一次。返回 True=本次写入。任何异常吞掉(False)。"""
    try:
        from src.core.dark_flow import _cache_day
        day = _cache_day()
        if (tcode, day) in _ARCHIVED:
            return False
        if not _past_close():
            return False
        if not ticks or len(ticks) < 30:
            return False
        eng = _engine()
        with eng.begin() as conn:
            upsert_day(conn, tcode, symbol6 or "", day, ticks, result or {})
        _ARCHIVED.add((tcode, day))
        logger.info(f"[tick_archive] {tcode} {day} 存档 {len(ticks)} 笔")
        return True
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[tick_archive] 存档跳过 {tcode}: {e}")
        return False


def read_series(symbol6: str, limit: int = 60, engine=None) -> list[dict]:
    """跨日结论序列(新→旧): [{trade_date, main_net, tick_count, data_status}]。"""
    from sqlalchemy import text
    eng = engine or _engine()
    with eng.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT trade_date, main_net, tick_count, data_status "
                "FROM tick_archive WHERE symbol = :sym "
                "ORDER BY trade_date DESC LIMIT :lim"
            ),
            {"sym": symbol6, "lim": max(1, min(int(limit or 60), 500))},
        ).mappings().all()
    return [dict(r) for r in rows]
