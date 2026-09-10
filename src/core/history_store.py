"""历史落库: 决策先锋快照 + L2 逐笔(回测回查用)。

双库兼容(PG 生产 / SQLite 本地): 只用标准类型 + TEXT 存 JSON,
SQLite 靠类型宽松(type affinity)通过, PG 走原生类型。
失败永不抛(读链路 best-effort), 由调用方 try 包裹或本模块内部吞。
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, time, timedelta, timezone
from datetime import datetime as _datetime_cls
from zoneinfo import ZoneInfo

from sqlalchemy import text

logger = logging.getLogger(__name__)

DP_RETENTION_DAYS = 180
L2_RETENTION_DAYS = 60

# 方案A(设计文档 §5.1): l2_ticks 事件时间口径。ts = 交易日 + tick_time,
# 唯一键含 ts → 跨日同 tick_time 不再互相顶掉(旧键无日期的假去重缺陷)。
_CN_TZ = ZoneInfo("Asia/Shanghai")
_TICK_TIME_RE = re.compile(r"^\d{2}:\d{2}:\d{2}$")


def _l2_event_ts(tick_time, now: datetime) -> datetime:
    """逐笔事件时间: 采集交易日(上海时区) + tick_time。

    - tick_time 晚于当前本地时刻 → 属隔日凌晨拉取的前一交易日, 归属前一天
    - tick_time 缺失/非法(如 "24:00:00") → 回退采集时刻 now(旧口径, 不丢行)
    幂等: 已是事件时间的行再变换结果不变(可安全与回填脚本混跑)。
    """
    s = str(tick_time or "").strip()
    if not _TICK_TIME_RE.fullmatch(s):
        return now
    try:
        hh, mm, ss = (int(x) for x in s.split(":"))
        tod = time(hh, mm, ss)
    except ValueError:
        return now
    local = now.astimezone(_CN_TZ)
    day = local.date() if tod <= local.time() else local.date() - timedelta(days=1)
    # _datetime_cls: 不经模块级 datetime(测试会替换时钟), 保证返回真实 datetime 类型
    return _datetime_cls(day.year, day.month, day.day, hh, mm, ss, tzinfo=_CN_TZ)


def _engine():
    from src.db.session import engine

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
    """存 L2 逐笔(唯一索引去重, 重复拉取天然幂等)。返回实际写入行数(去重后)。

    事件时间口径(方案A): ts=交易日+tick_time; 唯一键 (symbol,market,source,ts,
    direction,price,vol,amt) 含日期, 跨日同秒不再假去重。
    PG 端 retention 交给 90 天 policy(压缩 hypertable 上跑行级 DELETE 有整事务
    回滚风险), 本函数不删; SQLite 仍按 L2_RETENTION_DAYS 就地清理。
    """
    if not rows:
        return 0
    try:
        from src.db.dialect import insert_ignore_sql, is_postgres

        now = datetime.now(timezone.utc)
        params = []
        ts_min = ts_max = None
        for r in rows:
            if not isinstance(r, dict):
                continue
            ts = _l2_event_ts(r.get("t"), now)
            if ts_min is None or ts < ts_min:
                ts_min = ts
            if ts_max is None or ts > ts_max:
                ts_max = ts
            params.append(
                {
                    "ts": ts,
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
        # W3.1(D2): 方言分叉收编 src/db/dialect.insert_ignore_sql
        stmt = insert_ignore_sql(
            "l2_ticks", ["ts", "symbol", "market", "source", "direction", "price", "vol", "amt", "tick_time"]
        )
        with _engine().begin() as conn:
            # changes()/RETURNING 在 executemany 下不精确 → 用本批事件 ts 窗口
            # (min..max, 走 ix_l2_ticks_symbol_ts)前后计数得精确写入数; 窗口内既有行
            # 前后两次计数都含, 差值即本批净写入。
            cnt_sql = (
                "SELECT COUNT(*) FROM l2_ticks WHERE symbol=:s AND market=:m "
                "AND source=:src AND ts>=:t0 AND ts<=:t1"
            )
            wparams = {"s": symbol, "m": market, "src": source, "t0": ts_min, "t1": ts_max}
            before = conn.execute(text(cnt_sql), wparams).scalar() or 0
            conn.execute(text(stmt), params)
            after = conn.execute(text(cnt_sql), wparams).scalar() or 0
            written = max(0, int(after) - int(before))
            if not is_postgres():
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
