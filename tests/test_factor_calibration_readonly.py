"""因子标定只读建议:只算提案,零写入,不提供任何改权/落地入口。

覆盖:
- 默认入口只读:不建 FactorWeight 行、不写 meta、不写 History、不改
  weight/reason/effective_from。
- 写权参数已撤:mode / approved_by / persist_proposals 不再存在,传即 TypeError
  (绝不静默降级成写权重)。
- apply_pending_proposal 已删除:不存在任何「提案落地」入口。
- pin / auto_calibrate=False / 样本不足 / OOS 不通过 → 只给提案状态,无副作用。
- 评估失败显式返回 error,不得把「失败」伪装成「本轮无提案」。
- 调度器两个入口不传任何写权参数,并把失败显式记 WARNING。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import src.web.models  # noqa: F401  注册 ORM 模型
from src.web.database import Base


def _mem_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _old_date(days_ago=30):
    return (date.today() - timedelta(days=days_ago)).strftime("%Y-%m-%d")


def _seed_pair(db, sid, *, market, snapshot_date, alpha=0.0, ret=0.0, horizon=5,
               catalyst=None):
    from src.web.models import StrategyFactorSnapshot, StrategyOutcome

    db.add(StrategyFactorSnapshot(
        signal_run_id=sid, snapshot_date=snapshot_date, stock_symbol=f"S{sid}",
        stock_market=market, strategy_code="trend_follow",
        alpha_score=alpha, catalyst_score=catalyst, final_score=50.0,
    ))
    db.add(StrategyOutcome(
        signal_run_id=sid, strategy_code="trend_follow", stock_symbol=f"S{sid}",
        stock_market=market, snapshot_date=snapshot_date, horizon_days=horizon,
        target_date=snapshot_date, outcome_return_pct=ret, outcome_status="evaluated",
    ))


def _seed_strong_alpha(db, *, reversed_from: int | None = None, market="CN"):
    """8 个快照日 × 5 只;每日横截面 alpha 与收益完全正相关(IC=+1)。"""
    sid = 1
    for di, days_ago in enumerate((50, 45, 40, 35, 30, 25, 20, 15)):
        rev = reversed_from is not None and di >= reversed_from
        for rank in range(1, 6):
            _seed_pair(db, sid, market=market, snapshot_date=_old_date(days_ago),
                       alpha=float(rank), ret=float(6 - rank) if rev else float(rank))
            sid += 1
    db.commit()


def _count(db, model, **filters):
    q = db.query(model)
    if filters:
        q = q.filter_by(**filters)
    return q.count()


# ------------------------------ 只读:零写入 ------------------------------

def test_calibration_writes_nothing():
    """默认入口只出提案:不建 FactorWeight 行、不写 History、不改任何权重。"""
    from src.core.factor_calibration import calibrate_factor_weights
    from src.web.models import FactorWeight, FactorWeightHistory

    db = _mem_db()
    try:
        _seed_strong_alpha(db)
        res = calibrate_factor_weights("CN", min_samples=5, db=db)

        assert res["mode"] == "readonly"
        assert res["changed"] == 0
        assert res["write_back"] is False
        assert res["rows"] == []
        proposal = {p["factor_code"]: p for p in res["proposals"]}["alpha_score"]
        assert proposal["status"] == "proposed"
        assert proposal["current_weight"] == 1.0
        assert proposal["proposed_weight"] > 1.0
        # 提案里保留观测(IC/IR/样本),只是不落库
        assert proposal["ic"] is not None
        assert proposal["sample_size"] >= 5

        # 零写入:连 lazy seed 行都没有
        assert _count(db, FactorWeight) == 0
        assert _count(db, FactorWeightHistory) == 0
    finally:
        db.close()


def test_calibration_leaves_existing_rows_untouched():
    """已有行:weight/reason/effective_from/meta 全部保持原样。"""
    from src.core.factor_calibration import calibrate_factor_weights
    from src.web.models import FactorWeight, FactorWeightHistory

    db = _mem_db()
    try:
        row = FactorWeight(factor_code="alpha_score", market="CN", weight=1.0,
                           reason="manual", meta={"note": "keep-me"})
        db.add(row)
        db.commit()
        before = (row.weight, row.reason, row.effective_from, dict(row.meta or {}))
        _seed_strong_alpha(db)

        res = calibrate_factor_weights("CN", min_samples=5, db=db)

        db.refresh(row)
        assert (row.weight, row.reason, row.effective_from, dict(row.meta or {})) == before
        assert res["write_back"] is False
        assert _count(db, FactorWeightHistory) == 0
    finally:
        db.close()


@pytest.mark.parametrize("kwargs", [
    {"mode": "apply", "approved_by": "alice"},
    {"mode": "suggest"},
    {"approved_by": "alice"},
    {"persist_proposals": True},
])
def test_write_oriented_kwargs_are_rejected_loudly(kwargs):
    """任何改权/持久化参数都已不存在:调用直接 TypeError,库内零变化。"""
    from src.core.factor_calibration import calibrate_factor_weights
    from src.web.models import FactorWeight, FactorWeightHistory

    db = _mem_db()
    try:
        _seed_strong_alpha(db)
        with pytest.raises(TypeError):
            calibrate_factor_weights("CN", min_samples=5, db=db, **kwargs)
        assert _count(db, FactorWeight) == 0
        assert _count(db, FactorWeightHistory) == 0
    finally:
        db.close()


def test_apply_pending_proposal_is_gone():
    """不存在提案落地入口(审批/写入路径彻底撤除)。"""
    from src.core import factor_calibration as fc

    assert not hasattr(fc, "apply_pending_proposal")
    with pytest.raises(AttributeError):
        getattr(fc, "apply_pending_proposal")


def test_no_mode_constants_exposed():
    """不再暴露 suggest/apply 模式常量,避免调用方以为存在写权开关。"""
    from src.core import factor_calibration as fc

    assert not hasattr(fc, "MODE_APPLY")
    assert not hasattr(fc, "MODE_SUGGEST")


# ------------------------------ 门禁:无副作用 ------------------------------

def test_calibration_respects_pin_and_disabled_auto_calibrate():
    """pin / auto_calibrate=False 的因子不出可执行提案,更不改权重。"""
    from src.core.factor_calibration import calibrate_factor_weights
    from src.web.models import FactorWeight

    db = _mem_db()
    try:
        db.add(FactorWeight(factor_code="alpha_score", market="CN", weight=1.0, is_pinned=True))
        db.add(FactorWeight(factor_code="catalyst_score", market="CN", weight=1.0,
                            auto_calibrate=False))
        db.commit()
        _seed_strong_alpha(db)

        res = calibrate_factor_weights("CN", min_samples=5, db=db)
        by_code = {p["factor_code"]: p for p in res["proposals"]}
        assert by_code["alpha_score"]["status"] == "skipped_pinned"
        assert by_code["catalyst_score"]["status"] == "skipped_disabled"
        assert res["changed"] == 0
        assert _count(db, FactorWeight) == 2
    finally:
        db.close()


def test_calibration_records_oos_rejection_without_side_effects():
    """OOS 符号翻转:提案标记被拒,但不写 meta / History。"""
    from src.core.factor_calibration import calibrate_factor_weights
    from src.web.models import FactorWeight, FactorWeightHistory

    db = _mem_db()
    try:
        db.add(FactorWeight(factor_code="alpha_score", market="CN", weight=1.0))
        db.commit()
        _seed_strong_alpha(db, reversed_from=5)

        res = calibrate_factor_weights("CN", min_samples=5, db=db)
        by_code = {p["factor_code"]: p for p in res["proposals"]}
        assert by_code["alpha_score"]["status"] == "skipped_oos_sign_flip"
        assert res["skipped_oos"] >= 1
        assert res["changed"] == 0

        row = db.query(FactorWeight).filter_by(factor_code="alpha_score", market="CN").one()
        assert not (row.meta or {})
        assert row.weight == 1.0
        assert _count(db, FactorWeightHistory) == 0
    finally:
        db.close()


# ------------------------------ 失败必须显式 ------------------------------

def test_evaluation_failure_is_reported_not_masked(monkeypatch):
    """IC 评估失败 → 返回 error;不得伪装成「检查成功但无提案」。"""
    from src.core import factor_calibration as fc
    from src.web.models import FactorWeight, FactorWeightHistory

    db = _mem_db()
    try:
        _seed_strong_alpha(db)

        def _boom(**kwargs):
            raise RuntimeError("sqlite 读失败")

        monkeypatch.setattr(fc, "evaluate_factor_ic", _boom)
        res = fc.calibrate_factor_weights("CN", min_samples=5, db=db)

        assert "sqlite 读失败" in (res.get("error") or "")
        assert res["proposals"] == []
        assert res["proposed"] == 0
        assert res["write_back"] is False
        assert _count(db, FactorWeight) == 0
        assert _count(db, FactorWeightHistory) == 0
    finally:
        db.close()


# ------------------------------ 旧手工 API 不放宽 ------------------------------

def test_manual_set_factor_weight_path_unchanged():
    """既有手动授权路径语义不变:0.1~3.0 clamp + manual 审计。"""
    from src.core.factor_weights import set_factor_weight
    from src.web.models import FactorWeightHistory

    db = _mem_db()
    try:
        set_factor_weight("alpha_score", "CN", weight=9.0, db=db)
        set_factor_weight("alpha_score", "CN", weight=1.2, db=db)
        hist = db.query(FactorWeightHistory).filter_by(factor_code="alpha_score").all()
        assert [(h.old_weight, h.new_weight, h.reason) for h in hist] == [
            (1.0, 3.0, "manual"), (3.0, 1.2, "manual"),
        ]
    finally:
        db.close()


# ------------------------------ 调度器调用入口 ------------------------------

def _patch_scheduler_jobs(monkeypatch, captured):
    from src.core import context_scheduler as cs

    monkeypatch.setattr(cs, "evaluate_pending_prediction_outcomes", lambda **kw: {})
    monkeypatch.setattr(cs, "evaluate_entry_candidate_outcomes", lambda *a, **kw: {})
    monkeypatch.setattr(cs, "evaluate_strategy_outcomes", lambda *a, **kw: {})
    monkeypatch.setattr(cs, "rebalance_strategy_weights", lambda *a, **kw: {})

    @contextlib.contextmanager
    def _kline(*a, **kw):
        yield None

    monkeypatch.setattr(cs, "kline_source", _kline)

    def _fake_calibrate_all(*a, **kw):
        captured.append({"args": a, "kwargs": kw})
        return {"CN": {"mode": "readonly", "changed": 0, "proposed": 1, "proposals": []}}

    from src.core import factor_calibration as fc

    monkeypatch.setattr(fc, "calibrate_all_markets", _fake_calibrate_all)
    return cs


def test_evaluate_job_passes_no_write_kwargs(monkeypatch):
    captured: list[dict] = []
    cs = _patch_scheduler_jobs(monkeypatch, captured)
    scheduler = cs.ContextMaintenanceScheduler()

    asyncio.run(scheduler._evaluate_job())

    assert captured, "调度任务必须调用因子标定"
    assert captured[0]["kwargs"] == {}
    assert captured[0]["args"] == ()


def test_evaluate_once_passes_no_write_kwargs(monkeypatch):
    captured: list[dict] = []
    cs = _patch_scheduler_jobs(monkeypatch, captured)
    scheduler = cs.ContextMaintenanceScheduler()

    res = asyncio.run(scheduler.evaluate_once())

    assert captured[0]["kwargs"] == {}
    assert res["factor_calibration"]["CN"]["mode"] == "readonly"


def test_scheduler_surfaces_calibration_error_as_warning(monkeypatch, caplog):
    """标定失败(某市场 error)必须在日志里显式 WARNING,并留在返回体。"""
    captured: list[dict] = []
    cs = _patch_scheduler_jobs(monkeypatch, captured)
    broken = {
        "CN": {"market": "CN", "error": "boom", "proposed": 0, "proposals": []},
        "HK": {"market": "HK", "error": "boom", "proposed": 0, "proposals": []},
        "US": {"market": "US", "error": "boom", "proposed": 0, "proposals": []},
    }
    monkeypatch.setattr(
        "src.core.factor_calibration.calibrate_all_markets",
        lambda *a, **kw: broken,
    )
    scheduler = cs.ContextMaintenanceScheduler()

    with caplog.at_level(logging.WARNING, logger="src.core.context_scheduler"):
        res = asyncio.run(scheduler.evaluate_once())

    assert res["factor_calibration"]["CN"]["error"] == "boom"
    assert "boom" in caplog.text
