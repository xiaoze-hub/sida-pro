"""盘前后验的跨账户覆盖 P0 —— 用户隔离 / 版本追加 / system 独立桶 / 真错误分型。

审计复现的真实故障(2026-11-12):
  `agent_prediction_outcomes` 没有 user_id, 行键是 (agent, symbol, market, date, horizon)。
  上一版把"同日重跑"实现成按这个键**原地更新 pending 行**, 于是:
    - 两个真实账户同标的同日 → 撞同一个键, 后跑的账号把对方那行改掉(动作/置信度/触发价/meta);
    - 已评估行虽然不动, 但函数仍然 `return True` → 上层把"没写"报成"保存成功";
    - 历史库里同键多行时, `order_by(id).first()` 只挑一行改, 另一行原样留着继续污染统计。
  上一轮修复又出现两个缺口:
    - 同用户同日**后续版本被整条拒写** → 版本信息丢失(不是"保留"而是"没存");
    - 无会话的系统运行无处可去 / 容易被猜成某个真实用户;
    - `except IntegrityError` 一律当"并发重复", 外键/其它完整性错误被误分类;
    - migration 建了 `ix_agent_prediction_outcomes_user_id` 而模型声明 `ix_prediction_outcome_user`,
      新建库与迁移库的索引名/语义不一致。

本文件钉住修复后的契约:
  1. 两个真实用户同标的同日各写各的行, 任一方都不改对方的行;
  2. 同一用户同键: **一条计分行 + 追加的 superseded 审计版本**; 计分行永不被覆盖,
     审计版本不参与评估/命中率(不重复计数), 版本信息也不丢;
  3. 同键多条计分行(历史脏数据)时明确拒绝, 不挑一行改、不插第三行;
  4. 归属桶显式: 个人桶必须有 user_id; 无会话运行走 system 桶(user_id 恒空),
     system 桶绝不绑定真实用户, 也绝不并进任何个人成绩;
  5. 历史无归属行(user_id/owner_scope 均 NULL)保留 unknown: 不评估、不回填、不清洗、不删除;
  6. DB 错误分型: unique / foreign_key / 其它完整性错误分开返回;
  7. 迁移与模型一致(列 + 索引名), 迁移只加列/加索引(唯一删除动作是丢掉 m249 自己的索引)。

测试库纪律: 每个用例一套 tmp_path 独立 SQLite; 起测清空 DATABASE_*/SIDA_DB*/DATA_DIR/PYTHONPATH
并断言连的就是这次的临时库; 不碰生产库、不清任何既有数据。
"""

from __future__ import annotations

import os
import threading
from datetime import date, timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, select, text as sqltext
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

DAY = "2026-11-11"
PRE_AT = f"{DAY}T08:20:00+08:00"
PRE_DONE = f"{DAY}T08:22:00+08:00"

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


@pytest.fixture()
def db(tmp_path, monkeypatch):
    from src.db.models import (
        AgentContextRun,
        AgentPredictionOutcome,
        AnalysisHistory,
        NewsTopicSnapshot,
        PremarketFactorEvidence,
        StockContextSnapshot,
        StockSuggestion,
        User,
    )
    from src.db.session import Base

    for key in _DB_ENV:
        monkeypatch.delenv(key, raising=False)
    db_path = tmp_path / "isolation.db"
    url = f"sqlite:///{db_path}"
    assert str(tmp_path) in url and not os.path.exists(db_path)
    eng = create_engine(url, future=True)
    assert str(eng.url).startswith("sqlite:///") and str(tmp_path) in str(eng.url)

    Base.metadata.create_all(
        eng,
        tables=[
            User.__table__,
            AnalysisHistory.__table__,
            PremarketFactorEvidence.__table__,
            StockSuggestion.__table__,
            AgentPredictionOutcome.__table__,
            StockContextSnapshot.__table__,
            NewsTopicSnapshot.__table__,
            AgentContextRun.__table__,
        ],
    )
    Session = sessionmaker(bind=eng, autoflush=False, expire_on_commit=False, future=True)
    # 归属用户必须是**真实存在**的 users 行(2026-11-12 三次修正): 读取/评估/写入口都以
    # "users 里还有这个人"判定归属可证明, ghost(悬空 id)不算有归属。这里种下用例会用的账号,
    # 否则所有行都会按 ghost 排除 —— 那正是本次要修的行为, 不是测试想要的基线。
    with Session() as s:
        for uid, name in (("u-a", "account-a"), ("u-b", "account-b"), ("u-thread", "thread")):
            s.add(
                User(
                    id=uid,
                    username=name,
                    password_hash="x",
                    role="member",
                    is_active=True,
                )
            )
        s.commit()
    monkeypatch.setattr("src.db.session.SessionLocal", Session)
    monkeypatch.setattr("src.core.context_store.SessionLocal", Session)
    monkeypatch.setattr("src.core.analysis_history.SessionLocal", Session)
    monkeypatch.setattr("src.core.suggestion_pool.SessionLocal", Session)
    return SimpleNamespace(engine=eng, Session=Session, path=db_path)


def _rows(db) -> list:
    from src.db.models import AgentPredictionOutcome

    with db.Session() as s:
        return (
            s.execute(select(AgentPredictionOutcome).order_by(AgentPredictionOutcome.id))
            .scalars()
            .all()
        )


def _save(**over: Any):
    from src.core.context_store import save_agent_prediction_outcome

    kwargs: dict[str, Any] = dict(
        agent_name="premarket_outlook",
        stock_symbol="600183",
        stock_market="CN",
        prediction_date=DAY,
        horizon_days=1,
        action="buy",
        action_label="准备建仓",
        confidence=0.88,
        trigger_price=35.6,
        meta={"source": "premarket_outlook", "reason": "覆铜板提价"},
        user_id="u-a",
        owner_scope="user",
        run_trace_id="run-a",
    )
    kwargs.update(over)
    return save_agent_prediction_outcome(**kwargs)


# ── 1. 两个真实用户同标的同日 ────────────────────────────────────────────

def test_two_users_same_symbol_same_day_never_overwrite_each_other(db):
    a = _save(user_id="u-a", run_trace_id="run-a", action="buy")
    b = _save(
        user_id="u-b",
        run_trace_id="run-b",
        action="sell",
        action_label="减仓",
        confidence=0.31,
        trigger_price=99.0,
        meta={"source": "premarket_outlook", "reason": "B 账号的判断"},
    )
    assert a.written is True and a.scored is True
    assert b.written is True and b.scored is True

    rows = _rows(db)
    assert len(rows) == 2, "两个账号同键必须各存一行, 不能挤成一行"
    by_user = {r.user_id: r for r in rows}
    assert set(by_user) == {"u-a", "u-b"}
    # A 的每一列都还是 A 写进去的值 —— 没有被 B 的写入改掉
    assert by_user["u-a"].action == "buy" and by_user["u-a"].action_label == "准备建仓"
    assert by_user["u-a"].confidence == 0.88 and by_user["u-a"].trigger_price == 35.6
    assert by_user["u-a"].meta["reason"] == "覆铜板提价"
    assert by_user["u-a"].meta["run_trace_id"] == "run-a"
    assert by_user["u-a"].run_trace_id == "run-a"
    # B 自己的行也是自己的值
    assert by_user["u-b"].action == "sell" and by_user["u-b"].trigger_price == 99.0
    assert by_user["u-b"].meta["reason"] == "B 账号的判断"
    assert by_user["u-b"].meta["owner_scope"] == "user"
    assert by_user["u-b"].owner_scope == "user"


def test_legacy_unowned_row_is_never_touched_by_any_user(db):
    """加列前的历史行(user_id/owner_scope 均为空)不能被任何账号"顺手更新"。"""
    from src.db.models import AgentPredictionOutcome

    with db.Session() as s:
        s.add(
            AgentPredictionOutcome(
                user_id=None,
                owner_scope=None,
                agent_name="premarket_outlook",
                stock_symbol="600183",
                stock_market="CN",
                prediction_date=DAY,
                horizon_days=1,
                action="buy",
                action_label="准备建仓",
                confidence=0.5,
                outcome_status="pending",
                meta={"source": "premarket_outlook", "run_trace_id": "legacy-run"},
            )
        )
        s.commit()

    res = _save(user_id="u-a", run_trace_id="run-a", action="sell")
    assert res.written is True, "新账号应写自己的行, 不因历史行而整片禁写"

    rows = _rows(db)
    assert len(rows) == 2
    legacy = [r for r in rows if r.user_id is None][0]
    assert legacy.action == "buy" and legacy.confidence == 0.5
    assert legacy.meta["run_trace_id"] == "legacy-run"
    assert legacy.owner_scope is None, "历史未知归属必须保留 unknown, 不得被回填/清洗"


def test_duplicate_active_rows_block_writes_instead_of_picking_one(db):
    """同键多条**计分行**(历史重复/索引被删过)时, 不许挑一行改, 也不许再插第三行。"""
    from src.db.models import AgentPredictionOutcome

    with db.engine.begin() as conn:
        conn.execute(sqltext("DROP INDEX IF EXISTS ux_prediction_outcome_user_active_key"))
        conn.execute(sqltext("DROP INDEX IF EXISTS ux_prediction_outcome_user_run_key"))
    with db.Session() as s:
        for action in ("buy", "sell"):
            s.add(
                AgentPredictionOutcome(
                    user_id="u-a",
                    owner_scope="user",
                    run_trace_id=f"dup-{action}",
                    agent_name="premarket_outlook",
                    stock_symbol="600183",
                    stock_market="CN",
                    prediction_date=DAY,
                    horizon_days=1,
                    action=action,
                    action_label=action,
                    outcome_status="pending",
                    meta={"run_trace_id": f"dup-{action}"},
                )
            )
        s.commit()

    res = _save(user_id="u-a", run_trace_id="run-new", action="hold")
    assert res.written is False
    assert res.status == "refused_duplicate_rows"
    rows = _rows(db)
    assert len(rows) == 2, "历史重复行既不能被改也不能被第三行淹没"
    assert [r.action for r in rows] == ["buy", "sell"]


def test_other_user_write_does_not_touch_evaluated_row_and_is_a_real_insert(db):
    """A 的行已评估时, B 同键写入: A 不变, B 真插入(既不改 A, 也不谎报/误挡)。"""
    from src.core.context_store import mark_agent_prediction_outcome

    assert _save(user_id="u-a", run_trace_id="run-a", action="buy").written is True
    a_row = _rows(db)[0]
    assert mark_agent_prediction_outcome(
        record_id=a_row.id, outcome_price=40.0, outcome_return_pct=12.36
    ) is True

    res = _save(user_id="u-b", run_trace_id="run-b", action="sell", action_label="减仓")
    assert res.written is True, "B 是同键但不同账户, 应写自己的新行"
    assert res.status == "inserted" and res.scored is True

    rows = {r.user_id: r for r in _rows(db)}
    assert set(rows) == {"u-a", "u-b"}
    assert rows["u-a"].action == "buy" and rows["u-a"].outcome_status == "evaluated"
    assert rows["u-a"].outcome_return_pct == 12.36
    assert rows["u-b"].action == "sell" and rows["u-b"].outcome_status == "pending"


# ── 2. 计分行不被改写 + 后续版本追加(信息不丢、不重复计分) ────────────────

def test_evaluated_row_is_not_rewritten_and_later_version_is_only_appended(db):
    from src.core.context_store import mark_agent_prediction_outcome

    assert _save(user_id="u-a", run_trace_id="run-a").written is True
    rec = _rows(db)[0]
    assert mark_agent_prediction_outcome(
        record_id=rec.id, outcome_price=40.0, outcome_return_pct=12.36, status="evaluated"
    ) is True

    again = _save(
        user_id="u-a", run_trace_id="run-later", action="sell", confidence=0.02, trigger_price=1.0
    )
    assert again.scored is False, "计分行已被评估, 后续版本绝不能成为新的计分行"
    assert again.status == "appended_superseded"
    assert bool(again) is True, "版本本身是存下来了(不是丢失), 但 scored=False"

    rows = _rows(db)
    assert len(rows) == 2
    keep = [r for r in rows if r.outcome_status == "evaluated"][0]
    extra = [r for r in rows if r.outcome_status == "superseded"][0]
    assert keep.id == rec.id
    assert keep.action == "buy" and keep.confidence == 0.88 and keep.trigger_price == 35.6
    assert keep.outcome_price == 40.0 and keep.outcome_return_pct == 12.36
    # 后续版本原样留档, 不回写计分行
    assert extra.action == "sell" and extra.confidence == 0.02 and extra.trigger_price == 1.0
    assert extra.run_trace_id == "run-later"


def test_pending_row_of_another_version_is_appended_not_overwritten(db):
    assert _save(user_id="u-a", run_trace_id="run-a").written is True
    res = _save(user_id="u-a", run_trace_id="run-b", action="sell", confidence=0.1)
    assert res.written is True and res.scored is False
    assert res.status == "appended_superseded"

    rows = _rows(db)
    assert len(rows) == 2, "后续版本必须留下来(不能丢), 但只能有一条计分行"
    active = [r for r in rows if r.outcome_status != "superseded"]
    assert len(active) == 1, "同一键出现多条计分行 = 会重复计分"
    assert active[0].action == "buy" and active[0].confidence == 0.88
    assert active[0].run_trace_id == "run-a", "既有计分行不能被后来的版本顶掉"
    assert active[0].owner_scope == "user"


def test_same_version_repeat_is_idempotent(db):
    first = _save(user_id="u-a", run_trace_id="run-a")
    second = _save(user_id="u-a", run_trace_id="run-a", confidence=0.99)
    assert first.written is True and second.written is False
    assert second.status == "unchanged_same_version"
    rows = _rows(db)
    assert len(rows) == 1
    assert rows[0].confidence == 0.88, "幂等重放不应改写既有值"


def test_missing_version_is_refused_instead_of_duplicating(db):
    res = _save(user_id="u-a", run_trace_id=None)
    assert res.written is False and res.status == "refused_no_version"
    assert _rows(db) == []


# ── 3. 归属桶: 个人必须有 owner; system 独立且绝不绑定真实用户 ─────────────

def test_personal_scope_without_owner_is_refused_with_explicit_warning(db, caplog):
    with caplog.at_level("WARNING", logger="src.core.context_store"):
        res = _save(user_id=None, owner_scope="user", run_trace_id="run-sys")
    assert res.written is False and res.status == "refused_no_owner"
    assert _rows(db) == [], "个人桶无归属的预测行不能落库(无法隔离, 只会污染统计)"
    assert any(
        "缺少归属用户" in rec.message and "600183" in rec.message for rec in caplog.records
    ), "拒绝写入必须留下可定位的日志(标的/原因)"


def test_system_scope_goes_to_independent_bucket_and_never_binds_a_user(db):
    from src.core.context_store import list_agent_prediction_outcomes

    res = _save(user_id=None, owner_scope="system", run_trace_id="sys-1")
    assert res.written is True and res.scored is True and res.status == "inserted"

    rows = _rows(db)
    assert len(rows) == 1
    row = rows[0]
    assert row.owner_scope == "system"
    assert row.user_id is None, "system 桶绝不能猜测绑定某个真实用户"
    assert row.run_trace_id == "sys-1"

    # 任何账号都读不到 system 桶(它不属于任何账号)
    assert list_agent_prediction_outcomes(user_id="u-a") == []
    assert list_agent_prediction_outcomes(user_id="u-b") == []


def test_system_scope_rejects_real_user_id(db):
    res = _save(user_id="u-a", owner_scope="system", run_trace_id="sys-1")
    assert res.written is False and res.status == "refused_bad_scope"
    assert _rows(db) == []


def test_unknown_scope_is_refused(db):
    res = _save(user_id="u-a", owner_scope="global", run_trace_id="run-a")
    assert res.written is False and res.status == "refused_bad_scope"
    assert _rows(db) == []


def test_system_bucket_appends_versions_without_double_counting(db, monkeypatch):
    from src.collectors.kline_collector import KlineCollector
    from src.core.context_store import count_system_pending_prediction_outcomes
    from src.core.prediction_outcome import evaluate_pending_prediction_outcomes

    old_day = (date.today() - timedelta(days=10)).isoformat()
    monkeypatch.setattr(
        KlineCollector,
        "get_klines",
        lambda self, symbol, days=0: [
            SimpleNamespace(date=old_day, close=10.0),
            SimpleNamespace(
                date=(date.fromisoformat(old_day) + timedelta(days=7)).isoformat(), close=11.0
            ),
        ],
    )

    v1 = _save(
        user_id=None,
        owner_scope="system",
        run_trace_id="sys-1",
        prediction_date=old_day,
        trigger_price=10.0,
    )
    v2 = _save(
        user_id=None,
        owner_scope="system",
        run_trace_id="sys-2",
        prediction_date=old_day,
        action="sell",
        trigger_price=10.0,
    )
    assert v1.scored is True
    assert v2.written is True and v2.scored is False
    assert v2.status == "appended_superseded"

    rows = _rows(db)
    assert len(rows) == 2
    assert [r.outcome_status for r in rows] == ["pending", "superseded"]

    # system 桶独立计数, 但**不进入**个人评估
    assert count_system_pending_prediction_outcomes(max_horizon_days=10) == 1
    stats = evaluate_pending_prediction_outcomes(max_horizon_days=10, limit=10)
    assert stats["evaluated"] == 0
    assert stats["skipped_system_bucket"] == 1
    assert stats["user_scope"] == "all_user_scoped_rows"

    rows = _rows(db)
    assert [r.outcome_status for r in rows] == ["pending", "superseded"], (
        "system 桶被个人评估器改了(不允许); superseded 也不许被评估"
    )


def test_evaluator_only_scores_the_active_row(db, monkeypatch):
    """同键有 pending + superseded 时, 评估只落在计分行, 审计版本保持原样。"""
    from src.collectors.kline_collector import KlineCollector
    from src.core.prediction_outcome import evaluate_pending_prediction_outcomes

    old_day = (date.today() - timedelta(days=10)).isoformat()
    monkeypatch.setattr(
        KlineCollector,
        "get_klines",
        lambda self, symbol, days=0: [
            SimpleNamespace(date=old_day, close=10.0),
            SimpleNamespace(
                date=(date.fromisoformat(old_day) + timedelta(days=7)).isoformat(), close=11.0
            ),
        ],
    )
    assert _save(prediction_date=old_day, trigger_price=10.0, run_trace_id="run-1").scored is True
    later = _save(
        prediction_date=old_day, trigger_price=10.0, run_trace_id="run-2", action="sell"
    )
    assert later.status == "appended_superseded"

    stats = evaluate_pending_prediction_outcomes(max_horizon_days=10, limit=10)
    assert stats["evaluated"] == 1

    rows = _rows(db)
    assert [r.outcome_status for r in rows] == ["evaluated", "superseded"]
    assert rows[0].outcome_return_pct is not None
    assert rows[1].outcome_price is None and rows[1].outcome_return_pct is None


# ── 4. 并发: DB 唯一键兜底, 第二个不是"第二条计分行" ──────────────────────

def test_concurrent_insert_loses_to_the_unique_key(db, monkeypatch):
    """模拟两个进程都"没看到"已有行 → 第二个不能变成第二条计分行。"""
    import src.core.context_store as cs

    RealSession = db.Session

    class _BlindQuery:
        def filter(self, *a, **k):
            return self

        def order_by(self, *a, **k):
            return self

        def all(self):
            return []

    class _BlindSession:
        """包装真会话, 只把 query() 变成"永远查不到" —— 复现两个进程各自的空快照。"""

        def __init__(self, inner):
            self._inner = inner

        def query(self, *a, **k):
            return _BlindQuery()

        def __getattr__(self, name):
            return getattr(self._inner, name)

    monkeypatch.setattr(cs, "SessionLocal", lambda: _BlindSession(RealSession()))
    first = _save(user_id="u-a", run_trace_id="run-a")
    second = _save(user_id="u-a", run_trace_id="run-b", action="sell")
    assert first.scored is True
    assert second.scored is False
    assert second.status in ("appended_superseded", "concurrent_duplicate")

    rows = _rows(db)
    assert sum(1 for r in rows if r.outcome_status != "superseded") == 1, (
        f"并发下出现了多条计分行: {[(r.id, r.outcome_status) for r in rows]}"
    )
    assert all(r.action == "buy" for r in rows if r.outcome_status != "superseded")


def test_two_real_threads_create_at_most_one_scored_row(db):
    results: list[Any] = []
    barrier = threading.Barrier(2)

    def worker(tag: str):
        barrier.wait()
        results.append(_save(user_id="u-thread", run_trace_id=f"t-{tag}"))

    threads = [threading.Thread(target=worker, args=(t,)) for t in ("1", "2")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    rows = _rows(db)
    assert sum(1 for r in rows if r.outcome_status != "superseded") == 1, (
        f"并发出现多条计分行: {[(r.id, r.outcome_status) for r in rows]}"
    )
    assert sum(1 for r in results if r.scored) == 1, "并发里只能有一个计分行"
    assert len(rows) <= 2, f"版本应至多两条(计分 + 一份审计), 实际 {len(rows)}"


# ── 5. DB 错误分型 ───────────────────────────────────────────────────────

def test_db_error_kind_classifies_pg_and_sqlite_errors():
    from src.core.context_store import _db_error_kind

    def _exc(code=None, msg=""):
        attrs: dict[str, Any] = {}
        if code:
            attrs["pgcode"] = code
        if msg:
            attrs["__str__"] = lambda self: msg
        orig = type("O", (), attrs)()
        return type("E", (), {"orig": orig})()

    assert _db_error_kind(_exc("23505")) == "unique"
    assert _db_error_kind(_exc("23503")) == "foreign_key"
    assert _db_error_kind(_exc("23502")) == "not_null"
    assert _db_error_kind(_exc("23514")) == "check"
    assert _db_error_kind(_exc("40001")) == "pg_40001"
    assert _db_error_kind(_exc(msg="UNIQUE constraint failed: x.y")) == "unique"
    assert _db_error_kind(_exc(msg="FOREIGN KEY constraint failed")) == "foreign_key"
    assert _db_error_kind(_exc(msg="NOT NULL constraint failed: x.y")) == "not_null"
    assert _db_error_kind(_exc(msg="something else")) == "other"


def test_foreign_key_violation_is_not_reported_as_concurrency(db, monkeypatch):
    """归属用户不存在 → refused_unknown_user(不是 concurrent_duplicate, 也不是成功)。"""
    import src.core.context_store as cs

    RealSession = db.Session

    class _Orig:
        pgcode = "23503"

    class _FkSession:
        def __init__(self, inner):
            self._inner = inner

        def add(self, obj):
            self._inner.add(obj)

        def commit(self):
            raise IntegrityError("INSERT ...", {}, _Orig())

        def rollback(self):
            self._inner.rollback()

        def query(self, *a, **k):
            return self._inner.query(*a, **k)

        def close(self):
            self._inner.close()

    monkeypatch.setattr(cs, "SessionLocal", lambda: _FkSession(RealSession()))
    res = _save(user_id="ghost", run_trace_id="run-ghost")
    assert res.written is False
    assert res.status == "refused_unknown_user", res
    assert _rows(db) == []


# ── 6. 迁移与模型一致 / m250 只增量 ──────────────────────────────────────

LEGACY_DDL = """
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
"""


def _legacy_engine(tmp_path, monkeypatch, name: str):
    for key in _DB_ENV:
        monkeypatch.delenv(key, raising=False)
    path = tmp_path / name
    assert not os.path.exists(path)
    eng = create_engine(f"sqlite:///{path}", future=True)
    assert str(tmp_path) in str(eng.url)
    return eng


def test_m250_keeps_legacy_rows_and_enables_version_append(tmp_path, monkeypatch):
    from sqlalchemy import inspect

    from src.web import migrations as mig

    eng = _legacy_engine(tmp_path, monkeypatch, "m250.db")
    with eng.begin() as conn:
        conn.execute(sqltext(LEGACY_DDL))
        conn.execute(
            sqltext(
                "INSERT INTO agent_prediction_outcomes "
                "(agent_name, stock_symbol, stock_market, prediction_date, horizon_days, action) "
                "VALUES ('premarket_outlook','600183','CN','2026-11-11',1,'buy'),"
                "       ('premarket_outlook','600183','CN','2026-11-11',1,'sell')"
            )
        )

    with eng.begin() as conn:
        mig._m249_prediction_outcome_user_scope(conn)
        mig._m250_prediction_outcome_versions_and_system_scope(conn)

    with eng.begin() as conn:
        insp = inspect(conn)
        cols = {c["name"] for c in insp.get_columns("agent_prediction_outcomes")}
        assert {"user_id", "run_trace_id", "owner_scope"} <= cols
        names = {i["name"] for i in insp.get_indexes("agent_prediction_outcomes")}
        assert "ix_prediction_outcome_user" in names
        assert "ux_prediction_outcome_user_key" not in names, (
            "m249 的无状态唯一键会挡住版本追加, m250 必须把它删掉(只删索引, 不删行)"
        )
        assert {
            "ux_prediction_outcome_user_active_key",
            "ux_prediction_outcome_user_run_key",
            "ux_prediction_outcome_system_active_key",
            "ux_prediction_outcome_system_run_key",
        } <= names

        # 存量行一条不动
        rows = conn.execute(
            sqltext(
                "SELECT user_id, owner_scope, action, run_trace_id "
                "FROM agent_prediction_outcomes ORDER BY id"
            )
        ).fetchall()
        assert len(rows) == 2
        assert all(r[0] is None and r[1] is None and r[3] is None for r in rows)
        assert [r[2] for r in rows] == ["buy", "sell"]

        # 幂等: 再跑一次不改数据、不报错
        mig._m250_prediction_outcome_versions_and_system_scope(conn)
        assert conn.execute(sqltext("SELECT COUNT(*) FROM agent_prediction_outcomes")).scalar() == 2

        # 同用户: 一条计分行 + 一条 superseded 允许并存; 第二条计分行仍被拒
        conn.execute(
            sqltext(
                "INSERT INTO agent_prediction_outcomes "
                "(user_id, owner_scope, run_trace_id, agent_name, stock_symbol, stock_market, "
                "prediction_date, horizon_days, action) "
                "VALUES ('u-a','user','r1','premarket_outlook','600183','CN','2026-11-11',1,'buy')"
            )
        )
        conn.execute(
            sqltext(
                "INSERT INTO agent_prediction_outcomes "
                "(user_id, owner_scope, run_trace_id, agent_name, stock_symbol, stock_market, "
                "prediction_date, horizon_days, action, outcome_status) "
                "VALUES ('u-a','user','r2','premarket_outlook','600183','CN','2026-11-11',1,"
                "'sell','superseded')"
            )
        )
        with pytest.raises(IntegrityError):
            conn.execute(
                sqltext(
                    "INSERT INTO agent_prediction_outcomes "
                    "(user_id, owner_scope, run_trace_id, agent_name, stock_symbol, stock_market, "
                    "prediction_date, horizon_days, action) "
                    "VALUES ('u-a','user','r3','premarket_outlook','600183','CN','2026-11-11',1,"
                    "'hold')"
                )
            )


def test_model_and_migrations_declare_the_same_columns_and_index_names(tmp_path, monkeypatch):
    """create_all 的新库 vs 迁移过的老库: 列集合、索引名必须一致(不再各叫各的)。"""
    from sqlalchemy import inspect

    from src.db.models import AgentPredictionOutcome
    from src.db.session import Base
    from src.web import migrations as mig

    model_cols = {c.name for c in AgentPredictionOutcome.__table__.columns}
    model_indexes = {
        idx.name
        for idx in AgentPredictionOutcome.__table__.indexes
        if idx.name is not None
    }

    fresh = tmp_path / "fresh.db"
    fresh_eng = create_engine(f"sqlite:///{fresh}", future=True)
    Base.metadata.create_all(fresh_eng, tables=[AgentPredictionOutcome.__table__])
    with fresh_eng.connect() as conn:
        insp = inspect(conn)
        fresh_cols = {c["name"] for c in insp.get_columns("agent_prediction_outcomes")}
        fresh_indexes = {i["name"] for i in insp.get_indexes("agent_prediction_outcomes")}
    assert fresh_cols == model_cols
    assert fresh_indexes == model_indexes, (
        f"模型与 create_all 的索引名不一致: {sorted(model_indexes)} vs {sorted(fresh_indexes)}"
    )

    old = _legacy_engine(tmp_path, monkeypatch, "old.db")
    with old.begin() as conn:
        conn.execute(sqltext(LEGACY_DDL))
    with old.begin() as conn:
        mig._m249_prediction_outcome_user_scope(conn)
        mig._m250_prediction_outcome_versions_and_system_scope(conn)
    with old.connect() as conn:
        insp = inspect(conn)
        old_cols = {c["name"] for c in insp.get_columns("agent_prediction_outcomes")}
        old_indexes = {i["name"] for i in insp.get_indexes("agent_prediction_outcomes")}
    assert old_cols == model_cols, f"迁移库缺列: {model_cols - old_cols}"
    assert old_indexes == model_indexes, (
        f"迁移库与模型索引不一致: 缺 {model_indexes - old_indexes}, 多 {old_indexes - model_indexes}"
    )


# ── 7. 清理: 历史未知归属行永不删除; 默认 dry_run ────────────────────────

def test_cleanup_preserves_legacy_unknown_rows_and_dry_run_deletes_nothing(db):
    from src.core.context_store import cleanup_context_data
    from src.db.models import AgentPredictionOutcome

    old_day = (date.today() - timedelta(days=400)).isoformat()
    with db.Session() as s:
        s.add(
            AgentPredictionOutcome(
                user_id=None, owner_scope=None, agent_name="a", stock_symbol="600001",
                stock_market="CN", prediction_date=old_day, horizon_days=1, action="buy",
                action_label="准备建仓", outcome_status="pending", meta={},
            )
        )
        s.add(
            AgentPredictionOutcome(
                user_id="u-a", owner_scope="user", run_trace_id="r1", agent_name="a",
                stock_symbol="600002", stock_market="CN", prediction_date=old_day,
                horizon_days=1, action="buy", action_label="准备建仓",
                outcome_status="pending", meta={},
            )
        )
        s.add(
            AgentPredictionOutcome(
                user_id=None, owner_scope="system", run_trace_id="s1", agent_name="a",
                stock_symbol="600003", stock_market="CN", prediction_date=old_day,
                horizon_days=1, action="buy", action_label="准备建仓",
                outcome_status="pending", meta={},
            )
        )
        s.commit()

    dry = cleanup_context_data(outcome_days=60, dry_run=True)
    assert dry["dry_run"] is True
    assert dry["agent_prediction_outcomes"] == 2, "dry_run 只统计可清理的用户/system 行"
    assert dry["legacy_unowned_preserved"] == 1
    assert len(_rows(db)) == 3, "dry_run 一个行都不许删"

    done = cleanup_context_data(outcome_days=60, dry_run=False)
    assert done["agent_prediction_outcomes"] == 2
    rows = _rows(db)
    assert len(rows) == 1 and rows[0].owner_scope is None and rows[0].user_id is None, (
        "历史未知归属行被清理掉了(只能保留 unknown)"
    )


# ── 8. 盘前门槛: 完成时刻必须不早于采集结束时刻 ──────────────────────────

def test_preopen_gate_rejects_impossible_time_order():
    from src.core.premarket_factor_evidence import report_preopen_state

    ok = report_preopen_state(
        report_completed_at=f"{DAY}T08:22:00+08:00",
        collection_finished_at=f"{DAY}T08:20:00+08:00",
        report_day=DAY,
    )
    assert ok["preopen"] is True

    same = report_preopen_state(
        report_completed_at=f"{DAY}T08:20:00+08:00",
        collection_finished_at=f"{DAY}T08:20:00+08:00",
        report_day=DAY,
    )
    assert same["preopen"] is True, "完成时刻 == 采集结束时刻是合法的(边界含等号)"

    bad = report_preopen_state(
        report_completed_at=f"{DAY}T08:00:00+08:00",
        collection_finished_at=f"{DAY}T08:20:00+08:00",
        report_day=DAY,
    )
    assert bad["preopen"] is None, "两个时刻都看着盘前却时序不可能 = unknown, 不许放行"
    assert "时序不可能" in (bad["reason"] or "")

    # 时序也不可能, 但有一个时刻**可证明**已过 09:30 → 结论仍是"非盘前"(不依赖先后)
    provable = report_preopen_state(
        report_completed_at=f"{DAY}T09:00:00+08:00",
        collection_finished_at=f"{DAY}T09:31:00+08:00",
        report_day=DAY,
    )
    assert provable["preopen"] is False


def test_impossible_time_order_writes_no_prediction_rows(db):
    """走真实落库+建议写入路径: 时序不可能 → 盘前 unknown → 后验一行都不写。"""
    from src.agents.base import AnalysisResult
    from src.agents.premarket_outlook import PremarketOutlookAgent

    agent = PremarketOutlookAgent()
    user = SimpleNamespace(id="u-a")
    data = {
        "timestamp": f"{DAY}T08:20:00+08:00",
        "run_trace_id": "run-bad-order",
        "demon_factors": {},
        "demon_factors_frozen_at": PRE_AT,
        "report_completed_at": f"{DAY}T08:00:00+08:00",  # 早于采集结束: 不可能
    }
    saved = agent._persist_report(
        result=AnalysisResult(agent_name=agent.name, title="【盘前分析】测试", content=REPORT),
        data=data,
        context=cast(Any, SimpleNamespace(user=user)),
        status="success",
        report_completed_at=f"{DAY}T08:00:00+08:00",
    )
    assert saved.body_saved is True
    assert saved.preopen is None

    counters = _persist_suggestions(agent, user=user, trace="run-bad-order", save_result=saved)
    assert counters["outcome_saved"] == 0 and counters["skipped"] is True
    assert _rows(db) == [], "时序不可能的报告不能写后验(更不许悄悄变成盘前成绩)"


# ── 9. 真实写入路径: 两个账号各写各的; 同账号新版本只追加 ─────────────────

REPORT = """
事件驱动前瞻（双轨制 · 潜伏表）
| 事件 | 受益板块 | 代码 | 名称 | 受益逻辑 | 强度 | 埋伏方式 | 分批计划 | 止损 | 目标 | 证伪条件 |
|---|---|---|---|---|---|---|---|---|---|---|
| PCB涨价 | 覆铜板 | 600183 | 生益科技 | 龙头 | A | 回调埋伏 | 1/3 | 35.60 | +10% | 提价落空 |
"""


def _persist_suggestions(agent, *, user, trace: str, save_result):
    """直接调用真实写入路径(_persist_suggestions), 不绕道伪造 stats。"""
    from src.agents.base import AgentContext, AnalysisResult
    from src.config import AppConfig, Settings, StockConfig
    from src.models.market import MarketCode

    ctx = AgentContext(
        ai_client=cast(Any, MagicMock()),
        notifier=cast(Any, MagicMock()),
        config=AppConfig(
            settings=Settings(),
            watchlist=[StockConfig(symbol="600183", name="生益科技", market=MarketCode.CN)],
        ),
        user=user,
    )
    return agent._persist_suggestions(
        suggestions={
            "600183": {
                "action": "buy",
                "action_label": "准备建仓",
                "reason": "覆铜板提价",
                "signal": "PCB涨价",
            }
        },
        context=ctx,
        data={"signal_packs": {}, "symbol_contexts": {"600183": {"data_quality": {"score": 88}}}},
        analysis_date=DAY,
        user_content="prompt",
        result=AnalysisResult(agent_name=agent.name, title="t", content=REPORT),
        trace_id=trace,
        save_result=save_result,
    )


def test_real_write_path_keeps_two_users_apart_and_appends_later_versions(db):
    """两个真实用户同标的同日走真实写入路径: 各自一行; 同账号新版本只追加 superseded。"""
    from src.agents.premarket_outlook import PremarketOutlookAgent

    agent = PremarketOutlookAgent()
    premarket_ok = SimpleNamespace(
        body_saved=True, content_skipped=False, preopen=True, run_trace_id="run-a"
    )

    a = _persist_suggestions(
        agent, user=SimpleNamespace(id="u-a"), trace="run-a", save_result=premarket_ok
    )
    assert a["outcome_saved"] == 2, a  # h=1 与 h=5 各一行计分
    assert a["outcome_version_appended"] == 0

    b = _persist_suggestions(
        agent,
        user=SimpleNamespace(id="u-b"),
        trace="run-a",  # 同一 run_trace_id, 但用户不同
        save_result=premarket_ok,
    )
    assert b["outcome_saved"] == 2, "第二个账号写自己的行, 不该被第一个账号的行挡住"

    rows = _rows(db)
    assert len(rows) == 4
    assert {r.user_id for r in rows} == {"u-a", "u-b"}
    for uid in ("u-a", "u-b"):
        mine = sorted(r.horizon_days for r in rows if r.user_id == uid)
        assert mine == [1, 5]

    # A 账号用新版本重跑: 计分行不动, 新版本作为 superseded 审计版本留下(不丢也不重复计分)
    a2 = _persist_suggestions(
        agent,
        user=SimpleNamespace(id="u-a"),
        trace="run-a2",
        save_result=SimpleNamespace(
            body_saved=True, content_skipped=False, preopen=True, run_trace_id="run-a2"
        ),
    )
    assert a2["outcome_saved"] == 0 and a2["outcome_version_appended"] == 2, a2
    rows = _rows(db)
    assert len(rows) == 6, "新版本必须留档(audit), 不是被丢掉"
    scored = [r for r in rows if r.user_id == "u-a" and r.outcome_status != "superseded"]
    assert len(scored) == 2, "u-a 每个 horizon 只能有一条计分行"
    assert all(r.run_trace_id == "run-a" for r in scored)
    assert all((r.meta or {}).get("run_trace_id") == "run-a" for r in scored)
    assert sum(1 for r in rows if r.outcome_status == "superseded") == 2


def test_real_write_path_without_user_uses_explicit_system_bucket(db):
    """无会话运行(系统调度)落 system 桶: user_id 恒空, 不猜账号。"""
    from src.agents.premarket_outlook import PremarketOutlookAgent

    agent = PremarketOutlookAgent()
    saved = SimpleNamespace(
        body_saved=True, content_skipped=False, preopen=True, run_trace_id="run-sys"
    )
    counters = _persist_suggestions(agent, user=None, trace="run-sys", save_result=saved)
    assert counters["outcome_saved"] == 2, counters

    rows = _rows(db)
    assert len(rows) == 2
    assert all(r.user_id is None and r.owner_scope == "system" for r in rows)


# ── 10. 评估器: 不碰无归属历史行, 并如实报出被排除的条数 ─────────────────

def test_evaluator_skips_unowned_legacy_rows_and_reports_them(db, monkeypatch):
    from src.collectors.kline_collector import KlineCollector
    from src.core.prediction_outcome import evaluate_pending_prediction_outcomes
    from src.db.models import AgentPredictionOutcome

    old_day = (date.today() - timedelta(days=10)).isoformat()

    def _k(sym: str):
        return [
            SimpleNamespace(date=old_day, close=10.0),
            SimpleNamespace(date=(date.fromisoformat(old_day) + timedelta(days=7)).isoformat(), close=11.0),
        ]

    monkeypatch.setattr(KlineCollector, "get_klines", lambda self, symbol, days=0: _k(symbol))

    with db.Session() as s:
        s.add(
            AgentPredictionOutcome(
                user_id=None, owner_scope=None, agent_name="premarket_outlook",
                stock_symbol="600001", stock_market="CN", prediction_date=old_day,
                horizon_days=1, action="buy", action_label="准备建仓", trigger_price=10.0,
                outcome_status="pending", meta={},
            )
        )
        s.add(
            AgentPredictionOutcome(
                user_id="u-a", owner_scope="user", run_trace_id="run-a",
                agent_name="premarket_outlook", stock_symbol="600002", stock_market="CN",
                prediction_date=old_day, horizon_days=1, action="buy",
                action_label="准备建仓", trigger_price=10.0,
                outcome_status="pending", meta={"run_trace_id": "run-a"},
            )
        )
        s.commit()

    stats = evaluate_pending_prediction_outcomes(max_horizon_days=10, limit=10)
    assert stats["evaluated"] == 1, stats
    assert stats["skipped_unowned_legacy"] == 1, stats
    assert stats["scope"] == "owned_only"

    rows = {r.stock_symbol: r for r in _rows(db)}
    assert rows["600001"].outcome_status == "pending", "无归属历史行被评估器改了(不允许)"
    assert rows["600001"].outcome_price is None
    assert rows["600002"].outcome_status == "evaluated"
    assert rows["600002"].outcome_price is not None
    assert rows["600002"].outcome_return_pct is not None


# ── 11. 读取侧隔离: 导出 / 列表不串账户 ─────────────────────────────────

def test_export_and_list_endpoints_are_user_scoped(db):
    assert _save(user_id="u-a", run_trace_id="run-a", action="buy").written is True
    assert _save(
        user_id="u-b", run_trace_id="run-b", action="sell", action_label="减仓"
    ).written is True
    assert _save(user_id=None, owner_scope="system", run_trace_id="sys-1").written is True

    from src.core.context_store import list_agent_prediction_outcomes
    from src.web.api import context as context_api
    from src.web.api import export as export_api

    user_a = SimpleNamespace(id="u-a", username="a", role="member", nickname=None)

    # 存储层: 只给本账号的行(不含别的账号, 也不含 system 桶)
    owned = list_agent_prediction_outcomes(user_id="u-a")
    assert len(owned) == 1 and owned[0].user_id == "u-a"
    assert owned[0].action == "buy"
    # 不指定用户 = 不出数据(不提供跨账户读取)
    assert list_agent_prediction_outcomes() == []

    listed = context_api.list_prediction_outcomes(user=user_a, days=90, limit=200)
    assert len(listed) == 1 and listed[0].action == "buy"

    with db.Session() as s:
        resp = export_api.export_predictions(db=s, user=user_a)
        body = resp.body.decode("utf-8-sig")
    assert "buy" in body or "准备建仓" in body
    assert "减仓" not in body, "导出里出现了别的账号的预测(越权读取)"
    assert body.count("\n") == 2, f"应只有表头 + 本账号 1 行, 实际: {body!r}"


# ── 12. 全局写接口必须管理员 ─────────────────────────────────────────────

def test_global_evaluate_and_cleanup_require_admin(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.web.api import context as context_api
    from src.web.api.auth import get_current_user

    monkeypatch.setenv("STRICT_ADMIN_TOKEN", "0")  # 过渡模式: 只按 role 判定, 不依赖 admin 受众 token
    app = FastAPI()
    app.include_router(context_api.router, prefix="/api")
    c = TestClient(app)

    # 没登录 → 401(不再像原来那样谁都能 POST 就改全平台成绩)
    assert c.post("/api/context/predictions/evaluate").status_code in (401, 403)

    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        id="u1", role="member", username="u1", is_active=True
    )
    assert c.post("/api/context/predictions/evaluate").status_code == 403
    assert c.post("/api/context/cleanup").status_code == 403

    # owner 可以调, 但 cleanup 默认 dry_run: 不实际删除
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        id="owner", role="owner", username="owner", is_active=True
    )
    body = c.post("/api/context/cleanup").json()
    assert body["dry_run"] is True
