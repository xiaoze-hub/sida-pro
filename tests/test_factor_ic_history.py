"""B9 收口钉子: 因子 IC 时序落库的**三条口径** —— 幂等 / 样本不足留痕 / 绝不补 0。"""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

@pytest.fixture()
def db(tmp_path):
    """临时 SQLite 库 + 跑迁移(拿到真实的 factor_ic_snapshots 结构)。"""
    from src.web.migrations import run_versioned_migrations

    eng = create_engine(f"sqlite:///{tmp_path/'t.db'}")
    run_versioned_migrations(eng)
    S = sessionmaker(bind=eng)
    s = S()
    yield s
    s.close()


def _ins(rows):
    """伪造 evaluate_factor_ic 的返回(不碰真实数据, 只验落库口径)。"""
    return {"horizon": 5, "days": 90, "market": "CN", "factors": rows}


def test_it_writes_one_row_per_factor(db, monkeypatch):
    from src.core import factor_eval, factor_ic_history as fih

    monkeypatch.setattr(factor_eval, "evaluate_factor_ic",
                        lambda **kw: _ins({"mom": {"ic": 0.03, "ir": 0.5, "sample_size": 100, "ic_periods": 20},
                                           "rev": {"ic": -0.02, "sample_size": 80, "ic_periods": 18}}))
    r = fih.snapshot_ic(db=db, today=date(2026, 9, 16))
    assert r["written"] == 2
    rows = db.execute(text("SELECT factor_code, ic, ic_periods FROM factor_ic_snapshots ORDER BY factor_code")).all()
    assert rows == [("mom", 0.03, 20), ("rev", -0.02, 18)]


def test_idempotent_same_day_rerun_updates_not_duplicates(db, monkeypatch):
    """幂等: 同一天重跑只更新, 不重复插 —— 否则时序会被重复点污染。"""
    from src.core import factor_eval, factor_ic_history as fih

    monkeypatch.setattr(factor_eval, "evaluate_factor_ic",
                        lambda **kw: _ins({"mom": {"ic": 0.01, "ic_periods": 20}}))
    fih.snapshot_ic(db=db, today=date(2026, 9, 16))
    monkeypatch.setattr(factor_eval, "evaluate_factor_ic",
                        lambda **kw: _ins({"mom": {"ic": 0.09, "ic_periods": 21}}))
    fih.snapshot_ic(db=db, today=date(2026, 9, 16))

    n = db.execute(text("SELECT count(*) FROM factor_ic_snapshots")).scalar()
    ic = db.execute(text("SELECT ic FROM factor_ic_snapshots")).scalar()
    assert n == 1 and ic == 0.09          # 更新为新值, 不是插两行


def test_insufficient_samples_still_written_with_null_ic(db, monkeypatch):
    """**样本不足也留痕**: ic 为 NULL + ic_periods 记下, 而不是整行不写。"""
    from src.core import factor_eval, factor_ic_history as fih

    monkeypatch.setattr(factor_eval, "evaluate_factor_ic",
                        lambda **kw: _ins({"newfac": {"ic": None, "ic_t": None, "ir": None,
                                                       "sample_size": 5, "ic_periods": 1}}))
    r = fih.snapshot_ic(db=db, today=date(2026, 9, 16))
    assert r["written"] == 1 and r["insufficient"] == 1
    row = db.execute(text("SELECT ic, ic_periods FROM factor_ic_snapshots")).all()
    assert row == [(None, 1)]             # NULL 不是 0


def test_never_fabricates_zero_for_missing_metrics(db, monkeypatch):
    """任何缺的指标列一律 NULL —— 0 会被读成"真没相关性"。"""
    from src.core import factor_eval, factor_ic_history as fih

    monkeypatch.setattr(factor_eval, "evaluate_factor_ic",
                        lambda **kw: _ins({"mom": {"ic": None, "sample_size": 0}}))
    fih.snapshot_ic(db=db, today=date(2026, 9, 16))
    row = db.execute(text("SELECT ic, ic_t, ic_std, ir, ic_holdout, ic_pooled FROM factor_ic_snapshots")).one()
    assert all(v is None for v in row)


def test_calc_error_writes_nothing(db, monkeypatch):
    """计算失败**一行都不写**: 落一堆 NULL 会被误读成"这天样本不足"。"""
    from src.core import factor_eval, factor_ic_history as fih

    monkeypatch.setattr(factor_eval, "evaluate_factor_ic",
                        lambda **kw: {"horizon": 5, "days": 90, "market": "CN",
                                      "factors": {}, "error": "boom"})
    r = fih.snapshot_ic(db=db, today=date(2026, 9, 16))
    assert r["written"] == 0 and "boom" in r["error"]
    assert db.execute(text("SELECT count(*) FROM factor_ic_snapshots")).scalar() == 0


def test_history_ascending_and_horizon_scoped(db, monkeypatch):
    from src.core import factor_eval, factor_ic_history as fih

    ic_by_day = {"2026-09-14": 0.01, "2026-09-15": 0.02, "2026-09-16": 0.03}
    for d, ic in ic_by_day.items():
        monkeypatch.setattr(factor_eval, "evaluate_factor_ic",
                            lambda ic=ic, **kw: _ins({"mom": {"ic": ic, "ic_periods": 20}}))
        fih.snapshot_ic(db=db, today=date.fromisoformat(d))
    monkeypatch.setattr(factor_eval, "evaluate_factor_ic",
                        lambda **kw: _ins({"mom": {"ic": 9.9, "ic_periods": 20}}))
    fih.snapshot_ic(db=db, today=date(2026, 9, 16), horizon=10)   # 另一 horizon, 不能混进来

    h = fih.history(db=db, horizon=5, days=10)
    assert [x["trade_date"] for x in h] == ["2026-09-14", "2026-09-15", "2026-09-16"]   # 升序
    assert [x["ic"] for x in h] == [0.01, 0.02, 0.03]
