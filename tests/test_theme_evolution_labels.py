"""题材归属随涨停池同行落库: 保留采集时刻口径, 缺失即 unknown。

这些标签来自源对**同一行**的富化(`tq:get_relation`), 不是跨源拼接;
`block_as_of` 只是"反查归属的日期", 不证明历史成分。
"""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, text

from src.core import theme_evolution_capture as cap
from src.core import theme_evolution_store as store
from src.web import migrations as mig

CST = ZoneInfo("Asia/Shanghai")
NOW = dt.datetime(2026, 11, 16, 16, 20, tzinfo=CST)
DAY = "20261116"


def _pool_row(**over) -> dict:
    row = {
        "code": "000711",
        "name": "示例甲",
        "as_of": "2026-11-16",
        "as_of_basis": "kline",
        "pool_coverage": {
            "status": "complete", "requested_codes": 10, "fetched_codes": 10, "batch_failures": 0,
        },
    }
    row.update(over)
    return row


def _plan(rows):
    return cap.plan_capture(
        now=NOW,
        fetch_boards=lambda: [{"code": "880201", "name": "示例概念", "board_type": "concept"}],
        fetch_members=lambda code: {
            "members": ["000711.SZ"], "data_day": DAY, "date_basis": "source_reported",
        },
        fetch_pool=lambda day: rows,
        source_fresh=True,
        source_trade_date=DAY,
    )


def test_labels_are_carried_with_normalized_block_date():
    out = _plan([_pool_row(
        sector="通用设备",
        concepts=["机器人", "工业母机", "机器人"],
        block_as_of="2026-11-16",
        block_source="tq:get_relation",
    )])
    assert out.pool_state == cap.POOL_OK
    batch = out.as_store_batch()
    row = batch.limit_up[0]
    assert row.sector == "通用设备"
    assert row.concepts == ("机器人", "工业母机"), "去重但保序, 不改写标签原文"
    assert row.block_as_of == "20261116"
    assert row.block_source == "tq:get_relation"


def test_missing_labels_stay_none_not_empty():
    """没取到 ≠ 没有概念: 必须留 None, 不能编成空元组。"""
    out = _plan([_pool_row()])
    row = out.as_store_batch().limit_up[0]
    assert row.sector is None
    assert row.concepts is None
    assert row.block_as_of is None


def test_explicit_empty_concepts_is_preserved_as_empty():
    out = _plan([_pool_row(concepts=[], sector=None)])
    row = out.as_store_batch().limit_up[0]
    assert row.concepts == ()


@pytest.mark.parametrize("bad", ["机器人", {"a": "b"}, 123, ["机器人", 5]])
def test_malformed_concepts_become_unknown_with_note(bad):
    out = _plan([_pool_row(concepts=bad)])
    assert out.pool_state == cap.POOL_OK
    row = out.as_store_batch().limit_up[0]
    assert row.concepts is None
    assert "pool_concepts_malformed" in out.notes


def test_bad_block_date_is_dropped_with_note():
    out = _plan([_pool_row(block_as_of="16/11/2026", sector="通用设备")])
    row = out.as_store_batch().limit_up[0]
    assert row.block_as_of is None
    assert "pool_block_as_of_unparsable" in out.notes


def test_store_rejects_non_sequence_concepts():
    engine = create_engine("sqlite://")
    try:
        with engine.begin() as conn:
            mig._m252_theme_observation_evidence(conn)
        batch = store.ObservationBatch(
            day=DAY, run_key=f"tq:{DAY}:daily", rule_version="v1", source="tq",
            captured_at=dt.datetime(2026, 11, 16, 16, 20, tzinfo=CST),
            limit_up=(store.LimitUpEvidence("000711", "示例", concepts="机器人"),),
            pool_state=store.POOL_OK, pool_data_day=DAY, pool_date_basis="kline",
        )
        with pytest.raises(store.EvidenceError):
            store.append_observation(engine, batch)
    finally:
        engine.dispose()


def test_labels_round_trip_through_migration_schema():
    engine = create_engine("sqlite://")
    try:
        with engine.begin() as conn:
            mig._m252_theme_observation_evidence(conn)
        outcome = store.append_observation(engine, store.ObservationBatch(
            day=DAY, run_key=f"tq:{DAY}:daily", rule_version="v1", source="tq",
            captured_at=dt.datetime(2026, 11, 16, 16, 20, tzinfo=CST),
            boards=(store.BoardEvidence(
                board_code="880201", state=store.BOARD_OK, members=("000711.SZ",),
                data_day=DAY, date_basis="source_reported",
            ),),
            limit_up=(store.LimitUpEvidence(
                "000711", "示例甲", sector="通用设备",
                concepts=("机器人", "工业母机"), block_as_of="20261116",
                block_source="tq:get_relation",
            ),),
            pool_state=store.POOL_OK, pool_data_day=DAY, pool_date_basis="kline",
        ))
        with engine.connect() as conn:
            row = conn.execute(text(
                "SELECT sector, concepts_json, block_as_of, block_source "
                "FROM theme_obs_limitup WHERE run_id = :r"
            ), {"r": outcome.run_id}).one()
        assert row[0] == "通用设备"
        assert row[1] == '["机器人", "工业母机"]'
        assert row[2] == "20261116"
        assert row[3] == "tq:get_relation"
    finally:
        engine.dispose()
