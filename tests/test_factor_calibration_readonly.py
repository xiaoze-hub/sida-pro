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

        # 显式失败: 安全文案 + 关联号; 异常原文只进服务端日志, 不出现在响应里。
        assert res["ok"] is False
        assert "sqlite 读失败" not in (res.get("error") or "")
        assert res["error_ref"].startswith("ERR-")
        assert res["error_ref"] in res["error"]
        assert res["proposals"] == []
        assert res["proposed"] == 0
        assert res["write_back"] is False
        assert _count(db, FactorWeight) == 0
        assert _count(db, FactorWeightHistory) == 0
    finally:
        db.close()


# ------------------------------ 旧手工 API 不放宽 ------------------------------

def test_manual_set_factor_weight_path_unchanged():
    """既有手动授权路径语义不变:0.1~3.0 clamp + manual 审计。

    注意: 身份是硬门槛(空 actor 直接 ValueError), 所以这里显式带授权身份 ——
    语义断言(clamp / manual 历史 / 审计可追溯)与改造前一致。
    """
    from src.core.factor_weights import set_factor_weight
    from src.web.models import FactorWeightHistory

    db = _mem_db()
    try:
        set_factor_weight("alpha_score", "CN", weight=9.0, db=db, actor="alice")
        set_factor_weight("alpha_score", "CN", weight=1.2, db=db, actor="alice")
        hist = db.query(FactorWeightHistory).filter_by(factor_code="alpha_score").all()
        assert [(h.old_weight, h.new_weight, h.reason) for h in hist] == [
            (1.0, 3.0, "manual"), (3.0, 1.2, "manual"),
        ]
        assert all((h.meta or {}).get("by") == "alice" for h in hist)
    finally:
        db.close()


# ------------------------------ 身份是硬门槛(2026-11-12 复审) -------------------


def test_set_factor_weight_rejects_anonymous_and_writes_nothing():
    """空身份 / 无 actor 一律 ValueError, 且**一个字节都不写**(不 seed 行)。"""
    from src.core.factor_weights import set_factor_weight
    from src.web.models import AuditLog, FactorWeight, FactorWeightHistory

    db = _mem_db()
    try:
        for kwargs in ({}, {"actor": ""}, {"actor": "   "}, {"actor": None}):
            with pytest.raises(ValueError):
                set_factor_weight("alpha_score", "CN", weight=1.5, db=db, **kwargs)
        assert _count(db, FactorWeight) == 0, "被拒绝的写不得留下 lazy seed 行"
        assert _count(db, FactorWeightHistory) == 0
        assert _count(db, AuditLog) == 0
    finally:
        db.close()


def test_set_factor_weight_accepts_explicit_system_identity():
    """系统身份必须显式声明(actor_kind=system + system: 前缀), 审计可区分。"""
    import json

    from src.core.factor_weights import set_factor_weight
    from src.web.models import AuditLog, FactorWeightHistory

    db = _mem_db()
    try:
        res = set_factor_weight(
            "catalyst_score", "HK", weight=1.1, db=db,
            actor="system:scheduler", actor_kind="system", via="scheduler",
        )
        assert res["weight"] == 1.1
        hist = db.query(FactorWeightHistory).filter_by(factor_code="catalyst_score").one()
        assert (hist.meta or {})["by"] == "system:scheduler"
        assert (hist.meta or {})["by_kind"] == "system"
        audit = db.query(AuditLog).filter_by(action="update_factor_weight").one()
        assert audit.username == "system:scheduler"
        detail = json.loads(audit.detail)
        assert detail["actor_kind"] == "system" and detail["via"] == "scheduler"
    finally:
        db.close()


@pytest.mark.parametrize("kwargs", [
    {"actor": "scheduler", "actor_kind": "system"},   # 缺 system: 前缀
    {"actor": "alice", "actor_kind": "robot"},        # 未知类别
])
def test_set_factor_weight_rejects_malformed_identity(kwargs):
    from src.core.factor_weights import set_factor_weight
    from src.web.models import AuditLog, FactorWeight, FactorWeightHistory

    db = _mem_db()
    try:
        with pytest.raises(ValueError):
            set_factor_weight("alpha_score", "CN", weight=1.5, db=db, **kwargs)
        assert _count(db, FactorWeight) == 0
        assert _count(db, FactorWeightHistory) == 0
        assert _count(db, AuditLog) == 0
    finally:
        db.close()


def test_set_factor_weight_rolls_back_when_audit_fails(monkeypatch):
    """审计写不进去 ⇒ 整笔失败并回滚: 不允许「权重改了但查不到是谁改的」。

    走**自开 session** 的生产路径(db=None): 函数自己回滚, 事后用新 session
    读到的是改动前的值。
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from src.core import factor_weights as fw
    from src.web.database import Base
    from src.web.models import FactorWeight, FactorWeightHistory

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Factory = sessionmaker(bind=engine)
    monkeypatch.setattr(fw, "WriteSessionLocal", Factory)

    seed = Factory()
    try:
        seed.add(FactorWeight(factor_code="alpha_score", market="CN", weight=1.0))
        seed.commit()
    finally:
        seed.close()

    class _BoomAudit:
        def __init__(self, *a, **kw):
            raise RuntimeError("audit 表写失败")

    monkeypatch.setattr(fw, "AuditLog", _BoomAudit)
    with pytest.raises(RuntimeError):
        fw.set_factor_weight("alpha_score", "CN", weight=1.7, actor="alice")

    check = Factory()
    try:
        row = check.query(FactorWeight).filter_by(factor_code="alpha_score").one()
        assert row.weight == 1.0, "审计失败仍改动了权重(未回滚)"
        assert check.query(FactorWeightHistory).count() == 0
    finally:
        check.close()


# ------------------------------ 调度器调用入口 ------------------------------

def _patch_scheduler_jobs(monkeypatch, captured):
    from src.core import context_scheduler as cs

    monkeypatch.setattr(cs, "evaluate_pending_prediction_outcomes", lambda **kw: {})
    monkeypatch.setattr(cs, "evaluate_entry_candidate_outcomes", lambda *a, **kw: {})
    monkeypatch.setattr(cs, "evaluate_strategy_outcomes", lambda *a, **kw: {})
    # 自动调权已停用: 若调度器仍持有该符号, 打桩记录调用以证明它再也不会被触发。
    rebalance_calls: list[dict] = []
    if hasattr(cs, "rebalance_strategy_weights"):
        monkeypatch.setattr(
            cs, "rebalance_strategy_weights",
            lambda *a, **kw: rebalance_calls.append({"args": a, "kwargs": kw}) or {},
        )
    monkeypatch.setattr(cs, "_rebalance_calls_probe", rebalance_calls, raising=False)

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


# ------------------------------ 自动调权必须停用 ------------------------------

def test_scheduler_never_rebalances_strategy_weights(monkeypatch):
    """每 6h 的后验评估不得顺手改**全局**策略权重(无 actor 的静默写入)。"""
    captured: list[dict] = []
    cs = _patch_scheduler_jobs(monkeypatch, captured)
    scheduler = cs.ContextMaintenanceScheduler()

    asyncio.run(scheduler._evaluate_job())
    res = asyncio.run(scheduler.evaluate_once())

    assert getattr(cs, "_rebalance_calls_probe", []) == [], "调度器仍在自动调权"
    assert res["strategy_rebalance"]["disabled"] is True
    assert res["strategy_rebalance"]["changed"] == 0
    assert "手动" in res["strategy_rebalance"]["reason"]


def test_scheduler_module_has_no_rebalance_entrypoint():
    """结构性保证: 调度器模块不再导入/调用自动调权函数(防止改回来)。"""
    import re

    from src.core import context_scheduler as cs

    assert not hasattr(cs, "rebalance_strategy_weights")
    src = __import__("inspect").getsource(cs)
    # 注释/文档里提到名字没关系, 不允许的是 import 或实际调用。
    assert not re.search(
        r"^(?:\s*(?:from|import)\s+.*rebalance_strategy_weights"
        r"|\s*rebalance_strategy_weights\()",
        src,
        flags=re.MULTILINE,
    )


def test_rebalance_rejects_missing_actor_and_writes_nothing():
    """核心函数本身也拒绝匿名调用: 无认证身份 ⇒ ValueError, 且不落任何权重。"""
    from src.core.strategy_engine import rebalance_strategy_weights
    from src.db.session import SessionLocal
    from src.web.models import StrategyWeight, StrategyWeightHistory

    db = SessionLocal()
    try:
        before_w = db.query(StrategyWeight).count()
        before_h = db.query(StrategyWeightHistory).count()
    finally:
        db.close()

    with pytest.raises(ValueError):
        rebalance_strategy_weights(actor=None)
    with pytest.raises(ValueError):
        rebalance_strategy_weights()

    db = SessionLocal()
    try:
        assert db.query(StrategyWeight).count() == before_w
        assert db.query(StrategyWeightHistory).count() == before_h
    finally:
        db.close()


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
