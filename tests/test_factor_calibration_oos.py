"""B0.4: 因子标定的样本外(OOS)门禁 —— 权重调整必须在 holdout 段仍成立。"""

from __future__ import annotations

from datetime import date, timedelta

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


def _seed_pair(db, sid, *, market, snapshot_date, alpha=0.0, ret=0.0, horizon=5):
    from src.web.models import StrategyFactorSnapshot, StrategyOutcome

    db.add(StrategyFactorSnapshot(
        signal_run_id=sid, snapshot_date=snapshot_date, stock_symbol=f"S{sid}",
        stock_market=market, strategy_code="trend_follow",
        alpha_score=alpha, final_score=50.0,
    ))
    db.add(StrategyOutcome(
        signal_run_id=sid, strategy_code="trend_follow", stock_symbol=f"S{sid}",
        stock_market=market, snapshot_date=snapshot_date, horizon_days=horizon,
        target_date=snapshot_date, outcome_return_pct=ret, outcome_status="evaluated",
    ))


def _seed_dates(db, *, reversed_from: int | None = None):
    sid = 1
    for di, days_ago in enumerate((50, 45, 40, 35, 30, 25, 20, 15)):
        rev = reversed_from is not None and di >= reversed_from
        for rank in range(1, 6):
            _seed_pair(db, sid, market="CN", snapshot_date=_old_date(days_ago),
                       alpha=float(rank), ret=float(6 - rank) if rev else float(rank))
            sid += 1
    db.commit()


def test_oos_gate_allows_consistent_factor():
    """样本内外一致 → 权重上调, 无 OOS 拒绝。"""
    from src.core.factor_calibration import calibrate_factor_weights
    from src.web.models import FactorWeight

    db = _mem_db()
    try:
        _seed_dates(db)
        res = calibrate_factor_weights("CN", min_samples=5, db=db)
        row = db.query(FactorWeight).filter_by(factor_code="alpha_score", market="CN").first()
        assert row.weight > 1.0
        assert res["skipped_oos"] == 0
        assert row.meta.get("last_holdout_ic") == 1.0
    finally:
        db.close()


def test_oos_gate_blocks_sign_flip():
    """样本外段符号翻转 → 本轮不调权, meta 记录 oos_rejected。"""
    from src.core.factor_calibration import calibrate_factor_weights
    from src.web.models import FactorWeight, FactorWeightHistory

    db = _mem_db()
    try:
        _seed_dates(db, reversed_from=5)  # 后 3 个快照日横截面反向
        res = calibrate_factor_weights("CN", min_samples=5, db=db)
        row = db.query(FactorWeight).filter_by(factor_code="alpha_score", market="CN").first()
        assert row.weight == 1.0, "OOS 不通过不得调权"
        assert row.meta.get("oos_rejected") == "sign_flip"
        assert res["skipped_oos"] >= 1
        hist = (db.query(FactorWeightHistory)
                .filter_by(factor_code="alpha_score", market="CN", reason="auto").all())
        assert hist == []
    finally:
        db.close()
