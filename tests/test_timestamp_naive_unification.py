"""时间列口径统一（2026-11-02）：UTC naive → 北京 naive。

背景（生产实测，不是推断）：
- PG 容器 ``TZ=Asia/Shanghai``，``func.now()`` 写入 ``timestamp without time zone``
  得到的是**北京 naive**；
- 而 ``datetime.utcnow()`` / ``now(timezone.utc).replace(tzinfo=None)`` 写的是
  **UTC naive**。同一个事件在两张表里差 8 小时 —— 用 run_id 关联
  ``llm_usage``(func.now) 与 ``copilot_runs``(utcnow) 的 426 行，中位差 +8.01h。

本文件守住四件事：
1. 模型里不再有 ``datetime.utcnow`` 作默认值/onupdate；
2. ``src/`` 下不再有 ``datetime.utcnow()`` 运行时调用（只允许出现在文档字符串里）；
3. v234 在 SQLite 上是 no-op（SQLite 的 CURRENT_TIMESTAMP 本来就是 UTC naive，
   由 ``timezone._db_naive_tz`` 按方言解读，不需要也不应该平移）；
4. v234 在真 PG 上把存量行 +8h，且**只平移一次**（哨兵行兜住 checksum 变化导致的重跑）。

注意第 4 条需要真 PG（``SIDA_TEST_PG_URL``），无则跳过；SQLite 证不了这条。
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from src.core.timezone import beijing_now_naive
from src.db.models import Base
from src.web.migrations import _ensure_schema_table, _m234_utc_naive_to_app_tz

ROOT = Path(__file__).resolve().parents[1]

#: v234 要平移的列（与迁移里的清单同源，测试独立列一份，防两边一起改错）
EXPECTED_COLUMNS = {
    "user_ai_prefs": {"updated_at"},
    "copilot_memories": {"created_at", "last_used_at"},
    "copilot_schedules": {"created_at"},
    "copilot_quotas": {"updated_at"},
    "harness_proposals": {"first_seen_at", "last_seen_at", "decided_at"},
    "copilot_runs": {"created_at", "finished_at", "heartbeat_at"},
    "harness_experiments": {"created_at"},
    "webhook_subscriptions": {"created_at"},
    "recipe_versions": {"created_at"},
    "copilot_steps": {"created_at"},
    "copilot_feedback": {"created_at"},
    "copilot_actions": {"created_at", "applied_at"},
    "kline_annotations": {"created_at"},
    "stock_factor_daily": {"created_at"},
    "exday_daily": {"created_at"},
    "factor_winrate_daily": {"created_at"},
    "market_replay_daily": {"created_at"},
    "invite_codes": {"created_at", "expires_at"},
    "invite_code_uses": {"used_at"},
}


def _py_default_source(column) -> str:
    """把列的 Python 侧 default/onupdate 还原成源码文本，用于断言口径。"""
    out = []
    for attr in ("default", "onupdate"):
        fn = getattr(column, attr, None)
        if fn is None:
            continue
        arg = getattr(fn, "arg", None)
        out.append(getattr(arg, "__name__", repr(arg)))
    return " ".join(out)


def test_模型时间列默认值统一走北京naive():
    """18 列曾用 datetime.utcnow 做默认值，必须都改成 beijing_now_naive。"""
    utc_defaults = []
    for table_name, columns in EXPECTED_COLUMNS.items():
        table = Base.metadata.tables.get(table_name)
        if table is None:  # invite_* 是裸 SQL 表，不在 ORM 里
            continue
        for column_name in columns:
            column = table.columns.get(column_name)
            if column is None:
                continue
            src = _py_default_source(column)
            if not src:
                continue  # server_default 型（func.now）没有 Python default
            if "beijing_now_naive" not in src:
                utc_defaults.append(f"{table_name}.{column_name} -> {src}")
    assert utc_defaults == [], f"仍有非北京口径的默认值: {utc_defaults}"


def test_源码里不再有运行时utcnow():
    """只允许文档字符串提到 datetime.utcnow（说明为什么要避免它）。

    用 AST 找真正的调用节点 —— 字符串匹配会被单行文档字符串误伤
    （``src/db/mixins.py`` 的说明段落就是反例）。
    """
    offenders = []
    for path in (ROOT / "src").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute) or func.attr != "utcnow":
                continue
            base = func.value
            name = getattr(base, "id", None) or getattr(base, "attr", None)
            if name == "datetime":
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert offenders == [], "运行时仍在用 UTC naive 写库: " + ", ".join(offenders)


def test_v234在sqlite上是noop():
    """SQLite 的 CURRENT_TIMESTAMP 本身就是 UTC naive，平移会把正确行推错。"""
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE t_dummy (id INTEGER)"))
        _m234_utc_naive_to_app_tz(conn)  # 不应抛错、不应改任何东西
    assert True


@pytest.mark.skipif(
    not os.environ.get("SIDA_TEST_PG_URL"),
    reason="需要真 PG（SIDA_TEST_PG_URL）才能验证 +8h 平移与哨兵",
)
def test_v234在PG上平移8小时且只平移一次():
    url = os.environ["SIDA_TEST_PG_URL"]
    engine = create_engine(url)
    Base.metadata.create_all(bind=engine)  # 建全部表（含 invite_codes 之外的部分）
    with engine.begin() as conn:
        _ensure_schema_table(conn)  # 迁移框架自己的版本表，哨兵行写在这里
        # invite_* 是裸 SQL 表，测试里补建最小结构
        conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS invite_codes (code VARCHAR(32) PRIMARY KEY, "
                "created_at TIMESTAMP, expires_at TIMESTAMP)"
            )
        )
        conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS invite_code_uses (code VARCHAR(32), used_at TIMESTAMP)"
            )
        )
        conn.execute(text("DELETE FROM copilot_runs"))
        conn.execute(text("DELETE FROM schema_migrations WHERE version = 234000"))
        # 存量行：模拟历史 UTC naive 写入（北京时间 2026-11-02 10:00 的事，存成 02:00）
        conn.execute(
            text(
                "INSERT INTO copilot_runs (run_id, user_id, recipe, question, status, tool_calls, "
                "created_at, finished_at) VALUES "
                "('r1', 'u1', '', 'q', 'ok', 0, "
                "TIMESTAMP '2026-11-02 02:00:00', TIMESTAMP '2026-11-02 02:00:30')"
            )
        )

        _m234_utc_naive_to_app_tz(conn)
        row = conn.execute(
            text("SELECT created_at, finished_at FROM copilot_runs WHERE run_id = 'r1'")
        ).first()
        assert str(row[0]) == "2026-11-02 10:00:00", f"created_at 应 +8h，实得 {row[0]}"
        assert str(row[1]) == "2026-11-02 10:00:30", f"finished_at 应 +8h，实得 {row[1]}"
        sentinel = conn.execute(
            text("SELECT 1 FROM schema_migrations WHERE version = 234000")
        ).first()
        assert sentinel is not None

        # 再跑一次：哨兵拦住，绝不能变成 +16h
        _m234_utc_naive_to_app_tz(conn)
        again = conn.execute(
            text("SELECT created_at FROM copilot_runs WHERE run_id = 'r1'")
        ).first()
        assert str(again[0]) == "2026-11-02 10:00:00", f"重跑不该再平移，实得 {again[0]}"

    engine.dispose()
