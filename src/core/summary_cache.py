"""新增: summary_cache 表(PG/SQLite 兼容) + summary 落库/读库

设计:
  - 表 summary_cache: (symbol, market, computed_at, ttl_s, payload TEXT)
  - 读时检查 computed_at + ttl_s, 命中→直接返, miss→调原逻辑
  - 写时 upsert + 删过期(>24h, 防表膨胀)
  - payload 用 TEXT 存 JSON (双方言), 上限 50KB 防爆
W1.5/A5(2026-09-08): 建表收编进 B 层 src/web/migrations.py _m129,
此处不再运行时建表 —— schema 变更唯一入口是版本化迁移。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

logger = logging.getLogger(__name__)

SUMMARY_RETENTION_DAYS = 7
SUMMARY_PAYLOAD_MAX = 50_000  # 50KB 上限, 超过截断


def _engine():
    from src.db.session import engine
    return engine


def get_cached_summary(symbol: str, market: str, ttl_s: int) -> dict | None:
    """读 summary_cache: 命中且未过期 → 返 payload 字典; miss/过期 → None。失败永不抛。"""
    try:
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
            age = (now - computed).total_seconds()
            # ⚠️ 2026-09-23 修(影响面很大): 列是 `timestamp without time zone`, 而 PG 会话时区是
            # Asia/Shanghai ⇒ 写入侧传的 aware UTC 会被存成 **CST 墙上时间**(05:50 UTC → 存 13:50)。
            # 读侧原来把它当 UTC ⇒ age = 本地-UTC = **-8 小时(负数)** ⇒ 永远不 > ttl
            # ⇒ **缓存永不失效**。生产实测: 603629 那行冻结在 09:42(已 4 小时)仍被当作有效返回,
            # 导致 K 线图层的 gs_signals/fund_flow/events/chips 全部停在旧时刻, 与实时的数智决策
            # 面板互相矛盾(用户 2026-09-23 报障: 决策显示 S区, K线最新标记却是 G)。
            # 处理: ① age < 0(未来时间戳) 一律视为过期 —— 它只可能来自时区口径不一致或时钟异常;
            #       ② 写入侧改为存 naive UTC(见 put_cached_summary), 两侧口径从此一致。
            if age < 0 or age > max(ttl, ttl_s):
                return None
            return json.loads(row["payload"])
    except Exception as e:  # noqa: BLE001
        logger.debug("get_cached_summary %s failed: %s", symbol, e)
        return None


def put_cached_summary(symbol: str, market: str, payload: dict, ttl_s: int = 300) -> None:
    """写 summary_cache: upsert + 清过期。payload 超 50KB 截断。失败永不抛。"""
    try:
        from src.db.dialect import upsert_sql

        body = json.dumps(payload or {}, ensure_ascii=False, default=str)
        if len(body) > SUMMARY_PAYLOAD_MAX:
            body = body[:SUMMARY_PAYLOAD_MAX]
            payload = {"truncated": True, "note": f"payload>{SUMMARY_PAYLOAD_MAX}B 截断", "head": json.loads(body[:5000])}
            body = json.dumps(payload, ensure_ascii=False, default=str)
        # 2026-09-23: 存 **naive UTC** —— 列是 `timestamp without time zone`, 传 aware datetime 会被
        # PG 按会话时区(Asia/Shanghai)转成 CST 墙上时间, 与读侧"naive 即 UTC"的假设冲突 ⇒ 缓存永不失效。
        # 两侧统一为 naive UTC 后 age 计算才正确。
        now = datetime.now(timezone.utc)
        now_naive_utc = now.replace(tzinfo=None)
        # W3.1(D2): 原 PG/SQLite 双分支 SQL 仅 EXCLUDED 大小写之差(两后端均
        # 大小写不敏感), 收编为 src/db/dialect.upsert_sql 单一语句
        stmt = upsert_sql(
            "summary_cache",
            ["symbol", "market", "computed_at", "ttl_s", "payload"],
            ["symbol", "market"],
            ["computed_at", "ttl_s", "payload"],
        )
        with _engine().begin() as conn:
            conn.execute(
                text(stmt),
                {
                    "symbol": symbol,
                    "market": market,
                    "computed_at": now_naive_utc,
                    "ttl_s": int(ttl_s),
                    "payload": body,
                },
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
