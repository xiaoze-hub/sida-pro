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


def _actor():
    """直接调 handler 时用的服务端身份(HTTP 层由 require_settings_write 提供)。"""
    from src.web.api.settings import SettingsActor

    return SettingsActor(id="u-test", username="unit-owner", role="owner")


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
        res = factors.update_weight("alpha_score", "CN", payload, db=db, actor=_actor())
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
            factors.update_weight("bad_factor", "CN", payload, db=db, actor=_actor())
        assert ei.value.status_code == 400
    finally:
        db.close()


def test_factors_router_mounted():
    """/api/factors/weights 已挂载到 app(走 OpenAPI schema,兼容自定义 _IncludedRouter)。"""
    from src.web.app import app

    paths = set(app.openapi().get("paths", {}).keys())
    assert "/api/factors/weights" in paths
    assert "/api/factors/weights/{factor_code}/{market}" in paths
    # 2026-11-12: 未经审查的提案刷新/落地写入端点已撤除,不得再挂载。
    assert not [p for p in paths if "/api/factors/proposals" in p]


def test_no_proposal_write_handlers_exposed():
    """模块层也不留提案刷新/落地处理器(避免被重新挂载)。"""
    from src.web.api import factors

    assert not hasattr(factors, "refresh_proposals")
    assert not hasattr(factors, "apply_proposal")
    assert not hasattr(factors, "ProposalApply")


def test_list_weights_keeps_legacy_observation_fields():
    """兼容:列表仍给出最近 IC/IR 观测字段(只读;不再有提案写入槽位)。"""
    from src.web.api import factors

    db = _mem_db()
    try:
        res = factors.list_weights(db=db)
        item = res["items"][0]
        for field in ("last_ic", "last_ir", "last_sample_size", "last_calibrated_at"):
            assert field in item
        assert "pending_proposal" not in item
        assert "applied_proposal" not in item
    finally:
        db.close()
