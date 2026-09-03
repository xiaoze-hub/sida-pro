"""历史落库: 决策先锋快照 + L2 逐笔(回测回查用)。

双库兼容(PG 生产 / SQLite 本地): 只用标准类型 + TEXT 存 JSON,
SQLite 靠类型宽松(type affinity)通过, PG 走原生类型。
失败永不抛(读链路 best-effort), 由调用方 try 包裹或本模块内部吞。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

logger = logging.getLogger(__name__)

DP_RETENTION_DAYS = 180
L2_RETENTION_DAYS = 60


def _engine():
    from src.web.database import engine

    return engine


def record_dp_snapshot(symbol: str, market: str, data: dict) -> None:
    """存一条决策先锋快照(只在新鲜计算后调, 30s 缓存天然节流)。"""
    try:
        act = (data or {}).get("institution_activity") or {}
        gs = (data or {}).get("gs") or {}
        l2 = (data or {}).get("l2") or {}
        mi = (data or {}).get("main_intent") or {}
        now = datetime.now(timezone.utc)
        payload = json.dumps(data or {}, ensure_ascii=False, default=str)[:20000]
        with _engine().begin() as conn:
            conn.execute(
                text(
                    """
INSERT INTO decision_pioneer_history
  (ts, symbol, market, activity, level, gs_side, fund_net, main_net, payload)
VALUES (:ts, :symbol, :market, :activity, :level, :gs_side, :fund_net, :main_net, :payload)
"""
                ),
                {
                    "ts": now,
                    "symbol": symbol,
                    "market": market,
                    "activity": act.get("activity"),
                    "level": act.get("level"),
                    "gs_side": gs.get("side") or gs.get("signal"),
                    "fund_net": l2.get("main_net") if isinstance(l2, dict) else None,
                    "main_net": mi.get("main_net") if isinstance(mi, dict) else None,
                    "payload": payload,
                },
            )
            conn.execute(
                text(
                    """
DELETE FROM decision_pioneer_history
WHERE ts < :cut
"""
                ),
                {"cut": now - timedelta(days=DP_RETENTION_DAYS)},
            )
    except Exception as e:  # noqa: BLE001
        logger.debug("record_dp_snapshot %s failed: %s", symbol, e)


def query_dp_history(symbol: str, market: str, days: int = 30, limit: int = 500) -> list[dict]:
    """读决策先锋历史(按 ts 升序, 回查/回测用)。"""
    try:
        days = max(1, min(int(days or 30), DP_RETENTION_DAYS))
        limit = max(1, min(int(limit or 500), 2000))
        cut = datetime.now(timezone.utc) - timedelta(days=days)
        with _engine().begin() as conn:
            rows = conn.execute(
                text(
                    """
SELECT ts, symbol, market, activity, level, gs_side, fund_net, main_net
FROM decision_pioneer_history
WHERE symbol = :symbol AND market = :market AND ts >= :cut
ORDER BY ts ASC
LIMIT :limit
"""
                ),
                {"symbol": symbol, "market": market, "cut": cut, "limit": limit},
            ).mappings()
            return [dict(r) for r in rows]
    except Exception as e:  # noqa: BLE001
        logger.debug("query_dp_history %s failed: %s", symbol, e)
        return []


def persist_l2_ticks(symbol: str, market: str, source: str, rows: list[dict]) -> int:
    """存 L2 逐笔(唯一索引去重, 重复拉取天然幂等)。返回实际写入行数(去重后)。"""
    if not rows:
        return 0
    try:
        from src.web.database import IS_PG

        now = datetime.now(timezone.utc)
        params = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            params.append(
                {
                    "ts": now,
                    "symbol": symbol,
                    "market": market,
                    "source": source,
                    "direction": r.get("d"),
                    "price": r.get("price"),
                    "vol": r.get("vol"),
                    "amt": r.get("amt"),
                    "tick_time": r.get("t"),
                }
            )
        if not params:
            return 0
        if IS_PG:
            stmt = """
INSERT INTO l2_ticks (ts, symbol, market, source, direction, price, vol, amt, tick_time)
VALUES (:ts, :symbol, :market, :source, :direction, :price, :vol, :amt, :tick_time)
ON CONFLICT DO NOTHING
"""
        else:
            stmt = """
INSERT OR IGNORE INTO l2_ticks (ts, symbol, market, source, direction, price, vol, amt, tick_time)
VALUES (:ts, :symbol, :market, :source, :direction, :price, :vol, :amt, :tick_time)
"""
        with _engine().begin() as conn:
            # changes()/RETURNING 在 executemany 下不精确 → 用本批 ts 水位前后计数得精确写入数
            before = conn.execute(
                text(
                    "SELECT COUNT(*) FROM l2_ticks WHERE symbol=:s AND market=:m AND source=:src AND ts>=:w"
                ),
                {"s": symbol, "m": market, "src": source, "w": now},
            ).scalar() or 0
            conn.execute(text(stmt), params)
            after = conn.execute(
                text(
                    "SELECT COUNT(*) FROM l2_ticks WHERE symbol=:s AND market=:m AND source=:src AND ts>=:w"
                ),
                {"s": symbol, "m": market, "src": source, "w": now},
            ).scalar() or 0
            written = max(0, int(after) - int(before))
            conn.execute(
                text("DELETE FROM l2_ticks WHERE ts < :cut"),
                {"cut": now - timedelta(days=L2_RETENTION_DAYS)},
            )
        return int(written)
    except Exception as e:  # noqa: BLE001
        logger.debug("persist_l2_ticks %s failed: %s", symbol, e)
        return 0


def query_l2_ticks(
    symbol: str, market: str, days: int = 5, source: str = "", limit: int = 5000
) -> list[dict]:
    """读 L2 落库(按 ts 升序; 回测回查用, 默认近 5 天、上限 5000 笔防爆)。"""
    try:
        days = max(1, min(int(days or 5), L2_RETENTION_DAYS))
        limit = max(1, min(int(limit or 5000), 20000))
        cut = datetime.now(timezone.utc) - timedelta(days=days)
        cond = "AND source = :source" if source else ""
        with _engine().begin() as conn:
            rows = conn.execute(
                text(
                    f"""
SELECT ts, symbol, market, source, direction, price, vol, amt, tick_time
FROM l2_ticks
WHERE symbol = :symbol AND market = :market AND ts >= :cut {cond}
ORDER BY ts ASC
LIMIT :limit
"""
                ),
                {"symbol": symbol, "market": market, "cut": cut, "source": source, "limit": limit},
            ).mappings()
            return [dict(r) for r in rows]
    except Exception as e:  # noqa: BLE001
        logger.debug("query_l2_ticks %s failed: %s", symbol, e)
        return []
