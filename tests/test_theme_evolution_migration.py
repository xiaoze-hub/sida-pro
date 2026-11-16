"""迁移 252 与 store 元数据必须逐列一致, 且真能写读。

为什么单独一个文件: 生产只由迁移建表, `theme_evolution_store.create_schema`
是测试专用。两边一旦漂移, 单测全绿、上线第一次写入就炸 —— 这类错误在
`create_schema` 建的库里永远看不到, 所以这里**只用迁移建表**再走 store。
"""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine

from src.core import theme_evolution_store as store
from src.web import migrations as mig

CST = ZoneInfo("Asia/Shanghai")
DAY = "20261116"
TABLES = ("theme_obs_run", "theme_obs_board", "theme_obs_limitup")


def _migrated_engine() -> Engine:
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        mig._m252_theme_observation_evidence(conn)
    return engine


def _meta_columns(table: str) -> list[tuple[str, bool, bool]]:
    t = store.THEME_OBS_METADATA.tables[table]
    return [(c.name, bool(c.nullable), bool(c.primary_key)) for c in t.columns]


def test_tables_are_created_by_the_migration():
    engine = _migrated_engine()
    try:
        names = set(inspect(engine).get_table_names())
        assert set(TABLES) <= names
    finally:
        engine.dispose()


def test_columns_nullability_and_pk_match_store_metadata():
    """列名/可空性/主键必须一致。

    `id` 的可空性不比: SQLite 的 `INTEGER PRIMARY KEY` 是 rowid 别名, 反射会报
    nullable=True, 而 SQLAlchemy 的 Integer 主键是 nullable=False —— 两种方言
    语义上都不允许 NULL 主键, 拿它做断言只会制造假失败。
    """
    engine = _migrated_engine()
    try:
        for table in TABLES:
            actual = {
                c["name"]: (bool(c["nullable"]), bool(c["primary_key"]))
                for c in inspect(engine).get_columns(table)
            }
            expected = {name: (nullable, pk) for name, nullable, pk in _meta_columns(table)}
            assert set(actual) == set(expected), f"{table} 列集合漂移"
            for name in expected:
                if name == "id":
                    assert actual[name][1] is True, f"{table}.id 必须是主键"
                    continue
                assert actual[name] == expected[name], (
                    f"{table}.{name} 可空性/主键与 store 元数据不一致: "
                    f"迁移={actual[name]} 元数据={expected[name]}"
                )
    finally:
        engine.dispose()


def test_named_indexes_exist():
    engine = _migrated_engine()
    try:
        expected = {
            "theme_obs_run": {
                "uq_theme_obs_run_day_attempt",
                "uq_theme_obs_run_key",
                "ix_theme_obs_run_day",
            },
            "theme_obs_board": {
                "uq_theme_obs_board_run_code",
                "ix_theme_obs_board_day",
                "ix_theme_obs_board_run",
            },
            "theme_obs_limitup": {
                "uq_theme_obs_limitup_run_code",
                "ix_theme_obs_limitup_day",
                "ix_theme_obs_limitup_run",
            },
        }
        insp = inspect(engine)
        for table, names in expected.items():
            got = {ix["name"] for ix in insp.get_indexes(table)}
            assert names <= got, f"{table} 缺索引: {names - got}"
    finally:
        engine.dispose()


def test_migration_is_idempotent():
    engine = _migrated_engine()
    try:
        with engine.begin() as conn:
            mig._m252_theme_observation_evidence(conn)
        with engine.begin() as conn:
            mig._m252_theme_observation_evidence(conn)
        assert set(TABLES) <= set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def test_migration_leaves_existing_tables_and_rows_untouched():
    engine = create_engine("sqlite://")
    try:
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE unrelated_note (id INTEGER PRIMARY KEY, body TEXT)"))
            conn.execute(text("INSERT INTO unrelated_note (id, body) VALUES (1, 'keep me')"))
            mig._m252_theme_observation_evidence(conn)
        with engine.connect() as conn:
            assert conn.execute(text("SELECT body FROM unrelated_note")).scalar_one() == "keep me"
    finally:
        engine.dispose()


def _batch(**over) -> store.ObservationBatch:
    base: dict = dict(
        day=DAY,
        run_key=f"tq:{DAY}:daily",
        rule_version="theme_evo_capture_v1",
        source="tq",
        captured_at=dt.datetime(2026, 11, 16, 16, 10, tzinfo=CST),
        boards=(
            store.BoardEvidence(
                board_code="880201", state=store.BOARD_OK, board_name="示例概念",
                board_type="concept", data_day=DAY, date_basis="source_reported",
                members=("000711.SZ", "600843.SH"),
            ),
            store.BoardEvidence(
                board_code="880202", state=store.BOARD_EMPTY, board_name="空板块",
                board_type="concept", members=(),
            ),
        ),
        limit_up=(store.LimitUpEvidence("000711.SZ", "示例甲"),),
        pool_state=store.POOL_OK,
        pool_data_day=DAY,
        pool_date_basis="kline",
        source_fresh=True,
        source_trade_date=DAY,
    )
    base.update(over)
    return store.ObservationBatch(**base)


def test_store_writes_and_reads_back_on_migration_created_schema():
    """最关键的一条: 只由迁移建表, 再用 store 写/读。列漂移会在这里直接炸。"""
    engine = _migrated_engine()
    try:
        outcome = store.append_observation(engine, _batch())
        assert outcome.inserted is True
        assert outcome.run_id is not None

        with engine.connect() as conn:
            evidence = store.evidence_for_run(conn, outcome.run_id)
        assert evidence is not None
        run = evidence["run"]
        assert run["day"] == DAY
        # 一个 empty_ok 板块把覆盖率压到 0.5, 因此这一批是 partial —— 这正是
        # "空板块不等于取到了成员"的口径; complete 的情形另有专测。
        assert run["status"] == store.STATUS_PARTIAL
        assert run["pool_state"] == store.POOL_OK
        assert run["pool_date_basis"] == "kline"
        assert run["limit_up_count"] == 1
        assert run["source_trade_date"] == DAY
        assert run["source_fresh"] in (True, 1)

        boards = {row["board_code"]: row for row in evidence["boards"]}
        assert boards["880201"]["state"] == store.BOARD_OK
        assert boards["880201"]["member_count"] == 2
        assert boards["880202"]["state"] == store.BOARD_EMPTY
        assert boards["880202"]["member_count"] == 0
        assert [row["stock_code"] for row in evidence["limit_up"]] == ["000711.SZ"]
    finally:
        engine.dispose()


def test_unknown_pool_is_null_not_zero_on_migration_schema():
    engine = _migrated_engine()
    try:
        outcome = store.append_observation(
            engine,
            _batch(
                run_key=f"tq:{DAY}:partial",
                limit_up=(),
                pool_state=store.POOL_UNKNOWN,
                pool_data_day=None,
                pool_date_basis=None,
            ),
        )
        assert outcome.inserted is True
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT pool_state, limit_up_count FROM theme_obs_run WHERE id = :i"),
                {"i": outcome.run_id},
            ).one()
        assert row[0] == store.POOL_UNKNOWN
        assert row[1] is None, "取不到涨停池必须写 NULL, 不能写 0"
    finally:
        engine.dispose()


def test_all_boards_ok_is_complete_on_migration_schema():
    engine = _migrated_engine()
    try:
        outcome = store.append_observation(
            engine,
            _batch(
                boards=(
                    store.BoardEvidence(
                        board_code="880201", state=store.BOARD_OK, board_name="示例概念",
                        board_type="concept", data_day=DAY, date_basis="source_reported",
                        members=("000711.SZ",),
                    ),
                ),
            ),
        )
        with engine.connect() as conn:
            evidence = store.evidence_for_run(conn, outcome.run_id)
        assert evidence is not None
        assert evidence["run"]["status"] == store.STATUS_COMPLETE
    finally:
        engine.dispose()


def test_second_attempt_same_day_keeps_both_runs():
    engine = _migrated_engine()
    try:
        first = store.append_observation(engine, _batch())
        second = store.append_observation(
            engine,
            _batch(
                run_key=f"tq:{DAY}:daily:retry",
                captured_at=dt.datetime(2026, 11, 16, 16, 40, tzinfo=CST),
            ),
        )
        assert {first.attempt_seq, second.attempt_seq} == {1, 2}
        with engine.connect() as conn:
            assert len(store.runs_for_day(conn, DAY)) == 2
    finally:
        engine.dispose()
