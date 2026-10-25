"""实验记录表：只存事实，verdict 必须来自对照数据。"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.core import harness_experiments as hx
from src.db.models import Base


@pytest.fixture()
def db(monkeypatch):
    eng = create_engine("sqlite://")
    # harness_experiments 用了 PG 的 JSON 类型，SQLite 下需允许（SQLAlchemy 会按 JSON 处理）
    Base.metadata.create_all(eng)
    Session = sessionmaker(bind=eng)
    s = Session()
    yield s
    s.close()


BASE = {"n": 3, "pass_rate": 0.67, "cost": 10.5, "errors": 0}
CAND = {"n": 3, "pass_rate": 1.0, "cost": 11.0, "errors": 0}


def test_写入一条保留记录(db):
    row = hx.record(
        db, recipe_id="postmarket_reviewer", change_summary="涨停梯队从 2 段拆成 3 段",
        baseline=BASE, candidate=CAND, verdict="keep", user_id="u1",
    )
    assert row.id
    assert row.verdict == "keep"
    assert row.baseline["pass_rate"] == 0.67
    assert row.user_id == "u1"


def test_回滚原因逐条落库(db):
    row = hx.record(
        db, recipe_id="chip_analyst", change_summary="把筹码口径换成 ths",
        baseline=BASE, candidate={"pass_rate": 0.3, "cost": 30}, verdict="rollback",
        reasons=["结构合规率下降 67%→30%", "成本上升 186%（上限 20%）"],
    )
    assert len(row.reasons) == 2
    assert "成本上升" in row.reasons[1]


def test_非法_verdict_直接抛(db):
    with pytest.raises(ValueError):
        hx.record(db, recipe_id="x", change_summary="y", baseline={}, candidate={},
                  verdict="maybe")


def test_改动摘要不能为空(db):
    with pytest.raises(ValueError):
        hx.record(db, recipe_id="x", change_summary="   ", baseline={}, candidate={},
                  verdict="keep")


def test_吃_compare_结果直接入库(db):
    result = {
        "baseline": BASE,
        "candidate": CAND,
        "decision": {"verdict": "keep", "reasons": []},
    }
    row = hx.record_compare(db, result, recipe_id="postmarket_reviewer",
                            change_summary="排序权重调整", evidence_path="/tmp/c.json")
    assert row.verdict == "keep"
    assert row.evidence_path == "/tmp/c.json"


def test_compare_缺_decision_时按回滚处理_不装作保留(db):
    row = hx.record_compare(db, {"baseline": BASE, "candidate": CAND},
                            recipe_id="x", change_summary="y")
    assert row.verdict == "rollback", "没有对照结论就不能记成保留"


def test_按_user_id_隔离(db):
    hx.record(db, recipe_id="a", change_summary="1", baseline={}, candidate={}, verdict="keep", user_id="u1")
    hx.record(db, recipe_id="b", change_summary="2", baseline={}, candidate={}, verdict="keep", user_id="u2")
    assert len(hx.list_recent(db, user_id="u1")) == 1
    assert len(hx.list_recent(db, user_id="u2")) == 1
    assert len(hx.list_recent(db)) == 2


def test_按配方筛选与倒序(db):
    for i in range(3):
        hx.record(db, recipe_id="r1", change_summary=f"c{i}", baseline={}, candidate={}, verdict="keep")
    hx.record(db, recipe_id="r2", change_summary="other", baseline={}, candidate={}, verdict="rollback")
    rows = hx.list_recent(db, recipe_id="r1")
    assert len(rows) == 3


def test_统计保留与回滚(db):
    hx.record(db, recipe_id="a", change_summary="1", baseline={}, candidate={}, verdict="keep")
    hx.record(db, recipe_id="b", change_summary="2", baseline={}, candidate={}, verdict="keep")
    hx.record(db, recipe_id="c", change_summary="3", baseline={}, candidate={}, verdict="rollback")
    st = hx.verdict_stats(db)
    assert st == {"keep": 2, "rollback": 1, "total": 3}


def test_空库统计全零_不编数(db):
    assert hx.verdict_stats(db) == {"keep": 0, "rollback": 0, "total": 0}
