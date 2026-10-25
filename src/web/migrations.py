"""Versioned database migrations for PanWatch."""

from __future__ import annotations

import hashlib
import inspect
import logging
import json
from dataclasses import dataclass
from datetime import date
from typing import Callable

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine


logger = logging.getLogger(__name__)

# W1.5(A5) 2026-09-08: PG 多实例并发启动时用会话级 advisory lock 串行化迁移。
# 固定常量 —— 换值会导致新旧实例之间锁不互斥。
_MIGRATION_LOCK_KEY = 729138


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    runner: Callable[[Connection], None]

    @property
    def checksum(self) -> str:
        try:
            body = inspect.getsource(self.runner)
        except Exception:
            body = self.name
        raw = f"{self.version}:{self.name}:{body}".encode("utf-8")
        return hashlib.sha256(raw).hexdigest()


def _dialect_is_pg(conn: Connection) -> bool:
    return conn.dialect.name == "postgresql"


def _has_table(conn: Connection, table: str) -> bool:
    if _dialect_is_pg(conn):
        row = conn.execute(
            text(
                """
SELECT table_name AS name
FROM information_schema.tables
WHERE table_schema = 'public' AND table_name = :table
LIMIT 1
"""
            ),
            {"table": table},
        ).first()
    else:
        row = conn.execute(
            text(
                """
SELECT name
FROM sqlite_master
WHERE type='table' AND name=:table
LIMIT 1
"""
            ),
            {"table": table},
        ).first()
    return bool(row)


def _has_column(conn: Connection, table: str, column: str) -> bool:
    if not _has_table(conn, table):
        return False
    if _dialect_is_pg(conn):
        rows = conn.execute(
            text(
                """
SELECT column_name
FROM information_schema.columns
WHERE table_schema = 'public' AND table_name = :table AND column_name = :column
LIMIT 1
"""
            ),
            {"table": table, "column": column},
        ).fetchall()
        return bool(rows)
    rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
    for r in rows:
        # PRAGMA table_info schema: cid, name, type, notnull, dflt_value, pk
        if len(r) > 1 and str(r[1]) == column:
            return True
    return False


def _add_column_if_missing(conn: Connection, table: str, column: str, sql: str) -> None:
    if not _has_table(conn, table):
        return
    if not _has_column(conn, table, column):
        conn.execute(text(sql))


def _create_index_if_missing(conn: Connection, name: str, sql: str) -> None:
    if _dialect_is_pg(conn):
        row = conn.execute(
            text(
                """
SELECT indexname AS name
FROM pg_indexes
WHERE schemaname = 'public' AND indexname = :name
LIMIT 1
"""
            ),
            {"name": name},
        ).first()
    else:
        row = conn.execute(
            text(
                """
SELECT name
FROM sqlite_master
WHERE type='index' AND name=:name
LIMIT 1
"""
            ),
            {"name": name},
        ).first()
    if not row:
        conn.execute(text(sql))


def _ensure_schema_table(conn: Connection) -> None:
    conn.execute(
        text(
            """
CREATE TABLE IF NOT EXISTS schema_migrations (
  version INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  checksum TEXT NOT NULL,
  applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  success INTEGER NOT NULL DEFAULT 0,
  error TEXT DEFAULT ''
)
"""
        )
    )


def _m101_agent_config_kind(conn: Connection) -> None:
    _add_column_if_missing(
        conn,
        "agent_configs",
        "kind",
        "ALTER TABLE agent_configs ADD COLUMN kind TEXT DEFAULT 'workflow'",
    )
    _add_column_if_missing(
        conn,
        "agent_configs",
        "visible",
        "ALTER TABLE agent_configs ADD COLUMN visible BOOLEAN DEFAULT TRUE",
    )
    _add_column_if_missing(
        conn,
        "agent_configs",
        "lifecycle_status",
        "ALTER TABLE agent_configs ADD COLUMN lifecycle_status TEXT DEFAULT 'active'",
    )
    _add_column_if_missing(
        conn,
        "agent_configs",
        "replaced_by",
        "ALTER TABLE agent_configs ADD COLUMN replaced_by TEXT DEFAULT ''",
    )
    _add_column_if_missing(
        conn,
        "agent_configs",
        "display_order",
        "ALTER TABLE agent_configs ADD COLUMN display_order INTEGER DEFAULT 0",
    )


def _m102_backfill_agent_kind(conn: Connection) -> None:
    if not _has_table(conn, "agent_configs"):
        return

    conn.execute(
        text(
            """
UPDATE agent_configs
SET kind = 'workflow'
WHERE kind IS NULL OR TRIM(kind) = ''
"""
        )
    )
    conn.execute(
        text(
            """
UPDATE agent_configs
SET kind = 'capability',
    visible = false,
    lifecycle_status = 'deprecated',
    replaced_by = CASE
      WHEN name = 'news_digest' THEN 'premarket_outlook,daily_report,intraday_monitor'
      WHEN name = 'chart_analyst' THEN 'intraday_monitor,daily_report,premarket_outlook'
      ELSE replaced_by
    END,
    enabled = false,
    schedule = ''
WHERE name IN ('news_digest', 'chart_analyst')
"""
        )
    )
    conn.execute(
        text(
            """
UPDATE agent_configs
SET kind = 'workflow',
    visible = true,
    lifecycle_status = 'active',
    replaced_by = ''
WHERE name IN ('premarket_outlook', 'intraday_monitor', 'daily_report')
"""
        )
    )
    conn.execute(
        text(
            """
UPDATE agent_configs
SET display_name = '收盘复盘'
WHERE name = 'daily_report'
  AND (display_name IS NULL OR TRIM(display_name) = '' OR display_name = '盘后日报')
"""
        )
    )
    conn.execute(
        text(
            """
UPDATE agent_configs
SET display_order = CASE name
  WHEN 'premarket_outlook' THEN 10
  WHEN 'intraday_monitor' THEN 20
  WHEN 'daily_report' THEN 30
  WHEN 'news_digest' THEN 110
  WHEN 'chart_analyst' THEN 120
  ELSE display_order
END
"""
        )
    )


def _m103_agent_run_observability(conn: Connection) -> None:
    _add_column_if_missing(
        conn,
        "agent_runs",
        "trace_id",
        "ALTER TABLE agent_runs ADD COLUMN trace_id TEXT DEFAULT ''",
    )
    _add_column_if_missing(
        conn,
        "agent_runs",
        "trigger_source",
        "ALTER TABLE agent_runs ADD COLUMN trigger_source TEXT DEFAULT ''",
    )
    _add_column_if_missing(
        conn,
        "agent_runs",
        "notify_attempted",
        "ALTER TABLE agent_runs ADD COLUMN notify_attempted BOOLEAN DEFAULT FALSE",
    )
    _add_column_if_missing(
        conn,
        "agent_runs",
        "notify_sent",
        "ALTER TABLE agent_runs ADD COLUMN notify_sent BOOLEAN DEFAULT FALSE",
    )
    _add_column_if_missing(
        conn,
        "agent_runs",
        "context_chars",
        "ALTER TABLE agent_runs ADD COLUMN context_chars INTEGER DEFAULT 0",
    )
    _add_column_if_missing(
        conn,
        "agent_runs",
        "model_label",
        "ALTER TABLE agent_runs ADD COLUMN model_label TEXT DEFAULT ''",
    )


def _m104_history_kind_snapshot(conn: Connection) -> None:
    _add_column_if_missing(
        conn,
        "analysis_history",
        "agent_kind_snapshot",
        "ALTER TABLE analysis_history ADD COLUMN agent_kind_snapshot TEXT DEFAULT ''",
    )

    if not _has_table(conn, "analysis_history"):
        return

    conn.execute(
        text(
            """
UPDATE analysis_history
SET agent_kind_snapshot = CASE
  WHEN agent_name IN ('news_digest', 'chart_analyst') THEN 'capability'
  ELSE 'workflow'
END
WHERE agent_kind_snapshot IS NULL OR TRIM(agent_kind_snapshot) = ''
"""
        )
    )


def _m105_indexes(conn: Connection) -> None:
    if _has_table(conn, "agent_configs"):
        _create_index_if_missing(
            conn,
            "ix_agent_configs_kind_visible",
            "CREATE INDEX ix_agent_configs_kind_visible ON agent_configs(kind, visible)",
        )
        _create_index_if_missing(
            conn,
            "ix_agent_configs_order",
            "CREATE INDEX ix_agent_configs_order ON agent_configs(display_order, name)",
        )
    if _has_table(conn, "agent_runs"):
        _create_index_if_missing(
            conn,
            "ix_agent_runs_agent_created",
            "CREATE INDEX ix_agent_runs_agent_created ON agent_runs(agent_name, created_at)",
        )
    if _has_table(conn, "analysis_history"):
        _create_index_if_missing(
            conn,
            "ix_analysis_history_kind_date",
            "CREATE INDEX ix_analysis_history_kind_date ON analysis_history(agent_kind_snapshot, analysis_date)",
        )
        _create_index_if_missing(
            conn,
            "ix_analysis_history_agent_updated",
            "CREATE INDEX ix_analysis_history_agent_updated ON analysis_history(agent_name, updated_at)",
        )


def _m106_log_observability(conn: Connection) -> None:
    _add_column_if_missing(
        conn,
        "log_entries",
        "trace_id",
        "ALTER TABLE log_entries ADD COLUMN trace_id TEXT DEFAULT ''",
    )
    _add_column_if_missing(
        conn,
        "log_entries",
        "run_id",
        "ALTER TABLE log_entries ADD COLUMN run_id TEXT DEFAULT ''",
    )
    _add_column_if_missing(
        conn,
        "log_entries",
        "agent_name",
        "ALTER TABLE log_entries ADD COLUMN agent_name TEXT DEFAULT ''",
    )
    _add_column_if_missing(
        conn,
        "log_entries",
        "event",
        "ALTER TABLE log_entries ADD COLUMN event TEXT DEFAULT ''",
    )
    _add_column_if_missing(
        conn,
        "log_entries",
        "tags",
        "ALTER TABLE log_entries ADD COLUMN tags TEXT DEFAULT '{}'",
    )
    _add_column_if_missing(
        conn,
        "log_entries",
        "notify_status",
        "ALTER TABLE log_entries ADD COLUMN notify_status TEXT DEFAULT ''",
    )
    _add_column_if_missing(
        conn,
        "log_entries",
        "notify_reason",
        "ALTER TABLE log_entries ADD COLUMN notify_reason TEXT DEFAULT ''",
    )

    if _has_table(conn, "log_entries"):
        _create_index_if_missing(
            conn,
            "ix_log_entries_time_id",
            "CREATE INDEX ix_log_entries_time_id ON log_entries(timestamp, id)",
        )
        _create_index_if_missing(
            conn,
            "ix_log_entries_trace",
            "CREATE INDEX ix_log_entries_trace ON log_entries(trace_id)",
        )
        _create_index_if_missing(
            conn,
            "ix_log_entries_agent_event",
            "CREATE INDEX ix_log_entries_agent_event ON log_entries(agent_name, event)",
        )


def _m107_suggestion_market_dimension(conn: Connection) -> None:
    _add_column_if_missing(
        conn,
        "stock_suggestions",
        "stock_market",
        "ALTER TABLE stock_suggestions ADD COLUMN stock_market TEXT DEFAULT 'CN'",
    )

    if not _has_table(conn, "stock_suggestions"):
        return

    # 历史数据平滑回填：优先从 stocks 里推断 market，否则回退 CN。
    conn.execute(
        text(
            """
UPDATE stock_suggestions
SET stock_market = COALESCE(
    (
      SELECT s.market
      FROM stocks s
      WHERE s.symbol = stock_suggestions.stock_symbol
      ORDER BY CASE WHEN s.market='CN' THEN 0 ELSE 1 END, s.id ASC
      LIMIT 1
    ),
    'CN'
)
WHERE stock_market IS NULL OR TRIM(stock_market) = ''
"""
        )
    )
    _create_index_if_missing(
        conn,
        "ix_suggestion_market_symbol_time",
        "CREATE INDEX ix_suggestion_market_symbol_time ON stock_suggestions(stock_market, stock_symbol, created_at)",
    )
    _create_index_if_missing(
        conn,
        "ix_suggestion_market_expires",
        "CREATE INDEX ix_suggestion_market_expires ON stock_suggestions(stock_market, expires_at)",
    )


def _m108_entry_candidates_table(conn: Connection) -> None:
    conn.execute(
        text(
            """
CREATE TABLE IF NOT EXISTS entry_candidates (
  id SERIAL PRIMARY KEY,
  stock_symbol TEXT NOT NULL,
  stock_market TEXT NOT NULL DEFAULT 'CN',
  stock_name TEXT DEFAULT '',
  snapshot_date TEXT NOT NULL,
  status TEXT DEFAULT 'active',
  score REAL NOT NULL DEFAULT 0,
  confidence REAL,
  action TEXT NOT NULL DEFAULT 'watch',
  action_label TEXT NOT NULL DEFAULT '观望',
  signal TEXT DEFAULT '',
  reason TEXT DEFAULT '',
  candidate_source TEXT NOT NULL DEFAULT 'watchlist',
  strategy_tags JSON DEFAULT '[]',
  is_holding_snapshot BOOLEAN DEFAULT FALSE,
  plan_quality INTEGER DEFAULT 0,
  entry_low REAL,
  entry_high REAL,
  stop_loss REAL,
  target_price REAL,
  invalidation TEXT DEFAULT '',
  source_agent TEXT DEFAULT '',
  source_suggestion_id INTEGER,
  source_trace_id TEXT DEFAULT '',
  evidence JSON DEFAULT '[]',
  plan JSON DEFAULT '{}',
  meta JSON DEFAULT '{}',
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT uq_entry_candidate_stock_date UNIQUE(stock_symbol, stock_market, snapshot_date)
)
"""
        )
    )
    _create_index_if_missing(
        conn,
        "ix_entry_candidate_score_date",
        "CREATE INDEX ix_entry_candidate_score_date ON entry_candidates(snapshot_date, score)",
    )
    _create_index_if_missing(
        conn,
        "ix_entry_candidate_status_updated",
        "CREATE INDEX ix_entry_candidate_status_updated ON entry_candidates(status, updated_at)",
    )

    # 历史平滑迁移：将每个市场/股票最新建议回填为“今日候选”基线记录。
    # W3.1(D2): meta 列历史上由 A 层迁移(现 143 号)先补; 本迁移必须自洽 ——
    # 更老的存量库可能没有 meta 列, 缺列时跳过回填(建表/索引已在前)。
    if not _has_column(conn, "stock_suggestions", "meta"):
        return
    today = date.today().strftime("%Y-%m-%d")
    conn.execute(
        text(
            """
INSERT INTO entry_candidates (
  stock_symbol, stock_market, stock_name, snapshot_date,
  status, score, action, action_label, signal, reason,
  candidate_source, source_agent, source_suggestion_id, evidence, plan, meta
)
SELECT
  s.stock_symbol,
  COALESCE(NULLIF(TRIM(s.stock_market), ''), 'CN') AS stock_market,
  COALESCE(s.stock_name, ''),
  :today AS snapshot_date,
  CASE
    WHEN s.action IN ('buy', 'add', 'hold', 'watch') THEN 'active'
    ELSE 'inactive'
  END AS status,
  CASE
    WHEN s.action = 'buy' THEN 78
    WHEN s.action = 'add' THEN 72
    WHEN s.action = 'hold' THEN 58
    WHEN s.action = 'watch' THEN 50
    ELSE 30
  END AS score,
  COALESCE(s.action, 'watch'),
  COALESCE(s.action_label, '观望'),
  COALESCE(s.signal, ''),
  COALESCE(s.reason, ''),
  'watchlist',
  COALESCE(s.agent_name, ''),
  s.id,
  '[]',
  '{}',
  COALESCE(s.meta, '{}')
FROM stock_suggestions s
JOIN (
  SELECT stock_symbol, COALESCE(NULLIF(TRIM(stock_market), ''), 'CN') AS stock_market, MAX(id) AS max_id
  FROM stock_suggestions
  GROUP BY stock_symbol, COALESCE(NULLIF(TRIM(stock_market), ''), 'CN')
) latest
ON latest.max_id = s.id ON CONFLICT DO NOTHING
"""
        ),
        {"today": today},
    )


def _m109_entry_candidate_upgrade(conn: Connection) -> None:
    _add_column_if_missing(
        conn,
        "entry_candidates",
        "candidate_source",
        "ALTER TABLE entry_candidates ADD COLUMN candidate_source TEXT DEFAULT 'watchlist'",
    )
    _add_column_if_missing(
        conn,
        "entry_candidates",
        "strategy_tags",
        "ALTER TABLE entry_candidates ADD COLUMN strategy_tags JSON DEFAULT '[]'",
    )
    _add_column_if_missing(
        conn,
        "entry_candidates",
        "is_holding_snapshot",
        "ALTER TABLE entry_candidates ADD COLUMN is_holding_snapshot BOOLEAN DEFAULT FALSE",
    )
    _add_column_if_missing(
        conn,
        "entry_candidates",
        "plan_quality",
        "ALTER TABLE entry_candidates ADD COLUMN plan_quality INTEGER DEFAULT 0",
    )
    _create_index_if_missing(
        conn,
        "ix_entry_candidate_source_score",
        "CREATE INDEX ix_entry_candidate_source_score ON entry_candidates(candidate_source, score)",
    )
    _create_index_if_missing(
        conn,
        "ix_entry_candidate_market_status",
        "CREATE INDEX ix_entry_candidate_market_status ON entry_candidates(stock_market, status)",
    )

    conn.execute(
        text(
            """
CREATE TABLE IF NOT EXISTS entry_candidate_feedback (
  id SERIAL PRIMARY KEY,
  snapshot_date TEXT DEFAULT '',
  stock_symbol TEXT NOT NULL,
  stock_market TEXT NOT NULL DEFAULT 'CN',
  candidate_source TEXT NOT NULL DEFAULT 'watchlist',
  strategy_tags JSON DEFAULT '[]',
  useful BOOLEAN DEFAULT TRUE,
  reason TEXT DEFAULT '',
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""
        )
    )
    _create_index_if_missing(
        conn,
        "ix_entry_feedback_time",
        "CREATE INDEX ix_entry_feedback_time ON entry_candidate_feedback(created_at)",
    )
    _create_index_if_missing(
        conn,
        "ix_entry_feedback_symbol_day",
        "CREATE INDEX ix_entry_feedback_symbol_day ON entry_candidate_feedback(stock_market, stock_symbol, snapshot_date)",
    )
    _create_index_if_missing(
        conn,
        "ix_entry_feedback_source",
        "CREATE INDEX ix_entry_feedback_source ON entry_candidate_feedback(candidate_source)",
    )


def _m110_entry_candidate_outcomes(conn: Connection) -> None:
    conn.execute(
        text(
            """
CREATE TABLE IF NOT EXISTS entry_candidate_outcomes (
  id SERIAL PRIMARY KEY,
  candidate_id INTEGER NOT NULL REFERENCES entry_candidates(id) ON DELETE CASCADE,
  snapshot_date TEXT DEFAULT '',
  stock_symbol TEXT NOT NULL,
  stock_market TEXT NOT NULL DEFAULT 'CN',
  candidate_source TEXT NOT NULL DEFAULT 'watchlist',
  strategy_tags JSON DEFAULT '[]',
  horizon_days INTEGER NOT NULL DEFAULT 1,
  target_date TEXT DEFAULT '',
  base_price REAL,
  outcome_price REAL,
  outcome_return_pct REAL,
  hit_target BOOLEAN,
  hit_stop BOOLEAN,
  outcome_status TEXT DEFAULT 'pending',
  meta TEXT DEFAULT '{}',
  evaluated_at TIMESTAMP,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT uq_entry_outcome_candidate_horizon UNIQUE(candidate_id, horizon_days)
)
"""
        )
    )
    _create_index_if_missing(
        conn,
        "ix_entry_outcome_status_horizon",
        "CREATE INDEX ix_entry_outcome_status_horizon ON entry_candidate_outcomes(outcome_status, horizon_days)",
    )
    _create_index_if_missing(
        conn,
        "ix_entry_outcome_symbol_day",
        "CREATE INDEX ix_entry_outcome_symbol_day ON entry_candidate_outcomes(stock_market, stock_symbol, snapshot_date)",
    )


def _m111_strategy_layer(conn: Connection) -> None:
    # 双方言兼容 (2026-08-31 SIDA-Pro 骨架验证):
    #   - PG: ON CONFLICT DO NOTHING (SELECT INTO INSERT 末尾)
    #   - SQLite: 不支持 ON CONFLICT, 改 OR IGNORE 放在 INSERT 头部
    is_pg = _dialect_is_pg(conn)
    conflict_clause = "ON CONFLICT DO NOTHING" if is_pg else ""
    insert_prefix = "" if is_pg else "OR IGNORE "  # OR IGNORE 紧跟 INSERT INTO
    conn.execute(
        text(
            """
CREATE TABLE IF NOT EXISTS strategy_catalog (
  id SERIAL PRIMARY KEY,
  code TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  description TEXT DEFAULT '',
  version TEXT DEFAULT 'v1',
  enabled BOOLEAN DEFAULT TRUE,
  market_scope TEXT DEFAULT 'ALL',
  risk_level TEXT DEFAULT 'medium',
  params TEXT DEFAULT '{}',
  default_weight REAL DEFAULT 1.0,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""
        )
    )
    _create_index_if_missing(
        conn,
        "ix_strategy_catalog_enabled",
        "CREATE INDEX ix_strategy_catalog_enabled ON strategy_catalog(enabled)",
    )

    conn.execute(
        text(
            """
CREATE TABLE IF NOT EXISTS strategy_signal_runs (
  id SERIAL PRIMARY KEY,
  snapshot_date TEXT NOT NULL,
  stock_symbol TEXT NOT NULL,
  stock_market TEXT NOT NULL DEFAULT 'CN',
  stock_name TEXT DEFAULT '',
  strategy_code TEXT NOT NULL,
  strategy_name TEXT DEFAULT '',
  strategy_version TEXT DEFAULT 'v1',
  risk_level TEXT DEFAULT 'medium',
  source_pool TEXT DEFAULT 'watchlist',
  score REAL NOT NULL DEFAULT 0,
  rank_score REAL NOT NULL DEFAULT 0,
  confidence REAL,
  status TEXT DEFAULT 'active',
  action TEXT DEFAULT 'watch',
  action_label TEXT DEFAULT '观望',
  signal TEXT DEFAULT '',
  reason TEXT DEFAULT '',
  evidence TEXT DEFAULT '[]',
  holding_days INTEGER DEFAULT 3,
  entry_low REAL,
  entry_high REAL,
  stop_loss REAL,
  target_price REAL,
  invalidation TEXT DEFAULT '',
  plan_quality INTEGER DEFAULT 0,
  source_agent TEXT DEFAULT '',
  source_suggestion_id INTEGER,
  source_candidate_id INTEGER,
  trace_id TEXT DEFAULT '',
  is_holding_snapshot BOOLEAN DEFAULT FALSE,
  context_quality_score REAL,
  payload TEXT DEFAULT '{}',
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT uq_strategy_signal_daily_unique UNIQUE(snapshot_date, stock_symbol, stock_market, strategy_code, source_candidate_id)
)
"""
        )
    )
    _create_index_if_missing(
        conn,
        "ix_strategy_signal_snapshot_rank",
        "CREATE INDEX ix_strategy_signal_snapshot_rank ON strategy_signal_runs(snapshot_date, rank_score)",
    )
    _create_index_if_missing(
        conn,
        "ix_strategy_signal_strategy_market",
        "CREATE INDEX ix_strategy_signal_strategy_market ON strategy_signal_runs(strategy_code, stock_market)",
    )
    _create_index_if_missing(
        conn,
        "ix_strategy_signal_status",
        "CREATE INDEX ix_strategy_signal_status ON strategy_signal_runs(status, updated_at)",
    )

    conn.execute(
        text(
            """
CREATE TABLE IF NOT EXISTS strategy_outcomes (
  id SERIAL PRIMARY KEY,
  signal_run_id INTEGER NOT NULL REFERENCES strategy_signal_runs(id) ON DELETE CASCADE,
  strategy_code TEXT NOT NULL,
  snapshot_date TEXT DEFAULT '',
  stock_symbol TEXT NOT NULL,
  stock_market TEXT NOT NULL DEFAULT 'CN',
  source_pool TEXT DEFAULT 'watchlist',
  horizon_days INTEGER NOT NULL DEFAULT 1,
  target_date TEXT DEFAULT '',
  base_price REAL,
  outcome_price REAL,
  outcome_return_pct REAL,
  hit_target BOOLEAN,
  hit_stop BOOLEAN,
  outcome_status TEXT DEFAULT 'pending',
  meta TEXT DEFAULT '{}',
  evaluated_at TIMESTAMP,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT uq_strategy_outcome_signal_horizon UNIQUE(signal_run_id, horizon_days)
)
"""
        )
    )
    _create_index_if_missing(
        conn,
        "ix_strategy_outcome_strategy_horizon",
        "CREATE INDEX ix_strategy_outcome_strategy_horizon ON strategy_outcomes(strategy_code, horizon_days)",
    )
    _create_index_if_missing(
        conn,
        "ix_strategy_outcome_market_date",
        "CREATE INDEX ix_strategy_outcome_market_date ON strategy_outcomes(stock_market, target_date)",
    )
    _create_index_if_missing(
        conn,
        "ix_strategy_outcome_status",
        "CREATE INDEX ix_strategy_outcome_status ON strategy_outcomes(outcome_status, evaluated_at)",
    )

    conn.execute(
        text(
            """
CREATE TABLE IF NOT EXISTS strategy_weights (
  id SERIAL PRIMARY KEY,
  strategy_code TEXT NOT NULL,
  market TEXT NOT NULL DEFAULT 'ALL',
  regime TEXT NOT NULL DEFAULT 'default',
  weight REAL NOT NULL DEFAULT 1.0,
  reason TEXT DEFAULT '',
  meta TEXT DEFAULT '{}',
  effective_from TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT uq_strategy_weight_key UNIQUE(strategy_code, market, regime)
)
"""
        )
    )
    _create_index_if_missing(
        conn,
        "ix_strategy_weight_effective",
        "CREATE INDEX ix_strategy_weight_effective ON strategy_weights(effective_from)",
    )

    conn.execute(
        text(
            """
CREATE TABLE IF NOT EXISTS strategy_weight_history (
  id SERIAL PRIMARY KEY,
  strategy_code TEXT NOT NULL,
  market TEXT NOT NULL DEFAULT 'ALL',
  regime TEXT NOT NULL DEFAULT 'default',
  old_weight REAL NOT NULL DEFAULT 1.0,
  new_weight REAL NOT NULL DEFAULT 1.0,
  reason TEXT DEFAULT '',
  window_days INTEGER DEFAULT 45,
  sample_size INTEGER DEFAULT 0,
  meta TEXT DEFAULT '{}',
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""
        )
    )
    _create_index_if_missing(
        conn,
        "ix_strategy_weight_history_time",
        "CREATE INDEX ix_strategy_weight_history_time ON strategy_weight_history(created_at)",
    )
    _create_index_if_missing(
        conn,
        "ix_strategy_weight_history_strategy_market",
        "CREATE INDEX ix_strategy_weight_history_strategy_market ON strategy_weight_history(strategy_code, market)",
    )

    # Seed strategy catalog (parameterized to avoid ':' bind parsing in JSON literals)
    seed_sql = text(
        """
INSERT INTO strategy_catalog(
  code, name, description, version, enabled, market_scope, risk_level, params, default_weight
)
VALUES(
  :code, :name, :description, :version, :enabled, :market_scope, :risk_level, :params, :default_weight
) ON CONFLICT DO NOTHING
"""
    )
    seed_rows = [
        {
            "code": "trend_follow",
            "name": "趋势延续",
            "description": "顺势跟随，优先均线多头且动量延续",
            "version": "v1",
            "enabled": True,
            "market_scope": "ALL",
            "risk_level": "medium",
            "params": '{"horizon_days":5}',
            "default_weight": 1.15,
        },
        {
            "code": "macd_golden",
            "name": "MACD金叉",
            "description": "MACD 金叉确认，偏中短线",
            "version": "v1",
            "enabled": True,
            "market_scope": "ALL",
            "risk_level": "medium",
            "params": '{"horizon_days":3}',
            "default_weight": 1.10,
        },
        {
            "code": "volume_breakout",
            "name": "放量突破",
            "description": "放量突破关键位，偏进攻",
            "version": "v1",
            "enabled": True,
            "market_scope": "ALL",
            "risk_level": "high",
            "params": '{"horizon_days":3}',
            "default_weight": 1.18,
        },
        {
            "code": "pullback",
            "name": "回踩确认",
            "description": "回踩支撑后二次启动",
            "version": "v1",
            "enabled": True,
            "market_scope": "ALL",
            "risk_level": "low",
            "params": '{"horizon_days":5}',
            "default_weight": 1.05,
        },
        {
            "code": "rebound",
            "name": "超跌反弹",
            "description": "超跌后的反弹交易",
            "version": "v1",
            "enabled": True,
            "market_scope": "ALL",
            "risk_level": "high",
            "params": '{"horizon_days":3}',
            "default_weight": 0.95,
        },
        {
            "code": "watchlist_agent",
            "name": "Agent建议",
            "description": "来自既有 Agent 的综合建议映射",
            "version": "v1",
            "enabled": True,
            "market_scope": "ALL",
            "risk_level": "medium",
            "params": '{"horizon_days":3}',
            "default_weight": 1.00,
        },
        {
            "code": "market_scan",
            "name": "市场扫描",
            "description": "市场池扫描策略（热门与活跃）",
            "version": "v1",
            "enabled": True,
            "market_scope": "ALL",
            "risk_level": "medium",
            "params": '{"horizon_days":3}',
            "default_weight": 1.08,
        },
    ]
    for row in seed_rows:
        conn.execute(seed_sql, row)

    # Legacy smooth migration: entry_candidates -> strategy_signal_runs
    if _has_table(conn, "entry_candidates"):
        conn.execute(
            text(
                f"""
INSERT {insert_prefix}INTO strategy_signal_runs (
  snapshot_date, stock_symbol, stock_market, stock_name,
  strategy_code, strategy_name, strategy_version, risk_level, source_pool,
  score, rank_score, confidence, status, action, action_label, signal, reason,
  evidence, holding_days, entry_low, entry_high, stop_loss, target_price, invalidation,
  plan_quality, source_agent, source_suggestion_id, source_candidate_id, trace_id,
  is_holding_snapshot, payload, created_at, updated_at
)
SELECT
  ec.snapshot_date,
  ec.stock_symbol,
  ec.stock_market,
  ec.stock_name,
  CASE
    WHEN CAST(ec.strategy_tags AS TEXT) LIKE '%trend_follow%' THEN 'trend_follow'
    WHEN CAST(ec.strategy_tags AS TEXT) LIKE '%macd_golden%' THEN 'macd_golden'
    WHEN CAST(ec.strategy_tags AS TEXT) LIKE '%volume_breakout%' THEN 'volume_breakout'
    WHEN CAST(ec.strategy_tags AS TEXT) LIKE '%pullback%' THEN 'pullback'
    WHEN CAST(ec.strategy_tags AS TEXT) LIKE '%rebound%' THEN 'rebound'
    WHEN ec.candidate_source = 'market_scan' THEN 'market_scan'
    ELSE 'watchlist_agent'
  END AS strategy_code,
  CASE
    WHEN CAST(ec.strategy_tags AS TEXT) LIKE '%trend_follow%' THEN '趋势延续'
    WHEN CAST(ec.strategy_tags AS TEXT) LIKE '%macd_golden%' THEN 'MACD金叉'
    WHEN CAST(ec.strategy_tags AS TEXT) LIKE '%volume_breakout%' THEN '放量突破'
    WHEN CAST(ec.strategy_tags AS TEXT) LIKE '%pullback%' THEN '回踩确认'
    WHEN CAST(ec.strategy_tags AS TEXT) LIKE '%rebound%' THEN '超跌反弹'
    WHEN ec.candidate_source = 'market_scan' THEN '市场扫描'
    ELSE 'Agent建议'
  END AS strategy_name,
  'v1' AS strategy_version,
  CASE
    WHEN ec.action IN ('buy', 'add') AND ec.score >= 80 THEN 'high'
    WHEN ec.action IN ('watch', 'hold') THEN 'low'
    ELSE 'medium'
  END AS risk_level,
  COALESCE(ec.candidate_source, 'watchlist') AS source_pool,
  ec.score,
  ec.score,
  ec.confidence,
  ec.status,
  ec.action,
  ec.action_label,
  COALESCE(ec.signal, ''),
  COALESCE(ec.reason, ''),
  COALESCE(ec.evidence, '[]'),
  3,
  ec.entry_low,
  ec.entry_high,
  ec.stop_loss,
  ec.target_price,
  COALESCE(ec.invalidation, ''),
  COALESCE(ec.plan_quality, 0),
  COALESCE(ec.source_agent, ''),
  ec.source_suggestion_id,
  ec.id,
  COALESCE(ec.source_trace_id, ''),
  COALESCE(ec.is_holding_snapshot, FALSE),
  COALESCE(ec.meta, '{{}}'),
  ec.created_at,
  ec.updated_at
FROM entry_candidates ec {conflict_clause}
"""
            )
        )

    # Legacy smooth migration: entry_candidate_outcomes -> strategy_outcomes
    if _has_table(conn, "entry_candidate_outcomes"):
        conn.execute(
            text(
                f"""
INSERT INTO strategy_outcomes (
  signal_run_id, strategy_code, snapshot_date, stock_symbol, stock_market, source_pool,
  horizon_days, target_date, base_price, outcome_price, outcome_return_pct,
  hit_target, hit_stop, outcome_status, meta, evaluated_at, created_at
)
SELECT
  sr.id,
  sr.strategy_code,
  eco.snapshot_date,
  eco.stock_symbol,
  eco.stock_market,
  COALESCE(eco.candidate_source, 'watchlist'),
  eco.horizon_days,
  eco.target_date,
  eco.base_price,
  eco.outcome_price,
  eco.outcome_return_pct,
  eco.hit_target,
  eco.hit_stop,
  eco.outcome_status,
  COALESCE(eco.meta, '{{}}'),
  eco.evaluated_at,
  eco.created_at
FROM entry_candidate_outcomes eco
JOIN strategy_signal_runs sr ON sr.source_candidate_id = eco.candidate_id {conflict_clause}
"""
            )
        )


def _m112_strategy_analytics_snapshots(conn: Connection) -> None:
    conn.execute(
        text(
            """
CREATE TABLE IF NOT EXISTS market_regime_snapshots (
  id SERIAL PRIMARY KEY,
  snapshot_date TEXT NOT NULL,
  market TEXT NOT NULL DEFAULT 'CN',
  regime TEXT NOT NULL DEFAULT 'neutral',
  regime_score REAL NOT NULL DEFAULT 0.0,
  confidence REAL NOT NULL DEFAULT 0.0,
  breadth_up_pct REAL,
  avg_change_pct REAL,
  volatility_pct REAL,
  active_ratio REAL,
  sample_size INTEGER DEFAULT 0,
  meta TEXT DEFAULT '{}',
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT uq_market_regime_day_market UNIQUE(snapshot_date, market)
)
"""
        )
    )
    _create_index_if_missing(
        conn,
        "ix_market_regime_snapshot",
        "CREATE INDEX ix_market_regime_snapshot ON market_regime_snapshots(snapshot_date, market)",
    )
    _create_index_if_missing(
        conn,
        "ix_market_regime_type",
        "CREATE INDEX ix_market_regime_type ON market_regime_snapshots(regime)",
    )

    conn.execute(
        text(
            """
CREATE TABLE IF NOT EXISTS strategy_factor_snapshots (
  id SERIAL PRIMARY KEY,
  signal_run_id INTEGER NOT NULL REFERENCES strategy_signal_runs(id) ON DELETE CASCADE,
  snapshot_date TEXT NOT NULL,
  stock_symbol TEXT NOT NULL,
  stock_market TEXT NOT NULL DEFAULT 'CN',
  strategy_code TEXT NOT NULL,
  alpha_score REAL DEFAULT 0.0,
  catalyst_score REAL DEFAULT 0.0,
  quality_score REAL DEFAULT 0.0,
  risk_penalty REAL DEFAULT 0.0,
  crowd_penalty REAL DEFAULT 0.0,
  source_bonus REAL DEFAULT 0.0,
  regime_multiplier REAL DEFAULT 1.0,
  final_score REAL NOT NULL DEFAULT 0.0,
  factor_payload TEXT DEFAULT '{}',
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT uq_strategy_factor_signal UNIQUE(signal_run_id)
)
"""
        )
    )
    _create_index_if_missing(
        conn,
        "ix_strategy_factor_snapshot_score",
        "CREATE INDEX ix_strategy_factor_snapshot_score ON strategy_factor_snapshots(snapshot_date, final_score)",
    )
    _create_index_if_missing(
        conn,
        "ix_strategy_factor_strategy_market",
        "CREATE INDEX ix_strategy_factor_strategy_market ON strategy_factor_snapshots(strategy_code, stock_market)",
    )

    conn.execute(
        text(
            """
CREATE TABLE IF NOT EXISTS portfolio_risk_snapshots (
  id SERIAL PRIMARY KEY,
  snapshot_date TEXT NOT NULL,
  market TEXT NOT NULL DEFAULT 'CN',
  total_signals INTEGER DEFAULT 0,
  active_signals INTEGER DEFAULT 0,
  held_signals INTEGER DEFAULT 0,
  unheld_signals INTEGER DEFAULT 0,
  high_risk_ratio REAL,
  concentration_top5 REAL,
  avg_rank_score REAL,
  risk_level TEXT DEFAULT 'medium',
  meta TEXT DEFAULT '{}',
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT uq_portfolio_risk_day_market UNIQUE(snapshot_date, market)
)
"""
        )
    )
    _create_index_if_missing(
        conn,
        "ix_portfolio_risk_snapshot",
        "CREATE INDEX ix_portfolio_risk_snapshot ON portfolio_risk_snapshots(snapshot_date, market)",
    )

    if not _has_table(conn, "strategy_signal_runs"):
        return

    rows = conn.execute(
        text(
            """
SELECT
  id,
  snapshot_date,
  stock_symbol,
  stock_market,
  strategy_code,
  status,
  risk_level,
  rank_score,
  is_holding_snapshot,
  payload
FROM strategy_signal_runs
ORDER BY snapshot_date DESC, stock_market ASC, rank_score DESC
"""
        )
    ).fetchall()
    if not rows:
        return

    factor_insert = text(
        """
INSERT INTO strategy_factor_snapshots(
  signal_run_id, snapshot_date, stock_symbol, stock_market, strategy_code,
  alpha_score, catalyst_score, quality_score, risk_penalty, crowd_penalty, source_bonus,
  regime_multiplier, final_score, factor_payload
)
VALUES(
  :signal_run_id, :snapshot_date, :stock_symbol, :stock_market, :strategy_code,
  :alpha_score, :catalyst_score, :quality_score, :risk_penalty, :crowd_penalty, :source_bonus,
  :regime_multiplier, :final_score, :factor_payload
) ON CONFLICT DO NOTHING
"""
    )

    bucket: dict[tuple[str, str], dict] = {}
    for r in rows:
        signal_id = int(r[0])
        snapshot_date = str(r[1] or "")
        stock_symbol = str(r[2] or "")
        stock_market = str(r[3] or "CN")
        strategy_code = str(r[4] or "")
        status = str(r[5] or "")
        risk_level = str(r[6] or "medium")
        rank_score = float(r[7] or 0.0)
        is_holding = bool(r[8] or 0)
        payload_raw = r[9]

        payload_obj = {}
        if isinstance(payload_raw, str) and payload_raw.strip():
            try:
                payload_obj = json.loads(payload_raw)
            except Exception:
                payload_obj = {}
        elif isinstance(payload_raw, dict):
            payload_obj = payload_raw

        change_pct = None
        source_meta = payload_obj.get("source_meta") if isinstance(payload_obj, dict) else None
        if isinstance(source_meta, dict):
            quote = source_meta.get("quote") if isinstance(source_meta.get("quote"), dict) else {}
            try:
                if quote.get("change_pct") is not None:
                    change_pct = float(quote.get("change_pct"))
            except Exception:
                change_pct = None

        # Backfill factor snapshot with conservative decomposition.
        conn.execute(
            factor_insert,
            {
                "signal_run_id": signal_id,
                "snapshot_date": snapshot_date,
                "stock_symbol": stock_symbol,
                "stock_market": stock_market,
                "strategy_code": strategy_code,
                "alpha_score": round(rank_score * 0.35, 4),
                "catalyst_score": 0.0,
                "quality_score": round(rank_score * 0.15, 4),
                "risk_penalty": 0.0,
                "crowd_penalty": 0.0,
                "source_bonus": 0.0,
                "regime_multiplier": 1.0,
                "final_score": round(rank_score, 4),
                "factor_payload": json.dumps(
                    {
                        "backfilled": True,
                        "change_pct": change_pct,
                    },
                    ensure_ascii=False,
                ),
            },
        )

        key = (snapshot_date, stock_market)
        agg = bucket.setdefault(
            key,
            {
                "scores": [],
                "changes": [],
                "total": 0,
                "active": 0,
                "held": 0,
                "high_risk": 0,
            },
        )
        agg["total"] += 1
        if status == "active":
            agg["active"] += 1
        if is_holding:
            agg["held"] += 1
        if risk_level == "high":
            agg["high_risk"] += 1
        agg["scores"].append(rank_score)
        if change_pct is not None:
            agg["changes"].append(change_pct)

    regime_insert = text(
        """
INSERT INTO market_regime_snapshots(
  snapshot_date, market, regime, regime_score, confidence, breadth_up_pct,
  avg_change_pct, volatility_pct, active_ratio, sample_size, meta, updated_at
)
VALUES(
  :snapshot_date, :market, :regime, :regime_score, :confidence, :breadth_up_pct,
  :avg_change_pct, :volatility_pct, :active_ratio, :sample_size, :meta, CURRENT_TIMESTAMP
)
ON CONFLICT (snapshot_date, market) DO UPDATE SET
  regime = EXCLUDED.regime,
  regime_score = EXCLUDED.regime_score,
  confidence = EXCLUDED.confidence,
  breadth_up_pct = EXCLUDED.breadth_up_pct,
  avg_change_pct = EXCLUDED.avg_change_pct,
  volatility_pct = EXCLUDED.volatility_pct,
  active_ratio = EXCLUDED.active_ratio,
  sample_size = EXCLUDED.sample_size,
  meta = EXCLUDED.meta,
  updated_at = EXCLUDED.updated_at
"""
    )
    risk_insert = text(
        """
INSERT INTO portfolio_risk_snapshots(
  snapshot_date, market, total_signals, active_signals, held_signals, unheld_signals,
  high_risk_ratio, concentration_top5, avg_rank_score, risk_level, meta, updated_at
)
VALUES(
  :snapshot_date, :market, :total_signals, :active_signals, :held_signals, :unheld_signals,
  :high_risk_ratio, :concentration_top5, :avg_rank_score, :risk_level, :meta, CURRENT_TIMESTAMP
)
ON CONFLICT (snapshot_date, market) DO UPDATE SET
  total_signals = EXCLUDED.total_signals,
  active_signals = EXCLUDED.active_signals,
  held_signals = EXCLUDED.held_signals,
  unheld_signals = EXCLUDED.unheld_signals,
  high_risk_ratio = EXCLUDED.high_risk_ratio,
  concentration_top5 = EXCLUDED.concentration_top5,
  avg_rank_score = EXCLUDED.avg_rank_score,
  risk_level = EXCLUDED.risk_level,
  meta = EXCLUDED.meta,
  updated_at = EXCLUDED.updated_at
"""
    )

    for (snap, market), agg in bucket.items():
        total = int(agg["total"] or 0)
        active = int(agg["active"] or 0)
        held = int(agg["held"] or 0)
        unheld = max(0, total - held)
        scores = sorted([float(x) for x in agg["scores"] if x is not None], reverse=True)
        score_sum = sum(scores)
        avg_score = (score_sum / total) if total else 0.0
        top5 = sum(scores[:5])
        concentration = (top5 / score_sum) if score_sum > 0 else 0.0
        high_risk_ratio = (float(agg["high_risk"]) / total) if total else 0.0

        changes = [float(x) for x in agg["changes"] if x is not None]
        breadth_up_pct = (
            sum(1 for c in changes if c > 0) / len(changes) * 100.0 if changes else None
        )
        avg_change_pct = (sum(changes) / len(changes)) if changes else None
        volatility_pct = None
        if len(changes) >= 2:
            mean = sum(changes) / len(changes)
            variance = sum((c - mean) ** 2 for c in changes) / (len(changes) - 1)
            volatility_pct = variance ** 0.5

        active_ratio = (active / total) if total else 0.0
        breadth_norm = (
            max(-1.0, min(1.0, ((breadth_up_pct or 50.0) - 50.0) / 50.0))
            if breadth_up_pct is not None
            else 0.0
        )
        change_norm = (
            max(-1.0, min(1.0, (avg_change_pct or 0.0) / 3.0))
            if avg_change_pct is not None
            else 0.0
        )
        active_norm = max(-1.0, min(1.0, (active_ratio - 0.5) / 0.5))
        regime_score = 0.45 * breadth_norm + 0.30 * change_norm + 0.25 * active_norm
        if regime_score >= 0.20:
            regime = "bullish"
        elif regime_score <= -0.20:
            regime = "bearish"
        else:
            regime = "neutral"
        confidence = min(
            1.0,
            max(0.0, abs(regime_score) * 1.4 + min(0.4, total / 250.0)),
        )

        if high_risk_ratio >= 0.45 or concentration >= 0.65:
            risk_level = "high"
        elif high_risk_ratio >= 0.28 or concentration >= 0.48:
            risk_level = "medium"
        else:
            risk_level = "low"

        conn.execute(
            regime_insert,
            {
                "snapshot_date": snap,
                "market": market,
                "regime": regime,
                "regime_score": round(regime_score, 4),
                "confidence": round(confidence, 4),
                "breadth_up_pct": round(breadth_up_pct, 4) if breadth_up_pct is not None else None,
                "avg_change_pct": round(avg_change_pct, 4) if avg_change_pct is not None else None,
                "volatility_pct": round(volatility_pct, 4) if volatility_pct is not None else None,
                "active_ratio": round(active_ratio, 4),
                "sample_size": total,
                "meta": json.dumps(
                    {
                        "from_strategy_runs": True,
                        "active_signals": active,
                    },
                    ensure_ascii=False,
                ),
            },
        )
        conn.execute(
            risk_insert,
            {
                "snapshot_date": snap,
                "market": market,
                "total_signals": total,
                "active_signals": active,
                "held_signals": held,
                "unheld_signals": unheld,
                "high_risk_ratio": round(high_risk_ratio, 4),
                "concentration_top5": round(concentration, 4),
                "avg_rank_score": round(avg_score, 4),
                "risk_level": risk_level,
                "meta": json.dumps(
                    {
                        "from_strategy_runs": True,
                        "score_sum": round(score_sum, 4),
                    },
                    ensure_ascii=False,
                ),
            },
        )


def _m113_market_scan_snapshot_and_mixed_source(conn: Connection) -> None:
    conn.execute(
        text(
            """
CREATE TABLE IF NOT EXISTS market_scan_snapshots (
  id SERIAL PRIMARY KEY,
  snapshot_date TEXT NOT NULL,
  stock_symbol TEXT NOT NULL,
  stock_market TEXT NOT NULL DEFAULT 'CN',
  stock_name TEXT DEFAULT '',
  source TEXT NOT NULL DEFAULT 'market_scan',
  score_seed REAL NOT NULL DEFAULT 0.0,
  quote TEXT DEFAULT '{}',
  meta TEXT DEFAULT '{}',
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT uq_market_scan_snapshot_symbol UNIQUE(snapshot_date, stock_symbol, stock_market)
)
"""
        )
    )
    _create_index_if_missing(
        conn,
        "ix_market_scan_snapshot_day_market",
        "CREATE INDEX ix_market_scan_snapshot_day_market ON market_scan_snapshots(snapshot_date, stock_market)",
    )
    _create_index_if_missing(
        conn,
        "ix_market_scan_snapshot_source",
        "CREATE INDEX ix_market_scan_snapshot_source ON market_scan_snapshots(snapshot_date, source)",
    )

    if _has_table(conn, "entry_candidates"):
        conn.execute(
            text(
                """
UPDATE entry_candidates
SET candidate_source = 'mixed'
WHERE candidate_source = 'market_scan'
  AND source_suggestion_id IS NOT NULL
"""
            )
        )
    if _has_table(conn, "strategy_signal_runs"):
        conn.execute(
            text(
                """
UPDATE strategy_signal_runs
SET source_pool = 'mixed'
WHERE source_pool = 'market_scan'
  AND source_suggestion_id IS NOT NULL
"""
            )
        )


def _m114_paper_trading_tables(conn: Connection) -> None:
    """创建模拟盘三张表。"""
    if not _has_table(conn, "paper_trading_account"):
        conn.execute(
            text(
                """
CREATE TABLE paper_trading_account (
    id SERIAL PRIMARY KEY,
    initial_capital REAL NOT NULL DEFAULT 1000000.0,
    current_capital REAL NOT NULL DEFAULT 1000000.0,
    total_pnl REAL NOT NULL DEFAULT 0.0,
    total_trades INTEGER NOT NULL DEFAULT 0,
    winning_trades INTEGER NOT NULL DEFAULT 0,
    max_drawdown_pct REAL NOT NULL DEFAULT 0.0,
    peak_capital REAL NOT NULL DEFAULT 1000000.0,
    enabled BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""
            )
        )
    if not _has_table(conn, "paper_trading_positions"):
        conn.execute(
            text(
                """
CREATE TABLE paper_trading_positions (
    id SERIAL PRIMARY KEY,
    stock_symbol TEXT NOT NULL,
    stock_market TEXT NOT NULL DEFAULT 'CN',
    stock_name TEXT DEFAULT '',
    quantity INTEGER NOT NULL DEFAULT 100,
    entry_price REAL NOT NULL,
    stop_loss REAL,
    target_price REAL,
    current_price REAL,
    unrealized_pnl REAL NOT NULL DEFAULT 0.0,
    status TEXT NOT NULL DEFAULT 'open',
    signal_run_id INTEGER,
    signal_snapshot_date TEXT DEFAULT '',
    signal_action TEXT DEFAULT '',
    strategy_code TEXT DEFAULT '',
    opened_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    closed_at TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""
            )
        )
        conn.execute(text("CREATE INDEX ix_paper_pos_status ON paper_trading_positions(status)"))
        conn.execute(text("CREATE INDEX ix_paper_pos_symbol_market ON paper_trading_positions(stock_symbol, stock_market)"))
    if not _has_table(conn, "paper_trading_trades"):
        conn.execute(
            text(
                """
CREATE TABLE paper_trading_trades (
    id SERIAL PRIMARY KEY,
    stock_symbol TEXT NOT NULL,
    stock_market TEXT NOT NULL DEFAULT 'CN',
    stock_name TEXT DEFAULT '',
    quantity INTEGER NOT NULL DEFAULT 100,
    entry_price REAL NOT NULL,
    exit_price REAL NOT NULL,
    pnl REAL NOT NULL DEFAULT 0.0,
    pnl_pct REAL NOT NULL DEFAULT 0.0,
    exit_reason TEXT NOT NULL DEFAULT '',
    signal_run_id INTEGER,
    signal_snapshot_date TEXT DEFAULT '',
    strategy_code TEXT DEFAULT '',
    holding_days INTEGER DEFAULT 0,
    opened_at TIMESTAMP,
    closed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    meta TEXT DEFAULT '{}'
)
"""
            )
        )
        conn.execute(text("CREATE INDEX ix_paper_trade_closed ON paper_trading_trades(closed_at)"))
        conn.execute(text("CREATE INDEX ix_paper_trade_symbol ON paper_trading_trades(stock_symbol, stock_market)"))


def _m115_paper_trading_excluded_markets(conn: Connection) -> None:
    """模拟盘账户新增 excluded_markets 字段。"""
    _add_column_if_missing(
        conn,
        "paper_trading_account",
        "excluded_markets",
        "ALTER TABLE paper_trading_account ADD COLUMN excluded_markets TEXT DEFAULT '[]'",
    )


def _m116_chat_tables(conn: Connection) -> None:
    """AI 对话表。"""
    conn.execute(
        text("""
        CREATE TABLE IF NOT EXISTS chat_conversations (
            id SERIAL PRIMARY KEY,
            title TEXT DEFAULT '',
            stock_symbol TEXT,
            stock_market TEXT,
            ai_model_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)
    )
    conn.execute(
        text("""
        CREATE TABLE IF NOT EXISTS chat_messages (
            id SERIAL PRIMARY KEY,
            conversation_id INTEGER NOT NULL,
            role TEXT NOT NULL DEFAULT 'user',
            content TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)
    )
    _create_index_if_missing(
        conn, "ix_chat_conv_updated",
        "CREATE INDEX ix_chat_conv_updated ON chat_conversations(updated_at)",
    )
    _create_index_if_missing(
        conn, "ix_chat_conv_stock",
        "CREATE INDEX ix_chat_conv_stock ON chat_conversations(stock_symbol, stock_market)",
    )
    _create_index_if_missing(
        conn, "ix_chat_msg_conv",
        "CREATE INDEX ix_chat_msg_conv ON chat_messages(conversation_id, created_at)",
    )


def _m117_chat_initial_context(conn: Connection) -> None:
    """Add initial_context column to chat_conversations."""
    _add_column_if_missing(
        conn,
        "chat_conversations",
        "initial_context",
        "ALTER TABLE chat_conversations ADD COLUMN initial_context TEXT",
    )


def _m119_notifications_table(conn: Connection) -> None:
    """站内消息中心表。后台任务完成/失败写一条, 前端铃铛读未读数。"""
    conn.execute(
        text("""
        CREATE TABLE IF NOT EXISTS notifications (
            id SERIAL PRIMARY KEY,
            category TEXT NOT NULL DEFAULT 'system',
            level TEXT NOT NULL DEFAULT 'info',
            title TEXT NOT NULL DEFAULT '',
            body TEXT DEFAULT '',
            link TEXT DEFAULT '',
            source TEXT DEFAULT '',
            trace_id TEXT DEFAULT '',
            push_status TEXT DEFAULT '',
            push_error TEXT DEFAULT '',
            read_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)
    )
    _create_index_if_missing(
        conn, "ix_notifications_unread",
        "CREATE INDEX ix_notifications_unread ON notifications(read_at, created_at)",
    )
    _create_index_if_missing(
        conn, "ix_notifications_trace",
        "CREATE INDEX ix_notifications_trace ON notifications(trace_id)",
    )


def _m120_notification_push_channels(conn: Connection) -> None:
    """通知记录保存当次实际尝试的外发渠道，不回填历史数据。"""
    _add_column_if_missing(
        conn,
        "notifications",
        "push_channels",
        "ALTER TABLE notifications ADD COLUMN push_channels TEXT DEFAULT '[]'",
    )


def _m118_paper_trading_market_allocations(conn: Connection) -> None:
    """模拟盘账户新增 market_allocations（各市场投资比例），并由 excluded_markets 回填。"""
    _add_column_if_missing(
        conn,
        "paper_trading_account",
        "market_allocations",
        "ALTER TABLE paper_trading_account ADD COLUMN market_allocations TEXT DEFAULT '{}'",
    )
    if not _has_table(conn, "paper_trading_account"):
        return

    # 复用引擎的纯函数推导比例（函数内导入，避免模块级循环依赖）
    from src.core.paper_trading_engine import allocations_from_excluded

    rows = conn.execute(
        text("SELECT id, excluded_markets, market_allocations FROM paper_trading_account")
    ).fetchall()
    for r in rows:
        row_id = r[0]

        # 已有非空比例则跳过，避免覆盖用户配置
        raw_alloc = r[2]
        has_alloc = False
        if isinstance(raw_alloc, str) and raw_alloc.strip() and raw_alloc.strip() not in ("{}", "null"):
            try:
                has_alloc = bool(json.loads(raw_alloc))
            except Exception:
                has_alloc = False
        elif isinstance(raw_alloc, dict):
            has_alloc = bool(raw_alloc)
        if has_alloc:
            continue

        excluded: list[str] = []
        raw_excluded = r[1]
        if isinstance(raw_excluded, str) and raw_excluded.strip():
            try:
                parsed = json.loads(raw_excluded)
                if isinstance(parsed, list):
                    excluded = [str(x) for x in parsed]
            except Exception:
                excluded = []
        elif isinstance(raw_excluded, list):
            excluded = [str(x) for x in raw_excluded]

        alloc = allocations_from_excluded(excluded)
        conn.execute(
            text("UPDATE paper_trading_account SET market_allocations = :alloc WHERE id = :id"),
            {"alloc": json.dumps(alloc, ensure_ascii=False), "id": row_id},
        )


def _m121_dedupe_stocks_unique(conn: Connection) -> None:
    """自选股 (symbol, market, user_id) 去重 + 唯一约束。

    背景: stocks 表历史无唯一约束, 重复添加自选股(如 688008 三连)产生噪音。
    做法: 按 (symbol, market, COALESCE(user_id,'')) 分组保留最早一条(id 最小),
    其余删除; positions/stock_agents/price_alert_rules/price_alert_hits 的
    引用先重指到保留行(避免 ON DELETE CASCADE 误删持仓), 再按各自唯一键去重;
    最后加唯一索引。SQLite 唯一索引中 NULL 彼此不相等, 因此对 user_id IS NULL
    的行补一条部分唯一索引, 兼容 legacy/单用户数据。
    """
    if not _has_table(conn, "stocks"):
        return
    has_user = _has_column(conn, "stocks", "user_id")

    conn.execute(text("DROP TABLE IF EXISTS _m121_stock_keep"))
    conn.execute(text("DROP TABLE IF EXISTS _m121_stock_dup"))

    if has_user:
        conn.execute(
            text(
                """
CREATE TEMP TABLE _m121_stock_keep AS
SELECT MIN(id) AS keep_id, symbol, market, COALESCE(user_id, '') AS user_key
FROM stocks
GROUP BY symbol, market, user_key
"""
            )
        )
        conn.execute(
            text(
                """
CREATE TEMP TABLE _m121_stock_dup AS
SELECT s.id AS dup_id, k.keep_id
FROM stocks s
JOIN _m121_stock_keep k
  ON s.symbol = k.symbol AND s.market = k.market
 AND COALESCE(s.user_id, '') = k.user_key
WHERE s.id <> k.keep_id
"""
            )
        )
    else:
        conn.execute(
            text(
                """
CREATE TEMP TABLE _m121_stock_keep AS
SELECT MIN(id) AS keep_id, symbol, market
FROM stocks
GROUP BY symbol, market
"""
            )
        )
        conn.execute(
            text(
                """
CREATE TEMP TABLE _m121_stock_dup AS
SELECT s.id AS dup_id, k.keep_id
FROM stocks s
JOIN _m121_stock_keep k
  ON s.symbol = k.symbol AND s.market = k.market
WHERE s.id <> k.keep_id
"""
            )
        )

    # 关联表: 先把 stock_id 映射为保留行 id, 在映射键上按自身唯一键去重,
    # 再统一重指到保留行(避免 UPDATE 中途触发目标表唯一冲突)。
    fk_tables: tuple[tuple[str, str, tuple[str, ...] | None], ...] = (
        ("positions", "stock_id", ("account_id", "stock_id")),
        ("stock_agents", "stock_id", ("stock_id", "agent_name")),
        ("price_alert_rules", "stock_id", None),
        ("price_alert_hits", "stock_id", None),
    )
    for table, col, uniq_cols in fk_tables:
        if not _has_table(conn, table) or not _has_column(conn, table, col):
            continue
        if uniq_cols:
            # 映射键: 把 uniq_cols 里的 stock_id 换成映射后的保留行 id
            # (COALESCE(d.keep_id, p.stock_id): 有 dup 则指向保留行, 无 dup 保持原值)
            mapped_cols = tuple(
                f"COALESCE(d.keep_id, p.{col}) AS mstock" if c == col else c
                for c in uniq_cols
            )
            select_cols = ", ".join(mapped_cols)
            # GROUP BY 引用临时表的列名(临时表里 stock_id 已被命名为 mstock, 不能用表达式)
            group_cols = ", ".join("mstock" if c == col else c for c in uniq_cols)
            temp = f"_m121_{table}_mapped"
            conn.execute(text(f"DROP TABLE IF EXISTS {temp}"))
            conn.execute(
                text(
                    f"""
CREATE TEMP TABLE {temp} AS
SELECT p.id, {select_cols}
FROM {table} p
LEFT JOIN _m121_stock_dup d ON p.{col} = d.dup_id
"""
                )
            )
            # 映射键组内保留 id 最小的一行, 其余删除(含同一 keep 下的多只 dup 引用)
            conn.execute(
                text(
                    f"""
DELETE FROM {table}
WHERE id NOT IN (
    SELECT MIN(id) FROM {temp} GROUP BY {group_cols}
)
"""
                )
            )
            conn.execute(text(f"DROP TABLE IF EXISTS {temp}"))
        # 统一重指到保留行
        conn.execute(
            text(
                f"""
UPDATE {table}
SET {col} = (SELECT keep_id FROM _m121_stock_dup WHERE dup_id = {table}.{col})
WHERE {col} IN (SELECT dup_id FROM _m121_stock_dup)
"""
            )
        )

    conn.execute(text("DELETE FROM stocks WHERE id IN (SELECT dup_id FROM _m121_stock_dup)"))
    conn.execute(text("DROP TABLE IF EXISTS _m121_stock_keep"))
    conn.execute(text("DROP TABLE IF EXISTS _m121_stock_dup"))

    # 唯一约束: 非空 user_id 组合唯一 + NULL user_id 部分唯一(防再重复)
    # W3.1(D2): user_id 列历史上由 A 层迁移(现 148 号)先补; 本迁移必须自洽 ——
    # 缺列时跳过唯一索引(148 号会补列, 下轮升级重建索引)。
    if not has_user:
        return
    _create_index_if_missing(
        conn,
        "uq_stocks_symbol_market_user",
        """
CREATE UNIQUE INDEX IF NOT EXISTS uq_stocks_symbol_market_user
ON stocks (symbol, market, user_id)
""",
    )
    _create_index_if_missing(
        conn,
        "uq_stocks_symbol_market_nulluser",
        """
CREATE UNIQUE INDEX IF NOT EXISTS uq_stocks_symbol_market_nulluser
ON stocks (symbol, market) WHERE user_id IS NULL
""",
    )


def _resolve_earliest_owner_user_id(conn: Connection) -> str | None:
    """查询最早创建的 owner 用户 id(UUID, String(36))。

    用于把迁移前的存量行(没有 user_id 的)归属到管理员, 避免空指针查询。
    返回 None 表示当前还没有 owner 用户(冷启动场景, 把存量行继续保留 NULL)。
    """
    if not _has_table(conn, "users"):
        return None
    if _dialect_is_pg(conn):
        row = conn.execute(
            text(
                """
SELECT id FROM users
WHERE role = 'owner' AND is_active = TRUE
ORDER BY created_at ASC, id ASC
LIMIT 1
"""
            )
        ).first()
    else:
        row = conn.execute(
            text(
                """
SELECT id FROM users
WHERE role = 'owner' AND is_active = true
ORDER BY created_at ASC, id ASC
LIMIT 1
"""
            )
        ).first()
    if not row:
        return None
    try:
        return str(row[0])
    except Exception:
        return None


def _m122_user_id_columns_chat_notif_suggestion(conn: Connection) -> None:
    """S1/S2/S4/M2: chat_conversations / notifications / stock_suggestions 加 user_id。

    - 列类型: String(36), 索引, nullable=True
    - 存量行(无 user_id)全部回填到最早的 owner 用户
    - 幂等可重跑
    """
    owner_id = _resolve_earliest_owner_user_id(conn)
    targets = [
        ("chat_conversations", "ix_chat_conv_user", "created_at"),
        ("notifications", "ix_notifications_user", "created_at"),
        ("stock_suggestions", "ix_stock_suggestions_user", "created_at"),
    ]
    for table, idx_name, order_col in targets:
        if not _has_table(conn, table):
            continue
        _add_column_if_missing(
            conn,
            table,
            "user_id",
            f"ALTER TABLE {table} ADD COLUMN user_id TEXT",
        )
        # 索引(可选加速按用户过滤的 list/detail/delete 端点)
        _create_index_if_missing(
            conn,
            idx_name,
            f"CREATE INDEX {idx_name} ON {table}(user_id, {order_col})",
        )
        # 存量行回填到最早 owner
        if owner_id:
            conn.execute(
                text(f"UPDATE {table} SET user_id = :uid WHERE user_id IS NULL OR TRIM(user_id) = ''"),
                {"uid": owner_id},
            )
    # analysis_history 已有 user_id 列(model 声明), 补一个复合索引加速按用户 + 列表查询
    if _has_table(conn, "analysis_history"):
        _create_index_if_missing(
            conn,
            "ix_analysis_history_user_date",
            "CREATE INDEX ix_analysis_history_user_date ON analysis_history(user_id, analysis_date)",
        )


def _m123_user_id_on_price_alerts(conn: Connection) -> None:
    """S3: price_alert_rules 加 user_id。

    - 列类型: String(36), 索引, nullable=True
    - 存量行(无 user_id)全部回填到最早的 owner 用户
    - 幂等可重跑
    """
    owner_id = _resolve_earliest_owner_user_id(conn)
    if not _has_table(conn, "price_alert_rules"):
        return
    _add_column_if_missing(
        conn,
        "price_alert_rules",
        "user_id",
        "ALTER TABLE price_alert_rules ADD COLUMN user_id TEXT",
    )
    _create_index_if_missing(
        conn,
        "ix_price_alert_user",
        "CREATE INDEX ix_price_alert_user ON price_alert_rules(user_id, updated_at)",
    )
    if owner_id:
        conn.execute(
            text(
                "UPDATE price_alert_rules SET user_id = :uid WHERE user_id IS NULL OR TRIM(user_id) = ''"
            ),
            {"uid": owner_id},
        )


def _m124_analysis_history_user_id_unique(conn: Connection) -> None:
    """S1/M3(2026-08-23): 重建 analysis_history UNIQUE 约束, 加入 user_id 维度。

    背景: 原 UniqueConstraint("agent_name", "stock_symbol", "analysis_date") 不含 user_id,
    多账号并存下同一 (agent, stock, date) 只能存一条, 用户 B 的报告会触发 IntegrityError 失败。
    新约束: (agent_name, stock_symbol, analysis_date, user_id) — 多账号各自独立存,
    user_id=NULL 的旧数据/系统报告继续保留 NULL 共享语义(SQLite/PG 都允许 NULL 与 NULL 不比较)。

    SQLite 限制: 不能 ALTER UNIQUE 约束, 只能重建表。本迁移:
    1. 检测旧索引 uq_agent_stock_date 是否存在
    2. 若不存在 (已是新约束或全新库), 跳过
    3. 否则: 备份全表 → DROP → CREATE(新约束) → 恢复 → 重建索引
    PG 限制: PG 支持 DROP CONSTRAINT IF EXISTS, 走 ALTER TABLE ... DROP CONSTRAINT,
    不需要重建表。
    """
    if not _has_table(conn, "analysis_history"):
        return

    # W3.1(D2): user_id 列历史上由 A 层迁移(现 148 号)先补; 本迁移必须自洽 ——
    # 老表缺 user_id 时先补列(存量行 NULL, 保持共享语义), 再走约束重建。
    _add_column_if_missing(
        conn,
        "analysis_history",
        "user_id",
        "ALTER TABLE analysis_history ADD COLUMN user_id VARCHAR(36)",
    )

    if _dialect_is_pg(conn):
        # PG: 直接 DROP CONSTRAINT + ADD CONSTRAINT（幂等：先删旧约束再建新约束）
        conn.execute(
            text(
                "ALTER TABLE analysis_history DROP CONSTRAINT IF EXISTS uq_agent_stock_date"
            )
        )
        conn.execute(
            text(
                "ALTER TABLE analysis_history DROP CONSTRAINT IF EXISTS uq_agent_stock_date_user"
            )
        )
        conn.execute(
            text(
                """
ALTER TABLE analysis_history
ADD CONSTRAINT uq_agent_stock_date_user UNIQUE (agent_name, stock_symbol, analysis_date, user_id)
"""
            )
        )
        return

    # SQLite: 检测表 schema 中是否包含旧 UNIQUE 约束
    # SQLite 把 UNIQUE 约束存在表定义里, 自动建 sqlite_autoindex_* 索引,
    # 所以查 sqlite_master 的 'index' type 找不到 uq_agent_stock_date 名字。
    schema_row = conn.execute(
        text(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='analysis_history'"
        )
    ).first()
    schema_sql = str(schema_row[0]) if schema_row else ""
    # 已是新约束则跳过(幂等)
    if "uq_agent_stock_date_user" in schema_sql:
        return
    # 没有旧约束(全新库或已重建过)→ 只需建新约束? 简化: 直接退出
    if "uq_agent_stock_date" not in schema_sql:
        # 全新表场景: 由 create_all 处理, 不必手动加
        return

    # 重建 analysis_history 表
    # 1. 重命名旧表
    conn.execute(text("ALTER TABLE analysis_history RENAME TO _m124_analysis_history_old"))
    # 2. 建新表(沿用 models.py 列定义, 新 UNIQUE)
    conn.execute(
        text(
            """
CREATE TABLE analysis_history (
    id SERIAL PRIMARY KEY,
    user_id VARCHAR(36),
    agent_name VARCHAR NOT NULL,
    stock_symbol VARCHAR NOT NULL,
    analysis_date VARCHAR NOT NULL,
    title VARCHAR DEFAULT '',
    content TEXT NOT NULL,
    raw_data TEXT DEFAULT '{}',
    agent_kind_snapshot VARCHAR DEFAULT 'workflow',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_agent_stock_date_user UNIQUE (agent_name, stock_symbol, analysis_date, user_id)
)
"""
        )
    )
    # 3. 拷贝数据(JSON 列若是 TEXT, JSON 字符串原样复制)
    conn.execute(
        text(
            """
INSERT INTO analysis_history
  (id, user_id, agent_name, stock_symbol, analysis_date, title, content, raw_data, agent_kind_snapshot, created_at, updated_at)
SELECT
  id, user_id, agent_name, stock_symbol, analysis_date,
  COALESCE(title, ''), content,
  COALESCE(raw_data, '{}'), COALESCE(agent_kind_snapshot, 'workflow'),
  COALESCE(created_at, CURRENT_TIMESTAMP), COALESCE(updated_at, CURRENT_TIMESTAMP)
FROM _m124_analysis_history_old
"""
        )
    )
    # 4. 删旧表
    conn.execute(text("DROP TABLE _m124_analysis_history_old"))
    # 5. 重建索引(v105/v122 创建的)
    _create_index_if_missing(
        conn,
        "ix_analysis_history_kind_date",
        "CREATE INDEX ix_analysis_history_kind_date ON analysis_history(agent_kind_snapshot, analysis_date)",
    )
    _create_index_if_missing(
        conn,
        "ix_analysis_history_agent_updated",
        "CREATE INDEX ix_analysis_history_agent_updated ON analysis_history(agent_name, updated_at)",
    )
    _create_index_if_missing(
        conn,
        "ix_analysis_history_user",
        "CREATE INDEX ix_analysis_history_user ON analysis_history(user_id)",
    )
    _create_index_if_missing(
        conn,
        "ix_analysis_history_user_date",
        "CREATE INDEX ix_analysis_history_user_date ON analysis_history(user_id, analysis_date)",
    )


def _m125_klines_table(conn: Connection) -> None:
    """K线缓存表(klines) — 优先 TimescaleDB hypertable, 无扩展则降级普通表。

    AGENTS.md 约定 K线读取走 PG 优先(get_klines), 支撑机会页全盘扫描。
    全盘扫描需预存全市场历史(百万行级), hypertable 时间分区+压缩+retention;
    数据量小或无 TimescaleDB 扩展时普通表+唯一索引够用。
    """
    conn.execute(
        text(
            """
CREATE TABLE IF NOT EXISTS klines (
  ts TIMESTAMPTZ NOT NULL,
  symbol TEXT NOT NULL,
  market TEXT NOT NULL,
  period TEXT NOT NULL,
  source TEXT NOT NULL,
  open DOUBLE PRECISION,
  high DOUBLE PRECISION,
  low DOUBLE PRECISION,
  close DOUBLE PRECISION,
  volume BIGINT,
  quality_flag INTEGER DEFAULT 1
)
"""
        )
    )
    try:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS timescaledb"))
        conn.execute(text("SELECT create_hypertable('klines', 'ts', if_not_exists => TRUE)"))
        logger.info("klines: TimescaleDB hypertable 已启用")
    except Exception as e:
        logger.warning(f"klines: TimescaleDB 不可用, 降级普通表: {e}")
    # 幂等唯一索引(ON CONFLICT 依赖; 含 ts 满足 hypertable 分区键要求)
    conn.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_klines_symbol_period_ts "
            "ON klines(symbol, market, period, ts, source)"
        )
    )


def _m127_dp_history_table(conn: Connection) -> None:
    """数智决策快照历史(09-03: 快照落库, 回查/回测用; PG/SQLite 通用类型)。"""
    conn.execute(
        text(
            """
CREATE TABLE IF NOT EXISTS decision_pioneer_history (
  ts TIMESTAMPTZ NOT NULL,
  symbol TEXT NOT NULL,
  market TEXT NOT NULL,
  activity DOUBLE PRECISION,
  level TEXT,
  gs_side TEXT,
  fund_net DOUBLE PRECISION,
  main_net DOUBLE PRECISION,
  payload TEXT
)
"""
        )
    )
    conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_dp_history_symbol_ts "
            "ON decision_pioneer_history(symbol, market, ts)"
        )
    )


def _m128_l2_ticks_table(conn: Connection) -> None:
    """L2 逐笔落库(09-03: 回测回查用; 唯一索引天然去重, 重复拉取幂等)。"""
    conn.execute(
        text(
            """
CREATE TABLE IF NOT EXISTS l2_ticks (
  ts TIMESTAMPTZ NOT NULL,
  symbol TEXT NOT NULL,
  market TEXT NOT NULL,
  source TEXT NOT NULL,
  direction TEXT,
  price DOUBLE PRECISION,
  vol DOUBLE PRECISION,
  amt DOUBLE PRECISION,
  tick_time TEXT
)
"""
        )
    )
    conn.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_l2_ticks_dedupe "
            "ON l2_ticks(symbol, market, source, tick_time, direction, price, vol, amt)"
        )
    )
    conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_l2_ticks_symbol_ts "
            "ON l2_ticks(symbol, market, ts)"
        )
    )


def _m126_datasource_health_columns(conn: Connection) -> None:
    """data_sources 健康累计列(2026-09-01 新增于 DataSource 模型, 但 PostgreSQL 下
    create_all 不会给已存在的表自动加列, 老库缺列会导致 /api/datasources SELECT *
    报 UndefinedColumn → 500)。幂等补齐 5 个健康列(last_used_at/last_error_at/
    success_count/error_count/last_status)。"""
    from src.core.source_health import HEALTH_COLUMNS

    for col, dtype in HEALTH_COLUMNS.items():
        _add_column_if_missing(
            conn,
            "data_sources",
            col,
            f"ALTER TABLE data_sources ADD COLUMN {col} {dtype}",
        )


def _m129_summary_cache_table(conn: Connection) -> None:
    """summary_cache 表(v0.4.77): summary / fundamentals 通用小数据 PG 落库缓存。

    summary 冷启动 20-30s 撞 502, 进程内 5min 缓存进程重启失效; 加 PG 落库,
    进程重启/容器迁移也能命中。fundamentals-detail 24h 缓存复用此表。

    字段:
      symbol TEXT NOT NULL  (用 'fundamentals:{mkt}:{sym}:{dt}' 当 fundamentals key)
      market TEXT NOT NULL  (用于隔离不同市场)
      computed_at TIMESTAMP NOT NULL
      ttl_s INTEGER NOT NULL
      payload TEXT NOT NULL  (JSON, 上限 50KB)
      PRIMARY KEY (symbol, market)
    """
    if _dialect_is_pg(conn):
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS summary_cache (
                    symbol TEXT NOT NULL,
                    market TEXT NOT NULL,
                    computed_at TIMESTAMP NOT NULL,
                    ttl_s INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY (symbol, market)
                )
                """
            )
        )
    else:
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS summary_cache (
                    symbol TEXT NOT NULL,
                    market TEXT NOT NULL,
                    computed_at TIMESTAMP NOT NULL,
                    ttl_s INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY (symbol, market)
                )
                """
            )
        )


def _m130_tick_archive_table(conn: Connection) -> None:
    """逐笔日存档 tick_archive(#3 回测底座 + #4 跨日结论序列, 2026-09-04)。

    收盘后快照一行(code, trade_date): 全天逐笔(JSON TEXT, ~5000 笔) + 当日结论
    (main_net/big_net/mid_net/retail_net/signal/data_status)。只存被查过的股,
    全市场不扫。一行一日, UNIQUE(code, trade_date) 幂等 upsert。
    序列查询 SELECT trade_date, main_net ... ORDER BY trade_date DESC 即
    "主力连续流入 N 天"的数据底座。
    """
    conn.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS tick_archive (
                trade_date TEXT NOT NULL,
                code TEXT NOT NULL,
                symbol TEXT NOT NULL,
                tick_count INTEGER,
                last_tick_t TEXT,
                tick_pages INTEGER,
                main_net DOUBLE PRECISION,
                big_net DOUBLE PRECISION,
                mid_net DOUBLE PRECISION,
                retail_net DOUBLE PRECISION,
                data_status TEXT,
                signal TEXT,
                ticks_json TEXT,
                created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (code, trade_date)
            )
            """
        )
    )
    conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_tick_archive_symbol_date "
            "ON tick_archive(symbol, trade_date)"
        )
    )


def _m131_seal_quality_samples(conn: Connection) -> None:
    """封单成色采样表(批次A, 2026-09-06 28号)。

    seal_sampler 盘中 60s 写入涨停股的累计字段快照, seal_quality 纯函数
    消费窗口差分。ts 用 ISO 文本(双方言安全, 定长 ISO 排序=时间排序),
    唯一键 symbol+ts 幂等(先查后插, 重复写跳过)。
    """
    conn.execute(
        text(
            f"""
CREATE TABLE IF NOT EXISTS seal_quality_samples (
  ts TEXT NOT NULL,
  symbol TEXT NOT NULL,
  market TEXT NOT NULL DEFAULT 'CN',
  price DOUBLE PRECISION,
  last_close DOUBLE PRECISION,
  limit_price DOUBLE PRECISION,
  is_sealed INTEGER,
  cancel_buy DOUBLE PRECISION,
  cancel_sell DOUBLE PRECISION,
  total_buy_vol DOUBLE PRECISION,
  total_sell_vol DOUBLE PRECISION,
  l2_tick_num DOUBLE PRECISION,
  l2_order_num DOUBLE PRECISION,
  seal_amount DOUBLE PRECISION,
  extra TEXT
)
"""
        )
    )
    conn.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_seal_samples_symbol_ts "
            "ON seal_quality_samples(symbol, ts)"
        )
    )
    conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_seal_samples_symbol_ts "
            "ON seal_quality_samples(symbol, market, ts)"
        )
    )


def _m132_limit_up_events(conn: Connection) -> None:
    """涨停事件表(批次B 妖股池, 2026-09-06 28号)。

    每日盘后回填全市场涨停(触及/封住)事件, 供 demon_score 六维评分与
    妖股领先效应回测。trade_date 用 YYYYMMDD 文本(双方言安全)。
    open_count(开板次数)需盘中逐笔, MVP 留空由后续逐笔补充; 一字板
    用 open==limit 近似判定(开盘即封且最低价未离开涨停价)。
    """
    conn.execute(
        text(
            """
CREATE TABLE IF NOT EXISTS limit_up_events (
  trade_date TEXT NOT NULL,
  symbol TEXT NOT NULL,
  market TEXT NOT NULL DEFAULT 'CN',
  name TEXT,
  prev_close DOUBLE PRECISION,
  limit_price DOUBLE PRECISION,
  open_price DOUBLE PRECISION,
  high_price DOUBLE PRECISION,
  low_price DOUBLE PRECISION,
  close_price DOUBLE PRECISION,
  touched INTEGER,
  is_sealed_close INTEGER,
  one_way INTEGER,
  open_count INTEGER,
  first_time TEXT,
  max_seal_amount DOUBLE PRECISION,
  limit_days INTEGER,
  circ_mv DOUBLE PRECISION,
  source TEXT,
  extra TEXT
)
"""
        )
    )
    conn.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_limit_up_events_sym_date "
            "ON limit_up_events(symbol, trade_date)"
        )
    )
    conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_limit_up_events_date "
            "ON limit_up_events(trade_date)"
        )
    )


def _m133_signal_snapshots(conn: Connection) -> None:
    """信号快照表(批次D 信号→复盘闭环, 2026-09-06 28号)。

    信号 emit 时落库, 每晚对账 job 回填 T+1/T+5 收盘收益。
    emit_ts ISO 文本(双方言安全); 同一 (signal_type, symbol, emit_date)
    只记首条(首条口径, 避免盘中重复信号刷库)。
    """
    conn.execute(
        text(
            """
CREATE TABLE IF NOT EXISTS signal_snapshots (
  emit_ts TEXT NOT NULL,
  emit_date TEXT NOT NULL,
  signal_type TEXT NOT NULL,
  symbol TEXT NOT NULL,
  market TEXT NOT NULL DEFAULT 'CN',
  direction TEXT,
  strength DOUBLE PRECISION,
  payload TEXT,
  outcome_t1 DOUBLE PRECISION,
  outcome_t5 DOUBLE PRECISION,
  checked_t1 INTEGER,
  checked_t5 INTEGER
)
"""
        )
    )
    conn.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_signal_snapshots_dedupe "
            "ON signal_snapshots(signal_type, symbol, emit_date)"
        )
    )
    conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_signal_snapshots_date "
            "ON signal_snapshots(emit_date, signal_type)"
        )
    )


def _m134_demon_factors(conn: Connection) -> None:
    """妖股因子表(批次B 存档化, 2026-09-06 28号)。

    六维因子每日增量落库(每股最新快照, unique(symbol)), 妖股池 API 直读
    不再从原始事件现算。原始 limit_up_events 是存档层, 因子可随时全量重算。
    """
    conn.execute(
        text(
            """
CREATE TABLE IF NOT EXISTS demon_factors (
  factor_date TEXT NOT NULL,
  symbol TEXT NOT NULL,
  market TEXT NOT NULL DEFAULT 'CN',
  name TEXT,
  total DOUBLE PRECISION,
  grade TEXT,
  n_events INTEGER,
  n_sealed INTEGER,
  max_streak INTEGER,
  participation TEXT,
  dims TEXT,
  flags TEXT,
  updated_at TEXT
)
"""
        )
    )
    conn.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_demon_factors_symbol "
            "ON demon_factors(symbol)"
        )
    )
    conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_demon_factors_total "
            "ON demon_factors(total)"
        )
    )


def _m135_paper_trading_user_id(conn: Connection) -> None:
    """T6(2026-09-08 审计): 模拟盘三表补 user_id —— 修复跨账号越权。

    - paper_trading_account / paper_trading_positions / paper_trading_trades
      原设计即"单例"(无 user_id), 任一登录用户可查看并操作其他账号的模拟盘。
    - 列类型: TEXT(String(36) UUID), 索引, nullable=True(冷启动无 owner 时保留 NULL)
    - 存量行回填到最早的 owner 用户(与 _m122 同口径)
    - 幂等可重跑
    """
    owner_id = _resolve_earliest_owner_user_id(conn)
    targets = [
        ("paper_trading_account", "ix_paper_account_user"),
        ("paper_trading_positions", "ix_paper_pos_user"),
        ("paper_trading_trades", "ix_paper_trade_user"),
    ]
    for table, idx_name in targets:
        if not _has_table(conn, table):
            continue
        _add_column_if_missing(
            conn,
            table,
            "user_id",
            f"ALTER TABLE {table} ADD COLUMN user_id TEXT",
        )
        _create_index_if_missing(
            conn,
            idx_name,
            f"CREATE INDEX {idx_name} ON {table}(user_id)",
        )
        if owner_id:
            conn.execute(
                text(f"UPDATE {table} SET user_id = :uid WHERE user_id IS NULL OR TRIM(user_id) = ''"),
                {"uid": owner_id},
            )


def _m136_analysis_history_status(conn: Connection) -> None:
    """0.3(2026-09-08): analysis_history 增加降级状态列 —— 降级报告显式落库。

    - status: success / degraded / failed, 存量行回填 'success'(历史数据视为正常)
    - error: 降级原因, nullable
    - 幂等可重跑
    """
    if not _has_table(conn, "analysis_history"):
        return
    _add_column_if_missing(
        conn,
        "analysis_history",
        "status",
        "ALTER TABLE analysis_history ADD COLUMN status TEXT DEFAULT 'success'",
    )
    _add_column_if_missing(
        conn,
        "analysis_history",
        "error",
        "ALTER TABLE analysis_history ADD COLUMN error TEXT",
    )
    conn.execute(
        text("UPDATE analysis_history SET status = 'success' WHERE status IS NULL")
    )


def _m137_klines_adjust_dimension(conn: Connection) -> None:
    """风险方案1.2/B1(2026-09-08): klines 增加复权维度 adjust, 并纳入唯一索引。

    0.7 勘查(docs/research/K线复权污染勘查_20260907.md)实证: 此前 qfq 与不复权
    无列区分, DO NOTHING 令复权基准漂移/双柱污染不可自愈。加 adjust 列后:
    - 取值 'qfq'(前复权, 主源) / 'none'(不复权, 新浪兜底) / 'hfq'(预留)
    - 唯一索引升为 (symbol, market, period, ts, source, adjust), qfq 与 none 互不覆盖
    - 存量行回填 'none'(待全量重刷后由 ingestor 以 'qfq' 重写)
    幂等可重跑。
    """
    if not _has_table(conn, "klines"):
        return
    _add_column_if_missing(
        conn,
        "klines",
        "adjust",
        "ALTER TABLE klines ADD COLUMN adjust VARCHAR(4) NOT NULL DEFAULT 'none'",
    )
    # 旧唯一索引让位: 新索引含 adjust, 满足 hypertable 分区键(ts)在约束内的要求
    conn.execute(text("DROP INDEX IF EXISTS uq_klines_symbol_period_ts"))
    conn.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_klines_symbol_period_ts_adjust "
            "ON klines(symbol, market, period, ts, source, adjust)"
        )
    )


def _m138_market_flow_snapshots_table(conn: Connection) -> None:
    """大盘资金快照表(W1.5/A5 收编: 原 market_data.py 运行时建表)。

    market-capital-flow 接口成功返回后异步写一条快照(30s 节流在应用层)。
    字段口径与接口返回一致: total_main_flow 两市主力净流入(亿元, 可负),
    up/down/flat_count 涨跌平家数(同花顺APP盘面口径), sh/sz_flow 沪深市主力净流入。
    """
    if _dialect_is_pg(conn):
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS market_flow_snapshots (
                    id SERIAL PRIMARY KEY,
                    ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    total_main_flow DOUBLE PRECISION,
                    up_count INTEGER,
                    down_count INTEGER,
                    flat_count INTEGER,
                    sh_flow DOUBLE PRECISION,
                    sz_flow DOUBLE PRECISION
                )
                """
            )
        )
    else:
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS market_flow_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts DATETIME DEFAULT CURRENT_TIMESTAMP,
                    total_main_flow REAL,
                    up_count INTEGER,
                    down_count INTEGER,
                    flat_count INTEGER,
                    sh_flow REAL,
                    sz_flow REAL
                )
                """
            )
        )


def _m139_mainline_rank_daily_table(conn: Connection) -> None:
    """主线排行日快照表(W1.5/A5 收编: 原 market_mainline.py 运行时建表)。

    字段: date(DATE) / name(TEXT) / rank(int) / score(float8);
    主键 (date, name) 保证同日同线只有一条最新快照。
    """
    if _dialect_is_pg(conn):
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS mainline_rank_daily (
                    date DATE NOT NULL,
                    name TEXT NOT NULL,
                    rank INTEGER NOT NULL,
                    score DOUBLE PRECISION,
                    PRIMARY KEY (date, name)
                )
                """
            )
        )
    else:
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS mainline_rank_daily (
                    date TEXT NOT NULL,
                    name TEXT NOT NULL,
                    rank INTEGER NOT NULL,
                    score REAL,
                    PRIMARY KEY (date, name)
                )
                """
            )
        )


def _m189_mainline_daily_snapshot_table(conn: Connection) -> None:
    """市场主线日频快照表(2026-10-14 缓存收口)。

    market_mainline 接口每次成功聚合后异步 upsert 当日一行(date 主键 → 同日覆盖为
    最新, 收盘后那次即当日定稿)。用途: 响应缓存(biz_cache)缺失时(进程重启/换 worker/
    新容器)直接读库, 不再冷启动 16~37s 重拉涨停池。先例: _m138 market_flow_snapshots。

    字段: date(DATE) 交易日 / payload(TEXT) 完整响应 JSON / computed_ts(epoch 秒)。
    """
    if _dialect_is_pg(conn):
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS mainline_daily_snapshot (
                    date DATE PRIMARY KEY,
                    payload TEXT,
                    computed_ts DOUBLE PRECISION
                )
                """
            )
        )
    else:
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS mainline_daily_snapshot (
                    date TEXT PRIMARY KEY,
                    payload TEXT,
                    computed_ts REAL
                )
                """
            )
        )


def _m190_ladder_daily_snapshot_table(conn: Connection) -> None:
    """连板梯队定型日快照表(2026-10-14 定型预渲染落库)。

    背景(审计定性): `/api/theme-mood/ladder` 的 `_ladder_payload` **每次请求重算** ——
    20 天全市场 limit_up_events + 1100+ 只个股 OHLC 批量查询, 代码注释自证实测 6.5s;
    且 TTL 盘中仅 30s, 一到期就再吃一次 6.5s。但梯队**历史交易日的连板数不可变**
    (只随 limit_up_events 落库而定), 盘中唯一会变的只有"当日行"(来自既有
    ladder_live_state 盘中状态机)。故收盘 cron 预渲染定型 payload 落库, 请求期读库为主。

    字段: window_days(INTEGER) = /ladder 的 `window` 参数(列名避开 window 关键字) /
    mode(TEXT) auto|finalized|live / date(DATE, 落库日 CST) / payload(TEXT) 完整响应
    JSON / computed_ts(epoch 秒)。主键 (window_days, mode, date) → 同日重跑幂等覆盖,
    收盘后那次即当日定稿。先例: _m189 mainline_daily_snapshot(同款 DDL 风格)。

    **合入提醒**: 并行分支 `feat/summary-precompute-20261014` 也申请了 190
    (`workbench_layer_daily`) —— check_migrations 要求版本 101 起**连续唯一**, 故两支
    只能有一支保留 190; 后落地的那支需顺延(函数名 `_m190_` → `_m191_` + 注册号 +1)。
    """
    if _dialect_is_pg(conn):
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS ladder_daily_snapshot (
                    window_days INTEGER NOT NULL,
                    mode TEXT NOT NULL,
                    date DATE NOT NULL,
                    payload TEXT,
                    computed_ts DOUBLE PRECISION,
                    PRIMARY KEY (window_days, mode, date)
                )
                """
            )
        )
    else:
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS ladder_daily_snapshot (
                    window_days INTEGER NOT NULL,
                    mode TEXT NOT NULL,
                    date TEXT NOT NULL,
                    payload TEXT,
                    computed_ts REAL,
                    PRIMARY KEY (window_days, mode, date)
                )
                """
            )
        )
def _m191_workbench_layer_daily_table(conn: Connection) -> None:
    """工作台日级图层派生块预落库表(2026-10-14 首屏 P0)。

    为什么需要: 个股工作台首屏 `/api/klines/{symbol}/summary` 的**日级派生块**
    (gs_signals 全量交叉序列 / fund_flow / activity_series / events / unlock_levels /
    chips / resonance)此前**每次请求都在 `_build_layer_data` 现算** —— 其中 chips
    (腾讯/新浪分价表)、events 的 wencai 两条查询(硬超时 10s)、resonance 的
    compute_pool_flow(明+暗) 都是网络调用, 冷启动实测 ~26s, 撞前端 fetchAPI 的
    20s abort。只有 300s TTL 兜底 ⇒ 隔夜首访/重启/换 worker 必重吃一遍。
    本表让收盘批算一次 + 请求期读库, 请求期只现算"当日实时块"(口径不变)。

    诚实语义: 行只在**该交易日已定型**(收盘 15:05 后)时才置 `settled=1`; 盘中写入的
    当日行 `settled=0` **永不被读侧使用**(只作"最近访问"痕迹) —— 不拿旧块冒充当日状态。
    读侧另用"最近已定型交易日"做新鲜度门禁(行过旧 → 回现算), 见
    `src/core/layer_precompute.py::usable`。

    字段(双方言同一 DDL: SQLite 对类型声明宽松, 与 _m183 同法):
      symbol TEXT / market TEXT / asof TEXT('YYYY-MM-DD' 交易日)  → 联合主键(保留历史行)
      settled INTEGER   -- 1 = 该日已定型(可被读侧使用)
      source TEXT       -- precompute(收盘批算) / request(请求期算完落库)
      computed_ts DOUBLE PRECISION -- 写入时刻(epoch 秒), 供"最近访问"与保留期清理
      payload TEXT      -- 日级块 JSON(上限 1MB, 超限宁可不写, 绝不存残缺载荷)
    """
    conn.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS workbench_layer_daily (
                symbol TEXT NOT NULL,
                market TEXT NOT NULL,
                asof TEXT NOT NULL,
                settled INTEGER NOT NULL DEFAULT 0,
                source TEXT DEFAULT '',
                computed_ts DOUBLE PRECISION,
                payload TEXT NOT NULL,
                PRIMARY KEY (symbol, market, asof)
            )
            """
        )
    )
    conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_workbench_layer_daily_ts "
            "ON workbench_layer_daily(computed_ts)"
        )
    )


def _m192_dragon_tiger_daily_table(conn: Connection) -> None:
    """龙虎榜按日明细表 dragon_tiger_daily(2026-10-14 资金类落库收口)。

    审计定性: `/market-data/fundamentals-detail` 经 `_lhb_from_tq` 每次请求**现拉** TQ GP
    长序列(个股整段历史), `/dragon-tiger/{date}` 亦逐次现拉东财+ftshare。本表把拉到
    的行**按日 upsert 落库**: 请求只读库, 仅当日(尚未落库)增量回源; 历史日永不回源。

    来源合一(TQ GP 序列 / 东财-ftshare 市场级), 同 `(trade_date, symbol, market)` 唯一键 →
    重拉幂等。TQ 侧独有字段(机构/营业部/沪深股通买卖拆解, 万元原口径)也一并落;
    top_buyers/top_sellers 结构化列表存 JSON 文本(席位明细)。
    """
    if _has_table(conn, "dragon_tiger_daily"):
        return
    money = "DOUBLE PRECISION" if _dialect_is_pg(conn) else "REAL"
    stamp = "TIMESTAMP" if _dialect_is_pg(conn) else "DATETIME"
    boolt = "BOOLEAN" if _dialect_is_pg(conn) else "INTEGER"
    conn.execute(
        text(
            f"""
            CREATE TABLE dragon_tiger_daily (
                trade_date TEXT NOT NULL,
                symbol TEXT NOT NULL,
                market TEXT NOT NULL DEFAULT 'CN',
                name TEXT,
                reason TEXT,
                close {money},
                change_pct {money},
                net_buy {money},
                buy_amt {money},
                sell_amt {money},
                turnover_pct {money},
                top_buyers TEXT,
                top_sellers TEXT,
                inst_buy_wan {money},
                inst_sell_wan {money},
                yyb_buy_wan {money},
                yyb_sell_wan {money},
                hsgt_buy_wan {money},
                hsgt_sell_wan {money},
                suspicious {boolt},
                source TEXT,
                updated_at {stamp} DEFAULT CURRENT_TIMESTAMP,
                CONSTRAINT uq_dragon_tiger_daily UNIQUE (trade_date, symbol, market)
            )
            """
        )
    )
    _create_index_if_missing(
        conn,
        "ix_dragon_tiger_daily_sym_date",
        "CREATE INDEX ix_dragon_tiger_daily_sym_date "
        "ON dragon_tiger_daily (symbol, trade_date DESC)",
    )
    _create_index_if_missing(
        conn,
        "ix_dragon_tiger_daily_date",
        "CREATE INDEX ix_dragon_tiger_daily_date "
        "ON dragon_tiger_daily (trade_date DESC, market)",
    )


def _m193_board_flow_snapshots_table(conn: Connection) -> None:
    """板块资金采样快照表 board_flow_snapshots(2026-10-14)。

    审计定性: `/market-data/board-capital-flow` **完全现拉**同花顺行业/概念资金, 仅用
    biz_cache 的 stale 备份兜源故障 —— 上游一挂整块缺失。本表按 1~5min 采样落一条,
    读侧『够新读库、过旧回源 + stale 标注』, 与 market_flow_snapshots(m138) 同思路。

    字段: ts(DB 端默认时间戳) / board_type(industry|concept) / payload(整块 items JSON) /
    item_count。行序采样(非定稿), 按 (board_type, ts DESC) 查最新。
    """
    if _has_table(conn, "board_flow_snapshots"):
        return
    pk = "SERIAL PRIMARY KEY" if _dialect_is_pg(conn) else "INTEGER PRIMARY KEY AUTOINCREMENT"
    stamp = "TIMESTAMP" if _dialect_is_pg(conn) else "DATETIME"
    conn.execute(
        text(
            f"""
            CREATE TABLE board_flow_snapshots (
                id {pk},
                ts {stamp} DEFAULT CURRENT_TIMESTAMP,
                board_type TEXT NOT NULL,
                item_count INTEGER,
                payload TEXT
            )
            """
        )
    )
    _create_index_if_missing(
        conn,
        "ix_board_flow_snapshots_type_ts",
        "CREATE INDEX ix_board_flow_snapshots_type_ts "
        "ON board_flow_snapshots (board_type, ts DESC)",
    )


def _m196_sentiment_selfcalc_daily(conn: Connection) -> None:
    """情绪序列**本地自算**明细/诊断表 sentiment_selfcalc_daily(2026-10-14)。

    ⚠️ 迁移号取 **196**(不是 195): 生产 `schema_migrations` 里 195 已被姊妹分支
    `ai_pattern_shadow` 占用(实测 `195|ai_pattern_shadow|success=0`), 撞号会让
    `has_pending_migrations` 因 checksum 不符反复重跑并互相覆盖记录。

    背景(用户决策): 不购买通达信云数据 Key, SC/GP **云**函数永久放弃(生产已断在
    2026-09-23)。情绪序列改由客户端本地接口自算(`src.core.sentiment_selfcalc`:
    get_zdt_data + get_pricevol + get_stock_list + limit_up_events 聚合)。

    为什么另起一张表而不是只写 market_sentiment_daily:
      · 自算的封板资金是**近似**(封单量×收盘价), 需要一个显式标注位(`approximation`)
        与原始元金额/口径标识留痕 —— market_sentiment_daily 的列是 SC 口径, 不该塞私货;
      · 诊断量(候选数/封住数/炸板次数分布/价量缺失数/分片失败数)供审计复核,
        不进被温度计高频读取的主表。
    兼容列(涨停/炸板/连板/封板资金)仍**同时**写入 market_sentiment_daily,
    温度计/API/新鲜度自检零改动即可读到。

    单位: 家数=家; `seal_*_money` = 亿元(与 SC15 同口径, `money_unit` 显式登记)。
    """
    if _has_table(conn, "sentiment_selfcalc_daily"):
        return
    id_col = "SERIAL PRIMARY KEY" if _dialect_is_pg(conn) else "INTEGER PRIMARY KEY AUTOINCREMENT"
    conn.execute(
        text(
            f"""
CREATE TABLE sentiment_selfcalc_daily (
  id {id_col},
  trade_date TEXT NOT NULL,
  market TEXT NOT NULL DEFAULT 'CN',
  limit_up_count INTEGER,
  limit_up_open_count INTEGER,
  streak_count INTEGER,
  seal_success_money REAL,
  seal_fail_money REAL,
  seal_success_rate REAL,
  blow_ratio REAL,
  candidates INTEGER,
  sealed_count INTEGER,
  open_times_gt0 INTEGER,
  price_missing INTEGER,
  chunks_total INTEGER NOT NULL DEFAULT 0,
  chunks_failed INTEGER NOT NULL DEFAULT 0,
  approximation INTEGER NOT NULL DEFAULT 1,
  caliber TEXT NOT NULL DEFAULT 'close_ge_limit_price',
  money_unit TEXT NOT NULL DEFAULT '亿元',
  source TEXT NOT NULL DEFAULT 'tdx_selfcalc',
  extra TEXT NOT NULL DEFAULT '{{}}',
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""
        )
    )
    _create_index_if_missing(
        conn,
        "uq_sentiment_selfcalc_daily",
        "CREATE UNIQUE INDEX uq_sentiment_selfcalc_daily "
        "ON sentiment_selfcalc_daily(trade_date, market)",
    )
    _create_index_if_missing(
        conn,
        "ix_sentiment_selfcalc_daily_date",
        "CREATE INDEX ix_sentiment_selfcalc_daily_date "
        "ON sentiment_selfcalc_daily(trade_date)",
    )


def _m194_orderbook_snapshots_table(conn: Connection) -> None:
    """十档盘口快照采集表 orderbook_snapshots(2026-10-14, 盘口移出请求路径)。

    审计定性: `/api/orderbook-ob`(硬超时 12s)与 klines summary 的 `_build_orderbook`
    (硬超时 8s)在**请求期**直接打 thsdk 十档(order_book_bid/ask), 上游单次实测可卡 30s
    → 卡顿/超时直接压在用户请求上。本表承载**后台采集**的十档快照, 请求端读快照优先。

    一行一票(`symbol` 唯一, upsert 原地更新滚动窗): `collected_at`=采集时刻 epoch 秒
    (陈旧度基准), `payload`=滚动窗帧 JSON(旧→新, 最多 N 帧), `latest_ts`/`frames` 便捷列。
    节流周期 / 陈旧阈值 / universe 上限均 env 可配。
    """
    if _has_table(conn, "orderbook_snapshots"):
        return
    dbl = "DOUBLE PRECISION" if _dialect_is_pg(conn) else "REAL"
    conn.execute(
        text(
            f"""
            CREATE TABLE orderbook_snapshots (
                symbol TEXT NOT NULL,
                market TEXT NOT NULL DEFAULT 'CN',
                collected_at {dbl} NOT NULL,
                latest_ts TEXT,
                frames INTEGER NOT NULL DEFAULT 0,
                payload TEXT NOT NULL,
                source TEXT DEFAULT 'thsdk'
            )
            """
        )
    )
    _create_index_if_missing(
        conn,
        "uq_orderbook_snap_symbol",
        "CREATE UNIQUE INDEX uq_orderbook_snap_symbol "
        "ON orderbook_snapshots (symbol)",
    )


def _m195_ai_pattern_shadow(conn: Connection) -> None:
    """AI 看图形态影子表 ai_pattern_shadow(2026-10-14 胜率创新 · 主线一并行)。

    **影子运行**: 每日盘后对自选∪持仓逐票做「K线渲染→视觉模型认形态」, 并与
    `src/core/kline_patterns` 规则形态同日同票并排落库, 只写本表, **不改任何现有
    信号/决策/展示语义** —— 供「分桶验证」(agree/conflict/仅AI/仅规则 四组 T+1/T+3
    命中差) 使用。

    一行一票(trade_date, symbol): AI 侧存主形态(最高置信)+全量形态 JSON, 规则侧存
    规则形态 JSON + 对照结论 compare_status。缺数据/无视觉模型 → status='degraded'
    (不出形态, 绝不编造)。schema 唯一入口是本迁移(AGENTS 铁律), 禁止运行时建表。
    先例: _m188 limit_strength_shadow。
    """
    conn.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS ai_pattern_shadow (
                trade_date TEXT NOT NULL,
                symbol TEXT NOT NULL,
                name TEXT,
                ai_pattern TEXT,
                ai_confidence DOUBLE PRECISION,
                ai_basis TEXT,
                ai_all TEXT,
                model_name TEXT,
                window_n INTEGER,
                rule_primary TEXT,
                rule_patterns TEXT,
                compare_status TEXT,
                status TEXT NOT NULL DEFAULT 'no_data',
                source TEXT,
                captured_at TIMESTAMP,
                PRIMARY KEY (trade_date, symbol)
            )
            """
        )
    )
    if _has_table(conn, "ai_pattern_shadow"):
        _create_index_if_missing(
            conn,
            "ix_ai_pattern_shadow_date",
            "CREATE INDEX ix_ai_pattern_shadow_date "
            "ON ai_pattern_shadow (trade_date DESC)",
        )
        _create_index_if_missing(
            conn,
            "ix_ai_pattern_shadow_compare",
            "CREATE INDEX ix_ai_pattern_shadow_compare "
            "ON ai_pattern_shadow (compare_status)",
        )


def _m140_market_scan_ranks_table(conn: Connection) -> None:
    """三榜快照表 market_scan_ranks(W1.5/A5 收编: 原 market_scan.py 运行时兜底建表)。

    ORM 模型建表: 方言类型由模型自带, 无需手写双份 DDL。延迟 import 规避
    database ↔ migrations 循环依赖。老库已由 create_all/兜底建过 → checkfirst 跳过。
    """
    from src.db.models import MarketScanRank

    MarketScanRank.__table__.create(bind=conn, checkfirst=True)


def _m141_signal_summary_daily_table(conn: Connection) -> None:
    """信号摘要日快照表 signal_summary_daily(W1.5/A5 收编: 原 signal_summary.py 运行时兜底建表)。"""
    from src.db.models import SignalSummaryDaily

    SignalSummaryDaily.__table__.create(bind=conn, checkfirst=True)


def _m142_dark_fund_top_snapshots_table(conn: Connection) -> None:
    """暗盘资金 TOP 快照表 dark_fund_top_snapshots(W1.5/A5 收编: 原 market_scan.py 运行时兜底建表)。"""
    from src.db.models import DarkFundTopSnapshot

    DarkFundTopSnapshot.__table__.create(bind=conn, checkfirst=True)


def _m149_stock_universe_snapshots(conn: Connection) -> None:
    """PIT 股票池日快照表 stock_universe_snapshots(B0.6/KI-036, 消除幸存者偏差)。"""
    from src.db.models import StockUniverseSnapshot

    StockUniverseSnapshot.__table__.create(bind=conn, checkfirst=True)


def _m150_klines_amount_column(conn: Connection) -> None:
    """klines 补 amount 列(B1.3/KI-040): 成交额, 供单位恒等式与金额口径使用。

    历史行留 NULL(诚实缺失, 不伪造); 写入端在 vendor 提供时填充。
    """
    if not _has_column(conn, "klines", "amount"):
        col_type = "DOUBLE PRECISION" if _dialect_is_pg(conn) else "REAL"
        conn.execute(text(f"ALTER TABLE klines ADD COLUMN amount {col_type}"))


def _m151_trading_halts_table(conn: Connection) -> None:
    """停牌区间表 trading_halts(B1.5/KI-040)。"""
    from src.db.models import TradingHalt

    TradingHalt.__table__.create(bind=conn, checkfirst=True)


def _m152_adj_factors_table(conn: Connection) -> None:
    """除权除息因子表 adj_factors(B1.4/KI-040)。"""
    from src.db.models import AdjFactor

    AdjFactor.__table__.create(bind=conn, checkfirst=True)


def _m153_datasource_failures_table(conn: Connection) -> None:
    """数据源失败明细表 datasource_failures(B1.6/KI-040)。"""
    from src.db.models import DatasourceFailure

    DatasourceFailure.__table__.create(bind=conn, checkfirst=True)


def _m154_auction_snapshots_table(conn: Connection) -> None:
    """集合竞价快照表 auction_snapshots(批次2, 2026-09-10)。

    竞价在 9:15-9:25 唯一 → `(trade_date, symbol, market)` 唯一键, 重跑覆盖(upsert)。
    字段: TQ(开盘价/涨幅/竞价成交额/开盘一字买量) + thsdk(方向/高低/09:20 前撤单率近似)。
    与 `_m138` 同款: 迁移建表(非 ORM), 写入走裸 SQL 幂等 upsert。
    """
    if _has_table(conn, "auction_snapshots"):
        return
    if _dialect_is_pg(conn):
        conn.execute(
            text(
                """
                CREATE TABLE auction_snapshots (
                    id SERIAL PRIMARY KEY,
                    trade_date DATE NOT NULL,
                    symbol TEXT NOT NULL,
                    market TEXT NOT NULL DEFAULT 'CN',
                    source TEXT,
                    auction_price DOUBLE PRECISION,
                    open_price DOUBLE PRECISION,
                    open_pct DOUBLE PRECISION,
                    open_amount DOUBLE PRECISION,
                    open_limit_buy DOUBLE PRECISION,
                    direction TEXT,
                    gap_pct DOUBLE PRECISION,
                    auction_high DOUBLE PRECISION,
                    auction_low DOUBLE PRECISION,
                    withdraw_rate_pre0920 DOUBLE PRECISION,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT uq_auction_snapshots UNIQUE (trade_date, symbol, market)
                )
                """
            )
        )
    else:
        conn.execute(
            text(
                """
                CREATE TABLE auction_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_date TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    market TEXT NOT NULL DEFAULT 'CN',
                    source TEXT,
                    auction_price REAL,
                    open_price REAL,
                    open_pct REAL,
                    open_amount REAL,
                    open_limit_buy REAL,
                    direction TEXT,
                    gap_pct REAL,
                    auction_high REAL,
                    auction_low REAL,
                    withdraw_rate_pre0920 REAL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE (trade_date, symbol, market)
                )
                """
            )
        )
    _create_index_if_missing(
        conn,
        "ix_auction_snapshots_symbol_date",
        "CREATE INDEX ix_auction_snapshots_symbol_date "
        "ON auction_snapshots (symbol, trade_date DESC)",
    )


def _m155_chip_daily_table(conn: Connection) -> None:
    """筹码分布日表 chip_daily(批次2, 2026-09-10)。

    每交易日一算一存(自选∪持仓) → `(trade_date, symbol, market)` 唯一, 重算覆盖。
    字段口径同 `src/core/chip_distribution.compute_chips` 的输出。
    """
    if _has_table(conn, "chip_daily"):
        return
    if _dialect_is_pg(conn):
        conn.execute(
            text(
                """
                CREATE TABLE chip_daily (
                    id SERIAL PRIMARY KEY,
                    trade_date DATE NOT NULL,
                    symbol TEXT NOT NULL,
                    market TEXT NOT NULL DEFAULT 'CN',
                    cost_10 DOUBLE PRECISION,
                    cost_50 DOUBLE PRECISION,
                    cost_90 DOUBLE PRECISION,
                    profit_ratio DOUBLE PRECISION,
                    peak_price DOUBLE PRECISION,
                    cost_band_low DOUBLE PRECISION,
                    cost_band_high DOUBLE PRECISION,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT uq_chip_daily UNIQUE (trade_date, symbol, market)
                )
                """
            )
        )
    else:
        conn.execute(
            text(
                """
                CREATE TABLE chip_daily (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_date TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    market TEXT NOT NULL DEFAULT 'CN',
                    cost_10 REAL,
                    cost_50 REAL,
                    cost_90 REAL,
                    profit_ratio REAL,
                    peak_price REAL,
                    cost_band_low REAL,
                    cost_band_high REAL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE (trade_date, symbol, market)
                )
                """
            )
        )
    _create_index_if_missing(
        conn,
        "ix_chip_daily_symbol_date",
        "CREATE INDEX ix_chip_daily_symbol_date ON chip_daily (symbol, trade_date DESC)",
    )


def _m156_dragon_tiger_events_table(conn: Connection) -> None:
    """龙虎榜事件表 dragon_tiger_events(2026-09-10, 妖股因子 lhb 维数据底座)。

    东财 datacenter RPT_DAILYBILLBOARD_DETAILSNEW 每日明细落档,
    `(trade_date, symbol, reason)` 唯一(同日多上榜原因各一行), 重拉幂等。
    demon_score lhb 维(近一年上榜天数)与 circ_mv(流通市值)从这里取。
    """
    if _has_table(conn, "dragon_tiger_events"):
        return
    if _dialect_is_pg(conn):
        conn.execute(
            text(
                """
                CREATE TABLE dragon_tiger_events (
                    trade_date TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    market TEXT NOT NULL DEFAULT 'CN',
                    name TEXT,
                    reason TEXT NOT NULL DEFAULT '',
                    close DOUBLE PRECISION,
                    change_pct DOUBLE PRECISION,
                    net_buy DOUBLE PRECISION,
                    buy_amt DOUBLE PRECISION,
                    sell_amt DOUBLE PRECISION,
                    deal_amt DOUBLE PRECISION,
                    turnover_pct DOUBLE PRECISION,
                    free_market_cap DOUBLE PRECISION,
                    source TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT uq_dragon_tiger_events UNIQUE (trade_date, symbol, reason)
                )
                """
            )
        )
    else:
        conn.execute(
            text(
                """
                CREATE TABLE dragon_tiger_events (
                    trade_date TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    market TEXT NOT NULL DEFAULT 'CN',
                    name TEXT,
                    reason TEXT NOT NULL DEFAULT '',
                    close REAL,
                    change_pct REAL,
                    net_buy REAL,
                    buy_amt REAL,
                    sell_amt REAL,
                    deal_amt REAL,
                    turnover_pct REAL,
                    free_market_cap REAL,
                    source TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE (trade_date, symbol, reason)
                )
                """
            )
        )
    _create_index_if_missing(
        conn,
        "ix_dragon_tiger_events_sym_date",
        "CREATE INDEX ix_dragon_tiger_events_sym_date ON dragon_tiger_events (symbol, trade_date DESC)",
    )


def _m161_resonance_scan_table(conn: Connection) -> None:
    """三指标共振扫描结果表 resonance_scan(2026-09-11, 数智决策升级)。

    全市场盘后扫描: 趋势(GS) × 强度(AI机构活跃度≥3) × 资金(主力净流入>0),
    `(trade_date, symbol)` 唯一, 重扫幂等(结果覆盖)。
    """
    if _has_table(conn, "resonance_scan"):
        return
    if _dialect_is_pg(conn):
        conn.execute(
            text(
                """
                CREATE TABLE resonance_scan (
                    trade_date TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    name TEXT,
                    trend TEXT,
                    activity DOUBLE PRECISION,
                    level TEXT,
                    fund_net DOUBLE PRECISION,
                    hits INTEGER NOT NULL DEFAULT 0,
                    resonance BOOLEAN NOT NULL DEFAULT FALSE,
                    near BOOLEAN NOT NULL DEFAULT FALSE,
                    close DOUBLE PRECISION,
                    change_pct DOUBLE PRECISION,
                    source TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT uq_resonance_scan UNIQUE (trade_date, symbol)
                )
                """
            )
        )
    else:
        conn.execute(
            text(
                """
                CREATE TABLE resonance_scan (
                    trade_date TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    name TEXT,
                    trend TEXT,
                    activity REAL,
                    level TEXT,
                    fund_net REAL,
                    hits INTEGER NOT NULL DEFAULT 0,
                    resonance INTEGER NOT NULL DEFAULT 0,
                    near INTEGER NOT NULL DEFAULT 0,
                    close REAL,
                    change_pct REAL,
                    source TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE (trade_date, symbol)
                )
                """
            )
        )
    _create_index_if_missing(
        conn,
        "ix_resonance_scan_date_hits",
        "CREATE INDEX ix_resonance_scan_date_hits ON resonance_scan (trade_date DESC, hits DESC)",
    )
    _create_index_if_missing(
        conn,
        "ix_resonance_scan_sym_date",
        "CREATE INDEX ix_resonance_scan_sym_date ON resonance_scan (symbol, trade_date DESC)",
    )


def _m162_resonance_ai_verdicts_table(conn: Connection) -> None:
    """共振 AI 判定存档(2026-09-11, 老板"批量跑一遍 AI 判定, 首页每行显示结论")。

    盘后扫描(15:40)完成后, 对当日共振标的批量跑 LLM 判定(单次多票, 控制调用量),
    结果按 `(trade_date, symbol)` 唯一落库, 供首页卡片行内显示与回看。
    """
    if _has_table(conn, "resonance_ai_verdicts"):
        return
    if _dialect_is_pg(conn):
        conn.execute(
            text(
                """
                CREATE TABLE resonance_ai_verdicts (
                    trade_date TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    verdict TEXT,
                    confidence DOUBLE PRECISION,
                    summary TEXT,
                    reasons TEXT,
                    risks TEXT,
                    watch TEXT,
                    missing TEXT,
                    model TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT uq_resonance_ai_verdicts UNIQUE (trade_date, symbol)
                )
                """
            )
        )
    else:
        conn.execute(
            text(
                """
                CREATE TABLE resonance_ai_verdicts (
                    trade_date TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    verdict TEXT,
                    confidence REAL,
                    summary TEXT,
                    reasons TEXT,
                    risks TEXT,
                    watch TEXT,
                    missing TEXT,
                    model TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE (trade_date, symbol)
                )
                """
            )
        )
    _create_index_if_missing(
        conn,
        "ix_resonance_ai_verdicts_date",
        "CREATE INDEX ix_resonance_ai_verdicts_date ON resonance_ai_verdicts (trade_date DESC)",
    )


def _m163_theme_mood_table(conn: Connection) -> None:
    """题材情绪分日表(2026-09-12): 每题材每日一行, 五维 + 置信度 + 核心股 + 明细。

    JSON 载荷一律 TEXT(SQLite/PG 同构, 读侧 json.loads)。
    """
    if _has_table(conn, "theme_mood_daily"):
        return
    if _dialect_is_pg(conn):
        conn.execute(
            text(
                """
                CREATE TABLE theme_mood_daily (
                    trade_date TEXT NOT NULL,
                    block_code TEXT NOT NULL,
                    block_name TEXT,
                    block_type TEXT,
                    score DOUBLE PRECISION,
                    s1 DOUBLE PRECISION, s2 DOUBLE PRECISION, s3 DOUBLE PRECISION,
                    s4 DOUBLE PRECISION, s5 DOUBLE PRECISION,
                    confidence INTEGER,
                    core BOOLEAN DEFAULT FALSE,
                    limit_up_cnt INTEGER DEFAULT 0,
                    touched_cnt INTEGER DEFAULT 0,
                    max_boards INTEGER DEFAULT 0,
                    ge2_cnt INTEGER DEFAULT 0,
                    core_stocks TEXT,
                    detail TEXT,
                    breadth TEXT,
                    source TEXT DEFAULT 'close',
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT uq_theme_mood_daily UNIQUE (trade_date, block_code)
                )
                """
            )
        )
    else:
        conn.execute(
            text(
                """
                CREATE TABLE theme_mood_daily (
                    trade_date TEXT NOT NULL,
                    block_code TEXT NOT NULL,
                    block_name TEXT,
                    block_type TEXT,
                    score REAL,
                    s1 REAL, s2 REAL, s3 REAL, s4 REAL, s5 REAL,
                    confidence INTEGER,
                    core INTEGER DEFAULT 0,
                    limit_up_cnt INTEGER DEFAULT 0,
                    touched_cnt INTEGER DEFAULT 0,
                    max_boards INTEGER DEFAULT 0,
                    ge2_cnt INTEGER DEFAULT 0,
                    core_stocks TEXT,
                    detail TEXT,
                    breadth TEXT,
                    source TEXT DEFAULT 'close',
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE (trade_date, block_code)
                )
                """
            )
        )
    _create_index_if_missing(
        conn, "ix_theme_mood_daily_date",
        "CREATE INDEX ix_theme_mood_daily_date ON theme_mood_daily (trade_date DESC)",
    )


def _m160_lhb_detail_tables(conn: Connection) -> None:
    """龙虎榜机构/营业部明细两张表(2026-09-10, 老板"切换到东财全自动方案")。

    - lhb_institution_daily: 东财 RPT_ORGANIZATION_TRADE_DETAILS(机构买卖统计),
      含上榜后 1/2/3/5/10 日涨幅; `(trade_date, symbol, reason)` 唯一, 重拉幂等。
    - lhb_seat_details: 东财 RPT_BILLBOARD_DAILYDETAILSBUY/SELL(营业部买卖明细),
      含席位买入/卖出/净额、3 日上涨概率; 行唯一键 = row_uid(内容哈希) ——
      同一营业部同日同股可在不同上榜原因下多行, 不能按(日期,营业部)粗去重。
    """
    if not _has_table(conn, "lhb_institution_daily"):
        if _dialect_is_pg(conn):
            conn.execute(
                text(
                    """
                    CREATE TABLE lhb_institution_daily (
                        trade_date TEXT NOT NULL,
                        symbol TEXT NOT NULL,
                        name TEXT,
                        reason TEXT NOT NULL DEFAULT '',
                        close DOUBLE PRECISION,
                        change_pct DOUBLE PRECISION,
                        buy_times INTEGER,
                        sell_times INTEGER,
                        buy_amt DOUBLE PRECISION,
                        sell_amt DOUBLE PRECISION,
                        net_amt DOUBLE PRECISION,
                        accum_amount DOUBLE PRECISION,
                        ratio DOUBLE PRECISION,
                        turnover_pct DOUBLE PRECISION,
                        free_cap DOUBLE PRECISION,
                        d1_pct DOUBLE PRECISION,
                        d2_pct DOUBLE PRECISION,
                        d3_pct DOUBLE PRECISION,
                        d5_pct DOUBLE PRECISION,
                        d10_pct DOUBLE PRECISION,
                        source TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        CONSTRAINT uq_lhb_institution_daily UNIQUE (trade_date, symbol, reason)
                    )
                    """
                )
            )
        else:
            conn.execute(
                text(
                    """
                    CREATE TABLE lhb_institution_daily (
                        trade_date TEXT NOT NULL,
                        symbol TEXT NOT NULL,
                        name TEXT,
                        reason TEXT NOT NULL DEFAULT '',
                        close REAL,
                        change_pct REAL,
                        buy_times INTEGER,
                        sell_times INTEGER,
                        buy_amt REAL,
                        sell_amt REAL,
                        net_amt REAL,
                        accum_amount REAL,
                        ratio REAL,
                        turnover_pct REAL,
                        free_cap REAL,
                        d1_pct REAL,
                        d2_pct REAL,
                        d3_pct REAL,
                        d5_pct REAL,
                        d10_pct REAL,
                        source TEXT,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE (trade_date, symbol, reason)
                    )
                    """
                )
            )
        _create_index_if_missing(
            conn,
            "ix_lhb_institution_sym_date",
            "CREATE INDEX ix_lhb_institution_sym_date ON lhb_institution_daily (symbol, trade_date DESC)",
        )
    if not _has_table(conn, "lhb_seat_details"):
        if _dialect_is_pg(conn):
            conn.execute(
                text(
                    """
                    CREATE TABLE lhb_seat_details (
                        row_uid TEXT PRIMARY KEY,
                        trade_date TEXT NOT NULL,
                        side TEXT NOT NULL,
                        symbol TEXT NOT NULL,
                        name TEXT,
                        reason TEXT NOT NULL DEFAULT '',
                        operate_dept_code TEXT NOT NULL DEFAULT '',
                        operate_dept_name TEXT NOT NULL DEFAULT '',
                        close DOUBLE PRECISION,
                        change_pct DOUBLE PRECISION,
                        accum_amount DOUBLE PRECISION,
                        accum_volume DOUBLE PRECISION,
                        buy_amt DOUBLE PRECISION,
                        sell_amt DOUBLE PRECISION,
                        net_amt DOUBLE PRECISION,
                        rise_probability_3d DOUBLE PRECISION,
                        change_type TEXT,
                        trade_id TEXT,
                        total_buy_ratio DOUBLE PRECISION,
                        total_sell_ratio DOUBLE PRECISION,
                        source TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
            )
        else:
            conn.execute(
                text(
                    """
                    CREATE TABLE lhb_seat_details (
                        row_uid TEXT PRIMARY KEY,
                        trade_date TEXT NOT NULL,
                        side TEXT NOT NULL,
                        symbol TEXT NOT NULL,
                        name TEXT,
                        reason TEXT NOT NULL DEFAULT '',
                        operate_dept_code TEXT NOT NULL DEFAULT '',
                        operate_dept_name TEXT NOT NULL DEFAULT '',
                        close REAL,
                        change_pct REAL,
                        accum_amount REAL,
                        accum_volume REAL,
                        buy_amt REAL,
                        sell_amt REAL,
                        net_amt REAL,
                        rise_probability_3d REAL,
                        change_type TEXT,
                        trade_id TEXT,
                        total_buy_ratio REAL,
                        total_sell_ratio REAL,
                        source TEXT,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
            )
        _create_index_if_missing(
            conn,
            "ix_lhb_seat_details_sym_date",
            "CREATE INDEX ix_lhb_seat_details_sym_date ON lhb_seat_details (symbol, trade_date DESC)",
        )
        _create_index_if_missing(
            conn,
            "ix_lhb_seat_details_date",
            "CREATE INDEX ix_lhb_seat_details_date ON lhb_seat_details (trade_date, side)",
        )


def _m157_quote_snapshots_table(conn: Connection) -> None:
    """快照行情 1 分钟桶表 quote_snapshots(批次2 2/2, 2026-09-10)。

    自选∪持仓∪指数 的报价每分钟落一桶((trade_date, market, symbol, ts) 唯一,
    重拉幂等)。唯一键含 trade_date(规避 l2_ticks 教训: 分区/裁剪列必须进唯一键)。
    指数走腾讯原始符号(sh000001 等), market='IDX' 避免与同号个股(平安银行 000001)撞键。
    设计保留 1 年 → agg 日线(治理脚本后续挂)。
    """
    if _has_table(conn, "quote_snapshots"):
        return
    if _dialect_is_pg(conn):
        conn.execute(
            text(
                """
                CREATE TABLE quote_snapshots (
                    trade_date TEXT NOT NULL,
                    ts TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    market TEXT NOT NULL DEFAULT 'CN',
                    name TEXT,
                    last DOUBLE PRECISION,
                    open DOUBLE PRECISION,
                    high DOUBLE PRECISION,
                    low DOUBLE PRECISION,
                    prev_close DOUBLE PRECISION,
                    change_pct DOUBLE PRECISION,
                    volume DOUBLE PRECISION,
                    turnover DOUBLE PRECISION,
                    turnover_rate DOUBLE PRECISION,
                    volume_ratio DOUBLE PRECISION,
                    circ_mv DOUBLE PRECISION,
                    source TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT uq_quote_snapshots UNIQUE (trade_date, market, symbol, ts)
                )
                """
            )
        )
    else:
        conn.execute(
            text(
                """
                CREATE TABLE quote_snapshots (
                    trade_date TEXT NOT NULL,
                    ts TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    market TEXT NOT NULL DEFAULT 'CN',
                    name TEXT,
                    last REAL,
                    open REAL,
                    high REAL,
                    low REAL,
                    prev_close REAL,
                    change_pct REAL,
                    volume REAL,
                    turnover REAL,
                    turnover_rate REAL,
                    volume_ratio REAL,
                    circ_mv REAL,
                    source TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE (trade_date, market, symbol, ts)
                )
                """
            )
        )
    _create_index_if_missing(
        conn,
        "ix_quote_snapshots_sym_ts",
        "CREATE INDEX ix_quote_snapshots_sym_ts ON quote_snapshots (symbol, ts DESC)",
    )


def _m158_l2_ticks_event_time_key(conn: Connection) -> None:
    """l2_ticks 唯一键升级: tick_time(无日期) → ts 事件时间(设计文档 §5.1 方案A)。

    旧键 (symbol,market,source,tick_time,direction,price,vol,amt) 不含日期 →
    跨日同秒同价同量互相顶掉(假去重); 新键以 ts=交易日+tick_time 为准。
    仅空表/新库就地重塑(用 SELECT..LIMIT 1 O(1) 探测, PG 8s statement_timeout
    下不允许 COUNT(*) 全表扫); 有数据的生产库 no-op, 由
    scripts/l2_ticks_event_time.py 在停写窗口备份+回填(79M 行重写 16GB)。
    """
    ddl = """
CREATE TABLE IF NOT EXISTS l2_ticks (
  ts TIMESTAMPTZ NOT NULL,
  symbol TEXT NOT NULL,
  market TEXT NOT NULL,
  source TEXT NOT NULL,
  direction TEXT,
  price DOUBLE PRECISION,
  vol DOUBLE PRECISION,
  amt DOUBLE PRECISION,
  tick_time TEXT
)
"""

    def _create_new():
        conn.execute(text(ddl))
        conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_l2_ticks_dedupe "
                "ON l2_ticks(symbol, market, source, ts, direction, price, vol, amt)"
            )
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_l2_ticks_symbol_ts "
                "ON l2_ticks(symbol, market, ts)"
            )
        )
        # TimescaleDB: 按天分块 + 压缩(分段键=唯一键前缀, Timescale 要求唯一
        # 约束覆盖全部分段列) + 90 天 retention(宽阈值, 保留期决策另行调整)。
        try:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS timescaledb"))
            conn.execute(
                text(
                    "SELECT create_hypertable('l2_ticks', 'ts', if_not_exists => TRUE, "
                    "chunk_time_interval => INTERVAL '1 day')"
                )
            )
            conn.execute(
                text(
                    "ALTER TABLE l2_ticks SET (timescaledb.compress, "
                    "timescaledb.compress_segmentby = 'symbol, market, source', "
                    "timescaledb.compress_orderby = 'ts DESC')"
                )
            )
            conn.execute(
                text(
                    "SELECT add_compression_policy('l2_ticks', INTERVAL '2 days', "
                    "if_not_exists => TRUE)"
                )
            )
            conn.execute(
                text(
                    "SELECT add_retention_policy('l2_ticks', INTERVAL '90 days', "
                    "if_not_exists => TRUE)"
                )
            )
        except Exception as e:
            logger.warning(f"l2_ticks: TimescaleDB 不可用, 降级普通表: {e}")

    if not _has_table(conn, "l2_ticks"):
        _create_new()
        return
    probe = conn.execute(text("SELECT 1 FROM l2_ticks LIMIT 1")).first()
    if probe is not None:
        logger.info(
            "l2_ticks 有数据, 唯一键事件时间化由 scripts/l2_ticks_event_time.py "
            "停写窗口回填执行, 迁移跳过"
        )
        return
    conn.execute(text("DROP TABLE l2_ticks"))
    _create_new()


def _m159_klines_minute_continuous_agg(conn: Connection) -> None:
    """klines 1m 分钟K → 日线 Timescale 连续聚合(PG+timescaledb 专属, 其余 no-op)。

    分钟K线(1m)盘中入库后, 日线 OHLCV 由连续聚合增量维护(实时段自动合并原始行),
    供回测/回查读取与日K一致性校验。不给 klines 挂 retention —— 会把 2023 年起
    的日线历史按 90 天误删, 保留期另行决策。
    """
    if not _dialect_is_pg(conn):
        return
    try:
        has_ext = conn.execute(
            text("SELECT 1 FROM pg_extension WHERE extname='timescaledb' LIMIT 1")
        ).first()
        if not has_ext:
            logger.info("klines 连续聚合: timescaledb 扩展不可用, 跳过")
            return
        has_ht = conn.execute(
            text(
                "SELECT 1 FROM timescaledb_information.hypertables "
                "WHERE hypertable_name='klines' LIMIT 1"
            )
        ).first()
        if not has_ht:
            logger.info("klines 连续聚合: klines 非 hypertable, 跳过")
            return
        conn.execute(
            text(
                """
CREATE MATERIALIZED VIEW IF NOT EXISTS klines_daily_agg
WITH (timescaledb.continuous) AS
SELECT time_bucket('1 day', ts, 'Asia/Shanghai') AS bucket,
       symbol, market,
       first(open, ts) AS open,
       max(high) AS high,
       min(low) AS low,
       last(close, ts) AS close,
       sum(volume) AS volume,
       count(*) AS bar_count
FROM klines
WHERE period = '1m'
GROUP BY bucket, symbol, market
WITH NO DATA
"""
            )
        )
        conn.execute(
            text(
                """
SELECT add_continuous_aggregate_policy('klines_daily_agg',
  start_offset => INTERVAL '3 days',
  end_offset => INTERVAL '1 hour',
  schedule_interval => INTERVAL '30 minutes',
  if_not_exists => TRUE)
"""
            )
        )
        logger.info("klines_daily_agg 连续聚合已创建(1m→日线, 30min 增量刷新)")
    except Exception as e:
        logger.warning(f"klines 连续聚合创建失败(不阻塞启动): {e}")


# ── 历史 A 层迁移收编(W3.1/D2, 2026-09-09) ────────────────────────────────
# 以下 143-148 是原 src/web/database.py 的 A 层 _migrate* 函数(database.py
# 210-876 行), 按 1.5/W3.1 决议搬进版本化迁移成为唯一 schema 变更入口。
# 行为保持逐条一致(幂等守卫/双方言 SQL/Boolean 绑定参数教训注释均保留),
# 仅做三点机械适配: engine.connect() 包装去掉(框架按版本给事务)、显式
# conn.commit() 去掉(由版本事务统一提交)、方言判定改连接级 conn.dialect.name。
# 这些版本在任何现役库上都是幂等 no-op(列/表已存在, 旧表已迁走), 只对
# 更老的存量库生效。

def _ddl_autoincrement(is_pg: bool, sql_template: str) -> str:
    """方言化建表 SQL: PostgreSQL 用 SERIAL/TIMESTAMP, SQLite 用 AUTOINCREMENT/DATETIME。"""
    if is_pg:
        return sql_template.format(pk="SERIAL PRIMARY KEY", ts="TIMESTAMP")
    return sql_template.format(pk="INTEGER PRIMARY KEY AUTOINCREMENT", ts="DATETIME")


def _legacy_insert_returning_id(conn: Connection, insert_sql: str, params: dict) -> int:
    """插入新行后方言无关地取自增主键 id(原 database.py _insert_returning_id)。

    2026-08-27 fix (5+1 评审 B 轨): ``SELECT last_insert_rowid()`` 是 SQLite 专属,
    PG 下报 ``function last_insert_rowid() does not exist`` 触发即启动失败。
    PG 用 INSERT ... RETURNING id; SQLite 保留 last_insert_rowid(全版本兼容)。
    """
    if _dialect_is_pg(conn):
        return conn.execute(text(f"{insert_sql} RETURNING id"), params).scalar()
    conn.execute(text(insert_sql), params)
    return conn.execute(text("SELECT last_insert_rowid()")).scalar()


def _drop_dangling_ai_provider_fk(conn: Connection, table: str) -> None:
    """删掉指向已不存在的 ai_providers 表的悬空外键列。

    背景: 144 号迁移把 ai_providers 表删了, 但
    agent_configs.ai_provider_id / stock_agents.ai_provider_id 这两列上的 FK 没清。
    SQLite 默认 PRAGMA foreign_keys 不开, 所以历史 INSERT 没事; 但某些路径下
    (比如 INSERT ... RETURNING + SQLAlchemy 校验)会报 "no such table: ai_providers"。

    SQLite 3.35+ 支持 ALTER TABLE DROP COLUMN, 直接 drop 即可。
    """
    if not _has_column(conn, table, "ai_provider_id"):
        return
    # ai_providers 还存在的话先不动(让 144 号迁移先迁移)
    if _has_table(conn, "ai_providers"):
        return
    try:
        conn.execute(text(f"ALTER TABLE {table} DROP COLUMN ai_provider_id"))
        logger.info(f"已清理 {table}.ai_provider_id 悬空外键列")
    except Exception as e:
        # 老 SQLite 不支持 DROP COLUMN — fallback 留 schema 不动, 改用 PRAGMA foreign_keys=OFF
        # (本进程级别, 不影响其他业务, 因为 ai_providers 不存在, FK 永远无效)
        logger.warning(
            f"DROP COLUMN {table}.ai_provider_id 失败 (SQLite < 3.35?): {e}; "
            f"将通过 PRAGMA foreign_keys=OFF 绕开"
        )
        try:
            conn.execute(text("PRAGMA foreign_keys = OFF"))
        except Exception:
            pass


def _m143_legacy_missing_columns(conn: Connection) -> None:
    """原 database.py _migrate: 历史增量列补齐 + 兜底建表 + 悬空 FK 清理。"""
    is_pg = _dialect_is_pg(conn)
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
    for table, column, sql in migrations:
        _add_column_if_missing(conn, table, column, sql)

    # 清理 legacy 悬空外键:agent_configs.ai_provider_id / stock_agents.ai_provider_id
    # 这两列原本 REFERENCES ai_providers(id), 但 144 号迁移已经把
    # ai_providers 表删了。如果保留 FK, 新 INSERT 会触发 SQLite "no such table" 错误
    # (因为 SQLite 在 INSERT 时校验 FK 引用的表是否存在)。
    _drop_dangling_ai_provider_fk(conn, "agent_configs")
    _drop_dangling_ai_provider_fk(conn, "stock_agents")

    # 初始化排序字段（仅对未初始化数据）
    if _has_column(conn, "stocks", "sort_order"):
        conn.execute(text("UPDATE stocks SET sort_order = id WHERE sort_order IS NULL OR sort_order = 0"))
    if _has_column(conn, "positions", "sort_order"):
        conn.execute(text("UPDATE positions SET sort_order = id WHERE sort_order IS NULL OR sort_order = 0"))

    # Create new tables if missing (双方言: SQLite / PostgreSQL)
    if not _has_table(conn, "suggestion_feedback"):
        conn.execute(
            text(
                _ddl_autoincrement(
                    is_pg,
                    """CREATE TABLE IF NOT EXISTS suggestion_feedback (
  id {pk},
  suggestion_id INTEGER NOT NULL REFERENCES stock_suggestions(id) ON DELETE CASCADE,
  useful INTEGER DEFAULT 1,
  created_at {ts} DEFAULT CURRENT_TIMESTAMP
);""",
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

    # 统一 LLM 配置中心(2026-08-12): 场景-模型绑定表。
    # create_all 通常已建表(ORM 注册), 这里兜底保证存量库直接升级也有该表。
    if not _has_table(conn, "ai_scene_bindings"):
        conn.execute(
            text(
                _ddl_autoincrement(
                    is_pg,
                    """CREATE TABLE IF NOT EXISTS ai_scene_bindings (
  id {pk},
  scene VARCHAR(64) NOT NULL UNIQUE,
  model_id INTEGER REFERENCES ai_models(id) ON DELETE SET NULL,
  updated_at {ts} DEFAULT CURRENT_TIMESTAMP
);""",
                )
            )
        )

    # 用户 BYOK AI 服务表(2026-08-15): 同上, create_all 已建(ORM 注册),
    # 这里兜底保证存量库直接升级也有该表。
    if not _has_table(conn, "user_ai_services"):
        conn.execute(
            text(
                _ddl_autoincrement(
                    is_pg,
                    """CREATE TABLE IF NOT EXISTS user_ai_services (
  id {pk},
  user_id VARCHAR(36) NOT NULL,
  name VARCHAR NOT NULL,
  base_url VARCHAR NOT NULL,
  api_key VARCHAR DEFAULT '',
  models_json TEXT NOT NULL DEFAULT '[]',
  created_at {ts} DEFAULT CURRENT_TIMESTAMP
);""",
                )
            )
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_user_ai_services_user_id ON user_ai_services(user_id);"
            )
        )

    # 竞价异动池(阶段1.2, v0.3.0): 工作日 09:25 竞价异动股落库。
    # create_all 通常已建表(ORM 注册), 这里兜底保证存量库直接升级也有该表。
    if not _has_table(conn, "auction_anomaly_records"):
        conn.execute(
            text(
                _ddl_autoincrement(
                    is_pg,
                    """CREATE TABLE IF NOT EXISTS auction_anomaly_records (
  id {pk},
  symbol VARCHAR(16) NOT NULL,
  name VARCHAR(64) DEFAULT '',
  gap_pct REAL,
  withdraw_rate REAL,
  volume_ratio REAL,
  note VARCHAR(255) DEFAULT '',
  created_at {ts} DEFAULT CURRENT_TIMESTAMP
);""",
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


def _m144_legacy_old_providers(conn: Connection) -> None:
    """原 database.py _migrate_old_providers: 旧 ai_providers 表迁移到
    ai_services + ai_models 后删表。"""
    if not _has_table(conn, "ai_providers"):
        return
    # Check if it has the old schema (has base_url column)
    if not _has_column(conn, "ai_providers", "base_url"):
        return

    rows = conn.execute(
        text("SELECT id, name, base_url, api_key, model, is_default FROM ai_providers")
    ).fetchall()
    if not rows:
        conn.execute(text("DROP TABLE IF EXISTS ai_providers"))
        return

    # Group by base_url+api_key to create services
    service_map = {}  # (base_url, api_key) -> service_id
    for row in rows:
        old_id, name, base_url, api_key, model, is_default = row
        key = (base_url, api_key)
        if key not in service_map:
            # Create service
            result = _legacy_insert_returning_id(
                conn,
                "INSERT INTO ai_services (name, base_url, api_key) VALUES (:name, :base_url, :api_key)",
                {"name": name, "base_url": base_url, "api_key": api_key},
            )
            service_map[key] = result

        service_id = service_map[key]
        new_model_id = _legacy_insert_returning_id(
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
    logger.info(f"已迁移 {len(rows)} 条旧 AI Provider 数据到 ai_services + ai_models")


def _m145_legacy_settings_to_models(conn: Connection) -> None:
    """原 database.py _migrate_settings_to_models: 旧 app_settings 中的 AI/通知
    配置迁移为 AIService+AIModel / NotifyChannel 记录。"""
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
            service_id = _legacy_insert_returning_id(
                conn,
                "INSERT INTO ai_services (name, base_url, api_key) VALUES (:name, :base_url, :api_key)",
                {"name": ai_model, "base_url": ai_base_url, "api_key": ai_api_key},
            )
            conn.execute(
                text(
                    "INSERT INTO ai_models (name, service_id, model, is_default) VALUES (:name, :service_id, :model, :is_default)"
                ),
                {
                    "name": ai_model,
                    "service_id": service_id,
                    "model": ai_model,
                    "is_default": True,
                },
            )
            logger.info(f"已迁移 AI 配置: {ai_model}")

    # Migrate Telegram settings if present and no channels exist yet
    bot_token = settings_map.get("notify_telegram_bot_token", "")
    chat_id = settings_map.get("notify_telegram_chat_id", "")

    if bot_token:
        existing = conn.execute(text("SELECT COUNT(*) FROM notify_channels")).scalar()
        if existing == 0:
            config_json = json.dumps({"bot_token": bot_token, "chat_id": chat_id})
            conn.execute(
                text(
                    "INSERT INTO notify_channels (name, type, config, enabled, is_default) VALUES (:name, :type, :config, :enabled, :is_default)"
                ),
                {
                    "name": "Telegram",
                    "type": "telegram",
                    "config": config_json,
                    "enabled": True,
                    "is_default": True,
                },
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
            conn.execute(text("DELETE FROM app_settings WHERE key = :key"), {"key": key})


def _m146_legacy_positions_to_accounts(conn: Connection) -> None:
    """原 database.py _migrate_positions_to_accounts: 旧 stocks 表持仓数据迁移到
    accounts + positions 表, 创建默认账户。"""
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
        # Boolean 列不吃整型字面量: PG 直接 DatatypeMismatch 崩, SQLite 宽容。
        # 绑定参数传 True 两方言通用(与 147 号迁移的方言字面量教训同源)。
        # 全新 PG 库首启必走这里, 不修则装不起来。
        conn.execute(
            text(
                "INSERT INTO accounts (name, available_funds, enabled) VALUES ('默认账户', 0, :enabled)"
            ),
            {"enabled": True},
        )
        logger.info("已创建默认账户")
        return

    # 创建默认账户
    # 先获取旧的 available_funds 设置
    old_funds = conn.execute(
        text("SELECT value FROM app_settings WHERE key = 'available_funds'")
    ).scalar()
    available_funds = float(old_funds) if old_funds else 0

    account_id = _legacy_insert_returning_id(
        conn,
        "INSERT INTO accounts (name, available_funds, enabled) VALUES (:name, :funds, :enabled)",
        {"name": "默认账户", "funds": available_funds, "enabled": True},
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
    logger.info(f"已迁移 {len(stocks_with_position)} 条持仓数据到默认账户")


def _m147_legacy_remove_stock_enabled(conn: Connection) -> None:
    """原 database.py _migrate_remove_stock_enabled: 移除历史 stocks.enabled
    软删除字段并清理残留数据。"""
    if not _has_table(conn, "stocks") or not _has_column(conn, "stocks", "enabled"):
        return

    # PG 的 enabled 是 Boolean, SQLite 存 0/1: 字面量按方言写
    # (历史教训: PG 上 `= 0`/`= 1` 直接 operator does not exist 崩)
    is_pg = _dialect_is_pg(conn)
    false_lit, true_lit = ("FALSE", "TRUE") if is_pg else ("0", "1")

    # 历史软删除数据：无任何关联则直接删除；有关联则恢复为有效股票。
    conn.execute(
        text(
            f"""
DELETE FROM stocks
WHERE COALESCE(enabled, {true_lit}) = {false_lit}
  AND id NOT IN (SELECT DISTINCT stock_id FROM positions)
  AND id NOT IN (SELECT DISTINCT stock_id FROM stock_agents)
  AND id NOT IN (SELECT DISTINCT stock_id FROM price_alert_rules)
"""
        )
    )
    conn.execute(
        text(f"UPDATE stocks SET enabled = {true_lit} WHERE COALESCE(enabled, {true_lit}) = {false_lit}")
    )

    # 优先直接删列；旧版 SQLite 不支持时，重建表以确保物理移除。
    # (PG 的 DROP COLUMN 一定成功, 重建分支仅 SQLite 可进: PRAGMA 在 PG 上必崩)
    try:
        conn.execute(text("ALTER TABLE stocks DROP COLUMN enabled"))
        logger.info("已移除 stocks.enabled 列")
    except Exception:
        conn.rollback()
        if is_pg:
            raise
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
        logger.info("已通过重建表移除 stocks.enabled 列")


def _m148_legacy_add_user_id_columns(conn: Connection) -> None:
    """原 database.py _migrate_add_user_id_columns: 多用户阶段2, 业务表加
    user_id 列 + 旧数据归 owner(2026-08-10)。

    - notify_channels/accounts/stocks/positions/agent_runs/analysis_history 加 user_id
    - 旧数据(NULL)自动归 owner(users 表第一个 owner)
    - 新建的 users 表由 create_all 自动创建, 此迁移只处理存量表加列
    """
    tables = ["notify_channels", "accounts", "stocks", "positions", "agent_runs", "analysis_history"]
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
            logger.info(f"多用户迁移: {table} 旧数据归 owner")
        except Exception as e:
            conn.rollback()
            logger.warning(f"多用户迁移 {table} 失败: {e}")


def _m164_market_phase_extra_columns(conn: Connection) -> None:
    """market_phase_daily 补两列(2026-09-12, 借鉴 TSP 阶段体系):

    - completeness: 梯队完整度(2..最高板 非空档占比, 最高板<3 记 NULL)
    - phase_raw:    2 日确认**前**的原始标签(与 phase 的差即被拦下的抖动, 供审计)
    """
    _add_column_if_missing(
        conn, "market_phase_daily", "completeness",
        "ALTER TABLE market_phase_daily ADD COLUMN completeness DOUBLE PRECISION",
    )
    _add_column_if_missing(
        conn, "market_phase_daily", "phase_raw",
        "ALTER TABLE market_phase_daily ADD COLUMN phase_raw VARCHAR(32)",
    )


def _m165_app_jobs_table(conn: Connection) -> None:
    """后台任务表(2026-09-12, 借鉴 TSP 作业框架): 扫描/AI 批量/回填的进度与结果落库。

    落库而不是只放内存: 重启后仍能看到"上一次跑成没有"; status=running 但 updated_at
    停滞过久的行由 JobStore.reap_stale 自愈为 failed(孤儿任务不会永远挂着)。
    """
    if _has_table(conn, "app_jobs"):
        return
    conn.execute(
        text(
            """
CREATE TABLE app_jobs (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  label TEXT DEFAULT '',
  status TEXT NOT NULL DEFAULT 'pending',
  progress INTEGER DEFAULT 0,
  stage TEXT DEFAULT '',
  message TEXT DEFAULT '',
  error TEXT DEFAULT '',
  created_at TIMESTAMP NOT NULL,
  updated_at TIMESTAMP NOT NULL,
  finished_at TIMESTAMP
)
"""
        )
    )
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_app_jobs_kind_created ON app_jobs(kind, created_at)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_app_jobs_status ON app_jobs(status)"))


def _m166_skill_gateway_tables(conn: Connection) -> None:
    """Skill Gateway: AppKey + 用量表(2026-09-15)。

    对外开放 skill 的鉴权与计量落库; key 只存 hash, 明文创建时返回一次。
    """
    if _has_table(conn, "skill_api_keys"):
        return
    is_pg = _dialect_is_pg(conn)
    pk = "SERIAL PRIMARY KEY" if is_pg else "INTEGER PRIMARY KEY AUTOINCREMENT"
    ts = "TIMESTAMP" if is_pg else "DATETIME"
    conn.execute(
        text(
            f"""
CREATE TABLE skill_api_keys (
  id {pk},
  key_hash TEXT NOT NULL UNIQUE,
  key_prefix TEXT NOT NULL DEFAULT '',
  owner_label TEXT DEFAULT '',
  tier TEXT NOT NULL DEFAULT 'free',
  status TEXT NOT NULL DEFAULT 'active',
  daily_limit INTEGER NOT NULL DEFAULT 100,
  created_at {ts} NOT NULL DEFAULT CURRENT_TIMESTAMP,
  expires_at {ts},
  last_used_at {ts},
  frozen_reason TEXT DEFAULT ''
)
"""
        )
    )
    conn.execute(
        text(
            f"""
CREATE TABLE skill_usage (
  id {pk},
  api_key_id INTEGER NOT NULL,
  skill_name TEXT NOT NULL,
  status_code INTEGER NOT NULL DEFAULT 200,
  duration_ms INTEGER DEFAULT 0,
  input_tokens INTEGER DEFAULT 0,
  output_tokens INTEGER DEFAULT 0,
  created_at {ts} NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""
        )
    )
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_skill_usage_key_created ON skill_usage(api_key_id, created_at)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_skill_usage_created ON skill_usage(created_at)"))


def _m167_user_sessions_table(conn: Connection) -> None:
    """用户会话表(2026-09-15): 设备限制, 同账号同时在线 ≤2。"""
    if _has_table(conn, "user_sessions"):
        return
    is_pg = _dialect_is_pg(conn)
    ts = "TIMESTAMP" if is_pg else "DATETIME"
    conn.execute(
        text(
            f"""
CREATE TABLE user_sessions (
  session_id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  created_at {ts} NOT NULL DEFAULT CURRENT_TIMESTAMP,
  last_seen {ts} NOT NULL DEFAULT CURRENT_TIMESTAMP,
  expires_at {ts} NOT NULL,
  ip TEXT,
  user_agent TEXT
)
"""
        )
    )
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_user_sessions_user_expires ON user_sessions(user_id, expires_at)"))


def _m168_high_value_api_logs(conn: Connection) -> None:
    """高价值接口调用日志(2026-09-15 C.1): L2/暗盘等, 记用户/时间/标的。"""
    if _has_table(conn, "high_value_api_logs"):
        return
    is_pg = _dialect_is_pg(conn)
    pk = "SERIAL PRIMARY KEY" if is_pg else "INTEGER PRIMARY KEY AUTOINCREMENT"
    ts = "TIMESTAMP" if is_pg else "DATETIME"
    conn.execute(
        text(
            f"""
CREATE TABLE high_value_api_logs (
  id {pk},
  user_id TEXT NOT NULL,
  username TEXT DEFAULT '',
  api_name TEXT NOT NULL,
  symbol TEXT DEFAULT '',
  created_at {ts} NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""
        )
    )
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_hv_api_user_created ON high_value_api_logs(user_id, created_at)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_hv_api_name_created ON high_value_api_logs(api_name, created_at)"))


def _m175_decision_log(conn: Connection) -> None:
    """决策日志(B6, 2026-09-18): "看图之后呢" —— 把每个信号连同**当时的证据与价格**留痕, 事后回填 T+1/3/5。

    为什么需要: 共振/GS/竞价池这些信号一直能"看", 但没有"看了之后结果如何"的账。
    没有账 → 无法证明也无法证伪 → 只能靠感觉调参数。这张表把"信号 → 结果"变成可统计的样本。

    诚实口径: 价格/收益/命中**取不到就 NULL**(绝不补 0 —— 0 是真实收益, 不能冒充"没有数据");
    回填只在**未来那根 K 线真的存在**时才写(不许拿之后的数据推算)。
    """
    if _has_table(conn, "decision_log"):
        return
    is_pg = _dialect_is_pg(conn)
    pk = "SERIAL PRIMARY KEY" if is_pg else "INTEGER PRIMARY KEY AUTOINCREMENT"
    ts = "TIMESTAMP" if is_pg else "DATETIME"
    conn.execute(
        text(
            f"""
CREATE TABLE decision_log (
  id {pk},
  signal_kind TEXT NOT NULL,
  symbol TEXT NOT NULL,
  trade_date TEXT NOT NULL,
  price_at_signal REAL,
  context_json TEXT DEFAULT '',
  source TEXT DEFAULT '',
  created_at {ts} NOT NULL DEFAULT CURRENT_TIMESTAMP,
  ret_t1 REAL,
  ret_t3 REAL,
  ret_t5 REAL,
  hit_t1 INTEGER,
  hit_t3 INTEGER,
  hit_t5 INTEGER,
  filled_at {ts}
)
"""
        )
    )
    conn.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_decision_log_key "
            "ON decision_log(signal_kind, symbol, trade_date)"
        )
    )
    conn.execute(
        text("CREATE INDEX IF NOT EXISTS ix_decision_log_kind_date ON decision_log(signal_kind, trade_date)")
    )


def _m174_caliber_snapshots(conn: Connection) -> None:
    """口径快照档案(2026-09-18 B5): 明盘 L2 / 暗盘逐笔 / 东财四档 的**逐日留痕**。

    为什么需要: 三个源对同一个"主力净额"天然给出不同数字(口径不同), 但**差异本身**是资产 ——
    留痕后才能回答"哪家源在什么行情下偏离多少", 也才能让页面上的数字点得开(可溯源)。
    诚实口径: 取不到就 `available=0` + reason, `value` 写 **NULL(不是 0)** —— 0 是一个真实数字, 不能冒充"没有"。
    """
    if _has_table(conn, "caliber_snapshots"):
        return
    is_pg = _dialect_is_pg(conn)
    pk = "SERIAL PRIMARY KEY" if is_pg else "INTEGER PRIMARY KEY AUTOINCREMENT"
    ts = "TIMESTAMP" if is_pg else "DATETIME"
    conn.execute(
        text(
            f"""
CREATE TABLE caliber_snapshots (
  id {pk},
  symbol TEXT NOT NULL,
  trade_date TEXT NOT NULL,
  source TEXT NOT NULL,
  field_key TEXT NOT NULL DEFAULT '',
  caliber TEXT DEFAULT '',
  unit TEXT DEFAULT '',
  value REAL,
  available INTEGER NOT NULL DEFAULT 0,
  reason TEXT DEFAULT '',
  quality TEXT DEFAULT '',
  captured_at {ts} NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""
        )
    )
    # 幂等键: 同标的+同交易日+同源 只留一行(重复采集走 UPDATE)
    conn.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_caliber_snap_key "
            "ON caliber_snapshots(symbol, trade_date, source, field_key)"
        )
    )
    conn.execute(
        text("CREATE INDEX IF NOT EXISTS ix_caliber_snap_date ON caliber_snapshots(trade_date)")
    )


def _m169_unified_identity_columns(conn: Connection) -> None:
    """统一身份(2026-09-16): skill_api_keys / skill_usage 关联用户。

    - skill_api_keys.user_id: 关联 users.id, nullable(旧 key 为 NULL 仍可用)
    - skill_usage.channel: 调用来源, 默认 'api'(api/web/guest)
    - skill_usage.user_id: 冗余列, 方便按用户统计
    - 回填: skill_api_keys.user_id 按 owner_label = users.username 匹配
    - 幂等可重跑
    """
    if _has_table(conn, "skill_api_keys"):
        _add_column_if_missing(
            conn,
            "skill_api_keys",
            "user_id",
            "ALTER TABLE skill_api_keys ADD COLUMN user_id TEXT",
        )
        _create_index_if_missing(
            conn,
            "ix_skill_api_keys_user",
            "CREATE INDEX ix_skill_api_keys_user ON skill_api_keys(user_id)",
        )
        # 回填: owner_label 匹配 users.username
        #
        # 2026-09-18 修(legacy 升级路径): 极老的单用户库 `users` 表可能**还没有 username 列**
        # (多用户改造之前的 schema), 此时上面的 UPDATE 直接 "no such column: u.username" 抛错,
        # 把整条迁移链打断(v169 失败)。按本仓纪律: 数据迁移必须 `_has_column` 守卫, 缺列就跳过
        # —— 回填是"能给就补", 不是升级成败的前提; 且新库/生产库该列恒在, 行为不变。
        if _has_column(conn, "users", "username"):
            conn.execute(
                text(
                    """
UPDATE skill_api_keys
SET user_id = (
    SELECT u.id FROM users u WHERE u.username = skill_api_keys.owner_label
)
WHERE user_id IS NULL
  AND owner_label IS NOT NULL
  AND TRIM(owner_label) != ''
  AND EXISTS (
    SELECT 1 FROM users u2 WHERE u2.username = skill_api_keys.owner_label
)
"""
                )
            )

    if _has_table(conn, "skill_usage"):
        _add_column_if_missing(
            conn,
            "skill_usage",
            "channel",
            "ALTER TABLE skill_usage ADD COLUMN channel TEXT DEFAULT 'api'",
        )
        _add_column_if_missing(
            conn,
            "skill_usage",
            "user_id",
            "ALTER TABLE skill_usage ADD COLUMN user_id TEXT",
        )
        # 存量行 channel 兜底(部分方言 ADD COLUMN DEFAULT 不回填已有行)
        conn.execute(
            text("UPDATE skill_usage SET channel = 'api' WHERE channel IS NULL OR TRIM(channel) = ''")
        )
        _create_index_if_missing(
            conn,
            "ix_skill_usage_user",
            "CREATE INDEX ix_skill_usage_user ON skill_usage(user_id, created_at)",
        )


def _m170_pro_applications(conn: Connection) -> None:
    """Pro 申请 + 到期降级日志表(2026-09-16 任务3.1/3.3)。

    - pro_applications: 用户 Pro 申请(owner 人工审核)
    - tier_downgrade_logs: trial/pro key 过期自动降 free 的结构化留痕
    """
    is_pg = _dialect_is_pg(conn)
    pk = "SERIAL PRIMARY KEY" if is_pg else "INTEGER PRIMARY KEY AUTOINCREMENT"
    ts = "TIMESTAMP" if is_pg else "DATETIME"
    bool_t = "BOOLEAN" if is_pg else "INTEGER"

    if not _has_table(conn, "pro_applications"):
        conn.execute(
            text(
                f"""
CREATE TABLE pro_applications (
  id {pk},
  user_id TEXT NOT NULL,
  username TEXT DEFAULT '',
  reason TEXT DEFAULT '',
  status TEXT NOT NULL DEFAULT 'pending',
  created_at {ts} NOT NULL DEFAULT CURRENT_TIMESTAMP,
  reviewed_at {ts},
  reviewed_by TEXT DEFAULT '',
  review_note TEXT DEFAULT ''
)
"""
            )
        )
        conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_pro_applications_user ON pro_applications(user_id)")
        )
        conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_pro_applications_status_created ON pro_applications(status, created_at)")
        )

    if not _has_table(conn, "tier_downgrade_logs"):
        conn.execute(
            text(
                f"""
CREATE TABLE tier_downgrade_logs (
  id {pk},
  api_key_id INTEGER NOT NULL,
  key_prefix TEXT DEFAULT '',
  user_id TEXT,
  from_tier TEXT NOT NULL,
  to_tier TEXT NOT NULL DEFAULT 'free',
  from_daily_limit INTEGER DEFAULT 0,
  to_daily_limit INTEGER DEFAULT 0,
  reason TEXT DEFAULT '',
  created_at {ts} NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""
            )
        )
        conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_tier_downgrade_key ON tier_downgrade_logs(api_key_id)")
        )
        conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_tier_downgrade_user ON tier_downgrade_logs(user_id)")
        )
        conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_tier_downgrade_created ON tier_downgrade_logs(created_at)")
        )
    _ = bool_t  # 本迁移暂未用到布尔列, 保留方言工具一致性


def _m171_tier_configs(conn: Connection) -> None:
    """档位配置表 + 默认数据(2026-09-16 任务3.2)。

    free: 100/天 burst 30; trial: 500/天 burst 50; pro: 5000/天 burst 100。
    skills_gateway 读此表(短 TTL 缓存)实现热更新; 改表后新请求生效。

    注意: init_db 的 create_all 会先按 ORM 建出空表, 本迁移必须在表已存在时
    仍补齐默认行(否则全新库 tier_configs 为空, 限流回落硬编码且无处可改)。
    """
    is_pg = _dialect_is_pg(conn)
    pk = "SERIAL PRIMARY KEY" if is_pg else "INTEGER PRIMARY KEY AUTOINCREMENT"
    ts = "TIMESTAMP" if is_pg else "DATETIME"
    bool_false = "FALSE" if is_pg else "0"
    bool_true = "TRUE" if is_pg else "1"
    if not _has_table(conn, "tier_configs"):
        conn.execute(
            text(
                f"""
CREATE TABLE tier_configs (
  id {pk},
  tier_name TEXT NOT NULL UNIQUE,
  daily_limit INTEGER NOT NULL DEFAULT 100,
  burst_limit INTEGER NOT NULL DEFAULT 30,
  skill_scope TEXT,
  is_default {('BOOLEAN' if is_pg else 'INTEGER')} NOT NULL DEFAULT {bool_false},
  created_at {ts} NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at {ts} NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""
            )
        )
    # 默认数据: skill_scope 里放 refill_per_min, 与 skills_gateway 硬编码默认对齐。
    # 只补缺失档位, 不覆盖已存在的(运维可能已改过)。
    defaults = (
        ("free", 100, 30, '{"refill_per_min": 15}', True),
        ("trial", 500, 50, '{"refill_per_min": 20}', False),
        ("pro", 5000, 100, '{"refill_per_min": 60}', False),
    )
    for tier, daily, burst, scope, is_def in defaults:
        try:
            row = conn.execute(
                text("SELECT id FROM tier_configs WHERE tier_name = :t LIMIT 1"),
                {"t": tier},
            ).first()
        except Exception:
            conn.rollback()
            row = None
        if row:
            continue
        conn.execute(
            text(
                """
INSERT INTO tier_configs (tier_name, daily_limit, burst_limit, skill_scope, is_default)
VALUES (:t, :d, :b, :s, :def)
"""
            ),
            {"t": tier, "d": daily, "b": burst, "s": scope, "def": is_def},
        )


def _m172_users_email(conn: Connection) -> None:
    """users 表加 email 列 + 唯一索引(2026-09-16 邮箱注册)。

    - nullable: 兼容迁移前存量账号(无邮箱)
    - 唯一索引: PG/SQLite 均允许多个 NULL, 不会误伤旧账号
    - 幂等可重跑
    """
    if not _has_table(conn, "users"):
        return
    _add_column_if_missing(
        conn,
        "users",
        "email",
        "ALTER TABLE users ADD COLUMN email VARCHAR(128)",
    )
    _create_index_if_missing(
        conn,
        "ix_users_email",
        "CREATE UNIQUE INDEX ix_users_email ON users(email)",
    )


def _m173_users_soft_delete(conn: Connection) -> None:
    """users 表加 is_deleted / deleted_at(P3 GDPR 用户数据删除, 2026-09-18)。

    - is_deleted: 软删除标记, 请求删除后置 True, 登录被拒
    - deleted_at: 请求删除的时间戳; 30 天后物理清除任务据此筛选
    - 幂等可重跑
    """
    if not _has_table(conn, "users"):
        return
    _add_column_if_missing(
        conn,
        "users",
        "is_deleted",
        "ALTER TABLE users ADD COLUMN is_deleted BOOLEAN NOT NULL DEFAULT FALSE",
    )
    _add_column_if_missing(
        conn,
        "users",
        "deleted_at",
        "ALTER TABLE users ADD COLUMN deleted_at TIMESTAMP",
    )


def _m176_factor_ic_snapshots(conn: Connection) -> None:
    """因子 IC 每日快照(B9 数据资产 E1 收口, 2026-09-19)。

    为什么需要: IC/IR 一直是**即时算**的 —— 每次都重算同一段窗口, 因此回答不了
    "某个因子的 IC 随时间怎么变、什么时候失效"。把每日 IC 落成时序, 才能看趋势与拐点,
    也是后续因子加权/归因的原料。

    诚实口径:
      - **样本不足也落行**, 只是 `ic` 等指标为 NULL + 记下 `ic_periods` —— 留下"这天样本不够"的痕迹,
        比留一个空洞更有信息量(空洞分不清"没跑"和"没法算");
      - 幂等: 唯一键 (factor_code, market, trade_date, horizon), 当天重跑只更新不重复插。
    """
    if _has_table(conn, "factor_ic_snapshots"):
        return
    is_pg = _dialect_is_pg(conn)
    pk = "SERIAL PRIMARY KEY" if is_pg else "INTEGER PRIMARY KEY AUTOINCREMENT"
    ts = "TIMESTAMP" if is_pg else "DATETIME"
    conn.execute(
        text(
            f"""
CREATE TABLE factor_ic_snapshots (
  id {pk},
  factor_code TEXT NOT NULL,
  market TEXT NOT NULL DEFAULT 'CN',
  trade_date TEXT NOT NULL,
  horizon INTEGER NOT NULL DEFAULT 5,
  ic REAL,
  ic_t REAL,
  ic_std REAL,
  ir REAL,
  ic_holdout REAL,
  ic_pooled REAL,
  -- 计数列**可空**: NULL = "没算/不知道"(与 0 = "确实没有样本" 区分开)
  sample_size INTEGER,
  ic_periods INTEGER,
  holdout_periods INTEGER,
  created_at {ts} NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""
        )
    )
    conn.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_factor_ic_snapshot "
            "ON factor_ic_snapshots (factor_code, market, trade_date, horizon)"
        )
    )
    conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_factor_ic_snapshot_date "
            "ON factor_ic_snapshots (market, horizon, trade_date)"
        )
    )



def _m177_invite_codes(conn: Connection) -> None:
    """邀请码注册(2026-09-19 用户拍板: 内部使用 + 邀请制)。

    为什么需要: 产品定位内部使用, 而"公开自助注册"是形式上**唯一还面向公众**的口子
    (合规红线是"向公众提供分析预测建议", 不以收费为要件)。邀请码把准入收回到管理员手里。

    两张表分工明确:
      - `invite_codes`   —— 凭证本身(可用次数/过期/停用), 核销就是 used_count+1;
      - `invite_code_uses` —— **审计流水**(谁/何时/什么 IP 用了哪个码), 凭证类操作必须可回溯。
    核销走单条条件 UPDATE(`used_count < max_uses` 写进 WHERE), 因此**并发不超发**。
    """
    if _has_table(conn, "invite_codes") and _has_table(conn, "invite_code_uses"):
        return
    is_pg = _dialect_is_pg(conn)
    ts = "TIMESTAMP" if is_pg else "DATETIME"
    f = "false" if is_pg else "0"
    pk = "SERIAL PRIMARY KEY" if is_pg else "INTEGER PRIMARY KEY AUTOINCREMENT"

    if not _has_table(conn, "invite_codes"):
        conn.execute(
            text(
                f"""
CREATE TABLE invite_codes (
  code TEXT PRIMARY KEY,
  note TEXT NOT NULL DEFAULT '',
  max_uses INTEGER NOT NULL DEFAULT 1,
  used_count INTEGER NOT NULL DEFAULT 0,
  expires_at {ts} NULL,
  disabled BOOLEAN NOT NULL DEFAULT {f},
  created_by TEXT NOT NULL DEFAULT '',
  created_at {ts} NOT NULL
)
"""
            )
        )
    if not _has_table(conn, "invite_code_uses"):
        conn.execute(
            text(
                f"""
CREATE TABLE invite_code_uses (
  id {pk},
  code TEXT NOT NULL,
  user_id INTEGER NULL,
  username TEXT NOT NULL DEFAULT '',
  client_ip TEXT NOT NULL DEFAULT '',
  used_at {ts} NOT NULL
)
"""
            )
        )
        conn.execute(text("CREATE INDEX ix_invite_code_uses_code ON invite_code_uses (code)"))
    logger.info("[migration 177] invite_codes + invite_code_uses 就绪(邀请码注册)")

def _m178_market_sentiment_daily(conn: Connection) -> None:
    """TQ 专业序列: 市场级情绪日序列(涨停/炸板/连板/打板资金/两融/龙虎榜...)。

    数据源 = 通达信客户端 TQ 网关 get_scjy_value(SCJYVALUE 编号空间),
    实测覆盖 2025-01-02 起每日 420+ 个交易日, 34/35 张表有值。
    东财类免费源只给**当日快照**, 这张表的意义是提供历史基线
    (如"今日涨停 34 家 vs 近 20 日均值")。

    宽表而非 JSON: 情绪周期要跑 AVG/PERCENTILE 这类聚合。
    单位: 家数=家, 金额见列注释。
    未标定的扩展序列(SC28-35)与原始响应进 extra, 便于后续补标。
    """
    id_col = "SERIAL PRIMARY KEY" if _dialect_is_pg(conn) else "INTEGER PRIMARY KEY AUTOINCREMENT"
    conn.execute(
        text(
            f"""
CREATE TABLE IF NOT EXISTS market_sentiment_daily (
  id {id_col},
  trade_date TEXT NOT NULL,
  market TEXT NOT NULL DEFAULT 'CN',
  limit_up_count INTEGER,
  limit_up_open_count INTEGER,
  limit_down_count INTEGER,
  limit_down_open_count INTEGER,
  streak_count INTEGER,
  streak_count_ex INTEGER,
  hard_limit_up_count INTEGER,
  hard_limit_down_count INTEGER,
  seal_success_money REAL,
  seal_fail_money REAL,
  margin_fin_balance REAL,
  margin_sec_balance REAL,
  margin_buy_amount REAL,
  margin_sell_volume REAL,
  lhb_buy REAL,
  lhb_sell REAL,
  lhb_inst_buy REAL,
  lhb_inst_sell REAL,
  lhb_yyb_buy REAL,
  lhb_yyb_sell REAL,
  lhb_hsgt_buy REAL,
  lhb_hsgt_sell REAL,
  holder_increase REAL,
  holder_decrease REAL,
  block_premium REAL,
  block_discount REAL,
  unlock_plan REAL,
  unlock_actual REAL,
  dividend_total REAL,
  fundraising_total REAL,
  ih_net_position REAL,
  ic_net_position REAL,
  im_net_position REAL,
  etf_scale REAL,
  etf_net_sub REAL,
  new_investor_natural REAL,
  new_investor_inst REAL,
  pboc_net_injection REAL,
  pledge_ratio_total REAL,
  extra TEXT NOT NULL DEFAULT '{{}}',
  source TEXT NOT NULL DEFAULT 'tdx_tq',
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""
        )
    )
    conn.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_market_sentiment_daily "
            "ON market_sentiment_daily(trade_date, market)"
        )
    )
    conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_market_sentiment_daily_date "
            "ON market_sentiment_daily(trade_date)"
        )
    )


def _m179_tq_formula_signal_daily(conn: Connection) -> None:
    """TQ 条件选股信号日序列(2026-09-25)。

    为什么: TQ 客户端自带 **108 个条件选股公式**, 全市场扫一遍约 8s 且**无限频**
    (对比: 问财限频 250ms; 免费行情源没有等价口径)。它给出"当日有多少只票触发某
    技术条件"这一**市场宽度**维度 —— 而免费源只给当日快照, 没有历史序列就算不出基线。

    为什么用窄表: 家数要跑 AVG/PERCENTILE 聚合(与 market_sentiment_daily 同理由)。
    命中清单另存 `hits_json`(回看"当日是谁触发"), 上限 500 只, 超出置 `truncated`。

    ⚠️ `complete=0` 表示扫描过程有分片失败 ⇒ 消费方**不得**把 `hit_count` 当
    "全市场命中数"(那是编造); 必须显示为不完整。
    单位: hit_count/scanned_count 均为**只**(当日全 A 口径)。
    """
    if _has_table(conn, "tq_formula_signal_daily"):
        return
    id_col = "SERIAL PRIMARY KEY" if _dialect_is_pg(conn) else "INTEGER PRIMARY KEY AUTOINCREMENT"
    conn.execute(
        text(
            f"""
CREATE TABLE IF NOT EXISTS tq_formula_signal_daily (
  id {id_col},
  trade_date TEXT NOT NULL,
  formula_code TEXT NOT NULL,
  formula_name TEXT NOT NULL DEFAULT '',
  formula_arg TEXT NOT NULL DEFAULT '',
  hit_count INTEGER NOT NULL DEFAULT 0,
  scanned_count INTEGER NOT NULL DEFAULT 0,
  chunks_failed INTEGER NOT NULL DEFAULT 0,
  complete INTEGER NOT NULL DEFAULT 1,
  truncated INTEGER NOT NULL DEFAULT 0,
  hits_json TEXT NOT NULL DEFAULT '[]',
  source TEXT NOT NULL DEFAULT 'tdx_tq',
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""
        )
    )
    conn.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_tq_formula_signal_daily "
            "ON tq_formula_signal_daily(trade_date, formula_code, formula_arg)"
        )
    )
    conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_tq_formula_signal_daily_date "
            "ON tq_formula_signal_daily(trade_date)"
        )
    )


def _m180_market_breadth_daily(conn: Connection) -> None:
    """市场广度日序列 —— 涨跌家数 + 自研广度指标（ADL/ADR/ARMS/BTI/MCL/STIX）。

    为什么自研: 通达信客户端公式库有同名指标，但**必须先在客户端注册**才能被 TQ 网关
    调用（实测 21 个高价值公式全部 `ErrorId=9: no find formula setting`），且
    `formula_set_data*` 只能设数据、无法注册公式；故按教科书定义自研，口径透明可审计，
    对外展示标注"自研口径"（见 src/core/market_breadth.py）。

    为什么从 PG 自算: `pricevol`（`tq.breadth` 数据源）只有**当日快照**，攒不出历史基线；
    而本库 klines 有全市场深历史（2026-09-21 及以前每日约 5480 只标的），
    一次 SQL 即可回溯 250+ 交易日，满足分位计算（样本 <20 不给分位）。

    单位: 家数=只；成交量=股（与 klines.volume 同源，不做换算）。
    `symbols` 为当日参与统计的标的数，供消费方判断该日数据是否收全
    （<1000 的交易日采集侧不落库，避免污染分位基线）。
    """
    if _has_table(conn, TABLE_NAME := "market_breadth_daily"):
        return
    id_col = "SERIAL PRIMARY KEY" if _dialect_is_pg(conn) else "INTEGER PRIMARY KEY AUTOINCREMENT"
    conn.execute(
        text(
            f"""
CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
  id {id_col},
  trade_date TEXT NOT NULL,
  up_count INTEGER,
  down_count INTEGER,
  flat_count INTEGER,
  up_volume REAL,
  down_volume REAL,
  adl REAL,
  adr REAL,
  arms REAL,
  bti REAL,
  bti_thrust INTEGER NOT NULL DEFAULT 0,
  mcl REAL,
  mcl_summation REAL,
  stix REAL,
  up_ratio REAL,
  sentiment_score INTEGER,
  symbols INTEGER,
  source TEXT NOT NULL DEFAULT 'pg_klines',
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""
        )
    )
    conn.execute(
        text(f"CREATE UNIQUE INDEX IF NOT EXISTS uq_market_breadth_daily_date ON {TABLE_NAME} (trade_date)")
    )


def _m181_img_orderbook_frames(conn: Connection) -> None:
    """`.img` 十档盘口帧落库表 img_orderbook_frames(2026-10-02)。

    把通达信 `.img` 逐帧十档盘口 + 委托队列落库(遗留 A2「.img 生产无消费方」收口),
    供盘后回放 / 盘口队列展示。**schema 唯一入口就是本迁移**(AGENTS 铁律),
    禁止运行时建表/加列。

    - 唯一键 (user_id, market, symbol, trade_date, seq): 同日同帧序号重复导入幂等;
      `seq` 为源文件内帧序(0 起) —— 因 .img 存在**同秒多帧**(实测 5066 帧里 22 处
      间隔 0s), 仅用 as_of 会丢帧; trade_date 进唯一键规避 l2_ticks 的裁剪列教训。
    - user_id: 多用户隔离(4 账号并存), 读路径一律按 user_id 过滤。
    - source / as_of: 来源与数据时间显式标注, 不拿陈旧值冒充实时(AGENTS 硬约束)。
    - 缺失一律写 NULL(**不补 0**): 0 是有意义的挂单量, 与「无数据」必须可区分。
    - 单位: 价格=元, 量=股; 档位/队列以 JSON(TEXT) 保序存储。
    """
    if _has_table(conn, "img_orderbook_frames"):
        return
    if _dialect_is_pg(conn):
        conn.execute(
            text(
                """
                CREATE TABLE img_orderbook_frames (
                    trade_date TEXT NOT NULL,
                    as_of TEXT NOT NULL,
                    seq INTEGER NOT NULL DEFAULT 0,
                    symbol TEXT NOT NULL,
                    market TEXT NOT NULL DEFAULT 'CN',
                    user_id TEXT NOT NULL DEFAULT 'shared',
                    source TEXT NOT NULL DEFAULT 'img',
                    img_path TEXT,
                    bid_prices TEXT,
                    bid_vols TEXT,
                    ask_prices TEXT,
                    ask_vols TEXT,
                    bid_queue TEXT,
                    ask_queue TEXT,
                    bid_orders INTEGER,
                    ask_orders INTEGER,
                    spread DOUBLE PRECISION,
                    bid_pressure DOUBLE PRECISION,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT uq_img_orderbook_frames UNIQUE
                        (user_id, market, symbol, trade_date, seq)
                )
                """
            )
        )
    else:
        conn.execute(
            text(
                """
                CREATE TABLE img_orderbook_frames (
                    trade_date TEXT NOT NULL,
                    as_of TEXT NOT NULL,
                    seq INTEGER NOT NULL DEFAULT 0,
                    symbol TEXT NOT NULL,
                    market TEXT NOT NULL DEFAULT 'CN',
                    user_id TEXT NOT NULL DEFAULT 'shared',
                    source TEXT NOT NULL DEFAULT 'img',
                    img_path TEXT,
                    bid_prices TEXT,
                    bid_vols TEXT,
                    ask_prices TEXT,
                    ask_vols TEXT,
                    bid_queue TEXT,
                    ask_queue TEXT,
                    bid_orders INTEGER,
                    ask_orders INTEGER,
                    spread REAL,
                    bid_pressure REAL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE (user_id, market, symbol, trade_date, seq)
                )
                """
            )
        )
    _create_index_if_missing(
        conn,
        "ix_img_orderbook_frames_sym_asof",
        "CREATE INDEX ix_img_orderbook_frames_sym_asof "
        "ON img_orderbook_frames (symbol, market, as_of DESC)",
    )
    _create_index_if_missing(
        conn,
        "ix_img_orderbook_frames_user_asof",
        "CREATE INDEX ix_img_orderbook_frames_user_asof "
        "ON img_orderbook_frames (user_id, symbol, as_of DESC)",
    )


def _m182_theme_mood_settled_at(conn: Connection) -> None:
    """题材情绪日表补 `settled_at`(2026-10-08): 收盘定型时间。

    schema 唯一入口是本迁移(AGENTS 铁律), 禁止运行时加列。

    - 盘中行(source='intraday')恒为 NULL —— 未定型;
    - 交易日 15:05 定型扫描(source='close')写入定型的 CST 时间戳;
    - 幂等复跑靠 upsert 的 `COALESCE(theme_mood_daily.settled_at, excluded.settled_at)`
      保留**首次**定型时间(不跳变), 故本列无默认值即可。
    """
    _add_column_if_missing(
        conn, "theme_mood_daily", "settled_at",
        "ALTER TABLE theme_mood_daily ADD COLUMN settled_at TIMESTAMP",
    )


def _m183_decision_cache(conn: Connection) -> None:
    """decision_cache 表(2026-10-10 冗余设计): /api/decision/{symbol} 预落库 + TTL 缓存。

    为什么需要: `/decision/{symbol}` 是**实时计算**端点 —— 每次现拉 120 天 K 线 + 明/暗盘
    资金算三信号(慢接口, 前端 SDK 显式放宽到 30s 超时)。首屏/工作台反复请求会重复算。
    落库后: 盘后批算命中直返(免重算), 非预热标的走短 TTL(盘中同标的不重复算)。

    **只存全局基底**(不含个性化): 自选/持仓/风险偏好由 API 层在**读取时**叠加,
    避免按用户维度爆炸(4 账号并存)。user_id 隔离红线不受影响 —— 基底与用户无关。

    诚实语义: payload 里含 `last_close`(供读取时算浮盈); 响应另带 `computed_at`/`cached`,
    由 API 层注入(**不写进 payload**, 因为 payload 是"计算时刻的数据快照")。

    字段:
      symbol TEXT NOT NULL / market TEXT NOT NULL  (联合主键, 双方言)
      computed_at TIMESTAMP NOT NULL   (naive UTC, 与 summary_cache 同口径)
      ttl_s INTEGER NOT NULL           (该行的有效秒数; 过期即视为 miss → 重算)
      last_close REAL                  (末根收盘, 供个性化浮盈; 缺则 NULL, 不编造)
      source TEXT                      (precompute / ttl, 供可观测)
      payload TEXT NOT NULL            (JSON 全局基底, 上限防爆)
    """
    conn.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS decision_cache (
                symbol TEXT NOT NULL,
                market TEXT NOT NULL,
                computed_at TIMESTAMP NOT NULL,
                ttl_s INTEGER NOT NULL,
                last_close DOUBLE PRECISION,
                source TEXT DEFAULT '',
                payload TEXT NOT NULL,
                PRIMARY KEY (symbol, market)
            )
            """
        )
    )


def _m184_decision_log_regime(conn: Connection) -> None:
    """决策账本补 `regime` 列(2026-10-10 A 决策提胜率): 情绪周期条件化的分桶维度。

    为什么需要: 三指标共振命中率此前是**全状态混算**的一个平均数(T+1 53.85%)。
    把信号产生时的情绪周期态(冰点/修复/发酵/高潮/退潮, 见 `src.core.market_regime`)
    落库后, `decision_log.stats` 可按 regime 分桶统计, 才能回答"哪套门槛在哪个周期
    真管用" —— 只加聚合维度, **不建回测 UI**。

    列可空(NULL = 该行产生时无 regime 数据 / 老行), 分桶时归入 'unknown'(显式缺
    数据, 不硬编状态)。schema 唯一入口是本迁移(AGENTS 铁律), 禁止运行时加列。
    """
    _add_column_if_missing(
        conn, "decision_log", "regime",
        "ALTER TABLE decision_log ADD COLUMN regime TEXT",
    )
    if _has_table(conn, "decision_log"):
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_decision_log_kind_regime "
                "ON decision_log(signal_kind, regime)"
            )
        )


def _m197_decision_log_outcome_state(conn: Connection) -> None:
    """决策账本补 `outcome_state`/`outcome_note` 列(2026-10-14 回填覆盖修复): 缺失显式标注。

    为什么需要(**生产实证**): 2026-09-25 为中秋休市, 当日却落了 597 条 `resonance3` 信号;
    这类"信号日无 K 线锚点"的行 T+n **永远算不出来**, 此前只能把收益列留 NULL —— 于是与
    "还没到 T+n"不可区分, 且每次回填都被重新选中并**占满 `LIMIT :lim` 候选窗口**
    (生产 2026-10-10 10:17 一跑: `scanned=500, filled=0`), 后面可填的行永远轮不到;
    分桶统计也因此"无法证实也无法证伪"(T+1 覆盖率长期停在 ~46%)。

    列语义(可空, 不设默认值 → 存量行为 NULL):
      - NULL      = 尚未定论(未到期 / 部分档已填 / 评估时取不到后续 K 线), **仍会进候选窗口**;
      - 'no_anchor' = 信号日那根 K 线不存在而之后有 K 线(休市/停牌) → 锚点永久缺失,
        **显式标注、收益列保持 NULL(绝不填 0)**; 以低优先级保留在候选里, K 线后续补齐即自动
        重新评估并清空标注。
    `outcome_note` 存人类可读原因(供明细/报告直读, 不再靠猜)。

    schema 唯一入口是本迁移(AGENTS 铁律), 禁止运行时建表/加列; 两列都用
    `_add_column_if_missing` 幂等, 重复执行不报错。
    """
    _add_column_if_missing(
        conn, "decision_log", "outcome_state",
        "ALTER TABLE decision_log ADD COLUMN outcome_state TEXT",
    )
    _add_column_if_missing(
        conn, "decision_log", "outcome_note",
        "ALTER TABLE decision_log ADD COLUMN outcome_note TEXT",
    )


def _m198_decision_log_t_n_span(conn: Connection) -> None:
    """决策账本补 `t_n_span` 列(2026-10-15 停牌跨期口径标注): T+n 取值日的交易日跨度。

    为什么需要(**生产实证**): 决策账本的 T+n 取的是「信号日起**第 n 根可用 K 线**」——
    遇长期停牌会把这**一个月后**的收盘当 T+1。实证 symbol=300082 信号日 20260918,
    下一根可用 K 线是 2026-10-13(相隔 11 个交易日), `ret_t1=-0.0886` 实为一月后跌幅,
    混进胜率会误导。用户裁决: **不重算存量数值, 只标注 + 排除统计**。

    列语义(可空, 不设默认值 → 存量行为 NULL):
      - `t_n_span` = 信号日 → T+1 取值日(信号日之后**第一根可用 K 线**)相隔的交易日数
        (走 `src.core.trading_calendar` 交易日历, **含停牌**, `1 = 次一个交易日`);
      - 正常票 T+1 取值日即次一交易日 → span=1; 长期停牌 → span 远大于 1;
      - `t_n_span > 5` → 该行 `outcome_state='suspended_gap'`(覆盖原状态), note 注明跨度,
        **收益列保持原值(绝不改写已填数值)**; 胜率/分桶统计据此排除该行并显式标注条数。

    schema 唯一入口是本迁移(AGENTS 铁律), 禁止运行时加列; 幂等(`_add_column_if_missing`)。
    """
    _add_column_if_missing(
        conn, "decision_log", "t_n_span",
        "ALTER TABLE decision_log ADD COLUMN t_n_span INTEGER",
    )


def _m185_dde_minute_flow_table(conn: Connection) -> None:
    """盘中 DDE 大单逐 N 分钟采样序列表 dde_minute_flow(P3 补差 A, 2026-10-10)。

    见 models.DdeMinuteFlow。schema 唯一入口是本迁移(AGENTS 铁律), 禁止运行时建表。
    """
    from src.db.models import DdeMinuteFlow

    DdeMinuteFlow.__table__.create(bind=conn, checkfirst=True)


def _m186_war_report_daily_table(conn: Connection) -> None:
    """主力资金战报日快照表 war_report_daily(规格 §4.4, 2026-10-10)。

    见 models.WarReportDaily。schema 唯一入口是本迁移, 禁止运行时建表。
    """
    from src.db.models import WarReportDaily

    WarReportDaily.__table__.create(bind=conn, checkfirst=True)


def _m187_l2_fund_snapshots(conn: Connection) -> None:
    """L2 成品资金收盘快照表 l2_fund_snapshots(2026-10-11 P1 数据断档修复)。

    背景: 工作台 L2「L2 成品资金」/ 暗盘卡「主力净流入(L2·TQ)」读 TQ
    `get_more_info` **实时会话值**(Zjl_HB/TotalBVol/L2TicNum/L2OrderNum ...), 非交易
    时段 TQ 一律回 0 —— UI 未回退未标注, 周末显示"0万 平衡 / 逐笔0笔·委托0笔",
    把 0 冒充真实值误导。

    修复: 交易日收盘后(15:05)采样落本表(全 0 不落); 非交易时段/会话值全 0 时 web 层
    回退读本表最近交易日行, 并显式标注 as_of; 无快照则显式 available:false。

    金额类(zjl_hb/zjl)单位=万元, 量类=股, 与 TQ get_more_info 口径一致。
    schema 唯一入口是本迁移(AGENTS 铁律), 禁止运行时建表。
    """
    conn.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS l2_fund_snapshots (
                symbol TEXT NOT NULL,
                market TEXT NOT NULL,
                trade_date TEXT NOT NULL,
                zjl_hb DOUBLE PRECISION,
                zjl DOUBLE PRECISION,
                total_buy_vol DOUBLE PRECISION,
                total_sell_vol DOUBLE PRECISION,
                cancel_buy DOUBLE PRECISION,
                cancel_sell DOUBLE PRECISION,
                l2_tick_num DOUBLE PRECISION,
                l2_order_num DOUBLE PRECISION,
                captured_at TIMESTAMP,
                PRIMARY KEY (symbol, market, trade_date)
            )
            """
        )
    )
    if _has_table(conn, "l2_fund_snapshots"):
        _create_index_if_missing(
            conn,
            "ix_l2_fund_snapshots_date",
            "CREATE INDEX ix_l2_fund_snapshots_date "
            "ON l2_fund_snapshots (trade_date DESC)",
        )


def _m188_limit_strength_shadow(conn: Connection) -> None:
    """首封时间×封成比 强度标签影子表 limit_strength_shadow(2026-10-11 胜率创新 P0-1)。

    **影子运行**: 每日盘后对当日涨停股打强度分层标签(S/A/B/C), 只落本表, **不改任何
    现有信号/决策/展示语义** —— 供只读展示(涨停池/聚宝盆徽章)与胜率验证分桶统计用。

    标签口径见 src/core/limit_up_strength.py; 缺数据行 status='no_data' 且 label 为空,
    绝不编造。schema 唯一入口是本迁移(AGENTS 铁律), 禁止运行时建表。
    """
    conn.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS limit_strength_shadow (
                trade_date TEXT NOT NULL,
                symbol TEXT NOT NULL,
                name TEXT,
                first_time TEXT,
                first_seal_bucket TEXT,
                seal_amount DOUBLE PRECISION,
                circulating_mktcap DOUBLE PRECISION,
                seal_ratio DOUBLE PRECISION,
                seal_ratio_grade TEXT,
                open_times INTEGER,
                strength_label TEXT,
                status TEXT NOT NULL DEFAULT 'no_data',
                source TEXT,
                captured_at TIMESTAMP,
                PRIMARY KEY (trade_date, symbol)
            )
            """
        )
    )
    if _has_table(conn, "limit_strength_shadow"):
        _create_index_if_missing(
            conn,
            "ix_limit_strength_shadow_date",
            "CREATE INDEX ix_limit_strength_shadow_date "
            "ON limit_strength_shadow (trade_date DESC)",
        )


def _m199_klines_period_ts_symbol_index(conn: Connection) -> None:
    """klines 查询覆盖索引 (2026-10-16 性能优化):
    针对 /api/theme-mood/ladder 等全市场批量查询 K 线的慢操作进行加固。
    在 (period, adjust, ts, symbol) 上建立联合索引，消除全表扫描与 6s 阻塞。
    """
    try:
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_klines_period_adjust_ts_symbol "
                "ON klines(period, adjust, ts, symbol)"
            )
        )
        logger.info("klines: 联合覆盖索引 ix_klines_period_adjust_ts_symbol 创建成功")
    except Exception as e:
        logger.warning(f"klines 索引创建失败(非致命): {e}")


def _m200_price_distribution_tables(conn: Connection) -> None:
    """I-8 分价博弈矩阵两张表(2026-10-18)。

    · `price_distribution_snapshots`: 盘中滚动快照(缓存 + 分价时序证据), 唯一键
      (trade_date, market, symbol, bucket_ts=5 分钟桶), 桶内原地覆盖(留最新);
    · `price_distribution_daily`: 日终定稿归档, 唯一键 (trade_date, market, symbol),
      历史回测/主力轨迹复盘的**永久事实源**(历史日永不回源)。

    建表走 ORM `__table__.create(checkfirst=True)`: JSON 列在 PG/SQLite 都由 SQLAlchemy
    适配, 手写 SQL 反而要分叉方言。先例: _m142 dark_fund_top_snapshots。
    """
    from src.db.models import PriceDistributionDaily, PriceDistributionSnapshot

    PriceDistributionSnapshot.__table__.create(bind=conn, checkfirst=True)
    PriceDistributionDaily.__table__.create(bind=conn, checkfirst=True)


def _m201_auction_tick_snapshots(conn: Connection) -> None:
    """竞价过程帧表 auction_tick_snapshots(2026-10-18 P0-a, 方案 §7.2②)。

    append-only: 原 `auction_snapshots` 唯一键 (trade_date, symbol, market) 把
    9:15-9:25 的过程压成一个 9:25 终值, 试盘/撤单/虚假封单全部丢失。本表按
    (标的, 帧时刻, 来源) 幂等写入, 支持逐分钟过程回放。

    保留期: 365 天(方案 §9), 由 `src.core.auction_tick_sampler.purge_old_frames`
    按 `trade_date` 文本比较清理(应用层保留期契约)。

    为什么没有 Timescale hypertable: `create_hypertable` 的分区列必须是时间类型,
    而本表 `trade_date` 是 ISO 文本(与 price_distribution_* 同口径, 双方言免转换)。
    强行转换会在 PG 上每次启动都告警失败 —— 宁可不做, 待列类型升级后再上
    (方案 §7.5 的存储治理不在本批验收里)。索引已按 (symbol, trade_date) /
    (trade_date) 覆盖回放与保留期清理两条主查询。
    """
    from src.db.models import AuctionTickSnapshot

    AuctionTickSnapshot.__table__.create(bind=conn, checkfirst=True)


def _m202_auction_wudao_daily(conn: Connection) -> None:
    """悟道竞价多维因子日归档 auction_wudao_daily(2026-10-18 P0-a, 方案 §7.2③)。

    唯一键 (trade_date, symbol, market); 悟道不可用的日子不写行(显式空, 不写 0)。
    schema 唯一入口是本迁移(AGENTS 铁律), 禁止运行时建表。
    """
    from src.db.models import AuctionWudaoDaily

    AuctionWudaoDaily.__table__.create(bind=conn, checkfirst=True)


def _m203_cb_tables(conn: Connection) -> None:
    """可转债两张表 cb_terms + cb_daily_snapshot(2026-10-18 P0-a, 方案 §7.2④)。

    · `cb_terms`: 条款版本化(版本=抓取日 as_of), 14 个业务字段 + raw 原样;
    · `cb_daily_snapshot`: 每日折溢价/双低快照, payload 存 radar 行完整返回。

    背景: 条款此前是 12h 进程内 dict(重启即失, 冷启动 N 次 kzz_info RPC), 快照
    只进 Redis 60s —— 既不能回测, 也无法秒开。schema 唯一入口是本迁移。
    """
    from src.db.models import CbDailySnapshot, CbTerms

    CbTerms.__table__.create(bind=conn, checkfirst=True)
    CbDailySnapshot.__table__.create(bind=conn, checkfirst=True)


def _m204_more_info_daily(conn: Connection) -> None:
    """TQ `get_more_info` 104 字段收盘定稿表 more_info_daily(2026-10-18 P0-b)。

    此前只有 8 个 L2 字段进 `l2_fund_snapshots`; 封单额/封成比/委比/连板/竞价买入
    等字段盘后一律回 0, 既不能回测也没有历史可回退。唯一键
    (trade_date, symbol, market), raw 保留 104 字段原样以便口径比对。
    """
    from src.db.models import MoreInfoDaily

    MoreInfoDaily.__table__.create(bind=conn, checkfirst=True)


def _m205_index_daily(conn: Connection) -> None:
    """主要指数日K表 index_daily(2026-10-18 P0-b)。

    `klines` 明确不收指数(裸码撞号), spark 此前每 30 分钟重付一轮 6×指数K(≈4s)。
    本表唯一键 (trade_date, code), 供首屏 spark 读库 + 大盘择时回测。
    """
    from src.db.models import IndexDaily

    IndexDaily.__table__.create(bind=conn, checkfirst=True)


def _m206_breadth_snapshot(conn: Connection) -> None:
    """涨跌分布快照表(2026-10-20): 首页分布从"每次都算"改为"读库 + 增量落库"。

    没有这张表时: biz_cache TTL 300s 一过就重拉新浪 79 页(~32s), 前端只能显示
    「后台计算中」; 容器/Redis 重启后同样从零开始。落表后冷启动秒出, 并留下
    盘中情绪扩散的历史序列。
    """
    from src.db.models import BreadthDistributionSnapshot

    BreadthDistributionSnapshot.__table__.create(bind=conn, checkfirst=True)


def _m207_flow_snapshot_total_amount(conn: Connection) -> None:
    """大盘资金快照补 total_amount(2026-10-20)。

    首页「两市成交额」长期显示 `--`：实时分支有值，但走库快照分支时 `_snapshot_to_response`
    显式置空（表里没这列）。补列后快照分支也能给出成交额，不再因为"读库"丢字段。
    """
    if not _has_table(conn, "market_flow_snapshots"):
        return
    if _has_column(conn, "market_flow_snapshots", "total_amount"):
        return
    ddl = "DOUBLE PRECISION" if _dialect_is_pg(conn) else "REAL"
    conn.execute(text(f"ALTER TABLE market_flow_snapshots ADD COLUMN total_amount {ddl}"))


def _m208_copilot_trace_tables(conn: Connection) -> None:
    """数智参谋运行时轨迹表(2026-10-20, Agent 内核 P0)。

    三张表: copilot_runs(一次问答) / copilot_steps(每步工具调用) /
    copilot_feedback(用户反馈=评测样本)。与既有 agent_runs 分开: 那张表记后台定时
    Agent 的执行与推送, 这张表记用户对话的运行时轨迹。
    """
    from src.db.models import CopilotFeedback, CopilotRun, CopilotStep

    CopilotRun.__table__.create(bind=conn, checkfirst=True)
    CopilotStep.__table__.create(bind=conn, checkfirst=True)
    CopilotFeedback.__table__.create(bind=conn, checkfirst=True)


def _m211_flow_snapshot_source(conn: Connection) -> None:
    """大盘资金快照补 source 列(2026-10-20)。

    缘由: 同一个表里既有「东财四档」写入的旧行, 又有「通达信 AMO/SUPAMO」汇总的新行。
    没有来源列就无法区分, 读侧会把东财的行当成 TQ 值再标成 TQ 口径(撒谎)。
    """
    if not _has_table(conn, "market_flow_snapshots"):
        return
    if _has_column(conn, "market_flow_snapshots", "source"):
        return
    conn.execute(text("ALTER TABLE market_flow_snapshots ADD COLUMN source VARCHAR(32)"))


def _m209_copilot_user_id_text(conn: Connection) -> None:
    """把 copilot_runs/copilot_feedback 的 user_id 从 integer 改成 varchar(36)(2026-10-20)。

    缘由: users.id 是 UUID 字符串(见 agent_runs.user_id 的既有约定)。_m208 误写成
    Integer, 于是每次写轨迹都 DataError: invalid input syntax for type integer。
    表此时必为空(读写都在同一版本), 直接 ALTER 类型即可。
    """
    for table in ("copilot_runs", "copilot_feedback"):
        if not _has_table(conn, table):
            continue
        if not _has_column(conn, table, "user_id"):
            continue
        if _dialect_is_pg(conn):
            conn.execute(text(
                f"ALTER TABLE {table} ALTER COLUMN user_id TYPE VARCHAR(36) "
                "USING user_id::varchar"
            ))
        # SQLite 是动态类型, 无需变更


def _m213_copilot_memory_schedules(conn: Connection) -> None:
    """长期记忆 + 会话定时任务（2026-10-21, P0）。

    记忆解决"换会话就忘了你的偏好"，定时任务解决"每天 9:25 帮我看竞价"——
    两者都不需要改 chat.py 逻辑，只是新增两张表和注入点。
    """
    from src.db.models import CopilotMemory, CopilotSchedule

    CopilotMemory.__table__.create(bind=conn, checkfirst=True)
    CopilotSchedule.__table__.create(bind=conn, checkfirst=True)


def _m212_copilot_governance(conn: Connection) -> None:
    """P3 治理：llm_usage 归因(用户/轨迹) + 多用户配额表(2026-10-20)。

    为什么必须加: 成本看板要回答"谁在烧 token", 配额要在网关层硬拒 ——
    原先 llm_usage 只有 scene/model, 没有 user_id, 连"某用户今天用了多少"都算不出来。
    """
    from src.db.models import CopilotQuota

    CopilotQuota.__table__.create(bind=conn, checkfirst=True)

    for col, dtype in (("user_id", "VARCHAR(36)"), ("run_id", "VARCHAR(36)")):
        if _has_table(conn, "llm_usage") and not _has_column(conn, "llm_usage", col):
            conn.execute(text(f"ALTER TABLE llm_usage ADD COLUMN {col} {dtype}"))


def _m210_copilot_actions_annotations(conn: Connection) -> None:
    """动作回写 + K 线标注表(2026-10-20, P1)。

    copilot_actions: AI 提议 → pending → 用户确认 → applied; 红线动作(改持仓/下单)
    永远不在这套框架里。
    kline_annotations: 把 AI 的结论落到图上, 让标注成为人机共用的证据语言。
    """
    from src.db.models import CopilotAction, KlineAnnotation

    CopilotAction.__table__.create(bind=conn, checkfirst=True)
    KlineAnnotation.__table__.create(bind=conn, checkfirst=True)


def _m214_cancel_rate_snapshots(conn: Connection) -> None:
    """撤单率时序（2026-10-21）。

    撤单率本体是"当日会话累计"，只能回答当前值；要回答"这半小时撤单是不是突变"
    必须自己按分钟落点。unique(symbol, trade_date, hhmm) 让重复采样幂等。
    """
    from src.db.models import CancelRateSnapshot

    CancelRateSnapshot.__table__.create(bind=conn, checkfirst=True)


def _m215_stock_factor_daily(conn: Connection) -> None:
    """个股因子日快照（2026-10-21）：诊断要"个股 × 日期"的因子，而不是信号级快照。"""
    from src.db.models import StockFactorDaily

    StockFactorDaily.__table__.create(bind=conn, checkfirst=True)


def _m216_factor_concentration(conn: Connection) -> None:
    """因子表补 CYQ 集中度与筹码峰（2026-10-21）：第 6 个因子 + 峰位迁移。"""
    for col, dtype in (("concentration", "DOUBLE PRECISION"), ("peak_price", "DOUBLE PRECISION")):
        if _has_table(conn, "stock_factor_daily") and not _has_column(conn, "stock_factor_daily", col):
            conn.execute(text(f"ALTER TABLE stock_factor_daily ADD COLUMN {col} {dtype}"))


def _m217_chat_conversation_pinned(conn: Connection) -> None:
    """chat_conversations 加 pinned 列（2026-10-22）：会话置顶。

    置顶与会话管理面板配套：置顶的会话在列表/搜索结果里排最前。
    BOOLEAN NOT NULL DEFAULT FALSE —— 存量行默认不置顶；SQLite 动态类型下
    `FALSE` 落库为 0, ORM 侧用 Boolean 读回布尔，语义一致。
    幂等可重跑（列已存在则跳过）。
    """
    _add_column_if_missing(
        conn,
        "chat_conversations",
        "pinned",
        "ALTER TABLE chat_conversations ADD COLUMN pinned BOOLEAN NOT NULL DEFAULT FALSE",
    )


def _m218_harness_proposals(conn: Connection) -> None:
    """新分析维度提案表（2026-10-22, P0 采集层）：只记录，不生效。

    为什么建这张表：分析维度写死在手写 YAML 配方里，用户在对话里提出配方没覆盖的
    维度（"股东户数变化""龙虎榜席位风格"）时无处落点。这张表是那份"需求账本"。

    幂等：`__table__.create(checkfirst=True)` —— 表已存在就跳过（PG/SQLite 都安全）。

    唯一约束 `(user_id, normalized_dimension, status)` 里**故意带 status**：
    同一维度被拒之后再次出现可以重新提（pending 位是空的），不被历史行永久压住。
    """
    from src.db.models import HarnessProposal

    HarnessProposal.__table__.create(bind=conn, checkfirst=True)


def _m219_auth_session_revocations(conn: Connection) -> None:
    """会话吊销墓碑表(2026-10-23 线上事故补偿)。

    背景: v0.45.0 把「踢设备」做成「删会话行 + 写墓碑」, 但墓碑表只在**写入路径**
    (`permissions.mark_session_revoked` 的懒 DDL)`才建 —— 生产从未踢过人, 表就没建。
    而 `is_session_revoked()` 在**每个鉴权请求**都会 SELECT 它 → PostgreSQL 抛
    `UndefinedTable` 并**中止整个事务** → 该请求后续所有查询 `InFailedSqlTransaction`
    → `/api/settings`、`/api/notifications`、`/api/users/admin/list` 等成片 500。

    两条修复缺一不可:
      1. 本迁移 —— 建表交给启动期迁移, 不再依赖「有人踢过设备」这个偶然前提;
      2. `permissions._safe_rollback` —— 任何 fail-open 探测失败后回滚,
         避免「可选表缺失」升级成「整站 500」。
    SQLite 不中止事务, 单测永远是绿的, 所以这两条只能靠**显式迁移 + 回滚**保证。
    """
    conn.execute(
        text(
            """
CREATE TABLE IF NOT EXISTS auth_session_revocations (
    session_id VARCHAR(64) PRIMARY KEY,
    expires_at TIMESTAMP NOT NULL,
    revoked_at TIMESTAMP NOT NULL
)
"""
        )
    )
    try:
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_auth_session_revocations_expires_at "
                "ON auth_session_revocations (expires_at)"
            )
        )
    except Exception:  # noqa: BLE001 - 索引建不出来不影响正确性(仅清理效率)
        pass


def _m220_exday_daily(conn: Connection) -> None:
    """TQ `get_exday_data` 四档资金结构日表(2026-10-23)。

    `get_exday_data` 只返**当日 1 行**(count/start_time/date 参数实测全无效), 所以
    四档结构的历史分位无法回填, 只能每天落库累积 —— 本表就是那个累积点。
    金额=元、量=手; 口径 tdx_exday4(按单笔成交金额分档), 与东财四档/腾讯逐笔不可换算。
    """
    if _has_table(conn, "exday_daily"):
        return
    id_col = "SERIAL PRIMARY KEY" if _dialect_is_pg(conn) else "INTEGER PRIMARY KEY AUTOINCREMENT"
    conn.execute(
        text(
            f"""
CREATE TABLE IF NOT EXISTS exday_daily (
    id {id_col},
    trade_date VARCHAR(8) NOT NULL,
    symbol VARCHAR(16) NOT NULL,
    market VARCHAR(8) NOT NULL DEFAULT 'CN',
    amount FLOAT,
    volume_lots FLOAT,
    active_buy_amt FLOAT,
    active_sell_amt FLOAT,
    active_ratio FLOAT,
    xl_buy_amt FLOAT, lg_buy_amt FLOAT, md_buy_amt FLOAT, sm_buy_amt FLOAT,
    xl_sell_amt FLOAT, lg_sell_amt FLOAT, md_sell_amt FLOAT, sm_sell_amt FLOAT,
    xl_active_net FLOAT, lg_active_net FLOAT, md_active_net FLOAT, sm_active_net FLOAT,
    retail_ratio FLOAT, xl_share FLOAT, main_active_net FLOAT, main_active_net_pct FLOAT,
    chip_migration FLOAT,
    b_order FLOAT, s_order FLOAT, total_b_order FLOAT, total_s_order FLOAT,
    b_cancel FLOAT, s_cancel FLOAT, cjbs FLOAT,
    avg_order_lots FLOAT, avg_trade_lots FLOAT, gran_hiding FLOAT, order_fill_ratio FLOAT,
    cancel_buy_rate FLOAT, cancel_sell_rate FLOAT, cancel_skew FLOAT,
    quality VARCHAR(16), reason VARCHAR(255), source VARCHAR(32),
    created_at TIMESTAMP
)
"""
        )
    )
    try:
        conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_exday_daily "
                "ON exday_daily (trade_date, symbol, market)"
            )
        )
        conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_exday_sym_day ON exday_daily (symbol, trade_date)")
        )
        conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_exday_day ON exday_daily (trade_date)")
        )
    except Exception as exc:  # noqa: BLE001 - 索引失败不该阻断建表(schema 已就位)
        logger.warning("exday_daily 索引创建失败(表已建): %s", exc)


def _m221_factor_winrate_daily(conn: Connection) -> None:
    """量化因子多维分桶胜率与超额收益日快照表(2026-10-23)。

    存 GS 区间/信号(原始 vs 次日确认)、AI 机构活跃度四档、GS×活跃度共振、
    威科夫量价四标、涨停封板质量 的 T+1/T+3/T+5 实测胜率、3%达标率、盈亏比及
    相对同池无条件基准的超额胜率/超额收益。
    """
    if _has_table(conn, "factor_winrate_daily"):
        return
    id_col = "SERIAL PRIMARY KEY" if _dialect_is_pg(conn) else "INTEGER PRIMARY KEY AUTOINCREMENT"
    conn.execute(
        text(
            f"""
CREATE TABLE IF NOT EXISTS factor_winrate_daily (
    id {id_col},
    as_of_date VARCHAR(10) NOT NULL,
    factor_family VARCHAR(40) NOT NULL,
    bucket_key VARCHAR(48) NOT NULL,
    bucket_label VARCHAR(64) NOT NULL,
    horizon INTEGER NOT NULL DEFAULT 5,
    sample_count INTEGER NOT NULL DEFAULT 0,
    win_count INTEGER NOT NULL DEFAULT 0,
    untradable_skipped INTEGER NOT NULL DEFAULT 0,
    win_rate_pct FLOAT,
    hit3_rate_pct FLOAT,
    avg_return_pct FLOAT,
    median_return_pct FLOAT,
    profit_loss_ratio FLOAT,
    baseline_win_rate_pct FLOAT,
    baseline_avg_return_pct FLOAT,
    excess_win_rate_pp FLOAT,
    excess_return_pct FLOAT,
    universe_size INTEGER,
    lookback_bars INTEGER,
    source VARCHAR(32),
    created_at TIMESTAMP
)
"""
        )
    )
    try:
        conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_factor_winrate_daily "
                "ON factor_winrate_daily (as_of_date, factor_family, bucket_key, horizon)"
            )
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_factor_winrate_family_date "
                "ON factor_winrate_daily (factor_family, as_of_date)"
            )
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_factor_winrate_date "
                "ON factor_winrate_daily (as_of_date)"
            )
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("factor_winrate_daily 索引创建失败(表已建): %s", exc)


def _m222_market_replay_daily(conn: Connection) -> None:
    """盘后复盘日快照: 涨停/炸板/跌停/晋级率/情绪周期一站落库(2026-10-23)。

    口径红线: 本表 limit_up/炸板 全部来自东财三池**同源同日**
    (getTopicZTPool / getTopicZBPool / getTopicDTPool), 与 market_sentiment_daily 的
    TQ 自算口径(tdx_selfcalc)并存不混算; payload 存完整证据 JSON。
    """
    if _has_table(conn, "market_replay_daily"):
        return
    id_col = "SERIAL PRIMARY KEY" if _dialect_is_pg(conn) else "INTEGER PRIMARY KEY AUTOINCREMENT"
    conn.execute(
        text(
            f"""
CREATE TABLE IF NOT EXISTS market_replay_daily (
    id {id_col},
    trade_date VARCHAR(8) NOT NULL,
    market VARCHAR(8) NOT NULL DEFAULT 'CN',
    limit_up_count INTEGER NOT NULL DEFAULT 0,
    limit_up_max_streak INTEGER NOT NULL DEFAULT 0,
    first_board_count INTEGER NOT NULL DEFAULT 0,
    ge2_count INTEGER NOT NULL DEFAULT 0,
    ge3_count INTEGER NOT NULL DEFAULT 0,
    one_word_count INTEGER NOT NULL DEFAULT 0,
    broken_count INTEGER NOT NULL DEFAULT 0,
    break_rate_pct FLOAT,
    seal_rate_pct FLOAT,
    limit_down_count INTEGER NOT NULL DEFAULT 0,
    limit_down_max_days INTEGER NOT NULL DEFAULT 0,
    promo_rate_pct FLOAT,
    phase VARCHAR(16),
    phase_label VARCHAR(16),
    mainline JSON,
    quality VARCHAR(12),
    missing_dims JSON,
    source VARCHAR(32),
    payload JSON,
    created_at TIMESTAMP
)
"""
        )
    )
    try:
        conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_market_replay_daily "
                "ON market_replay_daily (trade_date, market)"
            )
        )
        conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_market_replay_date ON market_replay_daily (trade_date)")
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("market_replay_daily 索引创建失败(表已建): %s", exc)


def _m223_position_defense_price(conn: Connection) -> None:
    """positions 加 defense_price 列(2026-10-23, 产品裁决: 硬防线方案 B)。

    为什么是绝对价而不是比例: 用户在不同标的上的纪律位来源不同(有人按成本价 -8%,
    有人按关键支撑位), 存比例就得反推价格、还会随成本价变动漂移; 存绝对价即"用户
    拍下的那个价"。

    为什么 nullable 且不填默认值: NULL 明确表达"用户未设置"。方案 B 下系统不再默认
    -8% 告警, 若这里回填 *0.92 就等于把系统臆造的纪律位写进了用户账本。

    幂等可重跑(列已存在则跳过)。
    """
    _add_column_if_missing(
        conn,
        "positions",
        "defense_price",
        "ALTER TABLE positions ADD COLUMN defense_price FLOAT",
    )


def _m224_copilot_run_heartbeat(conn: Connection) -> None:
    """copilot_runs 加 heartbeat_at 列（2026-10-25, P0-0.1 Durable Run）。

    为什么需要: 此前 run 只有 status, 进程重启/容器发布后库里会留下永久 running 的
    "幽灵运行" —— 用户看到"正在分析"卡住, 既不会失败也不会重试, 也没人收尸。
    有心跳才能按 run 级事实判断"还有没有人管它"(多 worker 下"进程活着"不等于"run 在跑")。

    为什么 nullable 且不回填: NULL 恰好表达"这条老记录从没写过心跳"; 接管逻辑把
    NULL 视为超时(写入它的那次进程早就不在了), 回填 now() 反而会把尸体伪装成活的。

    幂等可重跑(列已存在则跳过)。
    """
    _add_column_if_missing(
        conn,
        "copilot_runs",
        "heartbeat_at",
        "ALTER TABLE copilot_runs ADD COLUMN heartbeat_at TIMESTAMP",
    )


def _m226_copilot_parent_run(conn: Connection) -> None:
    """copilot_runs 加 parent_run_id 列（2026-10-25, P1-1.1 委派子 run）。

    为什么需要: 委派（delegate）要能回答"这次结论里哪几条是子任务查出来的"。
    没有 parent_run_id，子 run 只是一堆互不相干的轨迹，父 run 无法汇总，
    也无法在子 run 失败时判断"哪一块证据缺失"。

    为什么不加外键: copilot_runs 会按保留期清理，硬外键会让删父 run 失败或者
    级联删掉子 run 的审计痕迹。这里只是弱引用（子 run 可以比父活得久）。

    nullable 且不回填: NULL = 非委派的普通 run（绝大多数），回填没有意义。

    幂等可重跑（列/索引已存在则跳过）。
    """
    _add_column_if_missing(
        conn,
        "copilot_runs",
        "parent_run_id",
        "ALTER TABLE copilot_runs ADD COLUMN parent_run_id VARCHAR(36)",
    )
    try:
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_copilot_runs_parent ON copilot_runs (parent_run_id)"
        ))
    except Exception as exc:  # noqa: BLE001
        logger.warning("m226 建索引失败(不阻断): %s", exc)


def _m225_harness_experiments(conn: Connection) -> None:
    """实验记录表 harness_experiments（2026-10-25, P0-0.5）。

    为什么单开表: `copilot_memories` 存的是用户偏好, 混进"我改了配方、指标涨了几个点"
    会让偏好检索被实验噪音污染。两类事实不同生命周期, 必须分表。

    为什么 baseline/candidate 存 JSON 而不是拆列: 指标集会继续长(以后加 token/延迟),
    拆列每加一项就要迁移; 存 JSON 让旧记录仍可读, 且对照结论可原样回放。
    """
    conn.execute(
        text(
            """
CREATE TABLE IF NOT EXISTS harness_experiments (
    id SERIAL PRIMARY KEY,
    user_id VARCHAR(36),
    recipe_id VARCHAR(64) NOT NULL,
    change_summary TEXT NOT NULL,
    baseline JSON,
    candidate JSON,
    verdict VARCHAR(16) NOT NULL,
    reasons JSON,
    evidence_path VARCHAR(256),
    note TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""
        )
    )
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_harness_exp_created ON harness_experiments(created_at)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_harness_exp_recipe ON harness_experiments(recipe_id)"))


MIGRATIONS: tuple[Migration, ...] = (
    Migration(101, "agent_config_kind_and_visibility", _m101_agent_config_kind),
    Migration(102, "backfill_agent_kind_data", _m102_backfill_agent_kind),
    Migration(103, "agent_run_observability_fields", _m103_agent_run_observability),
    Migration(104, "analysis_history_kind_snapshot", _m104_history_kind_snapshot),
    Migration(105, "indexes_for_agent_kind_and_history", _m105_indexes),
    Migration(106, "log_entry_observability_fields", _m106_log_observability),
    Migration(107, "stock_suggestion_market_dimension", _m107_suggestion_market_dimension),
    Migration(108, "entry_candidates_table", _m108_entry_candidates_table),
    Migration(109, "entry_candidate_upgrade", _m109_entry_candidate_upgrade),
    Migration(110, "entry_candidate_outcomes", _m110_entry_candidate_outcomes),
    Migration(111, "strategy_layer", _m111_strategy_layer),
    Migration(112, "strategy_analytics_snapshots", _m112_strategy_analytics_snapshots),
    Migration(113, "market_scan_snapshot_and_mixed_source", _m113_market_scan_snapshot_and_mixed_source),
    Migration(114, "paper_trading_tables", _m114_paper_trading_tables),
    Migration(115, "paper_trading_excluded_markets", _m115_paper_trading_excluded_markets),
    Migration(116, "chat_tables", _m116_chat_tables),
    Migration(117, "chat_initial_context", _m117_chat_initial_context),
    Migration(118, "paper_trading_market_allocations", _m118_paper_trading_market_allocations),
    Migration(119, "notifications_table", _m119_notifications_table),
    Migration(120, "notification_push_channels", _m120_notification_push_channels),
    Migration(121, "dedupe_stocks_unique", _m121_dedupe_stocks_unique),
    Migration(
        122,
        "user_id_columns_chat_notif_suggestion",
        _m122_user_id_columns_chat_notif_suggestion,
    ),
    Migration(123, "user_id_on_price_alerts", _m123_user_id_on_price_alerts),
    Migration(
        124,
        "analysis_history_user_id_unique",
        _m124_analysis_history_user_id_unique,
    ),
    Migration(125, "klines_table", _m125_klines_table),
    Migration(126, "datasource_health_columns", _m126_datasource_health_columns),
    Migration(127, "dp_history_table", _m127_dp_history_table),
    Migration(128, "l2_ticks_table", _m128_l2_ticks_table),
    Migration(129, "summary_cache_table", _m129_summary_cache_table),
    Migration(130, "tick_archive_table", _m130_tick_archive_table),
    Migration(131, "seal_quality_samples_table", _m131_seal_quality_samples),
    Migration(132, "limit_up_events_table", _m132_limit_up_events),
    Migration(133, "signal_snapshots_table", _m133_signal_snapshots),
    Migration(134, "demon_factors_table", _m134_demon_factors),
    Migration(135, "paper_trading_user_id", _m135_paper_trading_user_id),
    Migration(136, "analysis_history_status", _m136_analysis_history_status),
    Migration(
        137,
        "klines_adjust_dimension",
        _m137_klines_adjust_dimension,
    ),
    Migration(
        138,
        "market_flow_snapshots_table",
        _m138_market_flow_snapshots_table,
    ),
    Migration(
        139,
        "mainline_rank_daily_table",
        _m139_mainline_rank_daily_table,
    ),
    Migration(
        140,
        "market_scan_ranks_table",
        _m140_market_scan_ranks_table,
    ),
    Migration(
        141,
        "signal_summary_daily_table",
        _m141_signal_summary_daily_table,
    ),
    Migration(
        142,
        "dark_fund_top_snapshots_table",
        _m142_dark_fund_top_snapshots_table,
    ),
    # 历史 A 层迁移收编(W3.1/D2, 2026-09-09): 原src/web/database.py _migrate*
    # 幂等守卫迁移, 对现役库均为 no-op, 仅供更老的存量库升级。
    Migration(143, "legacy_missing_columns_and_tables", _m143_legacy_missing_columns),
    Migration(144, "legacy_old_providers_to_services", _m144_legacy_old_providers),
    Migration(145, "legacy_settings_to_models", _m145_legacy_settings_to_models),
    Migration(146, "legacy_positions_to_accounts", _m146_legacy_positions_to_accounts),
    Migration(147, "legacy_remove_stock_enabled", _m147_legacy_remove_stock_enabled),
    Migration(148, "legacy_add_user_id_columns", _m148_legacy_add_user_id_columns),
    Migration(149, "stock_universe_snapshots", _m149_stock_universe_snapshots),
    Migration(150, "klines_amount_column", _m150_klines_amount_column),
    Migration(151, "trading_halts_table", _m151_trading_halts_table),
    Migration(152, "adj_factors_table", _m152_adj_factors_table),
    Migration(153, "datasource_failures_table", _m153_datasource_failures_table),
    # 批次2(2026-09-10): 把市场数据"最大化落库" —— 竞价快照 + 筹码日频。
    Migration(154, "auction_snapshots_table", _m154_auction_snapshots_table),
    Migration(155, "chip_daily_table", _m155_chip_daily_table),
    Migration(156, "dragon_tiger_events_table", _m156_dragon_tiger_events_table),
    Migration(157, "quote_snapshots_table", _m157_quote_snapshots_table),
    Migration(158, "l2_ticks_event_time_key", _m158_l2_ticks_event_time_key),
    Migration(159, "klines_minute_continuous_agg", _m159_klines_minute_continuous_agg),
    Migration(160, "lhb_detail_tables", _m160_lhb_detail_tables),
    Migration(161, "resonance_scan_table", _m161_resonance_scan_table),
    Migration(162, "resonance_ai_verdicts_table", _m162_resonance_ai_verdicts_table),
    Migration(163, "theme_mood_table", _m163_theme_mood_table),
    Migration(164, "market_phase_extra_columns", _m164_market_phase_extra_columns),
    Migration(165, "app_jobs_table", _m165_app_jobs_table),
    Migration(166, "skill_gateway_tables", _m166_skill_gateway_tables),
    Migration(167, "user_sessions_table", _m167_user_sessions_table),
    Migration(168, "high_value_api_logs", _m168_high_value_api_logs),
    Migration(169, "unified_identity_columns", _m169_unified_identity_columns),
    # Pro 付费系统(2026-09-16): 申请审核 + 档位配置 + 到期降级日志
    Migration(170, "pro_applications", _m170_pro_applications),
    Migration(171, "tier_configs", _m171_tier_configs),
    # 邮箱注册(2026-09-16): users.email + 唯一索引
    Migration(172, "users_email", _m172_users_email),
    # GDPR 用户数据删除(P3, 2026-09-18): 软删除标记 + 请求时间
    Migration(173, "users_soft_delete", _m173_users_soft_delete),
    # 口径快照档案(B5, 2026-09-18): 三源逐日留痕, 支撑"口径漂移曲线"与数字可溯源
    Migration(174, "caliber_snapshots", _m174_caliber_snapshots),
    # 决策日志(B6, 2026-09-18): 信号 + 当时证据/价格留痕, 事后回填 T+1/3/5
    Migration(175, "decision_log", _m175_decision_log),
    Migration(176, "factor_ic_snapshots", _m176_factor_ic_snapshots),
    # 邀请码注册(2026-09-19): 内部使用模式下收回"面向公众"的自助注册口
    Migration(177, "invite_codes", _m177_invite_codes),
    # TQ 市场级情绪日序列(2026-09-23): 涨停/炸板/连板/打板资金/两融/龙虎榜…
    # 东财类免费源只给当日快照, 这张表提供历史基线(情绪周期全靠在它上面跑聚合)
    Migration(178, "market_sentiment_daily_series", _m178_market_sentiment_daily),
    # TQ 条件选股信号日序列(2026-09-25): 108 个条件选股公式全市场扫描的"当日触发家数"
    # 这是免费源没有的市场宽度维度(只有当日快照, 无历史 → 算不出基线)
    Migration(179, "tq_formula_signal_daily", _m179_tq_formula_signal_daily),
    Migration(180, "market_breadth_daily", _m180_market_breadth_daily),
    # .img 十档盘口帧落库(2026-10-02): 校准入库 + 多用户隔离 + source/as_of 显式
    Migration(181, "img_orderbook_frames", _m181_img_orderbook_frames),
    # 题材情绪收盘定型(2026-10-08): 盘中实时刷新 + 收盘 15:05 定型(settled_at)
    Migration(182, "theme_mood_settled_at", _m182_theme_mood_settled_at),
    # 决策合成预落库 + TTL 缓存(2026-10-10 冗余设计): /decision/{symbol} 免重复现算
    Migration(183, "decision_cache", _m183_decision_cache),
    # 决策账本 regime 分桶(2026-10-10 A 决策提胜率): 情绪周期条件化的聚合维度
    Migration(184, "decision_log_regime", _m184_decision_log_regime),
    # 盘中 DDE 大单逐 N 分钟采样序列(2026-10-10 P3 补差 A): 解锁分时突破「突」的
    # 「DDE大单持续流入」判定 —— 现有链只有当日快照, 落序列表才能构造真序列。
    Migration(185, "dde_minute_flow_table", _m185_dde_minute_flow_table),
    # 主力资金战报日快照(2026-10-10 规格 §4.4): 当日主力动向汇总页的读快照底座。
    Migration(186, "war_report_daily_table", _m186_war_report_daily_table),
    Migration(187, "l2_fund_snapshots", _m187_l2_fund_snapshots),
    # 首封时间×封成比 强度标签影子表(2026-10-11 胜率创新 P0-1, 影子运行): 每日盘后打标,
    # 只读展示 + 分桶胜率验证, 不改任何现有信号/决策。
    Migration(188, "limit_strength_shadow", _m188_limit_strength_shadow),
    # 市场主线日频快照(2026-10-14 缓存收口): biz_cache 缺失时(重启/换 worker)读库,
    # 免掉冷启动 16~37s 重拉涨停池; date 主键同日覆盖, 收盘后那次即当日定稿。
    Migration(189, "mainline_daily_snapshot", _m189_mainline_daily_snapshot_table),
    # 连板梯队定型日快照(2026-10-14 定型预渲染落库): 收盘 cron 预渲染当日定型 payload,
    # 请求期读库为主(6.5s 现算 → 毫秒级), 盘中只叠加 live 快照的当日行; 缺行/落后回退现算。
    Migration(190, "ladder_daily_snapshot", _m190_ladder_daily_snapshot_table),
    # 工作台日级图层派生块预落库(2026-10-14 首屏 P0): 个股 summary 的日级块
    # (gs_signals/fund_flow/activity/events/unlock/chips/resonance)此前每请求现算
    # (chips/wencai/pool_flow 是网络调用, 冷启动 ~26s 撞前端 20s abort)。收盘批算 +
    # 请求期读库, 只现算当日实时块。先例: _m183 decision_cache。
    Migration(191, "workbench_layer_daily", _m191_workbench_layer_daily_table),
    # 龙虎榜按日落库(2026-10-14 资金类落库收口): `_lhb_from_tq` 此前每次请求现拉 TQ
    # GP 长序列(个股整段历史), 属审计定性「资金类仍现拉」。按日 upsert 后请求只读库 +
    # 当日增量回源, 历史日永不回源(先例: _m156 dragon_tiger_events / _m138 快照)。
    Migration(192, "dragon_tiger_daily_table", _m192_dragon_tiger_daily_table),
    # 板块资金采样落库(2026-10-14): board-capital-flow 完全现拉同花顺 + 仅 stale 备份。
    # 追加 1~5min 采样落库(仿 market_flow_snapshots 先例), 读侧够新读库/过旧回源+stale。
    Migration(193, "board_flow_snapshots_table", _m193_board_flow_snapshots_table),
    # 十档盘口快照采集表(2026-10-14): /api/orderbook-ob(12s 硬超时)与 klines summary
    # 的 _build_orderbook(8s 硬超时)在请求期直接打 thsdk 十档, 上游可卡 30s → 压用户请求。
    # 本表承载后台采集(交易时段 15~30s, 活跃票)的十档滚动窗, 请求端读快照优先 + 陈旧度标注。
    Migration(194, "orderbook_snapshots_table", _m194_orderbook_snapshots_table),
    # AI 看图形态影子表(2026-10-14 胜率创新 · 主线一并行, 影子运行): 盘后对自选∪持仓
    # 「渲染→视觉 LLM 认形态」并与规则形态(kline_patterns)同日同票对照落库, 只读用,
    # 不改任何现有信号/决策/展示。先例: _m188 limit_strength_shadow。
    Migration(195, "ai_pattern_shadow", _m195_ai_pattern_shadow),
    # 情绪序列本地自算(2026-10-14): 用户决策**不购通达信云数据 Key**, SC/GP 云函数永久
    # 放弃(生产断在 2026-09-23)。情绪四指标(涨停/炸板/封板资金/连板)改由客户端本地接口
    # 自算(+ limit_up_events 聚合), 本表存自算明细/近似标注/诊断; 兼容列同时写
    # market_sentiment_daily, 温度计/API/新鲜度自检零改动。
    Migration(196, "sentiment_selfcalc_daily", _m196_sentiment_selfcalc_daily),
    # 决策账本回填覆盖修复(2026-10-14): `outcome_state`/`outcome_note` —— 让"信号日无
    # K 线(休市/停牌)所以永远算不出 T+n"的行**显式标注**, 不再与"还没到 T+n"混为一谈,
    # 也不再每次占满回填候选窗口(生产: 20260925 中秋休市落 597 行, 挡在 waiters 最前)。
    Migration(197, "decision_log_outcome_state", _m197_decision_log_outcome_state),
    # 决策账本停牌跨期口径标注(2026-10-15): `t_n_span` —— T+n 取「第 n 根可用 K 线」,
    # 长期停牌会把一个月后的收盘当 T+1(symbol=300082 信号日 20260918 下一根是 2026-10-13)。
    # 加跨度列后 span>5 的行标 `suspended_gap`, 胜率/分桶统计排除并显式标注条数;
    # **不改 T+n 判定本体、不重算存量数值**(用户裁决: 只标注 + 排除统计)。
    Migration(198, "decision_log_t_n_span", _m198_decision_log_t_n_span),
    Migration(199, "klines_period_ts_symbol_index", _m199_klines_period_ts_symbol_index),
    # I-8 分价博弈矩阵落库(2026-10-18, 用户硬约束): 盘中快照(缓存 + 分价时序证据)
    # + 日终定稿归档(历史回测/主力轨迹复盘的永久事实源)。读侧读库优先, 免每次首屏
    # 跨网打腾讯(海外节点 1~8s); 历史日永不回源(分价表只给当日)。
    Migration(200, "price_distribution_tables", _m200_price_distribution_tables),
    # P0 落库加固批次 1(2026-10-18 P0-a, 方案 §8): 竞价过程 append-only + 悟道竞价
    # 独家字段 + 可转债条款/折溢价。共同目标: ① 过程可回放(原只留终值);
    # ② 冷启动不再 N 次 RPC(条款 12h 进程内 dict → 版本化表)。
    Migration(201, "auction_tick_snapshots", _m201_auction_tick_snapshots),
    Migration(202, "auction_wudao_daily", _m202_auction_wudao_daily),
    Migration(203, "cb_tables", _m203_cb_tables),
    # P0-b(方案 §8 批次 2): more_info 104 字段收盘定稿 + 指数日K。
    # 前者填补「盘后一律回 0」的会话值缺口, 后者让首屏 spark 零跨网。
    Migration(204, "more_info_daily", _m204_more_info_daily),
    Migration(205, "index_daily", _m205_index_daily),
    Migration(206, "breadth_distribution_snapshot", _m206_breadth_snapshot),
    Migration(207, "flow_snapshot_total_amount", _m207_flow_snapshot_total_amount),
    Migration(208, "copilot_trace_tables", _m208_copilot_trace_tables),
    Migration(209, "copilot_user_id_text", _m209_copilot_user_id_text),
    Migration(210, "copilot_actions_annotations", _m210_copilot_actions_annotations),
    Migration(211, "flow_snapshot_source", _m211_flow_snapshot_source),
    Migration(212, "copilot_governance", _m212_copilot_governance),
    Migration(213, "copilot_memory_schedules", _m213_copilot_memory_schedules),
    Migration(214, "cancel_rate_snapshots", _m214_cancel_rate_snapshots),
    Migration(215, "stock_factor_daily", _m215_stock_factor_daily),
    Migration(216, "factor_concentration", _m216_factor_concentration),
    Migration(217, "chat_conversation_pinned", _m217_chat_conversation_pinned),
    Migration(218, "harness_proposals", _m218_harness_proposals),
    # 见函数 docstring: 缺这条表 = 每个鉴权请求 500(2026-10-23 生产事故)
    Migration(219, "auth_session_revocations", _m219_auth_session_revocations),
    Migration(220, "exday_daily", _m220_exday_daily),
    Migration(221, "factor_winrate_daily", _m221_factor_winrate_daily),
    Migration(222, "market_replay_daily", _m222_market_replay_daily),
    Migration(223, "position_defense_price", _m223_position_defense_price),
    Migration(224, "copilot_run_heartbeat", _m224_copilot_run_heartbeat),
    Migration(225, "harness_experiments", _m225_harness_experiments),
    Migration(226, "copilot_parent_run", _m226_copilot_parent_run),
)


def _get_applied(conn: Connection, version: int) -> tuple[int, str, int] | None:
    row = conn.execute(
        text(
            """
SELECT version, checksum, success
FROM schema_migrations
WHERE version = :version
LIMIT 1
"""
        ),
        {"version": version},
    ).first()
    if not row:
        return None
    return int(row[0]), str(row[1]), int(row[2])


def has_pending_migrations(engine: Engine) -> bool:
    with engine.begin() as conn:
        _ensure_schema_table(conn)
        for m in MIGRATIONS:
            rec = _get_applied(conn, m.version)
            if not rec or rec[2] != 1 or rec[1] != m.checksum:
                return True
    return False


def run_versioned_migrations(engine: Engine) -> None:
    # W1.5(A5) 2026-09-08: PG 下多实例(生产 2 容器)同时启动会并发跑迁移,
    # 同一 DDL 并发执行可能撞死锁/duplicate。用会话级 advisory lock 串行化:
    # 拿锁的实例跑迁移, 等待实例轮到时迁移已全部 success=1, 循环秒过。
    # 锁必须挂在独立 AUTOCOMMIT 连接上(会话锁随事务回滚即释放, 不能放进
    # per-migration 事务), 持锁横跨全部迁移事务, finally 中解锁+关连接。
    if engine.dialect.name == "postgresql":
        lock_conn = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
        try:
            lock_conn.execute(
                text("SELECT pg_advisory_lock(:k)"), {"k": _MIGRATION_LOCK_KEY}
            )
        except Exception:
            lock_conn.close()
            raise
        try:
            _run_migrations_inner(engine)
        finally:
            try:
                lock_conn.execute(
                    text("SELECT pg_advisory_unlock(:k)"), {"k": _MIGRATION_LOCK_KEY}
                )
            except Exception:  # noqa: BLE001 — 解锁失败不掩盖迁移原始异常; 连接断开会话锁自动释放
                logger.warning("pg_advisory_unlock 失败(连接断开时会话锁自动释放)")
            lock_conn.close()
    else:
        _run_migrations_inner(engine)


def _run_migrations_inner(engine: Engine) -> None:
    with engine.begin() as conn:
        _ensure_schema_table(conn)

    for m in MIGRATIONS:
        try:
            with engine.begin() as conn:
                _ensure_schema_table(conn)
                rec = _get_applied(conn, m.version)
                if rec and rec[2] == 1 and rec[1] == m.checksum:
                    continue

                conn.execute(
                    text(
                        """
INSERT INTO schema_migrations(version, name, checksum, success, error)
VALUES(:version, :name, :checksum, 0, '')
ON CONFLICT(version) DO UPDATE SET
  name = excluded.name,
  checksum = excluded.checksum,
  success = 0,
  error = ''
"""
                    ),
                    {
                        "version": m.version,
                        "name": m.name,
                        "checksum": m.checksum,
                    },
                )
                logger.info("Applying migration v%s: %s", m.version, m.name)

                m.runner(conn)
                conn.execute(
                    text(
                        """
UPDATE schema_migrations
SET success = 1,
    error = '',
    applied_at = CURRENT_TIMESTAMP
WHERE version = :version
"""
                    ),
                    {
                        "version": m.version,
                    },
                )
        except Exception as exc:
            # 2026-08-27 fix (5+1 评审 B 轨): PG 下 m.runner 抛异常后原事务已进入
            # aborted 状态, 在同一事务里写 success=0 必报 "current transaction is
            # aborted", 失败记录写不进、原始错误被掩盖、启动持续失败。
            # 修法: 失败记录改用**新连接/新事务**写入(原事务回滚后此前
            # success=0 的 INSERT 也不复存在, 故用 INSERT ... ON CONFLICT
            # 保证失败行一定落库); 最后 re-raise 原始异常。
            try:
                with engine.begin() as conn2:
                    _ensure_schema_table(conn2)
                    conn2.execute(
                        text(
                            """
INSERT INTO schema_migrations(version, name, checksum, success, error, applied_at)
VALUES(:version, :name, :checksum, 0, :error, CURRENT_TIMESTAMP)
ON CONFLICT(version) DO UPDATE SET
  success = 0,
  error = excluded.error,
  applied_at = CURRENT_TIMESTAMP
"""
                        ),
                        {
                            "version": m.version,
                            "name": m.name,
                            "checksum": m.checksum,
                            "error": str(exc)[:2000],
                        },
                    )
            except Exception as log_exc:
                logger.error(
                    "Migration v%s 失败记录写入也失败: %s", m.version, log_exc
                )
            logger.exception("Migration v%s failed: %s", m.version, m.name)
            raise
