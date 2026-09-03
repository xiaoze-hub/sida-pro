"""新增: summary_cache 表(PG/SQLite 兼容) + summary 落库/读库

设计:
  - 表 summary_cache: (symbol, market, computed_at, ttl_s, payload TEXT)
  - 读时检查 computed_at + ttl_s, 命中→直接返, miss→调原逻辑
  - 写时 upsert + 删过期(>24h, 防表膨胀)
  - payload 用 TEXT 存 JSON (双方言), 上限 50KB 防爆
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

logger = logging.getLogger(__name__)

SUMMARY_RETENTION_DAYS = 7
SUMMARY_PAYLOAD_MAX = 50_000  # 50KB 上限, 超过截断


def _engine():
    from src.web.database import engine
    return engine


_table_ready = False
_table_lock = threading.Lock()


def _ensure_summary_cache_table() -> None:
    """幂等建表 summary_cache, 模块加载时跑一次。"""
    global _table_ready
    if _table_ready:
        return
    with _table_lock:
        if _table_ready:
            return
        try:
            from src.web.database import IS_PG, engine
            if IS_PG:
                ddl = """
                CREATE TABLE IF NOT EXISTS summary_cache (
                    symbol TEXT NOT NULL,
                    market TEXT NOT NULL,
                    computed_at TIMESTAMP NOT NULL,
                    ttl_s INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY (symbol, market)
                )
                """
            else:
                ddl = """
                CREATE TABLE IF NOT EXISTS summary_cache (
                    symbol TEXT NOT NULL,
                    market TEXT NOT NULL,
                    computed_at TIMESTAMP NOT NULL,
                    ttl_s INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY (symbol, market)
                )
                """
            with engine.begin() as conn:
                conn.execute(text(ddl))
            _table_ready = True
        except Exception as e:  # noqa: BLE001
            logger.debug("summary_cache 建表失败(静默): %s", e)


def get_cached_summary(symbol: str, market: str, ttl_s: int) -> dict | None:
    """读 summary_cache: 命中且未过期 → 返 payload 字典; miss/过期 → None。失败永不抛。"""
    try:
        if not _table_ready:
            _ensure_summary_cache_table()
        if not _table_ready:
            return None
        with _engine().begin() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT computed_at, ttl_s, payload FROM summary_cache
                    WHERE symbol = :symbol AND market = :market
                    """
                ),
                {"symbol": symbol, "market": market},
            ).mappings().first()
            if not row:
                return None
            computed = row["computed_at"]
            ttl = int(row["ttl_s"] or 0)
            # ts 可能是 naive datetime / aware datetime / str (SQLite 默认返 str)
            now = datetime.now(timezone.utc)
            if isinstance(computed, str):
                try:
                    computed = datetime.fromisoformat(computed.replace("Z", "+00:00"))
                except Exception:
                    return None  # ts 解析失败: 视为过期(避免卡住所有读)
            if computed.tzinfo is None:
                computed = computed.replace(tzinfo=timezone.utc)
            if (now - computed).total_seconds() > max(ttl, ttl_s):
                return None
            return json.loads(row["payload"])
    except Exception as e:  # noqa: BLE001
        logger.debug("get_cached_summary %s failed: %s", symbol, e)
        return None


def put_cached_summary(symbol: str, market: str, payload: dict, ttl_s: int = 300) -> None:
    """写 summary_cache: upsert + 清过期。payload 超 50KB 截断。失败永不抛。"""
    try:
        if not _table_ready:
            _ensure_summary_cache_table()
        if not _table_ready:
            return
        from src.web.database import IS_PG
        body = json.dumps(payload or {}, ensure_ascii=False, default=str)
        if len(body) > SUMMARY_PAYLOAD_MAX:
            body = body[:SUMMARY_PAYLOAD_MAX]
            payload = {"truncated": True, "note": f"payload>{SUMMARY_PAYLOAD_MAX}B 截断", "head": json.loads(body[:5000])}
            body = json.dumps(payload, ensure_ascii=False, default=str)
        now = datetime.now(timezone.utc)
        if IS_PG:
            stmt = """
                INSERT INTO summary_cache (symbol, market, computed_at, ttl_s, payload)
                VALUES (:symbol, :market, :ts, :ttl, :payload)
                ON CONFLICT (symbol, market) DO UPDATE
                SET computed_at = EXCLUDED.computed_at,
                    ttl_s = EXCLUDED.ttl_s,
                    payload = EXCLUDED.payload
            """
        else:
            stmt = """
                INSERT INTO summary_cache (symbol, market, computed_at, ttl_s, payload)
                VALUES (:symbol, :market, :ts, :ttl, :payload)
                ON CONFLICT(symbol, market) DO UPDATE
                SET computed_at = excluded.computed_at,
                    ttl_s = excluded.ttl_s,
                    payload = excluded.payload
            """
        with _engine().begin() as conn:
            conn.execute(
                text(stmt),
                {"symbol": symbol, "market": market, "ts": now, "ttl": int(ttl_s), "payload": body},
            )
            conn.execute(
                text("DELETE FROM summary_cache WHERE computed_at < :cut"),
                {"cut": now - timedelta(days=SUMMARY_RETENTION_DAYS)},
            )
    except Exception as e:  # noqa: BLE001
        logger.debug("put_cached_summary %s failed: %s", symbol, e)


def clear_summary_cache(symbol: str | None = None) -> int:
    """清缓存: symbol=None 全清; 否则删单股; 返回删除行数。"""
    try:
        with _engine().begin() as conn:
            if symbol:
                r = conn.execute(
                    text("DELETE FROM summary_cache WHERE symbol = :symbol"),
                    {"symbol": symbol},
                )
            else:
                r = conn.execute(text("DELETE FROM summary_cache"))
            return int(r.rowcount or 0)
    except Exception as e:  # noqa: BLE001
        logger.debug("clear_summary_cache %s failed: %s", symbol, e)
        return 0


# 模块加载时建表
_ensure_summary_cache_table()
