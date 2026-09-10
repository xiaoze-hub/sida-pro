"""l2_ticks 事件时间化回填(设计文档 §5.1 方案A, v0.5.49)。

生产表 ~79M 行/16GB(ts=入库时刻) → ts=交易日+tick_time 事件时间, 重写 16GB。
只能在**非交易时段**执行(08:00-17:00 上海时间为保守禁改窗口), 且必须先做表级
pg_dump 备份(--confirm-backup 门禁, 备份命令见下方 usage)。

用法(panwatch 容器内):
  干跑校验:   python scripts/l2_ticks_event_time.py
  正式执行:   python scripts/l2_ticks_event_time.py --apply --confirm-backup

备份(panwatch-postgres 容器, 宿主 WSL):
  docker exec panwatch-postgres sh -c \\
    'pg_dump -U "$POSTGRES_USER" -Fc -t l2_ticks -f /tmp/l2_ticks_backup.dump'
  docker cp panwatch-postgres:/tmp/l2_ticks_backup.dump <宿主备份目录>/

步骤(分事务, 失败可重跑; 回填产物 l2_ticks_new 先建后换):
  A) 预检: PG/timescaledb/非交易时段/未被迁移过; 记录快照起点
     建表: DROP 旧 l2_ticks_new → LIKE 建 l2_ticks_new + hypertable(1d 分块)
           + 新唯一键 (symbol,market,source,ts,direction,price,vol,amt) 含 ts
  B) 回填: INSERT SELECT 变换(幂等: tick_time 正则守卫, 本地时刻<tick_time 归
     前一天, 非法 tick_time 保持旧 ts; 冲突跳过)
  C) 校验+补拉+换名: 行数对账(差>0.1% 中止换名) → 快照间隙补拉 → 索引/表让位
     更名(l2_ticks→l2_ticks_old 留回滚, 新索引摘 _new 后缀)
  D) 压缩(分段 symbol,market,source, orderby ts DESC, 2 天) + retention 90 天
     + ANALYZE

回滚(验证异常时人工执行, 旧表索引带 _old_backup 后缀按需改回):
  DROP TABLE l2_ticks;  ALTER TABLE l2_ticks_old RENAME TO l2_ticks;
"""
from __future__ import annotations

import argparse
import logging
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import text

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("l2_event_time")

CN = ZoneInfo("Asia/Shanghai")
TICK_TIME_RE_SQL = r"'^\d{2}:\d{2}:\d{2}$'"

# INSERT SELECT 变换: 事件时间 = date_trunc(本地日) + tick_time;
# 采集本地时刻 < tick_time → 隔日拉取的前一交易日, -1 天。幂等。
TRANSFORM_SELECT = f"""
SELECT
  CASE
    WHEN tick_time ~ {TICK_TIME_RE_SQL} THEN
      (date_trunc('day', ts AT TIME ZONE 'Asia/Shanghai')
       + tick_time::interval
       + CASE WHEN (ts AT TIME ZONE 'Asia/Shanghai')::time < tick_time::time
              THEN INTERVAL '-1 day' ELSE INTERVAL '0 seconds' END
      ) AT TIME ZONE 'Asia/Shanghai'
    ELSE ts
  END,
  symbol, market, source, direction, price, vol, amt, tick_time
FROM l2_ticks
"""


def _connect():
    from src.db.session import engine

    if engine.dialect.name != "postgresql":
        raise SystemExit("仅支持 PostgreSQL(生产); SQLite 新库由迁移 v158 直接建新形态")
    return engine


def _maintenance_ok(force: bool) -> str:
    now = datetime.now(CN)
    if 8 <= now.hour < 17 and not force:
        return (
            f"上海时间 {now:%Y-%m-%d %H:%M} 在 08:00-17:00 保守禁改窗口内"
            "(L2 cron/盘中写入活跃); 确认非交易时段可用 --force 显式覆盖"
        )
    return f"上海时间 {now:%Y-%m-%d %H:%M} 允许执行"


def _already_migrated(conn) -> bool:
    defs = [
        str(r[0])
        for r in conn.execute(text("SELECT indexdef FROM pg_indexes WHERE tablename='l2_ticks'"))
    ]
    return any("(symbol, market, source, ts," in d for d in defs)


def _count(conn, table: str) -> int:
    return int(conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar() or 0)


def _has_timescale(conn) -> bool:
    return (
        conn.execute(text("SELECT 1 FROM pg_extension WHERE extname='timescaledb' LIMIT 1")).first()
        is not None
    )


def _stage_build(conn) -> None:
    """事务A: 建回填表(幂等: 丢弃上一轮残留)。"""
    conn.execute(text("DROP TABLE IF EXISTS l2_ticks_new"))
    conn.execute(text("CREATE TABLE l2_ticks_new (LIKE l2_ticks)"))
    if _has_timescale(conn):
        conn.execute(
            text(
                "SELECT create_hypertable('l2_ticks_new', 'ts', "
                "chunk_time_interval => INTERVAL '1 day')"
            )
        )
    conn.execute(
        text(
            "CREATE UNIQUE INDEX uq_l2_ticks_dedupe_new "
            "ON l2_ticks_new (symbol, market, source, ts, direction, price, vol, amt)"
        )
    )
    conn.execute(
        text("CREATE INDEX ix_l2_ticks_symbol_ts_new ON l2_ticks_new (symbol, market, ts)")
    )


def _stage_backfill(conn, snap: datetime) -> int:
    """事务A: 全量变换回填 + 快照之后新写行的增量补拉。返回写入行数。"""
    conn.execute(
        text(f"INSERT INTO l2_ticks_new {TRANSFORM_SELECT} ON CONFLICT DO NOTHING")
    )
    # 快照起点之后写入旧行的行(回填期间持续写入的少量增量)再补一遍, 幂等
    n = conn.execute(
        text(f"INSERT INTO l2_ticks_new {TRANSFORM_SELECT} WHERE ts >= :snap ON CONFLICT DO NOTHING"),
        {"snap": snap},
    ).rowcount
    return int(n or 0)


def _stage_swap(conn) -> tuple[int, int]:
    """事务B: 行数对账 → 换名让位。返回 (旧行数, 新行数)。"""
    old_n = _count(conn, "l2_ticks")
    new_n = _count(conn, "l2_ticks_new")
    if new_n == 0:
        raise SystemExit(f"回填表为空, 中止换名(旧表 {old_n} 行未动)")
    if new_n < old_n - max(1000, int(old_n * 0.001)):
        raise SystemExit(
            f"行数对账差异过大: 旧 {old_n} vs 新 {new_n}, 中止换名(旧表未动, 可排查后重跑)"
        )
    if new_n < old_n:
        logger.warning(f"新表比旧表少 {old_n - new_n} 行(唯一键冲突跳过, 预期≈0)")
    conn.execute(text("ALTER INDEX uq_l2_ticks_dedupe RENAME TO uq_l2_ticks_dedupe_old_backup"))
    conn.execute(text("ALTER INDEX ix_l2_ticks_symbol_ts RENAME TO ix_l2_ticks_symbol_ts_old_backup"))
    conn.execute(text("ALTER TABLE l2_ticks RENAME TO l2_ticks_old"))
    conn.execute(text("ALTER TABLE l2_ticks_new RENAME TO l2_ticks"))
    conn.execute(text("ALTER INDEX uq_l2_ticks_dedupe_new RENAME TO uq_l2_ticks_dedupe"))
    conn.execute(text("ALTER INDEX ix_l2_ticks_symbol_ts_new RENAME TO ix_l2_ticks_symbol_ts"))
    return old_n, new_n


def _stage_policies(conn) -> None:
    """事务C: 压缩 + 保留策略 + 统计。"""
    if _has_timescale(conn):
        conn.execute(
            text(
                "ALTER TABLE l2_ticks SET (timescaledb.compress, "
                "timescaledb.compress_segmentby = 'symbol, market, source', "
                "timescaledb.compress_orderby = 'ts DESC')"
            )
        )
        conn.execute(
            text("SELECT add_compression_policy('l2_ticks', INTERVAL '2 days', if_not_exists => TRUE)")
        )
        conn.execute(
            text("SELECT add_retention_policy('l2_ticks', INTERVAL '90 days', if_not_exists => TRUE)")
        )
    conn.execute(text("ANALYZE l2_ticks"))


def _verify(conn) -> None:
    ht = conn.execute(
        text(
            "SELECT num_chunks FROM timescaledb_information.hypertables "
            "WHERE hypertable_name='l2_ticks' LIMIT 1"
        )
    ).first()
    logger.info(f"hypertable: l2_ticks chunks={ht[0] if ht else 'N/A(普通表)'}")
    for r in conn.execute(
        text(
            "SELECT ts, tick_time, symbol FROM l2_ticks "
            "ORDER BY ts DESC LIMIT 5"
        )
    ):
        logger.info(f"  样本: ts={r[0]} tick_time={r[1]} symbol={r[2]}")
    bad = conn.execute(
        text(
            f"SELECT COUNT(*) FROM l2_ticks WHERE tick_time IS NULL OR tick_time !~ {TICK_TIME_RE_SQL}"
        )
    ).scalar()
    logger.info(f"tick_time 非法(保持旧 ts, 不影响查询)行数: {bad}")
    logger.info(f"l2_ticks: {_count(conn, 'l2_ticks')} 行 | l2_ticks_old(回滚用): {_count(conn, 'l2_ticks_old')} 行")


def main() -> None:
    ap = argparse.ArgumentParser(description="l2_ticks 事件时间化回填(方案A)")
    ap.add_argument("--apply", action="store_true", help="正式执行(默认干跑只打印计划)")
    ap.add_argument("--confirm-backup", action="store_true", help="确认已完成 l2_ticks 表级备份")
    ap.add_argument("--force", action="store_true", help="覆盖 08:00-17:00 保守禁改窗口(老板点头后用)")
    args = ap.parse_args()

    note = _maintenance_ok(args.force)
    logger.info(note)
    if not args.apply:
        logger.info("[干跑] 步骤: 预检 → 建回填表(hypertable 1d, 新唯一键含 ts) → 全量变换回填 "
                    "(幂等, 可重跑) → 行数对账(差>0.1%% 中止) → 换名(l2_ticks_old 留回滚) → "
                    "压缩(2d)+retention(90d)+ANALYZE。正式执行加 --apply --confirm-backup")
        return
    if not args.confirm_backup:
        raise SystemExit("拒绝执行: 必须先完成 l2_ticks 表级备份并加 --confirm-backup "
                         "(备份命令见脚本头部 usage)")
    if "保守禁改窗口" in note:
        raise SystemExit(f"拒绝执行: {note}")

    engine = _connect()
    t0 = time.time()
    with engine.begin() as conn:
        conn.execute(text("SET statement_timeout = 0"))
        if _already_migrated(conn):
            logger.info("l2_ticks 唯一键已含 ts(已迁移过), 无需执行; 如需清 l2_ticks_old 请人工处理")
            return
        logger.info("[A] 预检通过, 建回填表 l2_ticks_new …")
        snap = datetime.now(timezone.utc)
        _stage_build(conn)
        logger.info(f"[B] 全量变换回填(79M 行, 预计 10-40 分钟)… snap={snap}")
        _stage_backfill(conn, snap)
    logger.info(f"[A/B] 回填事务提交 / {time.time() - t0:.0f}s")

    with engine.begin() as conn:
        conn.execute(text("SET statement_timeout = 0"))
        conn.execute(text("SET lock_timeout = '30s'"))
        old_n, new_n = _stage_swap(conn)
        logger.info(f"[C] 换名完成: 旧 {old_n} 行 → 新 {new_n} 行")

    with engine.begin() as conn:
        conn.execute(text("SET statement_timeout = 0"))
        _stage_policies(conn)
        _verify(conn)
    logger.info(f"[D] 完成 / 总耗时 {time.time() - t0:.0f}s。l2_ticks_old 保留作回滚, "
                "观察 1-2 个交易日后人工 DROP")


if __name__ == "__main__":
    main()
