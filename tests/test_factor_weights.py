"""因子权重存取层(M1):纯读 + 显式补行,使用隔离的内存库。

2026-11-12 复审: 读路径**不再建行**(可能在只读副本上执行), 缺失因子按 1.0
返回; 需要补默认行的场景(权重面板列表 / 手工改权)显式走写库。
"""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import src.web.models  # noqa: F401  注册所有 ORM 模型到 Base.metadata
from src.web.database import Base


def _mem_db():
    """每个用例一个全新的内存 SQLite 会话,避免污染开发库。"""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_get_factor_weights_returns_defaults_without_writing():
    """首次读取: 缺失因子按 1.0 返回, 且**一行都不建**(读路径零写入)。"""
    from src.core.factor_weights import CALIBRATABLE_FACTORS, get_factor_weights
    from src.web.models import FactorWeight

    db = _mem_db()
    try:
        w = get_factor_weights("CN", db=db)
        assert set(w) == set(CALIBRATABLE_FACTORS)
        assert all(v == 1.0 for v in w.values())
        assert db.query(FactorWeight).count() == 0, "读路径不得 lazy seed"
    finally:
        db.close()


def test_get_all_factor_weights_seeds_rows_idempotently():
    """面板列表接口补默认行(写路径), 重复调用不产生重复行。"""
    from src.core.factor_weights import (
        CALIBRATABLE_FACTORS,
        MARKETS,
        get_all_factor_weights,
    )
    from src.web.models import FactorWeight

    db = _mem_db()
    try:
        get_all_factor_weights(db=db)
        get_all_factor_weights(db=db)
        n = db.query(FactorWeight).count()
        assert n == len(CALIBRATABLE_FACTORS) * len(MARKETS)
    finally:
        db.close()


def test_get_factor_weights_reads_stored_value():
    """已存在的非默认权重应被读出,不被 seed 覆盖。"""
    from src.core.factor_weights import get_factor_weights
    from src.web.models import FactorWeight

    db = _mem_db()
    try:
        db.add(FactorWeight(factor_code="alpha_score", market="CN", weight=1.3))
        db.commit()
        w = get_factor_weights("CN", db=db)
        assert w["alpha_score"] == 1.3
        # 其余因子仍补齐为默认 1.0
        assert w["catalyst_score"] == 1.0
    finally:
        db.close()


def test_get_all_factor_weights_lists_all_markets():
    """列出所有市场 × 因子,字段含 weight/is_pinned/auto_calibrate。"""
    from src.core.factor_weights import (
        CALIBRATABLE_FACTORS,
        MARKETS,
        get_all_factor_weights,
    )

    db = _mem_db()
    try:
        items = get_all_factor_weights(db=db)
        assert len(items) == len(CALIBRATABLE_FACTORS) * len(MARKETS)
        keys = {(i["factor_code"], i["market"]) for i in items}
        assert ("alpha_score", "CN") in keys
        sample = items[0]
        assert {"weight", "is_pinned", "auto_calibrate"} <= set(sample)
    finally:
        db.close()


def test_set_factor_weight_manual_writes_history():
    """手动改权重写 manual 审计,并能同时设 is_pinned(身份来自授权调用方)。"""
    import json

    from src.core.factor_weights import set_factor_weight
    from src.web.models import AuditLog, FactorWeightHistory

    db = _mem_db()
    try:
        res = set_factor_weight(
            "alpha_score", "CN", weight=1.3, is_pinned=True, db=db,
            actor="alice", actor_id="u-1", via="api",
        )
        assert res["weight"] == 1.3
        assert res["is_pinned"] is True
        hist = (db.query(FactorWeightHistory)
                .filter_by(factor_code="alpha_score", market="CN", reason="manual").all())
        assert len(hist) == 1
        assert hist[0].new_weight == 1.3
        assert (hist[0].meta or {}).get("by") == "alice"
        assert (hist[0].meta or {}).get("by_kind") == "user"
        audit = db.query(AuditLog).filter_by(action="update_factor_weight").all()
        assert len(audit) == 1 and audit[0].username == "alice"
        assert json.loads(audit[0].detail)["actor_kind"] == "user"
    finally:
        db.close()


def test_set_factor_weight_rejects_unknown_factor():
    """未知因子抛 ValueError(API 层转 400)。"""
    import pytest

    from src.core.factor_weights import set_factor_weight

    db = _mem_db()
    try:
        with pytest.raises(ValueError):
            set_factor_weight("not_a_factor", "CN", weight=1.1, db=db, actor="alice")
    finally:
        db.close()
