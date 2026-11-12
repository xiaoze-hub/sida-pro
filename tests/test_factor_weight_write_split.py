# -*- coding: utf-8 -*-
"""因子权重写入在**真实读写分离**下的正确性(2026-11-12 复审)。

审计问题(实测形态):
    `set_factor_weight` 先 `SELECT` 读旧值, 再 UPDATE + 写历史/审计。当会话是
    `RoutingSession` 且配了 `DATABASE_URL_READ` 时, 第一条 SELECT 会把整条事务
    绑到**读副本**: 要么旧值取自滞后副本算错 before/after, 要么 UPDATE 直接发到
    只读副本报「attempt to write a readonly database」, 要么读写分裂成两条连接
    (事务不再原子)。

本文件用两个**真实 SQLite 文件**充当主库/副本, 并给副本挂 DML 拦截器(任何
INSERT/UPDATE/DELETE 都会立刻报错并记录), 以此证明:
1. 权重写入只落在写库, 副本一个字节都没写;
2. 旧值取自写库(不是副本的滞后值);
3. 读路径(`get_factor_weights`)零写入, 不会在读副本上尝试建行;
4. 列表补行(`get_all_factor_weights`)只写写库;
5. `pin_write_bind` 对「已在副本事务里」的会话直接报错, 不静默分裂。

隔离: 两个库都是 pytest tmp_path 下的临时 SQLite 文件, 不碰 DATA_DIR / 真实库。
"""

from __future__ import annotations

import inspect

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

import src.db.session as session_mod
import src.web.models  # noqa: F401  注册 ORM 模型
from src.web.database import Base


def _mk_engine(path):
    eng = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    return eng


_DB_ENV_KEYS = (
    "DATABASE_URL",
    "DATABASE_URL_WRITE",
    "DATABASE_URL_READ",
    "DATABASE_URL_MIGRATE",
    "SIDA_DB_URL",
    "SIDA_DB",
    "SIDA_APP_DB_ROLE",
)


@pytest.fixture(autouse=True)
def _no_external_db(monkeypatch):
    """清空一切可能指向真实库的环境变量(用例结束自动还原)。"""
    for key in _DB_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    yield


@pytest.fixture()
def split_db(monkeypatch, tmp_path):
    """写库 + 副本两套真实 SQLite 引擎; 副本拒绝并记录任何 DML。"""
    write = _mk_engine(tmp_path / "write.db")
    read = _mk_engine(tmp_path / "read.db")
    # 断言用的是临时目录里的 SQLite 文件, 不是仓库/生产的库
    for eng in (write, read):
        assert str(eng.url).startswith("sqlite"), eng.url
        assert str(tmp_path) in str(eng.url), f"测试引擎不在 tmp_path: {eng.url}"

    dml_seen: list[str] = []
    armed = {"on": False}

    def _block_dml(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        verb = statement.lstrip().split(None, 1)[0].upper() if statement.strip() else ""
        if verb in ("INSERT", "UPDATE", "DELETE", "REPLACE"):
            dml_seen.append(statement[:120])
            raise RuntimeError("attempt to write a readonly database(副本拒绝写入)")

    def _arm():
        """在造好副本初始数据之后再上锁: 之后副本上任何 DML 都会被记录并拒绝。"""
        if not armed["on"]:
            event.listen(read, "before_cursor_execute", _block_dml)
            armed["on"] = True

    # RoutingSession.get_bind 在运行时读模块级 read_engine/write_engine
    monkeypatch.setattr(session_mod, "write_engine", write)
    monkeypatch.setattr(session_mod, "read_engine", read)
    monkeypatch.setattr(session_mod, "engine", write)

    RoutingFactory = sessionmaker(class_=session_mod.RoutingSession, bind=write)
    WriteFactory = sessionmaker(class_=Session, bind=write)

    from src.core import factor_weights as fw

    monkeypatch.setattr(fw, "SessionLocal", RoutingFactory)
    monkeypatch.setattr(fw, "WriteSessionLocal", WriteFactory)

    yield {
        "write": write,
        "read": read,
        "routing": RoutingFactory,
        "write_factory": WriteFactory,
        "dml_seen": dml_seen,
        "arm": _arm,
    }

    write.dispose()
    read.dispose()


def _seed(db, weight: float):
    from src.web.models import FactorWeight

    db.add(FactorWeight(factor_code="alpha_score", market="CN", weight=weight))
    db.commit()


def _weight(engine, factor="alpha_score", market="CN"):
    from src.web.models import FactorWeight

    with engine.connect() as conn:
        row = conn.exec_driver_sql(
            "SELECT weight FROM factor_weights WHERE factor_code=? AND market=?",
            (factor, market),
        ).fetchone()
    return None if row is None else float(row[0])


def _row_count(engine, market=None):
    sql = "SELECT COUNT(*) FROM factor_weights"
    args: tuple = ()
    if market is not None:
        sql += " WHERE market=?"
        args = (market,)
    with engine.connect() as conn:
        return int(conn.exec_driver_sql(sql, args).scalar())


def test_weight_write_reads_old_value_from_write_db_not_replica(split_db):
    """旧值必须来自写库; 写入只落写库, 副本收到零 DML。"""
    from src.core.factor_weights import set_factor_weight
    from src.web.models import FactorWeightHistory

    seed = split_db["write_factory"]()
    try:
        _seed(seed, 2.0)  # 写库: 2.0
    finally:
        seed.close()
    stale = split_db["write_factory"]()
    try:
        stale.bind = split_db["read"]  # 副本: 滞后值 0.5
        _seed(stale, 0.5)
    finally:
        stale.close()

    split_db["arm"]()
    db = split_db["routing"]()
    try:
        res = set_factor_weight("alpha_score", "CN", weight=1.5, db=db, actor="alice")
        assert res["weight"] == 1.5
    finally:
        db.close()

    assert _weight(split_db["write"]) == 1.5
    assert _weight(split_db["read"]) == 0.5, "副本被写坏了(或写入被路由到副本)"
    assert split_db["dml_seen"] == [], f"副本收到 DML: {split_db['dml_seen']}"

    hist = split_db["write_factory"]()
    try:
        rows = hist.query(FactorWeightHistory).all()
        assert len(rows) == 1
        assert (rows[0].old_weight, rows[0].new_weight) == (2.0, 1.5), (
            "before 值取自滞后副本(应为写库的 2.0)"
        )
    finally:
        hist.close()


def test_read_path_does_not_write_to_replica(split_db):
    """`get_factor_weights` 是纯读: 副本上的缺行不触发任何建行/写入。"""
    from src.core.factor_weights import get_factor_weights

    split_db["arm"]()
    db = split_db["routing"]()
    try:
        w = get_factor_weights("CN", db=db)
        assert w["alpha_score"] == 1.0
        assert w["risk_penalty"] == 1.0
    finally:
        db.close()

    assert split_db["dml_seen"] == [], f"读路径在副本上写了: {split_db['dml_seen']}"
    assert _row_count(split_db["read"]) == 0
    assert _row_count(split_db["write"]) == 0


def test_list_weights_seeds_rows_in_write_db_only(split_db):
    """面板列表补默认行: 只写写库, 副本零 DML、零行。"""
    from src.core.factor_weights import CALIBRATABLE_FACTORS, MARKETS, get_all_factor_weights

    split_db["arm"]()
    db = split_db["routing"]()
    try:
        items = get_all_factor_weights(db=db)
        assert len(items) == len(CALIBRATABLE_FACTORS) * len(MARKETS)
    finally:
        db.close()

    assert split_db["dml_seen"] == []
    assert _row_count(split_db["write"]) == len(CALIBRATABLE_FACTORS) * len(MARKETS)
    assert _row_count(split_db["read"]) == 0


def test_pin_write_bind_refuses_session_already_on_replica(split_db):
    """已在副本上开了事务的会话: 直接报错, 不静默改成「副本读+主库写」。"""
    from src.core.factor_weights import set_factor_weight
    from src.db.session import ReadOnlyBindError
    from src.web.models import FactorWeight

    split_db["arm"]()
    db = split_db["routing"]()
    try:
        # 第一条语句是 SELECT → 事务绑在副本上
        assert db.query(FactorWeight).count() == 0
        assert db.in_transaction()

        with pytest.raises(ReadOnlyBindError):
            set_factor_weight("alpha_score", "CN", weight=1.5, db=db, actor="alice")
        assert _weight(split_db["write"]) is None
        assert _weight(split_db["read"]) is None
    finally:
        db.close()


def test_factors_api_write_endpoints_depend_on_write_session():
    """端点依赖必须是专用写会话(get_write_db), 不能靠路由会话碰运气。"""
    from src.db.session import get_write_db
    from src.web.api import factors as factors_api

    for fn in (factors_api.update_weight, factors_api.list_weights, factors_api.calibration):
        dep = inspect.signature(fn).parameters["db"].default
        assert dep.dependency is get_write_db, f"{fn.__name__} 未使用写库会话"
