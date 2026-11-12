"""因子标定安全提案模式:默认只建议、不写有效权重。

覆盖:
- 默认(suggest)模式零写入:不建行、不写 meta、不写 History、不改 weight/reason。
- 显式 apply 模式仍需 approved_by(人工授权),否则拒绝且零写入。
- 提案持久化必须显式开启(persist_proposals=True),且只写已存在行,绝不 seed。
- 失败回滚:apply 中途异常 → 本轮无部分写入。
- 待审提案的落地走既有 manual 路径(set_factor_weight),不新增自动历史调整。
- 调度器两个入口(context_scheduler._evaluate_job / evaluate_once)默认 suggest。
"""

from __future__ import annotations

import asyncio
import contextlib
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


# ------------------------------ 默认 suggest 模式 ------------------------------

def test_default_mode_is_suggest_and_writes_nothing():
    """默认入口只出提案:不建 FactorWeight 行、不写 History、不改任何权重。"""
    from src.core.factor_calibration import calibrate_factor_weights
    from src.web.models import FactorWeight, FactorWeightHistory

    db = _mem_db()
    try:
        _seed_strong_alpha(db)
        res = calibrate_factor_weights("CN", min_samples=5, db=db)

        assert res["mode"] == "suggest"
        assert res["changed"] == 0 and res["applied"] == 0
        assert res["rows"] == []
        proposal = {p["factor_code"]: p for p in res["proposals"]}["alpha_score"]
        assert proposal["status"] == "proposed"
        assert proposal["current_weight"] == 1.0
        assert proposal["proposed_weight"] > 1.0

        # 零写入:连 lazy seed 行都没有
        assert _count(db, FactorWeight) == 0
        assert _count(db, FactorWeightHistory) == 0
    finally:
        db.close()


def test_suggest_does_not_touch_existing_row_weight_reason_or_meta():
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
        assert res["persisted"] is False
        assert _count(db, FactorWeightHistory) == 0
    finally:
        db.close()


def test_suggest_persist_proposals_only_touches_existing_rows():
    """显式 persist_proposals=True:只把待审提案写进已存在行的 meta,不 seed、不改权重。"""
    from src.core.factor_calibration import calibrate_factor_weights
    from src.web.models import FactorWeight, FactorWeightHistory

    db = _mem_db()
    try:
        db.add(FactorWeight(factor_code="alpha_score", market="CN", weight=1.0))
        db.commit()
        _seed_strong_alpha(db)

        res = calibrate_factor_weights("CN", min_samples=5, db=db, persist_proposals=True)

        assert res["persisted"] is True
        row = db.query(FactorWeight).filter_by(factor_code="alpha_score", market="CN").one()
        pending = (row.meta or {}).get("pending_proposal")
        assert pending and pending["proposed_weight"] > 1.0
        assert pending["base_weight"] == 1.0
        assert row.weight == 1.0
        assert row.reason != "auto"  # 未走自动写权路径
        # 只碰 alpha_score 一行,其余因子没有被 seed
        assert _count(db, FactorWeight) == 1
        assert _count(db, FactorWeightHistory) == 0
    finally:
        db.close()


def test_suggest_respects_pin_and_disabled_auto_calibrate():
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


def test_suggest_records_oos_rejection_without_side_effects():
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


# ------------------------------ 显式 apply 模式 ------------------------------

def test_apply_without_approver_is_rejected_and_writes_nothing():
    """申请写权重必须带 approved_by;缺失 → ValueError,库内零变化。"""
    from src.core.factor_calibration import calibrate_factor_weights
    from src.web.models import FactorWeight, FactorWeightHistory

    db = _mem_db()
    try:
        _seed_strong_alpha(db)
        with pytest.raises(ValueError):
            calibrate_factor_weights("CN", mode="apply", min_samples=5, db=db)
        assert _count(db, FactorWeight) == 0
        assert _count(db, FactorWeightHistory) == 0
    finally:
        db.close()


def test_unknown_mode_is_rejected_and_writes_nothing():
    """未知模式不得静默回落到写权重。"""
    from src.core.factor_calibration import calibrate_factor_weights
    from src.web.models import FactorWeight

    db = _mem_db()
    try:
        _seed_strong_alpha(db)
        with pytest.raises(ValueError):
            calibrate_factor_weights("CN", mode="auto", min_samples=5, db=db)
        assert _count(db, FactorWeight) == 0
    finally:
        db.close()


def test_apply_with_approver_moves_weight_and_audits_approval():
    """显式人工授权后才写权重,审计里能看到授权人。"""
    from src.core.factor_calibration import calibrate_factor_weights
    from src.web.models import FactorWeight, FactorWeightHistory

    db = _mem_db()
    try:
        _seed_strong_alpha(db)
        res = calibrate_factor_weights("CN", mode="apply", approved_by="alice",
                                       min_samples=5, db=db)

        assert res["mode"] == "apply" and res["changed"] >= 1
        row = db.query(FactorWeight).filter_by(factor_code="alpha_score", market="CN").one()
        assert row.weight > 1.0
        hist = db.query(FactorWeightHistory).filter_by(
            factor_code="alpha_score", market="CN", reason="auto").all()
        assert len(hist) == 1
        assert (hist[0].meta or {}).get("approved_by") == "alice"
    finally:
        db.close()


def test_apply_rolls_back_all_changes_on_midway_failure(monkeypatch):
    """apply 中途异常 → rollback,先前因子也不得留下部分写入。"""
    from src.core import factor_calibration as fc
    from src.web.models import FactorWeight, FactorWeightHistory

    db = _mem_db()
    try:
        # alpha 与 catalyst 都有强 IC,保证 alpha 先被写入、catalyst 再抛错。
        sid = 1
        for days_ago in (50, 45, 40, 35, 30, 25, 20, 15):
            for rank in range(1, 6):
                _seed_pair(db, sid, market="CN", snapshot_date=_old_date(days_ago),
                           alpha=float(rank), catalyst=float(rank), ret=float(rank))
                sid += 1
        db.commit()
        real_compute = fc.compute_target

        def _boom(code, ic, ir, **kw):
            if code == "catalyst_score":
                raise RuntimeError("injected failure")
            return real_compute(code, ic, ir, **kw)

        monkeypatch.setattr(fc, "compute_target", _boom)
        res = fc.calibrate_factor_weights("CN", mode="apply", approved_by="alice",
                                          min_samples=5, db=db)

        assert res["error"]
        assert res["changed"] == 0
        assert _count(db, FactorWeight) == 0
        assert _count(db, FactorWeightHistory) == 0
    finally:
        db.close()


# ------------------------------ 提案落地(manual 路径) ------------------------------

def test_apply_pending_proposal_goes_through_manual_audit_path():
    """待审提案落地 = 走既有 set_factor_weight(manual 审计),并清掉待审标记。"""
    from src.core.factor_calibration import apply_pending_proposal, calibrate_factor_weights
    from src.core.factor_weights import set_factor_weight
    from src.web.models import FactorWeight, FactorWeightHistory

    db = _mem_db()
    try:
        set_factor_weight("alpha_score", "CN", weight=1.0, db=db)  # 人工初始 1.0
        _seed_strong_alpha(db)
        calibrate_factor_weights("CN", min_samples=5, db=db, persist_proposals=True)

        res = apply_pending_proposal("alpha_score", "CN", approved_by="bob", db=db)
        row = db.query(FactorWeight).filter_by(factor_code="alpha_score", market="CN").one()
        assert row.weight == res["weight"] > 1.0
        assert row.reason == "manual"
        assert "pending_proposal" not in (row.meta or {})
        assert (row.meta or {}).get("applied_proposal", {}).get("approved_by") == "bob"

        hist = db.query(FactorWeightHistory).filter_by(
            factor_code="alpha_score", market="CN").all()
        assert [h.reason for h in hist] == ["manual"]
    finally:
        db.close()


def test_apply_pending_proposal_requires_approver():
    from src.core.factor_calibration import apply_pending_proposal

    db = _mem_db()
    try:
        with pytest.raises(ValueError):
            apply_pending_proposal("alpha_score", "CN", approved_by="  ", db=db)
    finally:
        db.close()


def test_apply_pending_proposal_rejects_stale_base():
    """提案基线权重已被人改动 → 拒绝落地,不改权重。"""
    from src.core.factor_calibration import apply_pending_proposal, calibrate_factor_weights
    from src.core.factor_weights import set_factor_weight
    from src.web.models import FactorWeight

    db = _mem_db()
    try:
        set_factor_weight("alpha_score", "CN", weight=1.0, db=db)
        _seed_strong_alpha(db)
        calibrate_factor_weights("CN", min_samples=5, db=db, persist_proposals=True)
        set_factor_weight("alpha_score", "CN", weight=1.3, db=db)  # 人工抢先改

        with pytest.raises(ValueError):
            apply_pending_proposal("alpha_score", "CN", approved_by="bob", db=db)
        row = db.query(FactorWeight).filter_by(factor_code="alpha_score", market="CN").one()
        assert row.weight == 1.3
    finally:
        db.close()


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
        captured.append(kw)
        return {"CN": {"mode": "suggest", "changed": 0, "proposed": 1, "proposals": []}}

    from src.core import factor_calibration as fc

    monkeypatch.setattr(fc, "calibrate_all_markets", _fake_calibrate_all)
    return cs


def test_evaluate_job_uses_suggest_mode(monkeypatch):
    captured: list[dict] = []
    cs = _patch_scheduler_jobs(monkeypatch, captured)
    scheduler = cs.ContextMaintenanceScheduler()

    asyncio.run(scheduler._evaluate_job())

    assert captured, "调度任务必须调用因子标定"
    assert captured[0].get("mode") == "suggest"
    assert not captured[0].get("persist_proposals")


def test_evaluate_once_uses_suggest_mode(monkeypatch):
    captured: list[dict] = []
    cs = _patch_scheduler_jobs(monkeypatch, captured)
    scheduler = cs.ContextMaintenanceScheduler()

    res = asyncio.run(scheduler.evaluate_once())

    assert captured[0].get("mode") == "suggest"
    assert res["factor_calibration"]["CN"]["mode"] == "suggest"


def test_evaluate_once_surfaces_proposal_counts(monkeypatch):
    """运维可见:返回体里要能读到提案数,而不是只有 changed=0。"""
    captured: list[dict] = []
    cs = _patch_scheduler_jobs(monkeypatch, captured)
    monkeypatch.setattr(
        "src.core.factor_calibration.calibrate_all_markets",
        lambda *a, **kw: {"CN": {"mode": "suggest", "changed": 0, "proposed": 3, "proposals": [
            {"factor_code": "alpha_score", "status": "proposed"}]}},
    )
    scheduler = cs.ContextMaintenanceScheduler()

    res = asyncio.run(scheduler.evaluate_once())
    assert res["factor_calibration"]["CN"]["proposed"] == 3
