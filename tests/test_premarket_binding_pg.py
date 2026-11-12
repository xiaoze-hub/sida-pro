"""盘前账本 P0 的**隔离 PostgreSQL** 端到端验证（JSON 列 / NULL 唯一约束 / ALTER 行为）。

SQLite 跑通不代表 PG 跑通: 本仓库 PG 用 JSON 列、`user_id IS NULL` 在唯一约束下不去重
（并发系统调度会撞出两行同键 NULL 记录）、`CREATE INDEX IF NOT EXISTS` / `ALTER TABLE
ADD COLUMN` 的实现也不同。这个文件在**真实 PG** 上重跑关键链路。

**只跑隔离临时库**（默认跳过）:
    LP_P0_TEST_PG_URL=postgresql+psycopg2:///lp_p0_verify?host=/var/run/postgresql \
      python -m pytest tests/test_premarket_binding_pg.py -q

安全闸: 库名必须以 `lp_p0_` 开头, 且不得是 sida/postgres 等真实库名 —— 起测前断言,
不满足直接 fail, 绝不在生产库上建表删表。
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any, cast

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("LP_P0_TEST_PG_URL"),
    reason="未提供 LP_P0_TEST_PG_URL(隔离临时 PG 库), 跳过真实 PG 验证",
)

DAY = "2026-11-11"
FORBIDDEN_DB_NAMES = {"sida", "postgres", "template1", "sida_prod"}

REPORT = """
事件驱动前瞻（双轨制 · 潜伏表）
| 事件 | 受益板块 | 代码 | 名称 | 受益逻辑 | 强度 | 埋伏方式 | 分批计划 | 止损 | 目标 | 证伪条件 |
|---|---|---|---|---|---|---|---|---|---|---|
| PCB涨价 | 覆铜板 | 600183 | 生益科技 | 龙头 | A | 回调埋伏 | 1/3 | 35.60 | +10% | 提价落空 |
"""


class _User:
    id = "u-pg-owner"
    role = "owner"


def _snap(total: float) -> dict:
    return {
        "600183": {
            "total": total,
            "grade": "妖",
            "n_sealed": 12,
            "max_streak": 4,
            "participation": "活跃",
            "factor_date": "20261110",
        }
    }


@pytest.fixture(scope="module")
def pg():
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker

    url = os.environ["LP_P0_TEST_PG_URL"]
    eng = create_engine(url, future=True)
    if eng.dialect.name != "postgresql":
        pytest.fail(f"LP_P0_TEST_PG_URL 不是 PostgreSQL: {eng.dialect.name}")
    with eng.connect() as conn:
        dbname = conn.execute(text("select current_database()")).scalar_one()
    assert str(dbname).startswith("lp_p0_"), f"拒绝在非临时库上跑: {dbname}"
    assert dbname not in FORBIDDEN_DB_NAMES, f"拒绝在真实库上跑: {dbname}"

    from src.db.models import AnalysisHistory, PremarketFactorEvidence, User
    from src.db.session import Base

    Base.metadata.drop_all(
        eng,
        tables=[
            PremarketFactorEvidence.__table__,
            AnalysisHistory.__table__,
            User.__table__,
        ],
    )
    Base.metadata.create_all(
        eng,
        tables=[User.__table__, AnalysisHistory.__table__, PremarketFactorEvidence.__table__],
    )
    Session = sessionmaker(bind=eng, autoflush=False, expire_on_commit=False, future=True)
    return SimpleNamespace(engine=eng, Session=Session, dbname=dbname, url=url)


@pytest.fixture()
def patched(pg, monkeypatch):
    """把落库/读取路径真接到隔离 PG, 并清掉所有 DB 环境变量。"""
    for key in (
        "DATABASE_URL_WRITE",
        "DATABASE_URL_READ",
        "SIDA_DB_URL",
        "SIDA_DB",
        "DATABASE_URL",
        "POSTGRES_DSN",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr("src.db.session.SessionLocal", pg.Session)
    monkeypatch.setattr("src.core.analysis_history.SessionLocal", pg.Session)
    monkeypatch.setattr("src.db.session.get_read_engine", lambda: pg.engine)
    from src.core import decision_log

    monkeypatch.setattr(
        decision_log,
        "_close_series_pg",
        lambda _eng, _sym, _start: [
            ("2026-11-09", 10.0),
            ("2026-11-10", 10.5),
            ("2026-11-11", 12.0),
        ],
    )
    from sqlalchemy import text

    with pg.engine.begin() as conn:
        conn.execute(text("DELETE FROM premarket_factor_evidence"))
        conn.execute(text("DELETE FROM analysis_history WHERE agent_name='premarket_outlook'"))
    # owner 行有 FK 到 users.id; 先建个最小用户(只在本临时库)
    with pg.engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO users (id, username, password_hash, role, is_active, token_version) "
                "VALUES (:id, :u, 'x', 'owner', true, 1) ON CONFLICT (id) DO NOTHING"
            ),
            {"id": _User.id, "u": "pg-owner"},
        )
    return pg


def _save(at: str, total: float, run: str, *, user_id: str | None, content: str):
    """真实 save_analysis 路径（含报告级盘前门槛 + 追加式凭证, 凭证单独事务）。"""
    from src.core.analysis_history import save_analysis
    from src.core.premarket_factor_evidence import SOURCE_TOP_N, build_snapshot_evidence

    snap = _snap(total)
    ev = build_snapshot_evidence(
        snapshot=snap,
        frozen_at=at,
        report_day=DAY,
        asof="2026-11-10",
        source=SOURCE_TOP_N,
        run_trace_id=run,
    )
    raw = {
        "timestamp": at,
        "run_trace_id": run,
        "demon_factors": snap,
        "demon_factors_frozen_at": at,
        "demon_factors_source": SOURCE_TOP_N,
        "demon_factors_asof": "2026-11-10",
        "demon_factors_dropped": [],
        "demon_factors_preopen": bool(ev and ev["preopen"]),
        "demon_factors_sha256": ev["snapshot_sha256"] if ev else None,
    }
    from datetime import date as _date

    return save_analysis(
        agent_name="premarket_outlook",
        stock_symbol="*",
        content=content,
        title="【盘前分析】PG 测试",
        raw_data=raw,
        analysis_date=_date.fromisoformat(DAY),
        user_id=user_id,
        premarket_evidence=ev,
        report_completed_at=at,
    )


def test_pg_json_column_and_scope_binding_end_to_end(patched):
    from src.web.api import decisions as dec

    sys_run, own_run = "run-pg-sys", "run-pg-owner"
    assert _save(f"{DAY}T08:20:00+08:00", 88.0, sys_run, user_id=None, content=REPORT).body_saved
    own = _save(
        f"{DAY}T16:00:00+08:00", 70.0, own_run, user_id=_User.id, content=REPORT + "\n盘后\n"
    )
    assert own.body_saved is True

    out = dec.decisions_premarket(days=5, max_rows=30, user=cast(Any, _User()))
    assert out["available"] is True and len(out["items"]) == 1
    item = out["items"][0]
    # 收敛到能证明盘前的那一版(系统 08:20), 因子与正文同源
    assert item["run_trace_id"] == sys_run
    assert item["record_scope"] == "system"
    assert item["preopen"] is True
    assert item["rows"][0]["signals"]["demon"]["score"] == 88.0
    assert item["pit"]["evidence_matched"] == 1
    assert item["rows"][0]["outcome"]["t0"] is not None
    assert {c["run_trace_id"] for c in item["pit"]["candidates"]} == {sys_run, own_run}

    # PG JSON 列读回来仍是 dict（不是字符串）, 指纹一致性校验通过
    from sqlalchemy import text

    with patched.engine.connect() as conn:
        row = conn.execute(
            text(
                "select snapshot, run_trace_id, preopen from premarket_factor_evidence "
                "order by id"
            )
        ).all()
    assert len(row) == 2
    assert row[0].run_trace_id == sys_run and row[0].preopen is True
    assert row[1].run_trace_id == own_run and row[1].preopen is False
    assert isinstance(row[0].snapshot, dict) and row[0].snapshot["600183"]["total"] == 88.0


def test_pg_duplicate_null_owner_rows_converge_to_premarket(patched):
    """PG 的 UNIQUE 不比较 NULL → 并发系统调度可撞出两行同键 NULL; 收敛规则必须确定。"""
    from sqlalchemy import text

    from src.db.models import AnalysisHistory
    from src.core.premarket_ambush import factor_snapshot_fingerprint

    pre_raw = {
        "timestamp": f"{DAY}T08:20:00+08:00",
        "run_trace_id": "run-pg-dup-pre",
        "report_completed_at": f"{DAY}T08:25:00+08:00",
        "demon_factors": _snap(88.0),
        "demon_factors_frozen_at": f"{DAY}T08:20:00+08:00",
        "premarket_report_preopen": True,
    }
    post_raw = {
        "timestamp": f"{DAY}T20:00:00+08:00",
        "run_trace_id": "run-pg-dup-post",
        "report_completed_at": f"{DAY}T20:05:00+08:00",
        "demon_factors": _snap(70.0),
        "demon_factors_frozen_at": f"{DAY}T20:00:00+08:00",
        "premarket_report_preopen": False,
    }
    with patched.Session() as s:
        for raw, content in ((post_raw, REPORT + "\n盘后\n"), (pre_raw, REPORT)):
            s.add(
                AnalysisHistory(
                    user_id=None,
                    agent_name="premarket_outlook",
                    stock_symbol="*",
                    analysis_date=DAY,
                    title="dup",
                    content=content,
                    raw_data=raw,
                    status="success",
                )
            )
        s.commit()
        rows = s.execute(
            text(
                "select count(*) from analysis_history "
                "where agent_name='premarket_outlook' and user_id is null"
            )
        ).scalar_one()
    assert rows == 2, "PG 允许同键多行 NULL 归属"

    for run, at, snap in (
        ("run-pg-dup-pre", f"{DAY}T08:20:00+08:00", _snap(88.0)),
        ("run-pg-dup-post", f"{DAY}T20:00:00+08:00", _snap(70.0)),
    ):
        from src.db.models import PremarketFactorEvidence

        with patched.Session() as s:
            s.add(
                PremarketFactorEvidence(
                    user_id=None,
                    agent_name="premarket_outlook",
                    stock_symbol="*",
                    analysis_date=DAY,
                    generated_at=at,
                    preopen=run.endswith("pre"),
                    source="pg-test",
                    snapshot=snap,
                    snapshot_sha256=factor_snapshot_fingerprint(snap),
                    run_trace_id=run,
                    note="",
                )
            )
            s.commit()

    from src.web.api import decisions as dec

    item = dec.decisions_premarket(days=5, max_rows=30, user=cast(Any, _User()))["items"][0]
    assert item["run_trace_id"] == "run-pg-dup-pre"
    assert item["preopen"] is True
    assert item["rows"][0]["signals"]["demon"]["score"] == 88.0
    assert len(item["pit"]["candidates"]) == 2


def test_pg_evidence_failure_keeps_report(patched, monkeypatch):
    """PG 上凭证写失败也必须留下正文, 并显式报告证据失败。"""
    from src.core.analysis_history import save_analysis
    from src.core.premarket_factor_evidence import SOURCE_TOP_N, build_snapshot_evidence

    def boom(db_, evidence, *, user_id=None):
        raise RuntimeError("pg 凭证表写失败")

    monkeypatch.setattr("src.core.premarket_factor_evidence.append_evidence", boom, raising=True)

    snap = _snap(88.0)
    at = f"{DAY}T08:20:00+08:00"
    ev = build_snapshot_evidence(
        snapshot=snap, frozen_at=at, report_day=DAY, asof="2026-11-10",
        source=SOURCE_TOP_N, run_trace_id="run-pg-fail",
    )
    from datetime import date as _date

    saved = save_analysis(
        agent_name="premarket_outlook",
        stock_symbol="*",
        content=REPORT,
        title="t",
        raw_data={
            "timestamp": at,
            "run_trace_id": "run-pg-fail",
            "demon_factors": snap,
            "demon_factors_frozen_at": at,
            "demon_factors_source": SOURCE_TOP_N,
        },
        analysis_date=_date.fromisoformat(DAY),
        user_id=None,
        premarket_evidence=ev,
        report_completed_at=at,
    )
    assert saved.body_saved is True and saved.evidence_saved is False and saved.ok is False

    from sqlalchemy import text

    with patched.engine.connect() as conn:
        n_report = conn.execute(
            text("select count(*) from analysis_history where agent_name='premarket_outlook'")
        ).scalar_one()
        n_ev = conn.execute(text("select count(*) from premarket_factor_evidence")).scalar_one()
    assert n_report == 1 and n_ev == 0

    from src.web.api import decisions as dec

    item = dec.decisions_premarket(days=5, max_rows=30, user=cast(Any, _User()))["items"][0]
    assert item["preopen"] is True
    assert item["pit"]["evidence_source_kind"] == "raw_data"
    assert item["rows"][0]["signals"]["demon"]["score"] == 88.0


def test_pg_migration_247_248_adds_run_trace_id_and_is_idempotent(pg):
    from sqlalchemy import inspect, text

    from src.web.migrations import (
        _m247_premarket_factor_evidence,
        _m248_premarket_evidence_run_binding,
    )

    with pg.engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS premarket_factor_evidence"))
        _m247_premarket_factor_evidence(conn)
        _m248_premarket_evidence_run_binding(conn)
        _m248_premarket_evidence_run_binding(conn)  # 幂等
    cols = {c["name"] for c in inspect(pg.engine).get_columns("premarket_factor_evidence")}
    assert "run_trace_id" in cols

    # 再建表(模拟完整流程)后仍可重复执行
    with pg.engine.begin() as conn:
        _m247_premarket_factor_evidence(conn)
        _m248_premarket_evidence_run_binding(conn)
    assert "run_trace_id" in {
        c["name"] for c in inspect(pg.engine).get_columns("premarket_factor_evidence")
    }

    # 清干净, 让后面的用例从 ORM 表结构继续
    from src.db.models import PremarketFactorEvidence

    PremarketFactorEvidence.__table__.drop(pg.engine, checkfirst=True)
    PremarketFactorEvidence.__table__.create(pg.engine)
