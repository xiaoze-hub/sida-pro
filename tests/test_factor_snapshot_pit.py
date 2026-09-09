"""B0.5: 因子快照前视护栏 —— 新闻/权重按快照日 as-of, 历史重跑拒绝, 落输入指纹。"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import src.web.models  # noqa: F401  注册 ORM 模型
from src.web.database import Base


def _mem_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


# ──────────────── 新闻窗口 as-of ────────────────

def test_news_metrics_respect_as_of_window():
    """as_of 之前 72h 内的新闻才计入; 之后发布的必须排除。"""
    from src.core.strategy_engine import _load_news_metrics
    from src.web.models import EntryCandidate, NewsCache

    db = _mem_db()
    try:
        as_of = datetime(2026, 8, 20, 23, 59, 59, tzinfo=timezone.utc)
        db.add(NewsCache(
            source="cls", external_id="old", title="公司A 中标重大项目",
            content="利好", publish_time=datetime(2026, 8, 20, 10, 0, 0), symbols=["600000"],
            importance=2,
        ))
        db.add(NewsCache(
            source="cls", external_id="future", title="公司A 业绩暴雷",
            content="利空", publish_time=datetime(2026, 8, 25, 10, 0, 0), symbols=["600000"],
            importance=3,
        ))
        cand = EntryCandidate(
            snapshot_date="2026-08-20", stock_symbol="600000", stock_name="公司A",
            stock_market="CN", score=60.0, status="active",
        )
        db.add(cand)
        db.commit()

        got = _load_news_metrics(db=db, candidates=[cand], as_of=as_of)
        assert "600000" in got
        assert got["600000"]["news_count"] == 1, "只应统计 as_of 之前的新闻"
        assert got["600000"]["event_bias"] > 0, "利好新闻 → 正向偏置"
    finally:
        db.close()


# ──────────────── 权重 as-of ────────────────

def test_factor_weights_as_of_reads_history():
    """as_of 时点取历史权重, 不回落到当前值。"""
    from src.core.factor_weights import get_factor_weights
    from src.web.models import FactorWeight, FactorWeightHistory

    db = _mem_db()
    try:
        db.add(FactorWeight(factor_code="alpha_score", market="CN", weight=1.5))
        db.add(FactorWeightHistory(
            factor_code="alpha_score", market="CN", old_weight=1.0, new_weight=1.2,
            created_at=datetime(2026, 8, 1, 0, 0, 0), reason="auto",
        ))
        db.commit()

        hist_w = get_factor_weights("CN", as_of=datetime(2026, 8, 15, 0, 0, 0), db=db)
        assert hist_w["alpha_score"] == 1.2, "应取 as_of 之前最后一次生效权重"
        cur_w = get_factor_weights("CN", db=db)
        assert cur_w["alpha_score"] == 1.5, "不带 as_of 时读当前值"
    finally:
        db.close()


# ──────────────── 历史重跑护栏 ────────────────

def test_historical_refresh_rejected_by_default(monkeypatch):
    """历史 snapshot_date 默认拒绝(400), 显式开关后才放行。"""
    from fastapi import HTTPException

    from src.web.api import recommendations as R

    monkeypatch.delenv("SIDA_ALLOW_FACTOR_BACKFILL", raising=False)
    past = (date.today() - timedelta(days=3)).isoformat()
    with pytest.raises(HTTPException) as ei:
        R.refresh_strategy_signal_list(snapshot_date=past, wait=True)
    assert ei.value.status_code == 400

    monkeypatch.setenv("SIDA_ALLOW_FACTOR_BACKFILL", "1")
    assert R._allow_factor_backfill() is True


# ──────────────── 输入指纹 ────────────────

def test_factor_snapshot_payload_has_input_hash():
    """因子行必须落 input_hash/news_window_hours/weight_version。"""
    from src.core.strategy_engine import _sync_factor_and_risk_snapshots
    from src.web.models import StrategyFactorSnapshot, StrategySignalRun

    db = _mem_db()
    try:
        run = StrategySignalRun(
            snapshot_date="2026-09-01", stock_symbol="600000", stock_market="CN",
            strategy_code="trend_follow", strategy_version="v2", rank_score=70.0,
            payload={"score_breakdown": {"alpha_score": 3.0, "weighted_score": 70.0}},
        )
        db.add(run)
        db.commit()

        _sync_factor_and_risk_snapshots(db=db, snapshot="2026-09-01", signals=[run])
        db.commit()

        row = db.query(StrategyFactorSnapshot).filter_by(signal_run_id=run.id).first()
        assert row is not None
        payload = row.factor_payload or {}
        assert payload.get("input_hash"), "缺 input_hash"
        assert len(payload["input_hash"]) == 16
        assert payload.get("news_window_hours") == 72
        assert payload.get("weight_version") == "v2"

        # 同输入复算 → 同指纹(可复现)
        _sync_factor_and_risk_snapshots(db=db, snapshot="2026-09-01", signals=[run])
        db.commit()
        row2 = db.query(StrategyFactorSnapshot).filter_by(signal_run_id=run.id).first()
        assert row2.factor_payload["input_hash"] == payload["input_hash"]
    finally:
        db.close()
