"""原型: A 题材观察批次证据存储(只追加) —— 契约与并发/原子性验收。

这不是接进主链路的代码, 是一份**可执行的方案验证**: 把「同名 run_key 幂等、同日多批次
不覆盖、失败整轮回滚、unknown ≠ 0」写成断言, 让 `src/core/theme_evolution_store.py`
先红后绿地落地。

硬约束(违反即视为 bug):
- 只用**显式传入的隔离 engine/session**: 测试自己建 `sqlite://` 内存库或 `tmp_path`
  文件库, 不碰 `data/sida.db`, 不读生产连接串, 不起网络;
- 原型用**独立 `MetaData`**, 表名带 `theme_obs_` 前缀, 与 251 生产表不同名 —— 误
  `create_all` 也不会在真库里建出生产表;
- 写入只 INSERT, 没有任何 UPDATE 证据的路径; 失败必须整轮消失(连 run 行一起);
- `pool_state=unknown` 与 `limit_up_count=0` 是两件事, 前者留 NULL。
"""

from __future__ import annotations

import datetime as dt
import inspect
import json
from zoneinfo import ZoneInfo

from typing import Any

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import Engine

from src.core import theme_evolution_store as store

CST = ZoneInfo("Asia/Shanghai")
NOW = dt.datetime(2026, 11, 16, 15, 16, tzinfo=CST)
DAY = "20261116"
RULE_VERSION = "theme_evo_v1"


# ── 构造 helper ─────────────────────────────────────────────────────────
def _board(code: str = "BK0001", state: str = store.BOARD_OK, members=("000001",), **kw):
    kw.setdefault("data_day", DAY)
    kw.setdefault("date_basis", "source_reported")
    return store.BoardEvidence(board_code=code, state=state, members=members, **kw)


def _batch(**kw):
    base: dict[str, Any] = dict(
        day=DAY,
        run_key="rk-20261116-1516",
        rule_version=RULE_VERSION,
        source="tq",
        captured_at=NOW,
        boards=(_board(),),
        limit_up=(store.LimitUpEvidence(stock_code="000001", stock_name="平安银行"),),
        pool_state=store.POOL_OK,
        pool_data_day=DAY,
        pool_date_basis="kline",
        source_fresh=True,
        source_trade_date=DAY,
    )
    base.update(kw)
    return store.ObservationBatch(**base)


@pytest.fixture()
def engine():
    """内存库: 每个用例独立, 用完销毁。"""
    eng = create_engine("sqlite://")
    store.create_schema(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture()
def file_engine(tmp_path):
    """临时文件库: 证明跨连接/跨进程重启同样只追加(不落仓库 data/)。"""
    eng = create_engine(f"sqlite:///{tmp_path / 'theme_obs.db'}")
    store.create_schema(eng)
    try:
        yield eng
    finally:
        eng.dispose()


def _count(eng, table, **where) -> int:
    stmt = select(func.count()).select_from(table)
    for col, val in where.items():
        stmt = stmt.where(table.c[col] == val)
    with eng.connect() as conn:
        return conn.execute(stmt).scalar_one()


def _runs(eng) -> list[dict]:
    with eng.connect() as conn:
        rows = conn.execute(select(store.THEME_OBS_RUN).order_by(store.THEME_OBS_RUN.c.id)).mappings()
        return [dict(r) for r in rows]


def _boards(eng) -> list[dict]:
    with eng.connect() as conn:
        rows = conn.execute(select(store.THEME_OBS_BOARD).order_by(store.THEME_OBS_BOARD.c.id)).mappings()
        return [dict(r) for r in rows]


def _pool(eng) -> list[dict]:
    with eng.connect() as conn:
        rows = conn.execute(
            select(store.THEME_OBS_LIMITUP).order_by(store.THEME_OBS_LIMITUP.c.id)
        ).mappings()
        return [dict(r) for r in rows]


# ── 1. 原型隔离: 独立 MetaData / 独立表名 / 不 import src.db ────────────
def test_prototype_uses_its_own_metadata_and_prefixed_tables():
    """独立 MetaData + 前缀表名: 主工程 `create_all` 不会顺带建出这些表。"""
    from src.db.models import ThemeBoardDaily
    from src.db.session import Base

    assert store.THEME_OBS_METADATA is not Base.metadata
    assert store.THEME_OBS_BOARD.metadata is store.THEME_OBS_METADATA
    assert ThemeBoardDaily.metadata is not store.THEME_OBS_METADATA
    names = set(store.THEME_OBS_METADATA.tables)
    assert names == {"theme_obs_run", "theme_obs_board", "theme_obs_limitup"}
    # 与生产表零重名 —— 这是"误跑 create_all 也不会建生产表"的那道闸。
    assert not (names & set(Base.metadata.tables))


def test_store_module_does_not_import_the_production_db_layer():
    """必须显式传 engine: 模块自身不解析环境变量、不构造引擎、不 import src.db。"""
    text = inspect.getsource(store)
    assert "src.db" not in text
    assert "create_engine" not in text
    assert not hasattr(store, "is_postgres")


def test_create_schema_is_idempotent(file_engine):
    store.create_schema(file_engine)  # 第二次不应抛
    store.THEME_OBS_METADATA.create_all(file_engine, checkfirst=True)
    with file_engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(store.THEME_OBS_RUN)).scalar_one() == 0


# ── 2. 追加语义 ─────────────────────────────────────────────────────────
def test_first_append_writes_one_run_and_its_details(engine):
    out = store.append_observation(engine, _batch())

    assert out.inserted is True
    assert out.reason is None
    assert out.attempt_seq == 1
    assert out.status == store.STATUS_COMPLETE
    assert out.coverage == 1.0
    assert out.boards_written == 1
    assert out.limit_up_written == 1

    runs = _runs(engine)
    assert len(runs) == 1
    run = runs[0]
    assert run["id"] == out.run_id
    assert run["day"] == DAY
    assert run["run_key"] == "rk-20261116-1516"
    assert run["status"] == store.STATUS_COMPLETE
    assert run["rule_version"] == RULE_VERSION
    assert run["source"] == "tq"
    assert run["source_fresh"] in (True, 1)
    assert run["source_trade_date"] == DAY
    assert (run["boards_total"], run["boards_ok"]) == (1, 1)
    assert run["pool_state"] == store.POOL_OK
    assert run["limit_up_count"] == 1
    assert run["finished_at"] is not None

    boards = _boards(engine)
    assert len(boards) == 1 and boards[0]["run_id"] == out.run_id
    assert json.loads(boards[0]["members_json"]) == ["000001"]
    assert boards[0]["member_count"] == 1
    pool = _pool(engine)
    assert len(pool) == 1 and pool[0]["run_id"] == out.run_id


def test_same_run_key_is_idempotent_and_never_double_writes(engine):
    first = store.append_observation(engine, _batch())
    second = store.append_observation(engine, _batch())

    assert second.inserted is False
    assert second.reason == "run_key_exists"
    assert (second.run_id, second.attempt_seq, second.status) == (
        first.run_id,
        first.attempt_seq,
        first.status,
    )
    assert _count(engine, store.THEME_OBS_RUN) == 1
    assert _count(engine, store.THEME_OBS_BOARD) == 1
    assert [b["board_code"] for b in _boards(engine)] == ["BK0001"]


def test_same_day_second_run_appends_and_leaves_first_run_untouched(engine):
    first = store.append_observation(engine, _batch())
    before = _boards(engine)

    second = store.append_observation(
        engine,
        _batch(
            run_key="rk-20261116-1540",
            captured_at=NOW + dt.timedelta(minutes=24),
            boards=(_board("BK0001", members=("000001", "000002")), _board("BK0002")),
            limit_up=(store.LimitUpEvidence(stock_code="000002"),),
            source_trade_date=DAY,
        ),
    )

    assert (second.inserted, second.attempt_seq) == (True, 2)
    assert second.run_id != first.run_id
    assert _count(engine, store.THEME_OBS_RUN) == 2
    # 第一批明细原样还在, 一行没被改。
    assert _boards(engine)[: len(before)] == before
    assert _count(engine, store.THEME_OBS_BOARD, run_id=first.run_id) == 1
    assert json.loads(
        [b for b in _boards(engine) if b["run_id"] == first.run_id][0]["members_json"]
    ) == ["000001"]
    assert _count(engine, store.THEME_OBS_LIMITUP, run_id=first.run_id) == 1


def test_attempt_seq_increments_monotonically_for_same_day(engine):
    for i, key in enumerate(["rk-a", "rk-b", "rk-c"], start=1):
        out = store.append_observation(engine, _batch(run_key=key))
        assert out.attempt_seq == i
    assert [r["attempt_seq"] for r in _runs(engine)] == [1, 2, 3]
    # 同日多批次 = 多套明细并存, 不是覆盖。
    assert _count(engine, store.THEME_OBS_BOARD) == 3


def test_latest_view_reads_exactly_one_run_and_never_blends(engine):
    store.append_observation(engine, _batch(boards=(_board("BK0001"), _board("BK0002"))))
    second = store.append_observation(
        engine, _batch(run_key="rk-2", boards=(_board("BK0003"),))
    )

    with engine.connect() as conn:
        view = store.latest_evidence(conn, DAY)

    assert view["run"]["run_id"] == second.run_id
    assert [b["board_code"] for b in view["boards"]] == ["BK0003"]
    # 第 1 批的板块没有混进来 —— 跨 run 拼接会让 coverage 与 hits 口径互相矛盾。
    assert "BK0001" not in {b["board_code"] for b in view["boards"]}

    with engine.connect() as conn:
        day_runs = store.runs_for_day(conn, DAY)
    assert [r["attempt_seq"] for r in day_runs] == [1, 2]
    assert day_runs[0]["coverage"] == 1.0


def test_refused_only_day_has_no_latest_evidence(engine):
    store.append_observation(
        engine, _batch(run_key="rk-stale", boards=(), limit_up=(), pool_state=store.POOL_UNKNOWN,
                       source_fresh=False, refused_reason="source_stale")
    )
    with engine.connect() as conn:
        assert store.latest_evidence(conn, DAY) is None


# ── 3. 失败回滚: 整轮消失, 且不烧 attempt_seq ───────────────────────────
def test_failure_before_commit_rolls_back_run_and_details(engine, monkeypatch):
    def _boom(*a, **kw):
        raise RuntimeError("injected mid-write failure")

    monkeypatch.setattr(store, "_verify_written_counts", _boom)

    with pytest.raises(RuntimeError, match="injected"):
        store.append_observation(engine, _batch())

    assert _count(engine, store.THEME_OBS_RUN) == 0
    assert _count(engine, store.THEME_OBS_BOARD) == 0
    assert _count(engine, store.THEME_OBS_LIMITUP) == 0


def test_failed_attempt_does_not_burn_attempt_seq(engine, monkeypatch):
    def _boom(*a, **kw):
        raise RuntimeError("injected")

    monkeypatch.setattr(store, "_verify_written_counts", _boom)
    with pytest.raises(RuntimeError):
        store.append_observation(engine, _batch())
    monkeypatch.undo()

    out = store.append_observation(engine, _batch())
    assert out.attempt_seq == 1
    assert _count(engine, store.THEME_OBS_RUN) == 1


def test_integrity_error_is_wrapped_and_rolled_back(engine, monkeypatch):
    from sqlalchemy.exc import IntegrityError

    def _conflict(*a, **kw):
        raise IntegrityError("INSERT ...", {}, Exception("UNIQUE constraint failed"))

    monkeypatch.setattr(store, "_insert_detail_rows", _conflict)

    with pytest.raises(store.EvidenceConflictError) as err:
        store.append_observation(engine, _batch())
    assert "已回滚" in str(err.value)
    assert "UNIQUE constraint" not in str(err.value)

    assert _count(engine, store.THEME_OBS_RUN) == 0
    assert _count(engine, store.THEME_OBS_BOARD) == 0


def test_caller_owned_connection_is_usable_after_a_failed_append(engine, monkeypatch):
    def _boom(*a, **kw):
        raise RuntimeError("injected")

    monkeypatch.setattr(store, "_verify_written_counts", _boom)
    with engine.connect() as conn:
        with pytest.raises(RuntimeError):
            store.append_observation(conn, _batch())
        # 回滚已经把会话从失败事务里拉出来: 后续语句仍能跑(PG 上就是 InFailedSqlTransaction 的反面)。
        assert conn.execute(select(func.count()).select_from(store.THEME_OBS_RUN)).scalar_one() == 0


# ── 4. unknown / 空 / 0 三态不可合并 ────────────────────────────────────
def test_empty_ok_and_fetch_failed_are_stored_distinctly(engine):
    store.append_observation(
        engine,
        _batch(
            run_key="rk-three-state",
            boards=(
                _board("BK-A", state=store.BOARD_OK, members=("000001",)),
                _board("BK-B", state=store.BOARD_EMPTY, members=()),
                _board("BK-C", state=store.BOARD_FAILED, members=None, error="timeout"),
            ),
        ),
    )
    rows = {b["board_code"]: b for b in _boards(engine)}
    assert rows["BK-A"]["state"] == "ok" and json.loads(rows["BK-A"]["members_json"]) == ["000001"]
    # 确为空: 存 "[]", member_count=0
    assert rows["BK-B"]["state"] == "empty_ok"
    assert rows["BK-B"]["members_json"] == "[]"
    assert rows["BK-B"]["member_count"] == 0
    # 取不到: 全 NULL, 不能被读成"该题材没有成员"
    assert rows["BK-C"]["state"] == "fetch_failed"
    assert rows["BK-C"]["members_json"] is None
    assert rows["BK-C"]["member_count"] is None

    run = _runs(engine)[0]
    assert (run["boards_ok"], run["boards_empty_ok"], run["boards_fetch_failed"]) == (1, 1, 1)
    assert run["boards_total"] == 3


def test_ok_state_requires_non_empty_members(engine):
    with pytest.raises(store.EvidenceError, match="ok.*非空|members"):
        store.append_observation(
            engine, _batch(boards=(_board("BK-A", state=store.BOARD_OK, members=()),))
        )
    assert _count(engine, store.THEME_OBS_RUN) == 0


def test_empty_ok_state_rejects_members(engine):
    with pytest.raises(store.EvidenceError):
        store.append_observation(
            engine, _batch(boards=(_board("BK-A", state=store.BOARD_EMPTY, members=("000001",)),))
        )
    assert _count(engine, store.THEME_OBS_BOARD) == 0


def test_fetch_failed_rejects_members(engine):
    with pytest.raises(store.EvidenceError):
        store.append_observation(
            engine,
            _batch(boards=(_board("BK-A", state=store.BOARD_FAILED, members=("000001",)),)),
        )


def test_unknown_pool_writes_zero_rows_and_null_count(engine):
    out = store.append_observation(
        engine, _batch(run_key="rk-pool-unknown", limit_up=(), pool_state=store.POOL_UNKNOWN)
    )
    assert out.inserted is True
    assert out.limit_up_written == 0
    run = _runs(engine)[0]
    assert run["pool_state"] == store.POOL_UNKNOWN
    # None(没取到) ≠ 0(今天一只都没涨停)
    assert run["limit_up_count"] is None
    assert _count(engine, store.THEME_OBS_LIMITUP) == 0


def test_pool_ok_requires_codes_to_be_present(engine):
    with pytest.raises(store.EvidenceError, match="pool"):
        store.append_observation(engine, _batch(limit_up=(), pool_state=store.POOL_OK))
    assert _count(engine, store.THEME_OBS_RUN) == 0


def test_duplicate_board_codes_are_rejected_not_deduped(engine):
    with pytest.raises(store.EvidenceError, match="重复"):
        store.append_observation(
            engine, _batch(boards=(_board("BK-A"), _board("BK-A", members=("000002",))))
        )
    assert _count(engine, store.THEME_OBS_BOARD) == 0


def test_duplicate_pool_codes_are_rejected(engine):
    with pytest.raises(store.EvidenceError):
        store.append_observation(
            engine,
            _batch(
                limit_up=(
                    store.LimitUpEvidence(stock_code="000001"),
                    store.LimitUpEvidence(stock_code="000001"),
                )
            ),
        )


# ── 5. 状态与覆盖率 ─────────────────────────────────────────────────────
def test_coverage_below_threshold_marks_partial(engine):
    boards = (
        _board("BK-A"),
        _board("BK-B", state=store.BOARD_FAILED, members=None),
        _board("BK-C", state=store.BOARD_FAILED, members=None),
        _board("BK-D", state=store.BOARD_FAILED, members=None),
    )
    out = store.append_observation(engine, _batch(run_key="rk-partial", boards=boards))
    assert out.status == store.STATUS_PARTIAL
    assert out.coverage == 0.25
    assert _runs(engine)[0]["status"] == store.STATUS_PARTIAL


def test_coverage_at_threshold_does_not_claim_complete(engine):
    boards = (
        _board("BK-A"),
        _board("BK-B"),
        _board("BK-C", state=store.BOARD_FAILED, members=None),
        _board("BK-D", state=store.BOARD_FAILED, members=None),
    )
    out = store.append_observation(engine, _batch(run_key="rk-boundary", boards=boards))
    assert out.coverage == store.MIN_COVERAGE_FOR_RANKING
    assert out.status == store.STATUS_PARTIAL


def test_zero_boards_is_partial_never_complete(engine):
    out = store.append_observation(
        engine, _batch(run_key="rk-empty", boards=(), limit_up=(), pool_state=store.POOL_UNKNOWN)
    )
    assert out.status == store.STATUS_PARTIAL
    assert out.coverage is None
    assert _runs(engine)[0]["boards_total"] == 0


def test_refused_run_writes_no_details_and_keeps_reason(engine):
    out = store.append_observation(
        engine,
        _batch(
            run_key="rk-refused",
            boards=(),
            limit_up=(),
            pool_state=store.POOL_UNKNOWN,
            source_fresh=False,
            refused_reason="source_stale",
        ),
    )
    assert out.inserted is True
    assert out.status == store.STATUS_REFUSED
    assert _count(engine, store.THEME_OBS_BOARD) == 0
    assert _count(engine, store.THEME_OBS_LIMITUP) == 0
    run = _runs(engine)[0]
    assert run["status"] == store.STATUS_REFUSED
    assert run["refused_reason"] == "source_stale"
    assert run["limit_up_count"] is None


def test_refused_run_cannot_carry_evidence(engine):
    with pytest.raises(store.EvidenceError, match="refused"):
        store.append_observation(engine, _batch(refused_reason="source_stale"))
    assert _count(engine, store.THEME_OBS_RUN) == 0


def test_stale_source_must_be_refused_explicitly(engine):
    with pytest.raises(store.EvidenceError, match="source_fresh|refused"):
        store.append_observation(engine, _batch(source_fresh=False))
    assert _count(engine, store.THEME_OBS_RUN) == 0


def test_trade_date_mismatch_must_be_refused_explicitly(engine):
    with pytest.raises(store.EvidenceError, match="source_trade_date|refused"):
        store.append_observation(engine, _batch(source_trade_date="20261113"))
    assert _count(engine, store.THEME_OBS_RUN) == 0


def test_refused_batch_still_requires_a_reason(engine):
    with pytest.raises(store.EvidenceError, match="refused_reason"):
        store.append_observation(
            engine, _batch(boards=(), limit_up=(), pool_state=store.POOL_UNKNOWN, refused_reason="")
        )


# ── 6. 日锁与方言 ───────────────────────────────────────────────────────
def test_day_lock_key_is_stable_namespaced_and_day_sensitive():
    assert store.day_lock_key(DAY) == store.day_lock_key(DAY)
    assert store.day_lock_key(DAY) != store.day_lock_key("20261117")
    assert isinstance(store.day_lock_key(DAY), int)
    # 不依赖 Python hash(): 它带进程随机化, 跨进程会算出不同 key, 锁等于没加。
    src = inspect.getsource(store.day_lock_key)
    assert "DAY_LOCK_NS" in src and "hashlib.blake2b" in src


def test_lock_plan_is_dialect_driven_for_sqlite_and_postgres():
    kind, sql = store._lock_plan("postgresql")
    assert "pg_advisory_xact_lock" in sql
    kind, sql = store._lock_plan("sqlite")
    assert (kind, sql) == ("begin_immediate", "BEGIN IMMEDIATE")


def test_unknown_dialect_refuses_to_write():
    with pytest.raises(store.EvidenceError, match="方言|dialect"):
        store._lock_plan("mysql")


def test_sqlite_lock_refuses_when_a_transaction_is_already_open(engine):
    """锁必须是事务的第一条语句; 已经写了一半再 BEGIN IMMEDIATE 只会得到驱动层乱码。"""
    with engine.connect() as conn:
        conn.execute(
            store.THEME_OBS_RUN.insert().values(
                day=DAY,
                attempt_seq=9,
                run_key="raw-in-tx",
                status=store.STATUS_PARTIAL,
                input_partial=True,
                rule_version=RULE_VERSION,
                source="tq",
                captured_at=NOW.replace(tzinfo=None),
                boards_total=0,
                boards_ok=0,
                boards_empty_ok=0,
                boards_fetch_failed=0,
            )
        )
        with pytest.raises(store.EvidenceError, match="事务|锁|statement"):
            store.append_observation(conn, _batch())
        conn.rollback()

    assert _count(engine, store.THEME_OBS_RUN) == 0


def test_attempt_cap_refuses_without_writing_more_rows(engine):
    for i in range(store.MAX_ATTEMPTS_PER_DAY):
        assert store.append_observation(engine, _batch(run_key=f"rk-{i}")).inserted is True

    out = store.append_observation(engine, _batch(run_key="rk-over-cap"))
    assert out.inserted is False
    assert out.reason == "attempt_cap"
    assert _count(engine, store.THEME_OBS_RUN) == store.MAX_ATTEMPTS_PER_DAY


# ── 7. 时点与未知保持 ───────────────────────────────────────────────────
def test_aware_captured_at_is_normalised_to_beijing_naive(engine):
    utc = dt.datetime(2026, 11, 16, 7, 16, tzinfo=dt.timezone.utc)
    store.append_observation(engine, _batch(run_key="rk-tz", captured_at=utc))
    assert _runs(engine)[0]["captured_at"] == dt.datetime(2026, 11, 16, 15, 16)


def test_unknown_provenance_stays_null_instead_of_false(engine):
    out = store.append_observation(
        engine,
        _batch(
            run_key="rk-unknown-prov",
            boards=(_board("BK-A", has_quote=None),),
            source_fresh=None,
            source_trade_date=None,
        ),
    )
    run = _runs(engine)[0]
    assert out.inserted is True
    assert run["source_fresh"] is None
    assert run["source_trade_date"] is None
    assert run["limit_up_count"] == 1
    assert _boards(engine)[0]["has_quote"] is None


def test_invalid_day_is_rejected_before_any_write(engine):
    with pytest.raises(store.EvidenceError, match="day"):
        store.append_observation(engine, _batch(day="2026-11-16"))
    assert _count(engine, store.THEME_OBS_RUN) == 0


def test_empty_run_key_is_rejected(engine):
    with pytest.raises(store.EvidenceError, match="run_key"):
        store.append_observation(engine, _batch(run_key="  "))


# ── 8. 绑定方式: Engine 与 Connection 都显式 ────────────────────────────
def test_append_accepts_engine_and_connection(file_engine):
    first = store.append_observation(file_engine, _batch())
    with file_engine.connect() as conn:
        second = store.append_observation(conn, _batch(run_key="rk-conn"))
    assert (first.attempt_seq, second.attempt_seq) == (1, 2)
    assert _count(file_engine, store.THEME_OBS_RUN) == 2

    with file_engine.connect() as conn:
        view = store.evidence_for_run(conn, second.run_id)
    assert view["run"]["run_key"] == "rk-conn"
    assert len(view["boards"]) == 1


def test_append_rejects_an_unusable_bind(tmp_path):
    with pytest.raises(store.EvidenceError, match="engine|Connection"):
        store.append_observation(object(), _batch())


def test_invalid_batch_is_rejected_before_the_bind_is_touched(file_engine, monkeypatch):
    """校验必须在开连接之前: 非法批次不该占用池化连接(池满时就是白等)。"""
    opened: list[object] = []
    real_open = store._open_bind

    def _spy(bind):
        opened.append(bind)
        return real_open(bind)

    monkeypatch.setattr(store, "_open_bind", _spy)

    with pytest.raises(store.EvidenceError):
        store.append_observation(
            file_engine, _batch(boards=(_board("BK-A", state=store.BOARD_OK, members=()),))
        )
    assert opened == []  # 一行没写, 也一条连接没开
    assert _count(file_engine, store.THEME_OBS_RUN) == 0


# ── 9. 真并发: 两个写者同时抢同一天 ─────────────────────────────────────
def _thread_engine(path) -> Engine:
    """每个线程自己的 engine(各自一条连接), 争的是同一个文件库的写锁。"""
    eng = create_engine(f"sqlite:///{path}", connect_args={"timeout": 30})
    return eng


def test_two_concurrent_writers_append_two_batches_without_losing_rows(file_engine, tmp_path):
    """P0-1 的现场: 两个写者同时通过"有没有快照"的检查, 后写者抹掉先写者。

    只追加 + 日锁之后, 两边都成功, 但落在**各自的 run** 里: attempt 1/2 两套明细并存,
    谁也没覆盖谁。
    """
    import threading
    from concurrent.futures import ThreadPoolExecutor

    db_path = tmp_path / "theme_obs.db"
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def _worker(key: str, code: str):
        eng = _thread_engine(db_path)
        try:
            barrier.wait(timeout=20)
            return store.append_observation(
                eng, _batch(run_key=key, boards=(_board(code),))
            )
        except BaseException as exc:  # noqa: BLE001 - 线程里要把异常带回来
            errors.append(exc)
            raise
        finally:
            eng.dispose()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(_worker, "rk-thread-a", "BK-A"),
            pool.submit(_worker, "rk-thread-b", "BK-B"),
        ]
        outs = [f.result(timeout=60) for f in futures]

    a, b = outs  # futures 顺序 = 提交顺序, 与两个线程谁先写入无关
    assert not errors, errors
    assert all(o.inserted for o in outs)
    assert sorted(o.attempt_seq for o in outs) == [1, 2]
    assert _count(file_engine, store.THEME_OBS_RUN) == 2
    # 两行明细都在: 旧实现的症状是其中一批的板块凭空消失。
    assert _count(file_engine, store.THEME_OBS_BOARD) == 2
    by_code = {b["board_code"]: b["run_id"] for b in _boards(file_engine)}
    assert by_code == {"BK-A": a.run_id, "BK-B": b.run_id}
    assert a.run_id != b.run_id


def test_two_concurrent_retries_of_the_same_run_key_write_once(file_engine, tmp_path):
    """同一 run_key 的并发重试(commit 结果未知后的重连): 只落一条 run、一套明细。"""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    db_path = tmp_path / "theme_obs.db"
    barrier = threading.Barrier(2)

    def _worker():
        eng = _thread_engine(db_path)
        try:
            barrier.wait(timeout=20)
            return store.append_observation(eng, _batch(run_key="rk-same"))
        finally:
            eng.dispose()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(_worker) for _ in range(2)]
        outs = [f.result(timeout=60) for f in futures]

    assert sorted(o.inserted for o in outs) == [False, True]
    winner = next(o for o in outs if o.inserted)
    loser = next(o for o in outs if not o.inserted)
    assert loser.reason == "run_key_exists"
    assert (loser.run_id, loser.attempt_seq) == (winner.run_id, winner.attempt_seq)
    assert _count(file_engine, store.THEME_OBS_RUN) == 1
    assert _count(file_engine, store.THEME_OBS_BOARD) == 1
    assert _count(file_engine, store.THEME_OBS_LIMITUP) == 1


# ── 10. PostgreSQL: 不连库也能证明表/索引/序列建得出来 ──────────────────
def test_postgresql_ddl_compiles_without_a_live_server():
    """原型声称支持 PG, 但单测不连生产库 —— 用 PG 方言编译 DDL 作为最低限度的证据。

    真正需要隔离 PG 才能验的(advisory lock 争用、断连后 commit 结果未知)列在
    模块文档的"设计限制"里, 不能靠 SQLite 绿了就说 PG 也绿。
    """
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import CreateIndex, CreateTable

    dialect = postgresql.dialect()
    run_ddl = str(CreateTable(store.THEME_OBS_RUN).compile(dialect=dialect))
    assert "SERIAL" in run_ddl
    assert "uq_theme_obs_run_key" in run_ddl
    assert "uq_theme_obs_run_day_attempt" in run_ddl
    for table in (store.THEME_OBS_BOARD, store.THEME_OBS_LIMITUP):
        ddl = str(CreateTable(table).compile(dialect=dialect))
        assert table.name in ddl
        assert "run_id" in ddl
    idx = str(
        CreateIndex(store.THEME_OBS_BOARD.indexes.__iter__().__next__()).compile(dialect=dialect)
    )
    assert "CREATE INDEX" in idx.upper()

