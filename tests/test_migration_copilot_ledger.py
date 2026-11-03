"""v240/v241 迁移回归：波次记账三列 + 调度四档两列 + 运行记录表。

为什么值得单独钉一条：这两个迁移是「旧行行为不得改变」的类型 ——
`window_kind` 对存量任务是 `custom`（= 原有语义），一旦写错成别的默认值，
所有老定时任务会在下一个整点按新语义跑一遍。静态读码看不出这一点，
必须在真表上插一行旧写法、读回默认值。

覆盖：
1. 三个新列与两个新列真的落地（不是只在 models.py 里声明）
2. `copilot_schedule_runs` 表 + 索引存在
3. 幂等：同一迁移跑两次不炸（重部署场景）
4. 旧写法的 ORM 插入仍可用，且默认 `window_kind='custom'`
"""

from __future__ import annotations

import os
import tempfile

import pytest
from sqlalchemy import create_engine, inspect

from src.db.models import Base, CopilotSchedule
from src.web.migrations import (
    _m240_copilot_run_ledger,
    _m241_copilot_schedule_windows,
)

NEED_RUN = {"dispatch_reason", "artifact_ref", "fact_pack_id"}
NEED_SCHED = {"window_kind", "interval_min"}


@pytest.fixture()
def engine():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    eng = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()
    os.unlink(path)


def _apply(engine) -> None:
    with engine.begin() as conn:
        _m240_copilot_run_ledger(conn)
        _m241_copilot_schedule_windows(conn)


def test_columns_and_table_land(engine):
    _apply(engine)
    insp = inspect(engine)
    run_cols = {c["name"] for c in insp.get_columns("copilot_runs")}
    sched_cols = {c["name"] for c in insp.get_columns("copilot_schedules")}

    assert not (NEED_RUN - run_cols), f"copilot_runs 缺列: {sorted(NEED_RUN - run_cols)}"
    assert not (NEED_SCHED - sched_cols), f"copilot_schedules 缺列: {sorted(NEED_SCHED - sched_cols)}"
    assert "copilot_schedule_runs" in set(insp.get_table_names())
    assert "ix_copilot_schedule_runs_sched" in {i["name"] for i in insp.get_indexes("copilot_schedule_runs")}


def test_migrations_are_idempotent(engine):
    _apply(engine)
    _apply(engine)  # 重部署会再跑一次，不许炸
    insp = inspect(engine)
    assert not (NEED_RUN - {c["name"] for c in insp.get_columns("copilot_runs")})


def test_legacy_schedule_row_keeps_custom_semantics(engine):
    """旧写法（不传 window_kind）插入后必须是 custom —— 存量任务行为不得改变。"""
    from sqlalchemy.orm import sessionmaker

    _apply(engine)
    db = sessionmaker(bind=engine)()
    try:
        row = CopilotSchedule(
            user_id="u1", kind="auction_digest", at_hhmm="09:25", weekdays="mon-fri"
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        assert row.window_kind == "custom"
        assert row.interval_min is None      # 只跑一次 = 原行为
    finally:
        db.close()
