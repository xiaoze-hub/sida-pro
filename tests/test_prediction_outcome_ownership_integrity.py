"""归属完整性 + 迁移重跑安全（2026-11-12 三次复审）。

审计复现的三类真实问题，本文件逐条钉住修好后的契约：

  A. **升级库缺 user_id 外键**（create_all 新库有 `users.id ON DELETE SET NULL`）：
     迁移库能把不存在的 user_id 写进去（ghost 行），用户被物理清除后行里留着悬空 id，
     而新库会被 SET NULL —— 同一份代码在两个库上归属/删除语义不同。
     修法：PG 加同名 `NOT VALID` 外键（存量孤儿行不校验不改写，新写立刻受约束）；
     SQLite 不重建表（重建会丢触发器/索引/行），用等价触发器（校验 + 删用户置空）。
     **schema 表示不同，行为一致** —— 差异在 migrations.py / models.py 里显式披露。

  B. **m249 重跑让服务起不来**：老版本建的无状态唯一键 `ux_prediction_outcome_user_key`，
     在库里已有"计分行 + superseded 版本行"之后重跑（函数体/注释任何改动都会改
     checksum → 框架重跑）直接 UNIQUE 冲突，整个启动迁移中断。
     修法：m249 不再建它；m250 的四条唯一索引一律"无数据冲突才建"，冲突就跳过并记 ERROR。

  C. **ghost 用户错误计分 / system 桶含糊**：读数侧改成"users 里还有这个人才算有归属"；
     ghost 与"账号注销留档"只报数（不评估、不删除、不回填）；system 桶保留并标清
     （设计上不评估、不并进任何账号、不据此自动交易、按保留期清理）；
     导出里 superseded 显式标"审计版本·不计分"。

测试库纪律：每个用例一套 tmp_path 独立 SQLite；起测清空 DATABASE_*/SIDA_DB*/DATA_DIR/
PYTHONPATH 并断言连的就是这次的临时库；不碰生产库、不清任何既有数据。
"""

from __future__ import annotations

import os
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select, text as sqltext
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

_DB_ENV = (
    "DATABASE_URL",
    "DATABASE_URL_WRITE",
    "DATABASE_URL_READ",
    "DATABASE_URL_READONLY",
    "SIDA_DB",
    "SIDA_DB_URL",
    "SIDA_DATA_DIR",
    "SIDA_CACHE_DIR",
    "DATA_DIR",
    "POSTGRES_DSN",
    "PYTHONPATH",
)

BASELINE_MAX = 246  # 生产基线: MIGRATIONS 最高 246(本次的 247-250 尚未上线)
OLD_DAY = (date.today() - timedelta(days=120)).isoformat()
TODAY = date.today().isoformat()

# 加 user_id 列之前的老库(基线 246): 没有 user_id/run_trace_id/owner_scope, 也没有任何唯一键
LEGACY_DDL = (
    """
CREATE TABLE users (
    id VARCHAR(36) PRIMARY KEY,
    username VARCHAR(64) NOT NULL,
    password_hash VARCHAR(256) NOT NULL DEFAULT '',
    role VARCHAR(16) NOT NULL DEFAULT 'member',
    is_active BOOLEAN NOT NULL DEFAULT 1,
    is_deleted BOOLEAN NOT NULL DEFAULT 0
)
""",
    """
CREATE TABLE agent_prediction_outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_name VARCHAR(64) NOT NULL,
    stock_symbol VARCHAR(32) NOT NULL,
    stock_market VARCHAR(16) NOT NULL DEFAULT 'CN',
    prediction_date VARCHAR(10) NOT NULL,
    horizon_days INTEGER NOT NULL DEFAULT 1,
    action VARCHAR(16) NOT NULL DEFAULT 'watch',
    action_label VARCHAR(32) NOT NULL DEFAULT '观望',
    confidence FLOAT,
    trigger_price FLOAT,
    outcome_price FLOAT,
    outcome_return_pct FLOAT,
    outcome_status VARCHAR(16) NOT NULL DEFAULT 'pending',
    meta TEXT,
    evaluated_at TIMESTAMP,
    created_at TIMESTAMP
)
""",
)


def _engine(tmp_path, name: str):
    for key in _DB_ENV:
        os.environ.pop(key, None)
    path = tmp_path / name
    assert not os.path.exists(path), "测试库必须是新建的, 不许复用既有库文件"
    eng = create_engine(f"sqlite:///{path}", future=True)
    assert str(eng.url).startswith("sqlite:///") and str(tmp_path) in str(eng.url)
    return eng


def _legacy_baseline(eng, *, dup_rows: int = 2, user_ids: tuple[str, ...] = ("u-a",)) -> None:
    """建一个"基线 246 + 老重复数据"的库, 并把 101..246 记成已成功应用。

    schema_migrations 里 101..246 的 checksum 用当前源码算 —— 与生产"已应用且未被改动"
    的状态等价, 于是真正的 runner 只会去跑 247..250, 这正是升级路径要验的东西。
    """
    from src.web import migrations as mig

    with eng.begin() as conn:
        for ddl in LEGACY_DDL:
            conn.execute(sqltext(ddl))
        for uid in user_ids:
            conn.execute(
                sqltext("INSERT INTO users(id, username) VALUES (:i, :n)"),
                {"i": uid, "n": f"user-{uid}"},
            )
        for i in range(dup_rows):
            conn.execute(
                sqltext(
                    "INSERT INTO agent_prediction_outcomes "
                    "(agent_name, stock_symbol, stock_market, prediction_date, "
                    "horizon_days, action) VALUES "
                    "('premarket_outlook','600183','CN','2026-11-11',1,'buy')"
                )
            )
    with eng.begin() as conn:
        mig._ensure_schema_table(conn)
        for m in mig.MIGRATIONS:
            if m.version > BASELINE_MAX:
                continue
            conn.execute(
                sqltext(
                    "INSERT INTO schema_migrations(version, name, checksum, success, error) "
                    "VALUES (:v, :n, :c, 1, '')"
                ),
                {"v": m.version, "n": m.name, "c": m.checksum},
            )


def _legacy_core_rows(eng) -> list[tuple]:
    """基线 246 就存在的列(升级前后都要一模一样)。"""
    with eng.begin() as conn:
        return conn.execute(
            sqltext(
                "SELECT id, action, stock_symbol, prediction_date, outcome_status "
                "FROM agent_prediction_outcomes ORDER BY id"
            )
        ).fetchall()


def _unowned_row_count(eng) -> int:
    with eng.begin() as conn:
        return int(
            conn.execute(
                sqltext(
                    "SELECT COUNT(*) FROM agent_prediction_outcomes "
                    "WHERE user_id IS NULL AND owner_scope IS NULL AND run_trace_id IS NULL"
                )
            ).scalar()
        )


def _trigger_names(eng) -> set[str]:
    with eng.begin() as conn:
        rows = conn.execute(
            sqltext("SELECT name FROM sqlite_master WHERE type='trigger'")
        ).fetchall()
    return {r[0] for r in rows}


def _index_names(eng) -> set[str]:
    with eng.begin() as conn:
        rows = conn.execute(
            sqltext("SELECT name FROM sqlite_master WHERE type='index'")
        ).fetchall()
    return {r[0] for r in rows}


# ── A. 基线 246(含旧重复数据) → 247–250 升级 + 重跑 ─────────────────────────

def test_baseline_246_with_legacy_duplicates_upgrades_and_reruns(tmp_path):
    from src.web import migrations as mig

    eng = _engine(tmp_path, "upgrade.db")
    _legacy_baseline(eng, dup_rows=2)
    assert mig.has_pending_migrations(eng) is True
    before = _legacy_core_rows(eng)
    assert len(before) == 2

    # 1) 真实 runner 升级到最新
    mig.run_versioned_migrations(eng)
    assert mig.has_pending_migrations(eng) is False, "升级后不应再有待跑迁移"

    with eng.begin() as conn:
        applied = dict(
            (int(r[0]), int(r[1]))
            for r in conn.execute(
                sqltext("SELECT version, success FROM schema_migrations WHERE version >= 247")
            ).fetchall()
        )
    assert applied == {247: 1, 248: 1, 249: 1, 250: 1}, applied

    # 2) 存量重复行一条不动(不删、不改写), 且归属列全空
    assert _legacy_core_rows(eng) == before
    assert _unowned_row_count(eng) == 2

    # 3) 归属完整性到位: 迁移库没有声明式外键 → 等价触发器必须补上
    triggers = _trigger_names(eng)
    assert {
        "trg_pred_outcome_user_fk_ins",
        "trg_pred_outcome_user_fk_upd",
        "trg_pred_outcome_user_fk_del",
        "trg_pred_outcome_system_no_user_ins",
        "trg_pred_outcome_system_no_user_upd",
    } <= triggers, triggers
    indexes = _index_names(eng)
    assert {
        "ix_prediction_outcome_user",
        "ux_prediction_outcome_user_active_key",
        "ux_prediction_outcome_user_run_key",
        "ux_prediction_outcome_system_active_key",
        "ux_prediction_outcome_system_run_key",
    } <= indexes, indexes

    # 4) 新版本记录出现(计分行 + superseded 审计版本) → 重跑整条链不能炸
    with eng.begin() as conn:
        conn.execute(
            sqltext(
                "INSERT INTO agent_prediction_outcomes (user_id, owner_scope, run_trace_id, "
                "agent_name, stock_symbol, stock_market, prediction_date, horizon_days, action) "
                "VALUES ('u-a','user','r1','premarket_outlook','600183','CN','2026-11-11',1,'buy')"
            )
        )
        conn.execute(
            sqltext(
                "INSERT INTO agent_prediction_outcomes (user_id, owner_scope, run_trace_id, "
                "agent_name, stock_symbol, stock_market, prediction_date, horizon_days, action, "
                "outcome_status) VALUES ('u-a','user','r2','premarket_outlook','600183','CN',"
                "'2026-11-11',1,'sell','superseded')"
            )
        )
        snapshot = conn.execute(
            sqltext(
                "SELECT id, user_id, owner_scope, run_trace_id, outcome_status, action "
                "FROM agent_prediction_outcomes ORDER BY id"
            )
        ).fetchall()

    # checksum 变化 = 框架重跑(生产上"改了函数体/注释"就是这个效果)
    with eng.begin() as conn:
        conn.execute(
            sqltext("UPDATE schema_migrations SET checksum = 'stale' WHERE version >= 249")
        )
    assert mig.has_pending_migrations(eng) is True
    mig.run_versioned_migrations(eng)  # 老版 m249 在这里会 UNIQUE 冲突 → 启动失败

    with eng.begin() as conn:
        assert (
            conn.execute(
                sqltext(
                    "SELECT id, user_id, owner_scope, run_trace_id, outcome_status, action "
                    "FROM agent_prediction_outcomes ORDER BY id"
                )
            ).fetchall()
            == snapshot
        ), "重跑改动了存量/版本行"
    assert mig.has_pending_migrations(eng) is False


def test_m249_rerun_is_idempotent_when_versions_already_exist(tmp_path):
    """直接重跑 m249(最坏形态): 库里已有同键多版本行, 不许抛错、不许删行。"""
    from src.web import migrations as mig

    eng = _engine(tmp_path, "m249_rerun.db")
    _legacy_baseline(eng, dup_rows=1)
    with eng.begin() as conn:
        mig._m249_prediction_outcome_user_scope(conn)
        mig._m250_prediction_outcome_versions_and_system_scope(conn)
    with eng.begin() as conn:
        for trace, status in (("r1", "pending"), ("r2", "superseded"), ("r3", "superseded")):
            conn.execute(
                sqltext(
                    "INSERT INTO agent_prediction_outcomes (user_id, owner_scope, run_trace_id, "
                    "agent_name, stock_symbol, stock_market, prediction_date, horizon_days, "
                    "action, outcome_status) VALUES ('u-a','user',:t,'premarket_outlook',"
                    "'600183','CN','2026-11-11',1,'buy',:s)"
                ),
                {"t": trace, "s": status},
            )

    # 老版本 m249 建的索引若还在库里(由旧函数体建过), 新版也必须能安全处理
    with eng.begin() as conn:
        conn.execute(
            sqltext(
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_prediction_outcome_user_key "
                "ON agent_prediction_outcomes "
                "(user_id, agent_name, stock_symbol, stock_market, prediction_date, horizon_days) "
                "WHERE user_id IS NOT NULL AND outcome_status = 'pending'"
            )
        )

    with eng.begin() as conn:
        mig._m249_prediction_outcome_user_scope(conn)  # 老实现: UNIQUE 冲突 → 服务起不来
    with eng.begin() as conn:
        mig._m250_prediction_outcome_versions_and_system_scope(conn)
    with eng.begin() as conn:
        mig._m249_prediction_outcome_user_scope(conn)
        mig._m250_prediction_outcome_versions_and_system_scope(conn)
        count = conn.execute(sqltext("SELECT COUNT(*) FROM agent_prediction_outcomes")).scalar()
    assert count == 4, "计分行 + 两条 superseded + 基线 1 行, 一行都不许丢"
    assert "ux_prediction_outcome_user_key" not in _index_names(eng), (
        "老版 m249 的无状态唯一键必须被 m250 清掉(只删索引, 不删行)"
    )


# ── B. 老库/新库归属与删除语义一致 ────────────────────────────────────────

def _migrated_db(tmp_path, name: str = "migrated.db"):
    from src.web import migrations as mig

    eng = _engine(tmp_path, name)
    _legacy_baseline(eng, dup_rows=0)
    mig.run_versioned_migrations(eng)
    return eng


def _create_all_db(tmp_path, name: str = "fresh.db", *, fk_on: bool):
    """create_all 建的新库(模型里 user_id 是真外键)。fk_on=True 时打开 FK 强制。"""
    from src.db.models import AgentPredictionOutcome, User
    from src.db.session import Base

    eng = _engine(tmp_path, name)
    if fk_on:
        from sqlalchemy import event

        @event.listens_for(eng, "connect")
        def _fk_pragma(dbapi_conn, _rec):  # noqa: ANN001
            dbapi_conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(eng, tables=[User.__table__, AgentPredictionOutcome.__table__])
    return eng


def test_migrated_db_refuses_ghost_user_and_keeps_rows_on_user_delete(tmp_path):
    """迁移库: 不存在的用户写不进去; 删用户 → 行保留 + user_id 置空(等价 SET NULL)。"""
    eng = _migrated_db(tmp_path)
    with eng.begin() as conn:
        with pytest.raises(IntegrityError) as exc:
            conn.execute(
                sqltext(
                    "INSERT INTO agent_prediction_outcomes (user_id, owner_scope, run_trace_id, "
                    "agent_name, stock_symbol, stock_market, prediction_date, horizon_days, "
                    "action, action_label, outcome_status) VALUES ('ghost-uid','user','r1',"
                    "'premarket_outlook','600183','CN','2026-11-11',1,'buy','准备建仓','pending')"
                )
            )
        assert "foreign key" in str(exc.value.orig).lower(), exc.value
    with eng.begin() as conn:
        assert conn.execute(sqltext("SELECT COUNT(*) FROM agent_prediction_outcomes")).scalar() == 0

    with eng.begin() as conn:
        conn.execute(
            sqltext(
                "INSERT INTO agent_prediction_outcomes (user_id, owner_scope, run_trace_id, "
                "agent_name, stock_symbol, stock_market, prediction_date, horizon_days, action) "
                "VALUES ('u-a','user','r1','premarket_outlook','600183','CN','2026-11-11',1,'buy')"
            )
        )
    with eng.begin() as conn:
        conn.execute(sqltext("DELETE FROM users WHERE id = 'u-a'"))
    with eng.begin() as conn:
        row = conn.execute(
            sqltext(
                "SELECT user_id, owner_scope, run_trace_id FROM agent_prediction_outcomes"
            )
        ).fetchall()
    assert row == [(None, "user", "r1")], row


def test_create_all_db_has_the_same_ownership_semantics(tmp_path):
    """create_all 新库: 同样的两条语义(拒 ghost / 删用户置空) —— 与迁移库**行为**一致。"""
    eng = _create_all_db(tmp_path, fk_on=True)
    with eng.begin() as conn:
        conn.execute(
            sqltext(
                "INSERT INTO users(id, username, password_hash, role, is_active, is_deleted, "
                "token_version) VALUES ('u-a','a','x','member',1,0,1)"
            )
        )
        with pytest.raises(IntegrityError):
            conn.execute(
                sqltext(
                    "INSERT INTO agent_prediction_outcomes (user_id, owner_scope, run_trace_id, "
                    "agent_name, stock_symbol, stock_market, prediction_date, horizon_days, "
                    "action, action_label, outcome_status) VALUES ('ghost-uid','user','r1',"
                    "'premarket_outlook','600183','CN','2026-11-11',1,'buy','准备建仓','pending')"
                )
            )
    with eng.begin() as conn:
        conn.execute(
            sqltext(
                "INSERT INTO agent_prediction_outcomes (user_id, owner_scope, run_trace_id, "
                "agent_name, stock_symbol, stock_market, prediction_date, horizon_days, action, "
                "action_label, outcome_status) VALUES ('u-a','user','r1','premarket_outlook',"
                "'600183','CN','2026-11-11',1,'buy','准备建仓','pending')"
            )
        )
    with eng.begin() as conn:
        conn.execute(sqltext("DELETE FROM users WHERE id = 'u-a'"))
    with eng.begin() as conn:
        assert conn.execute(
            sqltext("SELECT user_id, owner_scope FROM agent_prediction_outcomes")
        ).fetchall() == [(None, "user")]


def test_preexisting_ghost_rows_are_preserved_by_migration(tmp_path):
    """老库已有 ghost 行: 迁移不改写/不删除, 只让新写入不再产生 ghost。"""
    from src.web import migrations as mig

    eng = _engine(tmp_path, "ghost_legacy.db")
    _legacy_baseline(eng, dup_rows=0)
    with eng.begin() as conn:
        # 老库已手工加过 user_id 列, 其中一行指向不存在的用户
        conn.execute(
            sqltext("ALTER TABLE agent_prediction_outcomes ADD COLUMN user_id VARCHAR(36)")
        )
        conn.execute(
            sqltext(
                "INSERT INTO agent_prediction_outcomes (user_id, agent_name, stock_symbol, "
                "stock_market, prediction_date, horizon_days, action) VALUES "
                "('ghost-uid','premarket_outlook','600183','CN','2026-11-11',1,'buy'),"
                "('u-a','premarket_outlook','600184','CN','2026-11-11',1,'sell')"
            )
        )
    with eng.begin() as conn:
        mig._m249_prediction_outcome_user_scope(conn)
    with eng.begin() as conn:
        rows = conn.execute(
            sqltext("SELECT user_id FROM agent_prediction_outcomes ORDER BY id")
        ).fetchall()
    assert [r[0] for r in rows] == ["ghost-uid", "u-a"], "存量 ghost 行被改写了"
    # 新写入仍被拦
    with pytest.raises(IntegrityError):
        with eng.begin() as conn:
            conn.execute(
                sqltext(
                    "INSERT INTO agent_prediction_outcomes (user_id, agent_name, stock_symbol, "
                    "stock_market, prediction_date, horizon_days, action) VALUES "
                    "('ghost-2','premarket_outlook','600185','CN','2026-11-11',1,'buy')"
                )
            )


# ── C. ghost / system / superseded 的统计口径 ──────────────────────────────

@pytest.fixture()
def live(tmp_path, monkeypatch):
    """完整 ORM 库(FK 不强制) + 打补丁的 SessionLocal, 用于跑真实读写路径。"""
    from src.db.models import AgentPredictionOutcome, User
    from src.db.session import Base
    from src.db.models import (
        AgentContextRun,
        AnalysisHistory,
        NewsTopicSnapshot,
        PremarketFactorEvidence,
        StockContextSnapshot,
        StockSuggestion,
    )

    eng = _engine(tmp_path, "live.db")
    Base.metadata.create_all(
        eng,
        tables=[
            User.__table__,
            AgentPredictionOutcome.__table__,
            AnalysisHistory.__table__,
            PremarketFactorEvidence.__table__,
            StockSuggestion.__table__,
            StockContextSnapshot.__table__,
            NewsTopicSnapshot.__table__,
            AgentContextRun.__table__,
        ],
    )
    Session = sessionmaker(bind=eng, autoflush=False, expire_on_commit=False, future=True)
    monkeypatch.setattr("src.db.session.SessionLocal", Session)
    monkeypatch.setattr("src.core.context_store.SessionLocal", Session)
    with Session() as s:
        s.add(User(id="u-a", username="a", password_hash="x", role="member", is_active=True))
        s.add(User(id="u-gone", username="gone", password_hash="x", role="member", is_active=True))
        s.commit()
    return SimpleNamespace(engine=eng, Session=Session)


def _add_outcome(
    live,
    *,
    user_id: str | None,
    owner_scope: str | None,
    action: str = "buy",
    symbol: str = "600183",
    day: str = TODAY,
    status: str = "pending",
    ret: float | None = None,
    trace: str = "r1",
) -> int:
    from src.db.models import AgentPredictionOutcome

    with live.Session() as s:
        rec = AgentPredictionOutcome(
            user_id=user_id,
            owner_scope=owner_scope,
            run_trace_id=trace,
            agent_name="premarket_outlook",
            stock_symbol=symbol,
            stock_market="CN",
            prediction_date=day,
            horizon_days=1,
            action=action,
            action_label="准备建仓",
            outcome_status=status,
            outcome_return_pct=ret,
            meta={},
        )
        s.add(rec)
        s.commit()
        return int(rec.id)


def test_ghost_rows_are_never_scored_or_listed(live):
    """ghost(用户已被物理清除, 迁移库缺外键的产物)不许评估、不许呈现、不许计入成绩。"""
    from src.core.context_store import (
        count_ghost_user_prediction_outcomes,
        list_agent_prediction_outcomes,
    )
    from src.core.prediction_outcome import evaluate_pending_prediction_outcomes
    from src.db.models import AgentPredictionOutcome, User
    from src.web.api import profile as profile_api

    ghost_pending = _add_outcome(live, user_id="u-gone", owner_scope="user", day=OLD_DAY)
    ghost_evaluated = _add_outcome(
        live,
        user_id="u-gone",
        owner_scope="user",
        symbol="600184",
        day=OLD_DAY,
        status="evaluated",
        ret=9.9,
        trace="r2",
    )
    # 真实账号的一条正常命中样本
    real = _add_outcome(live, user_id="u-a", owner_scope="user", day=OLD_DAY)
    # 直接删用户行(FK 未强制) → 两条 u-gone 行变成悬空 user_id, 正是生产上的 ghost 形态
    with live.Session() as s:
        s.query(User).filter(User.id == "u-gone").delete(synchronize_session=False)
        s.query(AgentPredictionOutcome).filter(
            AgentPredictionOutcome.id == real
        ).update({"outcome_status": "evaluated", "outcome_return_pct": 1.5})
        s.commit()

    assert count_ghost_user_prediction_outcomes(max_horizon_days=10) == 1

    stats = evaluate_pending_prediction_outcomes(max_horizon_days=10, limit=10)
    assert stats["evaluated"] == 0, stats
    assert stats["skipped_ghost_user"] == 1, stats
    with live.Session() as s:
        assert (
            s.get(AgentPredictionOutcome, ghost_pending).outcome_status == "pending"
        ), "ghost 行被评估器改了(等于给不存在的账号计分)"
        assert s.get(AgentPredictionOutcome, ghost_evaluated).outcome_status == "evaluated"

    # 读取侧: 用 ghost 的 uid 也读不出行(归属不可证明 → 不呈现)
    assert list_agent_prediction_outcomes(user_id="u-gone") == []

    with live.Session() as s:
        board = profile_api._accuracy_board(s, user_id="u-a")
    assert board["overall"]["total"] == 1 and board["overall"]["hit_count"] == 1
    assert board["ghost_user_rows"] == 1, board
    assert "ghost" in board["note"] and "未删除" in board["note"]


def test_cleanup_preserves_ghost_and_detached_rows(live):
    """清理只删"归属仍在"和 system 桶; ghost / 账号注销留档只报数, 不删除。"""
    from src.core.context_store import cleanup_context_data

    # 可删: 归属用户还在 + system 桶 (都已过保留期)
    _add_outcome(live, user_id="u-a", owner_scope="user", day=OLD_DAY, symbol="600190")
    _add_outcome(live, user_id=None, owner_scope="system", day=OLD_DAY, symbol="600191")
    # 不可删: ghost + 账号注销留档 + 加列前历史行
    ghost = _add_outcome(live, user_id="u-gone", owner_scope="user", day=OLD_DAY, symbol="600192")
    detached = _add_outcome(live, user_id=None, owner_scope="user", day=OLD_DAY, symbol="600193")
    legacy = _add_outcome(live, user_id=None, owner_scope=None, day=OLD_DAY, symbol="600194")

    from src.db.models import AgentPredictionOutcome, User

    with live.Session() as s:
        s.query(User).filter(User.id == "u-gone").delete(synchronize_session=False)
        s.commit()

    dry = cleanup_context_data(outcome_days=60, dry_run=True)
    assert dry["dry_run"] is True
    assert dry["agent_prediction_outcomes"] == 2, dry
    assert dry["ghost_user_preserved"] == 1 and dry["detached_account_preserved"] == 1, dry
    assert dry["legacy_unowned_preserved"] == 1, dry

    done = cleanup_context_data(outcome_days=60, dry_run=False)
    assert done["agent_prediction_outcomes"] == 2, done
    with live.Session() as s:
        left = {r.id for r in s.query(AgentPredictionOutcome).all()}
    assert left == {ghost, detached, legacy}, (
        "ghost / 注销留档 / 历史未知归属行必须保留(归属问题不能当过期数据删掉)"
    )


def test_system_bucket_is_labelled_and_never_scored_or_auto_traded(live):
    """system 桶: 保留 + 标清(不评估、不并进任何账号、不据此自动交易、有界清理)。"""
    from src.core.prediction_outcome import evaluate_pending_prediction_outcomes
    from src.web.api import profile as profile_api

    _add_outcome(live, user_id=None, owner_scope="system", day=OLD_DAY, symbol="600183")
    _add_outcome(
        live,
        user_id=None,
        owner_scope="system",
        symbol="600184",
        day=OLD_DAY,
        status="evaluated",
        ret=3.0,
        trace="s2",
    )
    _add_outcome(live, user_id="u-a", owner_scope="user", day=OLD_DAY, symbol="600185")

    stats = evaluate_pending_prediction_outcomes(max_horizon_days=10, limit=10)
    assert stats["skipped_system_bucket"] == 1, stats
    assert stats["evaluated"] == 0, stats

    with live.Session() as s:
        board = profile_api._accuracy_board(s, user_id="u-a")
    assert board["system_scoped_rows"] == 1, board
    assert board["system_pending_rows"] == 1, board
    assert board["overall"]["total"] == 0, "system 桶被并进了个人成绩"
    assert "不据此自动交易" in board["note"], board["note"]
    assert "不评估" in board["note"], board["note"]


def test_superseded_rows_are_labelled_in_export_and_never_counted(live):
    """导出: superseded 显式标'审计版本·不计分', 计分列=否, 且不进命中率。"""
    from src.web.api import export as export_api
    from src.web.api import profile as profile_api

    _add_outcome(live, user_id="u-a", owner_scope="user", day=OLD_DAY, symbol="600183",
                 status="evaluated", ret=2.0, trace="r1")
    _add_outcome(live, user_id="u-a", owner_scope="user", day=OLD_DAY, symbol="600184",
                 status="superseded", ret=5.0, trace="r2")

    user = SimpleNamespace(id="u-a", username="a", role="member", nickname=None)
    with live.Session() as s:
        body = export_api.export_predictions(db=s, user=user).body.decode("utf-8-sig")
        board = profile_api._accuracy_board(s, user_id="u-a")

    lines = [ln for ln in body.splitlines() if ln.strip()]
    assert lines[0].split(",") == ["代码", "方向", "目标价", "日期", "结果", "计分", "归属"]
    assert len(lines) == 3, body
    superseded_line = next(ln for ln in lines if "600184" in ln)
    assert "superseded(审计版本·不计分)" in superseded_line, superseded_line
    assert "否(审计版本)" in superseded_line, superseded_line
    assert "hit" not in superseded_line, "审计版本按收益冒充 hit(会被当成一次成绩)"
    # 统计口径: 只有一条计分行
    assert board["overall"]["total"] == 1, board["overall"]
    assert board["overall"]["hit_count"] == 1


def test_system_scope_cannot_bind_a_user_at_db_level(tmp_path):
    """system 桶绑定真实用户 → DB 层直接拒(代码拒写之外的第二道, 手工 SQL 也拦)。"""
    eng = _migrated_db(tmp_path, "syscheck.db")
    with pytest.raises(IntegrityError):
        with eng.begin() as conn:
            conn.execute(
                sqltext(
                    "INSERT INTO agent_prediction_outcomes (user_id, owner_scope, run_trace_id, "
                    "agent_name, stock_symbol, stock_market, prediction_date, horizon_days, "
                    "action, action_label, outcome_status) VALUES ('u-a','system','s1',"
                    "'premarket_outlook','600183','CN','2026-11-11',1,'buy','准备建仓','pending')"
                )
            )
    with eng.begin() as conn:
        conn.execute(
            sqltext(
                "INSERT INTO agent_prediction_outcomes (user_id, owner_scope, run_trace_id, "
                "agent_name, stock_symbol, stock_market, prediction_date, horizon_days, action, "
                "action_label, outcome_status) VALUES (NULL,'system','s1','premarket_outlook',"
                "'600183','CN','2026-11-11',1,'buy','准备建仓','pending')"
            )
        )
        assert conn.execute(sqltext("SELECT COUNT(*) FROM agent_prediction_outcomes")).scalar() == 1


def test_real_save_path_refuses_unknown_user_on_migrated_db(tmp_path, monkeypatch):
    """走真实写入路径: 迁移库(触发器约束)上的 ghost user_id 必须返回 refused_unknown_user。

    这条把"DB 层拦住了"和"应用层如实报错"接起来 —— 否则外键错误会被当成"并发重复"或
    "保存成功", 上层计数就又开始说谎。
    """
    from sqlalchemy.orm import sessionmaker

    from src.core.context_store import save_agent_prediction_outcome
    from src.db.models import AgentPredictionOutcome

    eng = _migrated_db(tmp_path, "save_path.db")
    Session = sessionmaker(bind=eng, autoflush=False, expire_on_commit=False, future=True)
    monkeypatch.setattr("src.db.session.SessionLocal", Session)
    monkeypatch.setattr("src.core.context_store.SessionLocal", Session)

    bad = save_agent_prediction_outcome(
        agent_name="premarket_outlook",
        stock_symbol="600183",
        stock_market="CN",
        prediction_date="2026-11-11",
        horizon_days=1,
        action="buy",
        action_label="准备建仓",
        user_id="ghost-uid",
        owner_scope="user",
        run_trace_id="run-1",
    )
    assert bad.written is False
    assert bad.status == "refused_unknown_user", bad

    good = save_agent_prediction_outcome(
        agent_name="premarket_outlook",
        stock_symbol="600183",
        stock_market="CN",
        prediction_date="2026-11-11",
        horizon_days=1,
        action="buy",
        action_label="准备建仓",
        user_id="u-a",
        owner_scope="user",
        run_trace_id="run-2",
    )
    assert good.written is True and good.scored is True, good
    with Session() as s:
        rows = s.query(AgentPredictionOutcome).all()
    assert len(rows) == 1 and rows[0].user_id == "u-a"
