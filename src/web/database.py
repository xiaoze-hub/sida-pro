import json
import logging
import os
import shutil
from datetime import datetime
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker, DeclarativeBase
from sqlalchemy.pool import NullPool

from src.web.migrations import has_pending_migrations, run_versioned_migrations

logger = logging.getLogger(__name__)

# 数据库连接(2026-08-17: 双方言兼容改造)
# - 默认 SQLite(现状), 通过环境变量 SIDA_DB_URL 切换 PostgreSQL
# - 例: SIDA_DB_URL="postgresql+psycopg2://sida:xxx@127.0.0.1:5432/sida"
DB_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "data", "panwatch.db")
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

DB_URL = os.environ.get("SIDA_DB_URL", f"sqlite:///{DB_PATH}")

IS_PG = DB_URL.startswith("postgresql")

if IS_PG:
    engine = create_engine(
        DB_URL,
        echo=False,
        pool_pre_ping=True,
        # 修复 2026-08-21: 调大连接池, 实测 size=5 + overflow=10 在 26 并发下被打满,
        # 触发 sqlalchemy.exc.TimeoutError: QueuePool limit of size 5 overflow 10 reached
        # 调为 10 + 20 (30 总上限) 应对 Dashboard 一次刷新 26 API 并发
        pool_size=10,
        max_overflow=20,
        pool_timeout=30,
    )
else:
    engine = create_engine(
        DB_URL,
        echo=False,
        connect_args={
            "timeout": 30,
            "check_same_thread": False,
        },
        poolclass=NullPool,
    )


@event.listens_for(engine, "connect")
def _set_db_pragma(dbapi_conn, connection_record):
    cursor = dbapi_conn.cursor()
    if IS_PG:
        cursor.execute("SET statement_timeout = 30000")  # 30s 语句超时
    else:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=60000")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        # 积极 checkpoint: 减少 WAL 膨胀, 降低锁竞争窗口
        cursor.execute("PRAGMA wal_autocheckpoint=1000")
    cursor.close()


SessionLocal = sessionmaker(bind=engine)


class Base(DeclarativeBase):
    pass


# SQLite 写锁信号量(2026-08-11): WAL 模式读写不互斥, 但写-写互斥。
# 多用户后写请求变多(订阅推送/定时任务), 并发写会互相等待超时 → database is locked。
# 用信号量把并发写限制为 1, 排队而非冲突, 根治锁死。
import threading as _threading
_sqlite_write_lock = _threading.Semaphore(1)


def acquire_write():
    """写操作前调用: SQLite 排队等写锁(最多30s); PG 模式 MVCC 行级锁, 无需排队。"""
    if IS_PG:
        class _Noop:
            def release(self):
                pass
        return _Noop()
    acquired = _sqlite_write_lock.acquire(timeout=30)
    if not acquired:
        raise TimeoutError("数据库写入繁忙, 请稍后重试")
    return _sqlite_write_lock


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    Base.metadata.create_all(bind=engine)
    _migrate(engine)
    _migrate_old_providers(engine)
    _migrate_settings_to_models(engine)
    _migrate_positions_to_accounts(engine)
    _migrate_remove_stock_enabled(engine)
    _migrate_add_user_id_columns(engine)
    if has_pending_migrations(engine):
        _backup_db_before_migration()
    run_versioned_migrations(engine)


def _has_column(conn, table: str, column: str) -> bool:
    try:
        conn.execute(text(f"SELECT {column} FROM {table} LIMIT 1"))
        return True
    except Exception:
        # PG: 失败语句会让事务进入 aborted, 必须 rollback 才能继续后续语句。
        # SQLite: rollback 无副作用(没有未提交事务时等价 no-op)。
        try:
            conn.rollback()
        except Exception:
            pass
        return False


def _has_table(conn, table: str) -> bool:
    try:
        conn.execute(text(f"SELECT 1 FROM {table} LIMIT 1"))
        return True
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return False


def _drop_dangling_ai_provider_fk(conn, table: str) -> None:
    """删掉指向已不存在的 ai_providers 表的悬空外键列。

    背景: _migrate_old_providers 把 ai_providers 表删了,但
    agent_configs.ai_provider_id / stock_agents.ai_provider_id 这两列上的 FK 没清。
    SQLite 默认 PRAGMA foreign_keys 不开,所以历史 INSERT 没事;但某些路径下
    (比如 INSERT ... RETURNING + SQLAlchemy 校验)会报 "no such table: ai_providers"。

    SQLite 3.35+ 支持 ALTER TABLE DROP COLUMN,直接 drop 即可。
    """
    if not _has_column(conn, table, "ai_provider_id"):
        return
    # ai_providers 还存在的话先不动(让 _migrate_old_providers 先迁移)
    if _has_table(conn, "ai_providers"):
        return
    try:
        conn.execute(text(f"ALTER TABLE {table} DROP COLUMN ai_provider_id"))
        conn.commit()
        logger.info(f"已清理 {table}.ai_provider_id 悬空外键列")
    except Exception as e:
        # 老 SQLite 不支持 DROP COLUMN — fallback 留 schema 不动,改用 PRAGMA foreign_keys=OFF
        # (本进程级别,不影响其他业务,因为 ai_providers 不存在,FK 永远无效)
        logger.warning(
            f"DROP COLUMN {table}.ai_provider_id 失败 (SQLite < 3.35?): {e}; "
            f"将通过 PRAGMA foreign_keys=OFF 绕开"
        )
        try:
            conn.execute(text("PRAGMA foreign_keys = OFF"))
            conn.commit()
        except Exception:
            pass


def _ddl_autoincrement(sql_template: str) -> str:
    """方言化建表 SQL: PostgreSQL 用 SERIAL/TIMESTAMP, SQLite 用 AUTOINCREMENT/DATETIME。"""
    if IS_PG:
        return sql_template.format(pk="SERIAL PRIMARY KEY", ts="TIMESTAMP")
    return sql_template.format(pk="INTEGER PRIMARY KEY AUTOINCREMENT", ts="DATETIME")


def _backup_db_before_migration() -> None:
    """Create a timestamped sqlite backup before versioned migrations."""
    if not os.path.exists(DB_PATH):
        return
    try:
        size = os.path.getsize(DB_PATH)
        if size <= 0:
            return
    except Exception:
        return

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = f"{DB_PATH}.bak.{ts}"
    try:
        shutil.copy2(DB_PATH, backup_path)
        logger.info(f"数据库迁移前备份已创建: {backup_path}")
    except Exception as e:
        logger.warning(f"数据库迁移前备份失败: {e}")


def _migrate(engine):
    """增量 schema 迁移（SQLite ALTER TABLE ADD COLUMN）"""
    migrations = [
        # 个人中心(2026-08-15): 昵称/头像
        (
            "users",
            "nickname",
            "ALTER TABLE users ADD COLUMN nickname VARCHAR(64)",
        ),
        (
            "users",
            "avatar",
            "ALTER TABLE users ADD COLUMN avatar VARCHAR(255)",
        ),
        # Phase 1(模拟盘求真):持仓期最高价,移动止损用
        (
            "paper_trading_positions",
            "highest_price",
            "ALTER TABLE paper_trading_positions ADD COLUMN highest_price REAL",
        ),
        (
            "stock_agents",
            "schedule",
            "ALTER TABLE stock_agents ADD COLUMN schedule TEXT DEFAULT ''",
        ),
        (
            "agent_configs",
            "ai_model_id",
            "ALTER TABLE agent_configs ADD COLUMN ai_model_id INTEGER REFERENCES ai_models(id) ON DELETE SET NULL",
        ),
        (
            "agent_configs",
            "notify_channel_ids",
            "ALTER TABLE agent_configs ADD COLUMN notify_channel_ids TEXT DEFAULT '[]'",
        ),
        (
            "stock_agents",
            "ai_model_id",
            "ALTER TABLE stock_agents ADD COLUMN ai_model_id INTEGER REFERENCES ai_models(id) ON DELETE SET NULL",
        ),
        (
            "stock_agents",
            "notify_channel_ids",
            "ALTER TABLE stock_agents ADD COLUMN notify_channel_ids TEXT DEFAULT '[]'",
        ),
        # Phase 3: 持仓增强
        (
            "stocks",
            "invested_amount",
            "ALTER TABLE stocks ADD COLUMN invested_amount REAL",
        ),
        # Phase 4: Agent 执行模式
        (
            "agent_configs",
            "execution_mode",
            "ALTER TABLE agent_configs ADD COLUMN execution_mode TEXT DEFAULT 'batch'",
        ),
        # Phase 4: 持仓交易风格
        (
            "positions",
            "trading_style",
            "ALTER TABLE positions ADD COLUMN trading_style TEXT DEFAULT 'swing'",
        ),
        # 排序字段：关注列表/持仓拖拽排序
        (
            "stocks",
            "sort_order",
            "ALTER TABLE stocks ADD COLUMN sort_order INTEGER DEFAULT 0",
        ),
        (
            "positions",
            "sort_order",
            "ALTER TABLE positions ADD COLUMN sort_order INTEGER DEFAULT 0",
        ),
        # 数据源增强
        (
            "data_sources",
            "supports_batch",
            "ALTER TABLE data_sources ADD COLUMN supports_batch INTEGER DEFAULT 0",
        ),
        (
            "data_sources",
            "test_symbols",
            "ALTER TABLE data_sources ADD COLUMN test_symbols TEXT DEFAULT '[]'",
        ),
        # Phase 5: 建议池元数据
        (
            "stock_suggestions",
            "meta",
            "ALTER TABLE stock_suggestions ADD COLUMN meta TEXT DEFAULT '{}'",
        ),
        # 影子账户画像落库(2026-08-12): users 表加 shadow_profile_json 列
        (
            "users",
            "shadow_profile_json",
            "ALTER TABLE users ADD COLUMN shadow_profile_json TEXT",
        ),
        # 模型功能选型(2026-08-15): ai_models 表加 capabilities 列(逗号分隔能力标签, 空=默认 chat)
        (
            "ai_models",
            "capabilities",
            "ALTER TABLE ai_models ADD COLUMN capabilities TEXT DEFAULT ''",
        ),
    ]
    with engine.connect() as conn:
        for table, column, sql in migrations:
            if not _has_column(conn, table, column):
                conn.execute(text(sql))
                conn.commit()

        # 清理 legacy 悬空外键:agent_configs.ai_provider_id / stock_agents.ai_provider_id
        # 这两列原本 REFERENCES ai_providers(id),但 _migrate_old_providers 已经把
        # ai_providers 表删了。如果保留 FK,新 INSERT 会触发 SQLite "no such table" 错误
        # (因为 SQLite 在 INSERT 时校验 FK 引用的表是否存在)。
        _drop_dangling_ai_provider_fk(conn, "agent_configs")
        _drop_dangling_ai_provider_fk(conn, "stock_agents")

        # 初始化排序字段（仅对未初始化数据）
        if _has_column(conn, "stocks", "sort_order"):
            conn.execute(text("UPDATE stocks SET sort_order = id WHERE sort_order IS NULL OR sort_order = 0"))
            conn.commit()
        if _has_column(conn, "positions", "sort_order"):
            conn.execute(text("UPDATE positions SET sort_order = id WHERE sort_order IS NULL OR sort_order = 0"))
            conn.commit()

        # Create new tables if missing (双方言: SQLite / PostgreSQL)
        if not _has_table(conn, "suggestion_feedback"):
            conn.execute(
                text(
                    _ddl_autoincrement(
                        """CREATE TABLE IF NOT EXISTS suggestion_feedback (
  id {pk},
  suggestion_id INTEGER NOT NULL REFERENCES stock_suggestions(id) ON DELETE CASCADE,
  useful INTEGER DEFAULT 1,
  created_at {ts} DEFAULT CURRENT_TIMESTAMP
);"""
                    )
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_feedback_suggestion_id ON suggestion_feedback(suggestion_id);"
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_feedback_created_at ON suggestion_feedback(created_at);"
                )
            )
            conn.commit()

        # 统一 LLM 配置中心(2026-08-12): 场景-模型绑定表。
        # create_all 通常已建表(ORM 注册), 这里兜底保证存量库直接升级也有该表。
        if not _has_table(conn, "ai_scene_bindings"):
            conn.execute(
                text(
                    _ddl_autoincrement(
                        """CREATE TABLE IF NOT EXISTS ai_scene_bindings (
  id {pk},
  scene VARCHAR(64) NOT NULL UNIQUE,
  model_id INTEGER REFERENCES ai_models(id) ON DELETE SET NULL,
  updated_at {ts} DEFAULT CURRENT_TIMESTAMP
);"""
                    )
                )
            )
            conn.commit()

        # 用户 BYOK AI 服务表(2026-08-15): 同上, create_all 已建(ORM 注册),
        # 这里兜底保证存量库直接升级也有该表。
        if not _has_table(conn, "user_ai_services"):
            conn.execute(
                text(
                    _ddl_autoincrement(
                        """CREATE TABLE IF NOT EXISTS user_ai_services (
  id {pk},
  user_id VARCHAR(36) NOT NULL,
  name VARCHAR NOT NULL,
  base_url VARCHAR NOT NULL,
  api_key VARCHAR DEFAULT '',
  models_json TEXT NOT NULL DEFAULT '[]',
  created_at {ts} DEFAULT CURRENT_TIMESTAMP
);"""
                    )
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_user_ai_services_user_id ON user_ai_services(user_id);"
                )
            )
            conn.commit()

        # 竞价异动池(阶段1.2, v0.3.0): 工作日 09:25 竞价异动股落库。
        # create_all 通常已建表(ORM 注册), 这里兜底保证存量库直接升级也有该表。
        if not _has_table(conn, "auction_anomaly_records"):
            conn.execute(
                text(
                    _ddl_autoincrement(
                        """CREATE TABLE IF NOT EXISTS auction_anomaly_records (
  id {pk},
  symbol VARCHAR(16) NOT NULL,
  name VARCHAR(64) DEFAULT '',
  gap_pct REAL,
  withdraw_rate REAL,
  volume_ratio REAL,
  note VARCHAR(255) DEFAULT '',
  created_at {ts} DEFAULT CURRENT_TIMESTAMP
);"""
                    )
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_auction_anomaly_sym ON auction_anomaly_records(symbol);"
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_auction_anomaly_sym_created ON auction_anomaly_records(symbol, created_at);"
                )
            )
            conn.commit()


def _insert_returning_id(conn, insert_sql: str, params: dict) -> int:
    """插入新行后方言无关地取自增主键 id。

    2026-08-27 fix (5+1 评审 B 轨): ``SELECT last_insert_rowid()`` 是 SQLite 专属,
    PG 下报 ``function last_insert_rowid() does not exist`` 触发即启动失败。
    PG 用 INSERT ... RETURNING id; SQLite 保留 last_insert_rowid(全版本兼容)。
    """
    if IS_PG:
        return conn.execute(text(f"{insert_sql} RETURNING id"), params).scalar()
    conn.execute(text(insert_sql), params)
    return conn.execute(text("SELECT last_insert_rowid()")).scalar()


def _migrate_old_providers(engine):
    """如果存在旧的 ai_providers 表，迁移数据到 ai_services + ai_models"""
    with engine.connect() as conn:
        if not _has_table(conn, "ai_providers"):
            return
        # Check if it has the old schema (has base_url column)
        if not _has_column(conn, "ai_providers", "base_url"):
            return

        rows = conn.execute(
            text(
                "SELECT id, name, base_url, api_key, model, is_default FROM ai_providers"
            )
        ).fetchall()
        if not rows:
            conn.execute(text("DROP TABLE IF EXISTS ai_providers"))
            conn.commit()
            return

        # Group by base_url+api_key to create services
        service_map = {}  # (base_url, api_key) -> service_id
        for row in rows:
            old_id, name, base_url, api_key, model, is_default = row
            key = (base_url, api_key)
            if key not in service_map:
                # Create service
                result = _insert_returning_id(
                    conn,
                    "INSERT INTO ai_services (name, base_url, api_key) VALUES (:name, :base_url, :api_key)",
                    {"name": name, "base_url": base_url, "api_key": api_key},
                )
                service_map[key] = result

            service_id = service_map[key]
            new_model_id = _insert_returning_id(
                conn,
                "INSERT INTO ai_models (name, service_id, model, is_default) VALUES (:name, :service_id, :model, :is_default)",
                {
                    "name": name,
                    "service_id": service_id,
                    "model": model,
                    "is_default": is_default,
                },
            )

            # Update references: agent_configs.ai_provider_id → ai_model_id
            if _has_column(conn, "agent_configs", "ai_provider_id"):
                conn.execute(
                    text(
                        "UPDATE agent_configs SET ai_model_id = :new_id WHERE ai_provider_id = :old_id"
                    ),
                    {"new_id": new_model_id, "old_id": old_id},
                )
            # stock_agents.ai_provider_id → ai_model_id
            if _has_column(conn, "stock_agents", "ai_provider_id"):
                conn.execute(
                    text(
                        "UPDATE stock_agents SET ai_model_id = :new_id WHERE ai_provider_id = :old_id"
                    ),
                    {"new_id": new_model_id, "old_id": old_id},
                )

        conn.execute(text("DROP TABLE ai_providers"))
        conn.commit()
        logger.info(
            f"已迁移 {len(rows)} 条旧 AI Provider 数据到 ai_services + ai_models"
        )


def _migrate_settings_to_models(engine):
    """将旧的 app_settings 中的 AI/通知配置迁移为 AIService+AIModel / NotifyChannel 记录"""
    with engine.connect() as conn:
        if not _has_table(conn, "app_settings"):
            return

        rows = conn.execute(text("SELECT key, value FROM app_settings")).fetchall()
        settings_map = {row[0]: row[1] for row in rows}

        ai_base_url = settings_map.get("ai_base_url", "")
        ai_api_key = settings_map.get("ai_api_key", "")
        ai_model = settings_map.get("ai_model", "")

        # Migrate AI settings if present and no services exist yet
        if ai_base_url and ai_model:
            existing = conn.execute(text("SELECT COUNT(*) FROM ai_services")).scalar()
            if existing == 0:
                service_id = _insert_returning_id(
                    conn,
                    "INSERT INTO ai_services (name, base_url, api_key) VALUES (:name, :base_url, :api_key)",
                    {"name": ai_model, "base_url": ai_base_url, "api_key": ai_api_key},
                )
                conn.execute(
                    text(
                        "INSERT INTO ai_models (name, service_id, model, is_default) VALUES (:name, :service_id, :model, 1)"
                    ),
                    {"name": ai_model, "service_id": service_id, "model": ai_model},
                )
                logger.info(f"已迁移 AI 配置: {ai_model}")

        # Migrate Telegram settings if present and no channels exist yet
        bot_token = settings_map.get("notify_telegram_bot_token", "")
        chat_id = settings_map.get("notify_telegram_chat_id", "")

        if bot_token:
            existing = conn.execute(
                text("SELECT COUNT(*) FROM notify_channels")
            ).scalar()
            if existing == 0:
                config_json = json.dumps({"bot_token": bot_token, "chat_id": chat_id})
                conn.execute(
                    text(
                        "INSERT INTO notify_channels (name, type, config, enabled, is_default) VALUES (:name, :type, :config, 1, 1)"
                    ),
                    {"name": "Telegram", "type": "telegram", "config": config_json},
                )
                logger.info("已迁移 Telegram 配置为 NotifyChannel")

        # Remove old settings keys
        old_keys = [
            "ai_base_url",
            "ai_api_key",
            "ai_model",
            "notify_telegram_bot_token",
            "notify_telegram_chat_id",
        ]
        for key in old_keys:
            if key in settings_map:
                conn.execute(
                    text("DELETE FROM app_settings WHERE key = :key"), {"key": key}
                )

        conn.commit()


def _migrate_positions_to_accounts(engine):
    """
    将旧的 stocks 表中的持仓数据迁移到 accounts + positions 表
    创建一个默认账户，并将有持仓的股票数据迁移过去
    """
    with engine.connect() as conn:
        # 检查是否已有账户数据（避免重复迁移）
        if not _has_table(conn, "accounts"):
            return

        existing_accounts = conn.execute(text("SELECT COUNT(*) FROM accounts")).scalar()
        if existing_accounts > 0:
            return

        # 检查 stocks 表是否有持仓数据需要迁移
        if not _has_column(conn, "stocks", "cost_price"):
            return

        stocks_with_position = conn.execute(
            text(
                "SELECT id, cost_price, quantity, invested_amount FROM stocks "
                "WHERE cost_price IS NOT NULL AND quantity IS NOT NULL"
            )
        ).fetchall()

        if not stocks_with_position:
            # 没有持仓数据，创建一个空的默认账户
            conn.execute(
                text(
                    "INSERT INTO accounts (name, available_funds, enabled) VALUES ('默认账户', 0, 1)"
                )
            )
            conn.commit()
            logger.info("已创建默认账户")
            return

        # 创建默认账户
        # 先获取旧的 available_funds 设置
        old_funds = conn.execute(
            text("SELECT value FROM app_settings WHERE key = 'available_funds'")
        ).scalar()
        available_funds = float(old_funds) if old_funds else 0

        account_id = _insert_returning_id(
            conn,
            "INSERT INTO accounts (name, available_funds, enabled) VALUES (:name, :funds, 1)",
            {"name": "默认账户", "funds": available_funds},
        )

        # 迁移持仓数据
        for row in stocks_with_position:
            stock_id, cost_price, quantity, invested_amount = row
            conn.execute(
                text(
                    "INSERT INTO positions (account_id, stock_id, cost_price, quantity, invested_amount) "
                    "VALUES (:account_id, :stock_id, :cost_price, :quantity, :invested_amount)"
                ),
                {
                    "account_id": account_id,
                    "stock_id": stock_id,
                    "cost_price": cost_price,
                    "quantity": quantity,
                    "invested_amount": invested_amount,
                },
            )

        # 删除旧的 available_funds 设置
        conn.execute(text("DELETE FROM app_settings WHERE key = 'available_funds'"))

        conn.commit()
        logger.info(f"已迁移 {len(stocks_with_position)} 条持仓数据到默认账户")


def _migrate_remove_stock_enabled(engine):
    """移除历史 stocks.enabled 软删除字段并清理残留数据。"""
    with engine.connect() as conn:
        if not _has_table(conn, "stocks") or not _has_column(conn, "stocks", "enabled"):
            return

        # 历史软删除数据：无任何关联则直接删除；有关联则恢复为有效股票。
        conn.execute(
            text(
                """
DELETE FROM stocks
WHERE COALESCE(enabled, 1) = 0
  AND id NOT IN (SELECT DISTINCT stock_id FROM positions)
  AND id NOT IN (SELECT DISTINCT stock_id FROM stock_agents)
  AND id NOT IN (SELECT DISTINCT stock_id FROM price_alert_rules)
"""
            )
        )
        conn.execute(text("UPDATE stocks SET enabled = 1 WHERE COALESCE(enabled, 1) = 0"))
        conn.commit()

        # 优先直接删列；旧版 SQLite 不支持时，重建表以确保物理移除。
        try:
            conn.execute(text("ALTER TABLE stocks DROP COLUMN enabled"))
            conn.commit()
            logger.info("已移除 stocks.enabled 列")
        except Exception:
            conn.rollback()
            logger.info("当前 SQLite 不支持 DROP COLUMN，改为重建 stocks 表移除 enabled")
            conn.execute(text("PRAGMA foreign_keys=OFF"))
            conn.execute(
                text(
                    """
CREATE TABLE IF NOT EXISTS stocks__new (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  symbol VARCHAR NOT NULL,
  name VARCHAR NOT NULL,
  market VARCHAR NOT NULL,
  cost_price FLOAT,
  quantity INTEGER,
  invested_amount FLOAT,
  sort_order INTEGER DEFAULT 0,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
)
"""
                )
            )
            conn.execute(
                text(
                    """
INSERT INTO stocks__new (
  id, symbol, name, market, cost_price, quantity, invested_amount, sort_order, created_at, updated_at
)
SELECT
  id, symbol, name, market, cost_price, quantity, invested_amount, COALESCE(sort_order, 0), created_at, updated_at
FROM stocks
"""
                )
            )
            conn.execute(text("DROP TABLE stocks"))
            conn.execute(text("ALTER TABLE stocks__new RENAME TO stocks"))
            conn.execute(text("PRAGMA foreign_keys=ON"))
            conn.commit()
            logger.info("已通过重建表移除 stocks.enabled 列")


def _migrate_add_user_id_columns(engine):
    """多用户阶段2: 业务表加 user_id 列 + 旧数据归 owner(2026-08-10)。

    - notify_channels/accounts/stocks/positions/agent_runs/analysis_history 加 user_id
    - 旧数据(NULL)自动归 owner(users 表第一个 owner)
    - 新建的 users 表由 create_all 自动创建, 此迁移只处理存量表加列
    """
    tables = ["notify_channels", "accounts", "stocks", "positions", "agent_runs", "analysis_history"]
    with engine.connect() as conn:
        # 确定 owner id(无用户表则跳过——首次部署 create_all 已建)
        try:
            owner_row = conn.execute(
                text("SELECT id FROM users WHERE role='owner' ORDER BY created_at LIMIT 1")
            ).fetchone()
        except Exception:
            return
        owner_id = owner_row[0] if owner_row else None
        if not owner_id:
            return

        for table in tables:
            try:
                if not _has_table(conn, table):
                    continue
                has_col = _has_column(conn, table, "user_id")
                if not has_col:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN user_id VARCHAR(36)"))
                    conn.execute(
                        text(f"CREATE INDEX IF NOT EXISTS ix_{table}_user_id ON {table} (user_id)")
                    )
                    logger.info(f"多用户迁移: {table}.user_id 列已加")
                # 旧数据归 owner(列存在也要执行, 兼容建表后存量数据)
                conn.execute(
                    text(f"UPDATE {table} SET user_id = :owner WHERE user_id IS NULL"),
                    {"owner": owner_id},
                )
                conn.commit()
                logger.info(f"多用户迁移: {table} 旧数据归 owner")
            except Exception as e:
                conn.rollback()
                logger.warning(f"多用户迁移 {table} 失败: {e}")
