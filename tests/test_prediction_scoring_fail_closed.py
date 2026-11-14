"""后验计分 fail-closed 闸门(2026-11-13)。

审计重现的问题: m250 的**四条部分唯一索引**缺失(或被人用同名但定义不同的索引顶替)时,
`schema_migrations.success=1` 仍是 1, 而评估/统计照旧出数 —— 同一个计分键可以有多条计分行,
命中率双计; 迁移"success"也不会自愈。

本文件钉住修好后的契约(逐条对应审计要求):

  A. 索引缺失 → 写入/回填/评估/个人统计**全部** `scoring_blocked`, 且**不动任何数据行**;
  B. 同名错定义索引(非唯一/列不全/无谓词)必须被识别为 `index_definition_mismatch`
     —— 只看名字的检查会漏;
  C. 安全状态下按需**幂等重建**(该谓词下无重复键才建); 有重复键则**不建**、不清理数据,
     继续阻断;
  D. 修复后重启(新引擎/新连接)看到 ok, 写入恢复; 重复体检不产生第二个索引;
  E. 缺索引时并发体检/重建收敛到"四条索引各一份", 数据不变;
  F. 状态检查与索引体检**不泄露其他用户**(不含 user_id/username/行内容);
  G. PG 与 SQLite 双样本: 两条 pending 行在阻断时**都还是 pending**(一条都没被回填)。
  H. 库**不可达 / 连接池超时**: 评估/写入/回填一律返回 `scoring_blocked`(或拒写),
    绝不把连接异常抛给调度方; reason/日志只报异常类型, 不含 SQL/绑定参数/连接串。

测试库纪律(与 test_prediction_outcome_ownership_integrity.py 一致): 每个用例一套
`tmp_path` 独立 SQLite; 起测清空 DATABASE_*/SIDA_DB*/DATA_DIR/PYTHONPATH; 断言连的就是
这次的临时库; 不碰生产库、不清任何既有数据。PG 用例在
`tests/test_prediction_outcome_user_scope_pg.py`(需要 `SIDA_TEST_PG_ADMIN` 的隔离库)。
"""

from __future__ import annotations

import json
import os
import threading
from datetime import date, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text as sqltext
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

DAY = (date.today() - timedelta(days=10)).isoformat()
DAY2 = (date.today() - timedelta(days=9)).isoformat()

_ACTIVE = "ux_prediction_outcome_user_active_key"
_RUN = "ux_prediction_outcome_user_run_key"
_SYS_ACTIVE = "ux_prediction_outcome_system_active_key"
_SYS_RUN = "ux_prediction_outcome_system_run_key"
_ALL = (_ACTIVE, _RUN, _SYS_ACTIVE, _SYS_RUN)


def _engine(tmp_path, name: str = "guard.db"):
    # This engine uses an explicit temporary SQLite URL; do not mutate process
    # environment here. Removing DATA_DIR leaks outside this test and sends later
    # tests' caches into the repository's real data directory.
    path = tmp_path / name
    assert not os.path.exists(path), "测试库必须是新建的, 不许复用既有库文件"
    eng = create_engine(f"sqlite:///{path}", future=True)
    assert str(eng.url).startswith("sqlite:///") and str(tmp_path) in str(eng.url)
    return eng


def _make_db(
    tmp_path,
    *,
    users=("u-a", "u-b"),
    rows: tuple[dict, ...] = (),
    drop_indexes: tuple[str, ...] = (),
):
    """create_all 建库(索引与生产同名的四条部分唯一索引都在) + 种子数据。"""
    import src.web.models as M  # noqa: F401 — 触发 ORM 注册
    from src.db.models import AgentPredictionOutcome, User
    from src.db.session import Base
    from src.core.json_safe import to_jsonable

    eng = _engine(tmp_path)
    Base.metadata.create_all(eng)
    Session = sessionmaker(bind=eng, future=True)
    # 先删索引再种数据: 模拟"索引缺失的老库", 这样重复键才写得进去(否则 DB 会先挡住)
    _drop(eng, *drop_indexes)
    with Session() as s:
        for uid in users:
            s.add(
                User(
                    id=uid,
                    username=f"account-{uid}",
                    password_hash="x",
                    role="member",
                    is_active=True,
                )
            )
        for r in rows:
            payload = dict(r)
            payload.setdefault("agent_name", "premarket_outlook")
            payload.setdefault("stock_symbol", "600183")
            payload.setdefault("stock_market", "CN")
            payload.setdefault("prediction_date", DAY)
            payload.setdefault("horizon_days", 1)
            payload.setdefault("action", "buy")
            payload.setdefault("action_label", "买入")
            payload.setdefault("outcome_status", "pending")
            payload.setdefault("owner_scope", "user")
            payload["meta"] = to_jsonable(payload.get("meta") or {})
            s.add(AgentPredictionOutcome(**payload))
        s.commit()
    return eng, Session


def _snapshot(eng) -> tuple[list[tuple], list[str]]:
    """整表快照(所有列, 按 id) + 全部索引名 —— 用来断言"一行都没变"。"""
    with eng.begin() as c:
        cols = [r[1] for r in c.execute(sqltext("PRAGMA table_info(agent_prediction_outcomes)"))]
        col_sql = ", ".join(cols)
        rows = [
            tuple(r)
            for r in c.execute(
                sqltext(f"SELECT {col_sql} FROM agent_prediction_outcomes ORDER BY id")
            ).fetchall()
        ]
        idx = sorted(
            r[0]
            for r in c.execute(
                sqltext("SELECT name FROM sqlite_master WHERE type='index' ORDER BY name")
            ).fetchall()
            if r[0]
        )
    return rows, idx


def _table_rows(eng) -> list[tuple]:
    return _snapshot(eng)[0]


def _index_names(eng) -> list[str]:
    return _snapshot(eng)[1]


def _drop(eng, *names: str) -> None:
    with eng.begin() as c:
        for n in names:
            c.execute(sqltext(f"DROP INDEX IF EXISTS {n}"))


def _index_ddl(eng, name: str) -> str | None:
    with eng.begin() as c:
        row = c.execute(
            sqltext("SELECT sql FROM sqlite_master WHERE type='index' AND name=:n"),
            {"n": name},
        ).first()
    return row[0] if row else None


def _save(**over):
    from src.core.context_store import save_agent_prediction_outcome

    kw = dict(
        agent_name="premarket_outlook",
        stock_symbol="600183",
        stock_market="CN",
        prediction_date=DAY,
        horizon_days=1,
        action="buy",
        action_label="买入",
        user_id="u-a",
        owner_scope="user",
        run_trace_id="run-1",
    )
    kw.update(over)
    return save_agent_prediction_outcome(**kw)


def test_engine_keeps_process_data_directory(tmp_path, monkeypatch):
    data_dir = str(tmp_path / "persistent-test-data")
    monkeypatch.setenv("DATA_DIR", data_dir)
    eng = _engine(tmp_path, "env_guard.db")
    try:
        assert os.environ["DATA_DIR"] == data_dir
    finally:
        eng.dispose()


def _wire(monkeypatch, eng, Session):
    """让 context_store 用这次临时库(写入路径的闸门也走同一个 bind)。"""
    import src.core.context_store as cs

    monkeypatch.setattr(cs, "SessionLocal", Session)
    return cs


# ── A/G. 索引缺失: 写入/回填/评估/统计全部阻断, 数据一行不动 ────────────────


def test_missing_index_blocks_every_scoring_path_and_changes_no_data(tmp_path, monkeypatch):
    from src.core.scoring_guard import check_scoring_health

    eng, Session = _make_db(
        tmp_path,
        rows=(
            {"user_id": "u-a", "run_trace_id": "run-1"},
            {"user_id": "u-a", "run_trace_id": "run-2", "stock_symbol": "600519",
             "prediction_date": DAY2},
        ),
    )
    _wire(monkeypatch, eng, Session)
    _drop(eng, _ACTIVE, _RUN)

    before = _snapshot(eng)

    # 写入: 拒写(不"先写着看")
    res = _save(run_trace_id="run-3")
    assert res.written is False and res.status == "scoring_blocked"
    assert "index_missing" in res.reason

    # 回填: 拒绝(此刻"这是唯一计分行"本身无法证明)
    from src.core.context_store import mark_agent_prediction_outcome

    assert mark_agent_prediction_outcome(record_id=1, outcome_price=40.0, outcome_return_pct=9.9) is False

    # 评估: 返回 blocked 骨架, evaluated=0, 一条都不回填
    # 个人统计: 不出数字, 只给 mark 与原因
    from src.web.api.profile import _accuracy_board

    with Session() as s:
        board = _accuracy_board(s, since="", user_id="u-a")
    assert board["scoring_blocked"] is True
    assert board["overall"]["hit_rate"] is None and board["overall"]["total"] == 0
    assert board["agents"] == []
    assert board["block_reason_code"].startswith("index_missing")
    assert "scoring_blocked" in board["note"]

    # /stats 的 prediction 也不能出现命中率
    from src.web.api.profile import _prediction_hit_stats

    with Session() as s:
        overall = _prediction_hit_stats(s, user_id="u-a")
    assert overall["scoring_blocked"] is True and overall["hit_rate"] is None
    assert overall["total"] == 0

    # 数据一行都没变(包括状态: 两条都还是 pending)
    after = _snapshot(eng)
    assert after[0] == before[0], "阻断路径不允许改/删/回填任何行"
    with eng.begin() as c:
        statuses = [
            r[0]
            for r in c.execute(
                sqltext(
                    "SELECT outcome_status FROM agent_prediction_outcomes ORDER BY id"
                )
            ).fetchall()
        ]
    assert statuses == ["pending", "pending"], "阻断时一条都不许被回填成 evaluated"
    # 只读路径不允许建索引
    assert after[1] == before[1], "只读巡检不得偷偷补索引"


def test_evaluation_only_checks_never_ddl_and_keeps_scoring_blocked(tmp_path, monkeypatch):
    """运行期评估**只检查、不 DDL**: 缺索引时直接 blocked, 不补索引、不回填、数据不变。"""
    from src.core import scoring_guard as sg
    from src.core.prediction_outcome import evaluate_pending_prediction_outcomes

    eng, Session = _make_db(tmp_path, rows=({"user_id": "u-a", "run_trace_id": "run-1"},))
    _wire(monkeypatch, eng, Session)
    _drop(eng, _ACTIVE, _RUN)
    before = _snapshot(eng)

    checks = {"n": 0}
    real_check = sg.check_scoring_health

    def _counting_check(*args, **kwargs):
        checks["n"] += 1
        assert kwargs.get("allow_repair") is not True, "运行期评估不得请求修复(DDL)"
        return real_check(*args, **kwargs)

    monkeypatch.setattr(sg, "check_scoring_health", _counting_check)

    stats = evaluate_pending_prediction_outcomes()
    assert stats["scoring_blocked"] is True
    assert stats["evaluated"] == 0 and stats["eligible"] == 0
    assert stats["total_pending"] == 1
    assert "index_missing" in stats["scoring_status"]["reason_code"]
    assert checks["n"] == 1, "批次边界只检查一次"
    # 只读路径不允许建索引, 数据一行不动
    assert _index_ddl(eng, _ACTIVE) is None
    assert _snapshot(eng) == before


def test_batch_evaluation_reuses_one_session_and_one_check(tmp_path, monkeypatch):
    """批量评估复用同一会话/只检查一次; 小连接池(1)跑 20 行不因按行新建连接而耗尽。"""
    import types

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import QueuePool

    import src.core.context_store as cs
    import src.core.prediction_outcome as po
    from src.core import scoring_guard as sg

    n_rows = 20
    rows = tuple(
        {
            "user_id": "u-a",
            "run_trace_id": f"run-{i}",
            "stock_symbol": f"{600000 + i}",
            "prediction_date": DAY,
            "horizon_days": 1,
            "trigger_price": 100.0,
        }
        for i in range(n_rows)
    )
    eng, _ = _make_db(tmp_path, rows=rows)
    # 全新 engine: 连接池只有 1 个连接, 按行新建连接就会超时
    pooled = create_engine(
        str(eng.url),
        future=True,
        poolclass=QueuePool,
        pool_size=1,
        max_overflow=0,
        pool_timeout=0.2,
    )
    Session = sessionmaker(bind=pooled, future=True)

    factory_calls = {"n": 0}

    def _factory(*a, **kw):
        factory_calls["n"] += 1
        return Session(*a, **kw)

    monkeypatch.setattr(cs, "SessionLocal", _factory)

    checks = {"n": 0}
    real_check = sg.check_scoring_health

    def _counting_check(*args, **kwargs):
        checks["n"] += 1
        return real_check(*args, **kwargs)

    monkeypatch.setattr(sg, "check_scoring_health", _counting_check)

    class _FakeKline:
        def __init__(self, symbol=None, days=None):
            pass

        def get_klines(self, symbol, days=120):
            target = po._add_trading_days(date.fromisoformat(DAY), 1).isoformat()
            return [types.SimpleNamespace(date=target, close=101.0)]

    monkeypatch.setattr(po, "KlineCollector", _FakeKline)

    stats = po.evaluate_pending_prediction_outcomes(limit=100)
    assert stats["scoring_blocked"] is False, stats
    assert stats["evaluated"] == n_rows, stats
    assert checks["n"] == 1, "批次只允许检查一次评分闸门"
    assert factory_calls["n"] == 1, "批次应复用同一个会话(不按行新建)"
    with eng.begin() as c:
        evaluated = c.execute(
            sqltext(
                "SELECT COUNT(*) FROM agent_prediction_outcomes "
                "WHERE outcome_status = 'evaluated'"
            )
        ).scalar()
    assert evaluated == n_rows


def test_readonly_check_never_creates_indexes(tmp_path, monkeypatch):
    from src.core.scoring_guard import check_scoring_health

    eng, Session = _make_db(tmp_path)
    _wire(monkeypatch, eng, Session)
    _drop(eng, _ALL[0])
    health = check_scoring_health(eng, allow_repair=False)
    assert health.blocked is True
    assert _index_ddl(eng, _ACTIVE) is None, "GET/统计路径不能因为有人刷新就做 DDL"


# ── B. 伪造成同名索引: 必须按定义识别, 不能只看名字 ────────────────────────


def test_forged_same_name_index_is_detected_then_rebuilt(tmp_path, monkeypatch):
    from src.core.scoring_guard import check_scoring_health

    eng, Session = _make_db(tmp_path, rows=({"user_id": "u-a", "run_trace_id": "run-1"},))
    _wire(monkeypatch, eng, Session)
    _drop(eng, _ACTIVE)
    # 同名、但非唯一 + 列不全 + 没有谓词 —— 名字对得上, 语义完全不对
    with eng.begin() as c:
        c.execute(
            sqltext(
                f"CREATE INDEX {_ACTIVE} ON agent_prediction_outcomes (user_id)"
            )
        )

    health = check_scoring_health(eng, allow_repair=False)
    assert health.blocked is True
    assert "index_definition_mismatch" in health.reason_code
    state = {i.name: i for i in health.indexes}[_ACTIVE]
    assert state.status == "mismatch"
    assert "唯一" in state.detail
    before = _table_rows(eng)

    # 写入同样被挡(约束不在位)
    assert _save(run_trace_id="run-2").status == "scoring_blocked"

    # 按需重建: 丢掉错定义的同名索引(只删索引) → 建正确的
    fixed = check_scoring_health(eng, allow_repair=True)
    assert fixed.blocked is False, fixed.detail
    assert _ACTIVE in fixed.repaired
    ddl = _index_ddl(eng, _ACTIVE)
    assert ddl and "UNIQUE" in ddl.upper()
    assert "outcome_status <> 'superseded'" in ddl
    assert _table_rows(eng) == before, "重建索引不得碰数据"

    # 新键(不同标的/日期)才是真插入; 同键只会按契约追加 superseded 审计版本
    assert _save(run_trace_id="run-2", stock_symbol="600519", prediction_date=DAY2).status == "inserted"


def test_same_name_unique_index_with_wrong_predicate_is_a_mismatch(tmp_path, monkeypatch):
    """唯一 + 列对, 但谓词少了 `outcome_status <> 'superseded'` → 仍算定义错误。"""
    from src.core.scoring_guard import check_scoring_health

    eng, Session = _make_db(tmp_path)
    _wire(monkeypatch, eng, Session)
    _drop(eng, _RUN)
    with eng.begin() as c:
        c.execute(
            sqltext(
                f"CREATE UNIQUE INDEX {_RUN} ON agent_prediction_outcomes "
                "(user_id, agent_name, stock_symbol, stock_market, prediction_date, "
                "horizon_days, run_trace_id)"
            )
        )
    health = check_scoring_health(eng, allow_repair=False)
    assert health.blocked is True
    state = {i.name: i for i in health.indexes}[_RUN]
    assert state.status == "mismatch" and "谓词" in state.detail
    assert check_scoring_health(eng, allow_repair=True).blocked is False


def test_sql_literal_case_is_semantic_not_noise(tmp_path, monkeypatch):
    """'Superseded' / 'SYSTEM' 不是 'superseded' / 'system': 大小写不同的同名索引必须
    判 mismatch(旧实现把整段谓词小写, 会假通过)。重建后必须写回小写字面量。"""
    from src.core.scoring_guard import check_scoring_health

    eng, Session = _make_db(tmp_path, rows=({"user_id": "u-a", "run_trace_id": "run-1"},))
    _wire(monkeypatch, eng, Session)

    cases = (
        (
            _ACTIVE,
            "user_id, agent_name, stock_symbol, stock_market, prediction_date, horizon_days",
            "user_id IS NOT NULL AND outcome_status <> 'Superseded'",
        ),
        (
            _SYS_ACTIVE,
            "agent_name, stock_symbol, stock_market, prediction_date, horizon_days",
            "owner_scope = 'SYSTEM' AND outcome_status <> 'superseded'",
        ),
    )
    for name, cols, where in cases:
        _drop(eng, name)
        with eng.begin() as c:
            c.execute(
                sqltext(f"CREATE UNIQUE INDEX {name} ON agent_prediction_outcomes ({cols}) WHERE {where}")
            )
        before = _table_rows(eng)
        health = check_scoring_health(eng, allow_repair=False)
        state = {i.name: i for i in health.indexes}[name]
        assert health.blocked is True, health.detail
        assert state.status == "mismatch" and "谓词" in state.detail, state
        # 只读路径不 DDL
        assert _index_ddl(eng, name) == (
            f"CREATE UNIQUE INDEX {name} ON agent_prediction_outcomes ({cols}) WHERE {where}"
        )

        fixed = check_scoring_health(eng, allow_repair=True)
        assert fixed.blocked is False, fixed.detail
        rebuilt = _index_ddl(eng, name) or ""
        assert "'Superseded'" not in rebuilt and "'SYSTEM'" not in rebuilt
        assert "'superseded'" in rebuilt
        assert _table_rows(eng) == before, "重建只碰索引, 不改任何数据行"


def test_predicate_with_or_is_never_treated_as_equivalent(tmp_path, monkeypatch):
    """抹平括号后做字符串比较的另一个 fail-open: 含 OR 的谓词不得与纯 AND 规范等价。"""
    from src.core.scoring_guard import check_scoring_health

    eng, Session = _make_db(tmp_path)
    _wire(monkeypatch, eng, Session)
    _drop(eng, _ACTIVE)
    with eng.begin() as c:
        c.execute(
            sqltext(
                f"CREATE UNIQUE INDEX {_ACTIVE} ON agent_prediction_outcomes "
                "(user_id, agent_name, stock_symbol, stock_market, prediction_date, horizon_days) "
                "WHERE (user_id IS NOT NULL AND outcome_status <> 'superseded') OR (user_id IS NULL)"
            )
        )
    health = check_scoring_health(eng, allow_repair=False)
    state = {i.name: i for i in health.indexes}[_ACTIVE]
    assert health.blocked is True
    assert state.status == "mismatch" and "谓词" in state.detail


def test_same_name_index_on_another_table_is_wrong_table_and_never_dropped(tmp_path, monkeypatch):
    """同名索引建在别的表上: 诊断 wrong table; 修复路径**绝不**删别人的索引, 只阻断。"""
    from src.core.scoring_guard import check_scoring_health

    eng, Session = _make_db(tmp_path, rows=({"user_id": "u-a", "run_trace_id": "run-1"},))
    _wire(monkeypatch, eng, Session)
    _drop(eng, _ACTIVE)
    other_ddl = (
        f"CREATE UNIQUE INDEX {_ACTIVE} ON other_prediction_outcomes "
        "(user_id, agent_name, stock_symbol, stock_market, prediction_date, horizon_days) "
        "WHERE user_id IS NOT NULL AND outcome_status <> 'superseded'"
    )
    with eng.begin() as c:
        c.execute(
            sqltext(
                "CREATE TABLE other_prediction_outcomes ("
                "user_id TEXT, agent_name TEXT, stock_symbol TEXT, stock_market TEXT, "
                "prediction_date TEXT, horizon_days INTEGER, outcome_status TEXT)"
            )
        )
        c.execute(sqltext(other_ddl))
    before_rows = _table_rows(eng)

    health = check_scoring_health(eng, allow_repair=False)
    state = {i.name: i for i in health.indexes}[_ACTIVE]
    assert health.blocked is True
    assert state.status == "wrong_table" and "别的表" in state.detail
    assert "index_wrong_table" in health.reason_code

    # 有权修复也不能删别的表的索引; 目标表保持缺失并继续阻断
    repaired = check_scoring_health(eng, allow_repair=True)
    assert repaired.blocked is True
    assert _index_ddl(eng, _ACTIVE) == other_ddl, "不得删除/改写其他表的索引"
    with eng.begin() as c:
        on_target = c.execute(
            sqltext(
                "SELECT 1 FROM sqlite_master WHERE type='index' AND name=:n "
                "AND tbl_name='agent_prediction_outcomes'"
            ),
            {"n": _ACTIVE},
        ).first()
    assert on_target is None, "名字被别的表占用, 不得在本表建同名索引"
    assert _table_rows(eng) == before_rows


def test_check_failure_exposes_generic_reason_and_logs_no_sql(tmp_path, monkeypatch, caplog):
    """检查本身抛错: 对外只报 check_failed(类型/SQLSTATE 白名单), 不回显 SQL/参数原文。"""
    import json as _json
    import logging

    from src.core import scoring_guard as sg

    eng, Session = _make_db(tmp_path)
    secret = "PARAM_SECRET_9f3a"
    real = sg._all_index_ddls

    def _boom(conn):
        raise RuntimeError(f"SELECT * FROM agent_prediction_outcomes WHERE x='{secret}'")

    monkeypatch.setattr(sg, "_all_index_ddls", _boom)
    with caplog.at_level(logging.ERROR, logger="src.core.scoring_guard"):
        health = sg.check_scoring_health(eng, allow_repair=False)
    monkeypatch.setattr(sg, "_all_index_ddls", real)

    assert health.blocked is True and health.reason_code == "check_failed"
    blob = _json.dumps(health.as_payload(), ensure_ascii=False)
    for leaked in (secret, "SELECT", "agent_prediction_outcomes WHERE"):
        assert leaked not in blob, f"对外原因泄露了 SQL/参数: {leaked}"
    assert secret not in caplog.text and "SELECT *" not in caplog.text


# ── C. 有重复键则不建索引、不清理数据, 继续阻断 ───────────────────────────


def test_duplicate_rows_prevent_rebuild_and_keep_scoring_blocked(tmp_path, monkeypatch):
    from src.core.scoring_guard import check_scoring_health

    # 先在"没有唯一索引"的库上种出同键两条计分行(历史脏数据的真实来历)
    eng, Session = _make_db(
        tmp_path,
        rows=(
            {"user_id": "u-a", "run_trace_id": "v1", "action": "buy"},
            {"user_id": "u-a", "run_trace_id": "v2", "action": "sell"},
        ),
        drop_indexes=(_ACTIVE, _RUN),
    )
    _wire(monkeypatch, eng, Session)
    before = _snapshot(eng)

    health = check_scoring_health(eng, allow_repair=True)
    assert health.blocked is True
    assert "duplicate_rows" in health.reason_code
    assert _ACTIVE in health.duplicate_blocked
    assert "重复键组" in health.detail
    # 契约: 有重复 → 不建索引、不删不改
    assert _index_ddl(eng, _ACTIVE) is None
    after = _snapshot(eng)
    assert after[0] == before[0], "有重复键也不许删/改行"

    # 另外两条 system 索引(无重复)该建的还是建了 —— 阻断不搞连坐
    assert _index_ddl(eng, _SYS_ACTIVE) is not None
    # run_key 谓词下这两行版本不同(run_trace_id 不同), 没有重复 → 它照建
    assert _index_ddl(eng, _RUN) is not None

    # 统计/写入继续阻断
    assert _save(run_trace_id="run-9").status == "scoring_blocked"
    from src.core.prediction_outcome import evaluate_pending_prediction_outcomes

    stats = evaluate_pending_prediction_outcomes()
    assert stats["scoring_blocked"] is True and stats["evaluated"] == 0
    assert _table_rows(eng) == before[0]


# ── D. 修复后"重启"看到 ok; 重复体检幂等 ─────────────────────────────────


def test_repair_then_restart_is_ok_and_idempotent(tmp_path, monkeypatch):
    from src.core.scoring_guard import check_scoring_health

    eng, Session = _make_db(tmp_path, rows=({"user_id": "u-a", "run_trace_id": "run-1"},))
    _wire(monkeypatch, eng, Session)
    _drop(eng, *_ALL)
    rows_before = _table_rows(eng)

    first = check_scoring_health(eng, allow_repair=True)
    assert first.blocked is False
    assert sorted(first.repaired) == sorted(_ALL)
    idx_after_first = _index_names(eng)

    # 幂等: 再体检一次不再建第二份、也不报 repaired
    second = check_scoring_health(eng, allow_repair=True)
    assert second.blocked is False and second.repaired == ()
    assert _index_names(eng) == idx_after_first
    counts = {n: idx_after_first.count(n) for n in _ALL}
    assert counts == {n: 1 for n in _ALL}

    # "重启": 全新引擎/连接池(不共享任何进程内状态) → 立刻看到 ok
    fresh = create_engine(str(eng.url), future=True)
    try:
        brand_new = check_scoring_health(fresh, allow_repair=False)
        assert brand_new.blocked is False, brand_new.detail
        assert all(i.status == "ok" for i in brand_new.indexes)
        assert _table_rows(fresh) == rows_before, "重建索引不得改数据"
    finally:
        fresh.dispose()

    # 写入恢复(数据只多出这一行; 必须是新键 —— 同键按契约只会追加 superseded)
    assert _save(run_trace_id="run-2", stock_symbol="600519", prediction_date=DAY2).status == "inserted"
    assert len(_table_rows(eng)) == len(rows_before) + 1


# ── E. 缺索引时并发体检/重建收敛 ─────────────────────────────────────────


def test_concurrent_repair_converges_to_one_index_each(tmp_path, monkeypatch):
    from src.core.scoring_guard import check_scoring_health

    eng, Session = _make_db(tmp_path, rows=({"user_id": "u-a", "run_trace_id": "run-1"},))
    _wire(monkeypatch, eng, Session)
    _drop(eng, *_ALL)
    before = _snapshot(eng)

    results: list[tuple[bool, tuple[str, ...]]] = []
    lock = threading.Lock()
    barrier = threading.Barrier(4)

    def worker():
        barrier.wait()
        h = check_scoring_health(eng, allow_repair=True)
        with lock:
            results.append((h.blocked, h.repaired))

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)

    # 并发下允许某个线程"这次没建成"(锁冲突), 但**最终状态**必须收敛且只有一个线程报修复
    final = check_scoring_health(eng, allow_repair=False)
    assert final.blocked is False, final.detail
    names = _index_names(eng)
    assert {n: names.count(n) for n in _ALL} == {n: 1 for n in _ALL}
    assert _table_rows(eng) == before[0], "并发重建不得改数据"
    assert sum(1 for blocked, _ in results if not blocked) >= 1


# ── F. 状态检查不泄露其他用户 ────────────────────────────────────────────


def test_health_payload_contains_no_user_data(tmp_path, monkeypatch):
    from src.core.scoring_guard import check_scoring_health

    eng, Session = _make_db(
        tmp_path,
        users=("u-a", "u-secret-uid-0001"),
        rows=(
            {"user_id": "u-a", "run_trace_id": "run-1"},
            {"user_id": "u-secret-uid-0001", "run_trace_id": "run-2", "stock_symbol": "000001"},
        ),
    )
    _wire(monkeypatch, eng, Session)
    _drop(eng, _ACTIVE)

    payload = check_scoring_health(eng, allow_repair=False).as_payload()
    assert payload["contains_user_data"] is False
    assert set(payload) == {
        "status",
        "scoring_blocked",
        "reason_code",
        "reason",
        "indexes",
        "repaired",
        "duplicate_blocked",
        "table",
        "contains_user_data",
    }
    blob = json.dumps(payload, ensure_ascii=False)
    for leaked in ("u-a", "u-secret-uid-0001", "account-u-a", "600183", "000001"):
        assert leaked not in blob, f"状态体检泄露了用户数据: {leaked}"
    # 聚合计数里也不带具体键
    assert "duplicate_keys" not in payload


def test_scoring_status_endpoint_is_identical_for_all_users_and_leaks_nothing(
    tmp_path, monkeypatch
):
    from src.web.api import profile as profile_api
    from src.web.api.auth import get_current_user
    from src.web.database import get_db
    from src.web.models import User

    eng, Session = _make_db(
        tmp_path,
        users=("u-a", "u-b"),
        rows=(
            {"user_id": "u-a", "run_trace_id": "run-1"},
            {"user_id": "u-b", "run_trace_id": "run-2", "stock_symbol": "600519"},
        ),
    )
    _drop(eng, _ACTIVE)

    class _U:
        def __init__(self, uid):
            self.id = uid
            self.username = f"account-{uid}"
            self.role = "member"
            self.nickname = None
            self.avatar = None
            self.shadow_profile_json = None

    def _build(user_id: str) -> TestClient:
        app = FastAPI()
        app.include_router(profile_api.router, prefix="/p")

        def _db():
            s = Session()
            try:
                yield s
            finally:
                s.close()

        async def _u():
            return _U(user_id)

        app.dependency_overrides[get_db] = _db
        app.dependency_overrides[get_current_user] = _u
        return TestClient(app, raise_server_exceptions=False)

    r_a = _build("u-a").get("/p/scoring/status")
    r_b = _build("u-b").get("/p/scoring/status")
    assert r_a.status_code == 200 and r_b.status_code == 200
    body = r_a.json()
    assert body["scoring_blocked"] is True and body["reason_code"] == "index_missing"
    assert body == r_b.json(), "schema 体检与登录用户无关(不是按用户过滤的数据)"
    for leaked in ("u-a", "u-b", "account-u-a", "account-u-b", "600183", "600519"):
        assert leaked not in r_a.text and leaked not in r_b.text

    # /stats 也不能出现别人的任何东西, 且命中率为 None
    s_body = _build("u-b").get("/p/stats").json()["prediction"]
    assert s_body["scoring_blocked"] is True and s_body["hit_rate"] is None
    assert "u-a" not in json.dumps(s_body, ensure_ascii=False)


# ── 导出: 阻断时"计分"列不得声称"是" ─────────────────────────────────────


def test_export_marks_predictions_as_not_scored_when_blocked(tmp_path, monkeypatch):
    from src.web.api import export as export_api
    from src.web.api.auth import get_current_user
    from src.web.database import get_db

    from src.db.models import AgentPredictionOutcome

    eng, Session = _make_db(tmp_path, rows=({"user_id": "u-a", "run_trace_id": "run-1"},))
    with Session() as s:
        row = s.query(AgentPredictionOutcome).first()
        row.outcome_status = "evaluated"
        row.outcome_return_pct = 3.2
        s.commit()
    _drop(eng, _ACTIVE)

    class _U:
        id = "u-a"
        username = "account-u-a"
        role = "member"
        nickname = None
        avatar = None
        shadow_profile_json = None

    app = FastAPI()
    app.include_router(export_api.router, prefix="/api")

    def _db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    async def _u():
        return _U()

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_current_user] = _u
    c = TestClient(app, raise_server_exceptions=False)

    r = c.get("/api/export/predictions")
    assert r.status_code == 200, r.text
    lines = [ln for ln in r.text.splitlines() if ln.strip()]
    header = lines[0].lstrip("\ufeff")
    assert "计分" in header
    assert "否(评分阻断)" in r.text
    assert ",是" not in r.text.replace("否(评分阻断)", "")


# ── H. 库不可达 / 连接池超时: 一律 fail-closed, 不外抛、不泄露原文 ─────────────
#
# 审计遗留(2026-11-13): 评估路径原先把 `db.connection()` 直接当实参传给闸门 —— 库不可达
# 或池超时时它先抛异常, 异常一路冒到调度方: 既没有 `scoring_blocked`, 也就没有"不评估/
# 不回填"的承诺。下面用**真实**不可达的临时库、真实池超时、真实 IntegrityError 钉住:
# 对外只报类别, 绝不回显 SQL/绑定参数/连接串/列值。


def _unreachable_engine(tmp_path):
    """真实不可达的临时库: sqlite 指向**不存在**的子目录, 驱动 connect 时直接失败。"""
    ghost = tmp_path / "no_such_dir" / "guard.db"
    assert not ghost.parent.exists(), "测试前提: 库文件的父目录不存在"
    eng = create_engine(f"sqlite:///{ghost}", future=True)
    assert str(tmp_path) in str(eng.url)
    return eng


def test_evaluation_unreachable_db_returns_scoring_blocked_not_exception(tmp_path, monkeypatch):
    """真·临时库不可达: 评估返回 blocked 骨架(不是抛异常), 条数如实报 -1。"""
    import src.core.context_store as cs
    from src.core.prediction_outcome import evaluate_pending_prediction_outcomes

    eng = _unreachable_engine(tmp_path)
    # 证明它**真的**连不上(不是靠 monkeypatch 假装的)
    with pytest.raises(Exception):
        with eng.connect() as c:
            c.execute(sqltext("SELECT 1"))

    monkeypatch.setattr(cs, "SessionLocal", sessionmaker(bind=eng, future=True))

    stats = evaluate_pending_prediction_outcomes()
    assert stats["scoring_blocked"] is True
    assert stats["evaluated"] == 0 and stats["eligible"] == 0
    assert stats["total_pending"] == -1, "连条数都读不到时必须报 -1, 不能假装是 0"
    assert stats["scoring_status"]["reason_code"] in {"unavailable", "check_failed"}
    blob = json.dumps(stats, ensure_ascii=False, default=str)
    for leaked in ("unable to open database file", "no_such_dir", "guard.db", "SELECT"):
        assert leaked not in blob, f"对外结果泄露了驱动原文/路径: {leaked}"


def test_evaluation_session_factory_failure_is_blocked_and_sanitized(
    tmp_path, monkeypatch, caplog
):
    """连会话都建不出来(引擎/DSN 配错): 同样 blocked, 连接串与口令不进结果/日志。"""
    import logging

    import src.core.context_store as cs
    from src.core.prediction_outcome import evaluate_pending_prediction_outcomes

    secret = "PARAM_SECRET_factory_7c1"

    def _boom(*_a, **_k):
        raise RuntimeError(f"could not connect: postgresql://acct:{secret}@127.0.0.1:5432/sida")

    monkeypatch.setattr(cs, "SessionLocal", _boom)
    with caplog.at_level(logging.ERROR, logger="src.core.prediction_outcome"):
        stats = evaluate_pending_prediction_outcomes()

    assert stats["scoring_blocked"] is True and stats["total_pending"] == -1
    blob = json.dumps(stats, ensure_ascii=False, default=str)
    assert secret not in blob and "postgresql://" not in blob
    assert secret not in caplog.text and "postgresql://" not in caplog.text


def test_evaluation_pool_timeout_is_blocked_not_exception(tmp_path, monkeypatch):
    """真实连接池超时(池只有 1 个连接且被占满): 评估必须 blocked, 且不挂死/不抛错。"""
    from sqlalchemy.pool import QueuePool

    import src.core.context_store as cs
    from src.core.prediction_outcome import evaluate_pending_prediction_outcomes

    eng, _ = _make_db(tmp_path, rows=({"user_id": "u-a", "run_trace_id": "run-1"},))
    pooled = create_engine(
        str(eng.url),
        future=True,
        poolclass=QueuePool,
        pool_size=1,
        max_overflow=0,
        pool_timeout=0.2,
    )
    monkeypatch.setattr(cs, "SessionLocal", sessionmaker(bind=pooled, future=True))
    holder = pooled.connect()  # 占满唯一的池连接
    try:
        stats = evaluate_pending_prediction_outcomes()
    finally:
        holder.close()
        pooled.dispose()

    assert stats["scoring_blocked"] is True
    assert stats["evaluated"] == 0 and stats["total_pending"] == -1
    assert stats["scoring_status"]["reason_code"] in {"unavailable", "check_failed"}
    blob = json.dumps(stats, ensure_ascii=False, default=str)
    # 只允许"异常类型名"这种分类; 池内部细节(SQL/配置/语句)一律不许出现。
    for leaked in ("QueuePool limit", "TimeoutError: ", "SELECT", "pool_size", "sqlite:///"):
        assert leaked not in blob, f"对外结果泄露了池内部细节: {leaked}"
    # 数据一行不动(仍是 pending)
    with eng.begin() as c:
        statuses = [
            r[0]
            for r in c.execute(
                sqltext("SELECT outcome_status FROM agent_prediction_outcomes ORDER BY id")
            ).fetchall()
        ]
    assert statuses == ["pending"]


def test_evaluation_pg_driver_connection_refused_is_blocked_not_exception(monkeypatch, caplog):
    """生产同款驱动(psycopg2)真实连接被拒: 评估必须 blocked, 不泄露 DSN/驱动原文。

    不连任何既有库: 指向本机保留端口(1), 连接立刻被拒 —— 这是真实的驱动级失败,
    不是 monkeypatch 出来的异常。
    """
    import logging

    import src.core.context_store as cs

    pytest.importorskip("psycopg2")
    from src.core.prediction_outcome import evaluate_pending_prediction_outcomes

    secret = "PARAM_SECRET_pg_5a2"
    eng = create_engine(
        f"postgresql+psycopg2://acct:{secret}@127.0.0.1:1/sida",
        future=True,
        connect_args={"connect_timeout": 3},
    )
    with pytest.raises(Exception):
        with eng.connect() as c:
            c.execute(sqltext("SELECT 1"))
    monkeypatch.setattr(cs, "SessionLocal", sessionmaker(bind=eng, future=True))
    with caplog.at_level(logging.ERROR, logger="src.core.prediction_outcome"):
        stats = evaluate_pending_prediction_outcomes()
    eng.dispose()

    assert stats["scoring_blocked"] is True
    assert stats["total_pending"] == -1 and stats["evaluated"] == 0
    assert stats["scoring_status"]["reason_code"] in {"unavailable", "check_failed"}
    blob = json.dumps(stats, ensure_ascii=False, default=str)
    for leaked in (secret, "postgresql://", "127.0.0.1", "psycopg", "connection to server"):
        assert leaked not in blob, f"对外结果泄露了 DSN/驱动原文: {leaked}"
    for leaked in (secret, "postgresql://", "psycopg"):
        assert leaked not in caplog.text, f"日志泄露了 DSN/驱动原文: {leaked}"


def test_write_and_backfill_fail_closed_when_db_unreachable(tmp_path, monkeypatch):
    """写入/回填在库不可达时: 拒写(不抛异常), 对外原因是通用类别而非驱动原文。"""
    import src.core.context_store as cs
    from src.core.context_store import mark_agent_prediction_outcome

    eng = _unreachable_engine(tmp_path)
    monkeypatch.setattr(cs, "SessionLocal", sessionmaker(bind=eng, future=True))

    res = _save(run_trace_id="run-unreachable")
    assert res.written is False
    assert res.status == "scoring_blocked", res
    assert "评分阻断" in res.reason
    for leaked in ("unable to open", "no_such_dir", "SELECT", "sqlite"):
        assert leaked not in res.reason, f"拒写原因泄露了驱动原文: {leaked}"

    assert (
        mark_agent_prediction_outcome(
            record_id=1, outcome_price=1.0, outcome_return_pct=1.0
        )
        is False
    )


def test_integrity_error_never_leaks_sql_or_user_values(tmp_path, monkeypatch, caplog):
    """故障注入: 真实 IntegrityError(带 SQL/绑定参数/DETAIL 列值)不进 reason 也不进日志。"""
    import logging

    from sqlalchemy.exc import IntegrityError

    import src.core.context_store as cs

    eng, Session = _make_db(tmp_path)
    secret = "u-secret-uid-0001"

    def _factory(orig_msg: str):
        class _Orig(Exception):
            pass

        class _Failing:
            def __init__(self):
                self._inner = Session()

            def add(self, obj):
                return self._inner.add(obj)

            def commit(self):
                raise IntegrityError(
                    "INSERT INTO agent_prediction_outcomes (user_id) VALUES (%(user_id)s)",
                    {"user_id": secret},
                    _Orig(orig_msg),
                )

            def rollback(self):
                return self._inner.rollback()

            def query(self, *a, **k):
                return self._inner.query(*a, **k)

            @property
            def bind(self):
                return self._inner.bind

            def connection(self):
                return self._inner.connection()

            def close(self):
                return self._inner.close()

        return _Failing

    cases = (
        (
            "insert or update on table \"agent_prediction_outcomes\" violates foreign key "
            f"constraint \"apo_user_id_fkey\"\nDETAIL:  Key (user_id)=({secret}) is not "
            "present in table \"users\".",
            "refused_unknown_user",
            "归属用户不存在(user_id 外键校验失败)",
        ),
        (
            "new row for relation \"agent_prediction_outcomes\" violates check constraint "
            "\"ck_apo_horizon\"\n"
            f"DETAIL:  Failing row contains (42, premarket_outlook, 600183, {secret}, 2026-11-13, 0).",
            "write_failed",
            "完整性约束(other)",
        ),
    )
    for msg, expect_status, expect_reason in cases:
        monkeypatch.setattr(cs, "SessionLocal", _factory(msg))
        caplog.clear()
        with caplog.at_level(logging.ERROR, logger="src.core.context_store"):
            res = _save(run_trace_id="run-integrity")
        assert res.status == expect_status, res
        assert res.reason == expect_reason, res.reason
        for leaked in (secret, "DETAIL", "INSERT INTO", "Failing row", "constraint \""):
            assert leaked not in res.reason, f"对外原因泄露: {leaked}"
            assert leaked not in caplog.text, f"日志泄露: {leaked}"
    assert _table_rows(eng) == [], "失败路径不得留下半条数据"


def test_error_kind_only_accepts_real_sqlstate_codes():
    """异常对象里的非 SQLSTATE 内容(可被拼成 SQL/文本)不得当分类码回显。"""
    from sqlalchemy.exc import IntegrityError

    import src.core.context_store as cs

    class _Orig(Exception):
        pass

    orig = _Orig("boom")
    orig.pgcode = "DROP TABLE users; --"
    assert cs._db_error_kind(IntegrityError("SQL", {}, orig)) == "other"
