"""因子标定(M4):calibrate_all_markets 只读产出提案,不改评分输入权重。"""

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


def _seed_pair(db, sid, *, market, snapshot_date, alpha, ret, horizon=5):
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


def test_calibrate_all_markets_reports_proposals_without_changing_scoring():
    """端到端:快照+outcome → 三市场各出提案 → 生效权重仍为 1.0,评分不变。

    2026-11-12: 标定只读,不会自动把提案变成有效权重。
    """
    from src.core.factor_calibration import calibrate_all_markets
    from src.core.factor_weights import get_factor_weights
    from src.web.models import FactorWeight

    db = _mem_db()
    try:
        sid = 1
        for days_ago in (50, 45, 40, 35, 30, 25):  # 6 个快照日 × 5 只, 每日横截面 IC=+1
            d = (date.today() - timedelta(days=days_ago)).strftime("%Y-%m-%d")
            for rank in range(1, 6):
                _seed_pair(db, sid, market="CN", snapshot_date=d,
                           alpha=float(rank), ret=float(rank))
                sid += 1
        db.commit()

        res = calibrate_all_markets(db=db, min_samples=5)
        assert set(res) == {"CN", "HK", "US"}
        assert all(r["mode"] == "readonly" and r["write_back"] is False for r in res.values())

        cn = {p["factor_code"]: p for p in res["CN"]["proposals"]}
        assert cn["alpha_score"]["status"] == "proposed"
        assert cn["alpha_score"]["proposed_weight"] > 1.0

        # 只读:三市场都没有落任何权重行
        assert db.query(FactorWeight).count() == 0

        # 评分输入权重仍是默认 1.0(提案未生效)
        w = get_factor_weights("CN", db=db)
        assert w["alpha_score"] == 1.0
    finally:
        db.close()


def test_calibrate_all_markets_marks_low_sample_markets(monkeypatch):
    """无观测的市场也得如实标 skipped_low_sample,不能伪装成「有提案」。"""
    from src.core.factor_calibration import calibrate_all_markets

    db = _mem_db()
    try:
        res = calibrate_all_markets(db=db, min_samples=5)
        for market in ("CN", "HK", "US"):
            statuses = {p["status"] for p in res[market]["proposals"]}
            assert statuses == {"skipped_low_sample"}
            assert res[market]["proposed"] == 0
    finally:
        db.close()
