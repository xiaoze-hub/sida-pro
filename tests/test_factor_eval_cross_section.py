"""B0.4: 因子 IC 主口径 = 按日横截面(均值 + t 统计量); pooled 仅作参考。"""

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


def test_cross_section_ic_ignores_date_level_shift():
    """日级收益平移会污染 pooled IC, 但不影响横截面 IC。"""
    from src.core.factor_eval import evaluate_factor_ic

    db = _mem_db()
    try:
        sid = 1
        for di, days_ago in enumerate((40, 38, 36, 34)):  # 4 个快照日
            offset = 100.0 if di < 2 else -100.0          # 日级平移(与因子无关)
            for rank in range(1, 6):                       # 每日 5 只
                _seed_pair(db, sid, market="CN", snapshot_date=_old_date(days_ago),
                           alpha=float(rank), ret=float(rank) + offset)
                sid += 1
        db.commit()

        res = evaluate_factor_ic(days=90, horizon=5, min_samples=5, market="CN", db=db)
        f = res["factors"]["alpha_score"]
        assert f["ic"] == 1.0, "横截面排序完美 → 主口径 IC 应为 +1"
        assert f["ic_periods"] == 4
        assert f["ic_std"] == 0.0 and f["ic_t"] is None, "每期 IC 恒为 1 → 无波动, t 未定义"
        assert f["ic_pooled"] is None or abs(f["ic_pooled"]) < 0.5, "pooled 应被日级平移污染"
    finally:
        db.close()


def test_holdout_ic_sliced_by_latest_dates():
    """holdout_ratio=0.3 → 后 30% 日期为样本外段, 单独算 IC。"""
    from src.core.factor_eval import evaluate_factor_ic

    db = _mem_db()
    try:
        sid = 1
        for di, days_ago in enumerate((50, 45, 40, 35, 30, 25, 20, 15)):  # 8 个快照日
            reversed_ = di >= 5                                            # 后 3 天反向
            for rank in range(1, 6):
                ret = float(6 - rank) if reversed_ else float(rank)
                _seed_pair(db, sid, market="CN", snapshot_date=_old_date(days_ago),
                           alpha=float(rank), ret=ret)
                sid += 1
        db.commit()

        res = evaluate_factor_ic(
            days=90, horizon=5, min_samples=5, market="CN", holdout_ratio=0.3, db=db
        )
        f = res["factors"]["alpha_score"]
        assert f["holdout_periods"] == 3
        assert f["ic_holdout"] == -1.0, "样本外段反向 → holdout IC 应为 -1"
        assert f["ic"] is not None and f["ic"] > 0, "全期均值仍为正"
        assert f["ic_t"] is not None, "各期 IC 有波动 → t 统计量应可算"
    finally:
        db.close()
