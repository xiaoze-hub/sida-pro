"""因子权重 API(M5):只读列表 + 手动覆盖 + 路由挂载。"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import src.web.models  # noqa: F401  注册 ORM 模型
from src.web.database import Base


def _mem_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_list_weights_returns_all_market_factor_pairs():
    """GET 列表返回 5 因子 × 3 市场。"""
    from src.web.api import factors

    db = _mem_db()
    try:
        res = factors.list_weights(db=db)
        assert "items" in res
        assert len(res["items"]) == 15
    finally:
        db.close()


def test_update_weight_pins_and_sets_value():
    """POST 手动设权重 + pin。"""
    from src.web.api import factors

    db = _mem_db()
    try:
        payload = factors.FactorWeightUpdate(weight=1.25, is_pinned=True)
        res = factors.update_weight("alpha_score", "CN", payload, db=db)
        assert res["weight"] == 1.25
        assert res["is_pinned"] is True
    finally:
        db.close()


def test_update_weight_unknown_factor_returns_400():
    """未知因子 → HTTP 400。"""
    from src.web.api import factors

    db = _mem_db()
    try:
        payload = factors.FactorWeightUpdate(weight=1.1)
        with pytest.raises(HTTPException) as ei:
            factors.update_weight("bad_factor", "CN", payload, db=db)
        assert ei.value.status_code == 400
    finally:
        db.close()


def test_factors_router_mounted():
    """/api/factors/weights 已挂载到 app(走 OpenAPI schema,兼容自定义 _IncludedRouter)。"""
    from src.web.app import app

    paths = set(app.openapi().get("paths", {}).keys())
    assert "/api/factors/weights" in paths
    assert "/api/factors/weights/{factor_code}/{market}" in paths
    assert "/api/factors/proposals/{market}/refresh" in paths
    assert "/api/factors/proposals/{factor_code}/{market}/apply" in paths


def test_list_weights_exposes_pending_proposal_field():
    """列表要给出待审提案槽位(默认 None),UI 才看得见「有建议但未生效」。"""
    from src.web.api import factors

    db = _mem_db()
    try:
        res = factors.list_weights(db=db)
        item = res["items"][0]
        assert item["pending_proposal"] is None
        assert item["applied_proposal"] is None
    finally:
        db.close()


def test_refresh_proposals_writes_no_effective_weight():
    """POST refresh:只在已有 meta 里留提案,不建行、不改权重。"""
    from src.web.api import factors
    from src.web.models import FactorWeight

    db = _mem_db()
    try:
        res = factors.refresh_proposals("CN", db=db)
        assert res["mode"] == "suggest"
        assert res["changed"] == 0
        assert len(res["proposals"]) == 5
        assert db.query(FactorWeight).count() == 0  # 空库:不因观测/提案建行
    finally:
        db.close()


def test_apply_proposal_without_pending_returns_400():
    """无待审提案 → HTTP 400,绝不顺手写权重。"""
    from src.web.api import factors
    from src.web.models import FactorWeightHistory

    db = _mem_db()
    try:
        payload = factors.ProposalApply(approved_by="alice")
        with pytest.raises(HTTPException) as ei:
            factors.apply_proposal("alpha_score", "CN", payload, db=db)
        assert ei.value.status_code == 400
        assert db.query(FactorWeightHistory).count() == 0
    finally:
        db.close()
