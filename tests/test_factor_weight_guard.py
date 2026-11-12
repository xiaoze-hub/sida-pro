# -*- coding: utf-8 -*-
"""因子分支复审三(2026-11-12): 残存日志泄密 / 错误响应 / 权重读锁。

审计列的问题, 逐条用**真实执行**验证(不是读代码猜):

1. `_notify_refresh_done` 还在用 `logger.exception` ⇒ SQLAlchemy 的 SQL 文本、
   `[parameters: (...)]`、DSN 会原样进服务端日志。改成白名单 `log_failure` 后,
   日志里只允许出现 关联号 + 异常类型 + 帧位置 + 白名单上下文; 且不得带 exc_info。
2. `GET /api/factors/weights` 的 DB 异常没有兜底 ⇒ 裸 500 / 无关联号。现在必须是
   503 + 安全 JSON(无 data、含关联号), 响应与日志都不含 SQL/表名/DSN/绑定参数。
3. `_load_row_for_update` 只加了 `FOR UPDATE`, 没加 `populate_existing()` ⇒ 行锁
   拿到了, 但历史里的 before 还是会话身份映射里的旧值(不是库里真实值)。
4. 首次补行时别的进程可能同时补同一批行 ⇒ 唯一键冲突。现在 SAVEPOINT 只回滚补行
   这一段, 再按库里真实行重读(可恢复); 重读后仍缺行则原样上抛(**不吞错**)。
5. 标定计算失败必须体现在 **HTTP 状态**上(503), 且节流窗口内的重复请求不许被
   改写成 429「过于频繁」(那会用限流文案掩盖真实故障)。

隔离: 全部用 tmp_path 下的临时 SQLite(或显式的隔离 PG 测试库)。任何指向真实库的
环境变量在用例里被清掉, 并断言当前的库不是生产/仓库数据文件。
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import src.web.models  # noqa: F401  注册 ORM 模型
from src.web.database import Base

DB_ENV_KEYS = (
    "DATABASE_URL",
    "DATABASE_URL_WRITE",
    "DATABASE_URL_READ",
    "DATABASE_URL_MIGRATE",
    "SIDA_DB_URL",
    "SIDA_DB",
    "SIDA_APP_DB_ROLE",
)

# ── 真实的假 secret(不是 `***` 占位): 任何一处泄漏都能被断言抓到 ──────────
DB_PASSWORD = "S1da-Pr0d-Pa55w0rd"
DSN = f"postgresql://sida:{DB_PASSWORD}@db.internal:5432/sida"
BOUND_PARAM = "612-U-NOISE"
SQL = (
    "INSERT INTO factor_weights (factor_code, market, weight) "
    "VALUES ('alpha_score', 'CN', 1.0)"
)


@pytest.fixture(autouse=True)
def _isolated_db(monkeypatch):
    """清掉一切可能指向真实库的环境变量, 并断言当前库是隔离测试库。"""
    for key in DB_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)

    from src.db.dialect import is_postgres
    from src.db.session import write_engine

    url = str(write_engine.url)
    if is_postgres():
        # PG 模式只允许指向**显式创建**的隔离测试库(名字里写死), 绝不碰生产库名。
        assert "factor_guard_test" in url, f"PG 模式必须指向隔离测试库, 实际: {url}"
    else:
        assert url.startswith("sqlite"), f"测试库必须是隔离 SQLite, 实际: {url}"
        assert "/data/sida.db" not in url, f"拒绝在真实数据文件上跑测试: {url}"
    yield


@pytest.fixture(scope="module", autouse=True)
def _schema_ready():
    """建表(幂等): SQLite 用例走 conftest 的临时库, PG 用例走隔离测试库。

    PG 分支**只 create_all 不跑迁移链**: 迁移是生产侧动作(依赖角色/扩展),
    而这两个竞态用例只关心 factor_weights 的唯一键与行锁语义。
    """
    from src.db.dialect import is_postgres
    from src.db.session import write_engine
    from src.web.database import Base, init_db

    if is_postgres():
        Base.metadata.create_all(write_engine)
    else:
        init_db()
    yield


def _mk_session(path: Path, *, expire_on_commit: bool = False) -> Session:
    """tmp_path 下的临时 SQLite 引擎 + 会话(不碰 conftest 的共享库)。"""
    engine = create_engine(
        f"sqlite:///{path}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    return Session(bind=engine, expire_on_commit=expire_on_commit)


def _logged(caplog, prefix: str = "src.") -> str:
    """只取被测模块的日志(排除 sqlalchemy 等第三方 logger 的噪声)。"""
    return "\n".join(
        rec.getMessage() for rec in caplog.records if rec.name.startswith(prefix)
    )


# ── 1. 站内通知失败: 日志不得带 SQL / 绑定参数 / DSN ───────────────────────
def test_notify_failure_log_has_no_sql_dsn_or_traceback(monkeypatch, caplog):
    from src.web.api import recommendations

    def _boom(*args, **kwargs):
        raise RuntimeError(
            f"{SQL}; dsn={DSN}; [parameters: ('{BOUND_PARAM}',)]"
        )

    monkeypatch.setattr("src.core.notify_center.notify_task_done", _boom)

    with caplog.at_level(logging.ERROR):
        # 通知失败绝不能顶掉刷新本身(调用方只关心「有没有抛出去」)。
        recommendations._notify_refresh_done(False, "安全文案: 刷新失败, 参考号 ERR-X")

    text = _logged(caplog)
    assert text, "通知失败必须留下一条服务端日志(否则排障无从下手)"
    assert "ERR-" in text and "RuntimeError" in text
    # 帧位置保留(排障需要代码位置), 但异常正文/参数一个都不能有
    assert "recommendations.py" in text
    for leak in (SQL, "INSERT INTO", DSN, DB_PASSWORD, BOUND_PARAM,
                 "parameters", "factor_weights"):
        assert leak not in text, f"日志泄漏: {leak!r} in {text!r}"
    assert "Traceback" not in text, f"仍有完整堆栈: {text!r}"
    # logger.exception 会带 exc_info ⇒ 这里必须为空(证明不是「换了个函数名」)
    assert all(rec.exc_info is None for rec in caplog.records), "仍带异常堆栈"


# ── 2. GET /api/factors/weights: DB 异常 → 503 + 安全 JSON + 关联号 ────────
def _owner_token_and_db():
    from src.web.api.auth import create_user
    from src.web.database import SessionLocal
    from src.web.models import User

    db = SessionLocal()
    create_user(db, "fwg_owner", "fwg_owner_pw_1234", "owner")
    user = db.query(User).filter(User.username == "fwg_owner").one()
    return user, db


@pytest.fixture()
def http_client(monkeypatch):
    """真实 app 的 TestClient; 用例后清掉本文件造的账号。"""
    monkeypatch.setattr("src.web.middleware.RATE_LIMIT_ENABLED", False)
    from src.web.app import app
    from src.web.database import SessionLocal
    from tests.conftest import purge_users

    db = SessionLocal()
    purge_users(db, only_username="fwg_owner")
    purge_users(db, only_username="fwg_member")
    db.close()

    c = TestClient(app)
    try:
        yield c
    finally:
        c.cookies.clear()
        db = SessionLocal()
        purge_users(db, only_username="fwg_owner")
        purge_users(db, only_username="fwg_member")
        db.close()


def _login(client: TestClient, username: str, password: str) -> dict:
    r = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['data']['token']}"}


def test_weights_list_db_error_is_safe_json_with_ref(http_client, caplog):
    """库读失败: 503 + 安全文案 + 关联号; 响应/日志都不带 SQL、表名、DSN。"""
    from src.web.api import factors as factors_api

    _owner_token_and_db()
    headers = _login(http_client, "fwg_owner", "fwg_owner_pw_1234")

    # 真实 DB 错误(表不存在), 不是打桩的字符串; 由依赖覆盖注入到端点。
    broken = Session(bind=create_engine("sqlite:///:memory:"))
    http_client.app.dependency_overrides[factors_api.get_write_db] = lambda: broken
    try:
        with caplog.at_level(logging.ERROR):
            r = http_client.get("/api/factors/weights", headers=headers)
    finally:
        http_client.app.dependency_overrides.clear()
        broken.close()

    assert r.status_code == 503, r.text
    body = r.json()
    assert body["success"] is False
    assert body["data"] is None, "失败时不能给半截数据"
    assert "ERR-" in body["message"], body["message"]
    for leak in ("factor_weights", "SELECT", "no such table", "sqlite", DSN,
                 DB_PASSWORD, "Traceback"):
        assert leak not in r.text, f"响应泄漏: {leak!r}"

    logs = _logged(caplog)
    assert "ERR-" in logs and "OperationalError" in logs, logs
    for leak in ("SELECT", "no such table", "factor_weights", DSN, DB_PASSWORD):
        assert leak not in logs, f"日志泄漏: {leak!r}"


def test_calibration_failure_memo_is_bounded_and_never_a_result_cache(
    http_client, monkeypatch
):
    """失败只留「刚失败」这个结论(有上限), 建议缓存里永远没有它。"""
    from src.core import factor_calibration as fc
    from src.web.api import factors as factors_api

    _owner_token_and_db()
    headers = _login(http_client, "fwg_owner", "fwg_owner_pw_1234")
    monkeypatch.setattr(
        fc, "evaluate_factor_ic", lambda **kw: {"factors": {}, "error": "boom"}
    )
    monkeypatch.setattr(factors_api, "_CALIB_CACHE_MAX_ENTRIES", 2)
    factors_api._reset_calibration_cache()
    try:
        for days in (30, 40, 50):
            monkeypatch.setattr(factors_api, "_CALIB_LAST_COMPUTE", 0.0)
            r = http_client.get(
                f"/api/factors/calibration?market=CN&days={days}", headers=headers
            )
            assert r.status_code == 503, r.text
        assert factors_api._CALIB_LAST_FAILURE, "失败结论要留下(供节流分支如实复述)"
        assert len(factors_api._CALIB_LAST_FAILURE) <= 2, "失败结论字典必须有上限"
        assert factors_api._CALIB_CACHE == {}, "失败绝不进建议缓存"
    finally:
        factors_api._reset_calibration_cache()


def test_calibration_fingerprint_db_error_is_safe_503(http_client, caplog):
    """计算前的指纹读失败也不能裸 500: 503 + 安全文案 + 关联号(无 SQL/DSN)。"""
    from src.web.api import factors as factors_api

    _owner_token_and_db()
    headers = _login(http_client, "fwg_owner", "fwg_owner_pw_1234")
    broken = Session(bind=create_engine("sqlite:///:memory:"))
    http_client.app.dependency_overrides[factors_api.get_write_db] = lambda: broken
    try:
        with caplog.at_level(logging.ERROR):
            r = http_client.get(
                "/api/factors/calibration?market=CN", headers=headers
            )
    finally:
        http_client.app.dependency_overrides.clear()
        broken.close()

    assert r.status_code == 503, r.text
    body = r.json()
    assert body["success"] is False and body["data"] is None
    assert "标定计算失败" in body["message"] and "ERR-" in body["message"]
    for leak in ("factor_weights", "SELECT", "no such table", DSN, DB_PASSWORD):
        assert leak not in r.text, f"响应泄漏: {leak!r}"

    logs = _logged(caplog)
    assert "ERR-" in logs and "OperationalError" in logs, logs
    for leak in ("SELECT", "no such table", DSN, DB_PASSWORD):
        assert leak not in logs, f"日志泄漏: {leak!r}"


def test_weights_list_roles_unchanged(http_client):
    """读仍是登录即可(owner/member 都 200); 标定仍只给 manage_settings。"""
    from src.web.api.auth import create_user
    from src.web.database import SessionLocal

    _owner_token_and_db()
    db = SessionLocal()
    create_user(db, "fwg_member", "fwg_member_pw_1234", "member")
    db.close()

    owner = _login(http_client, "fwg_owner", "fwg_owner_pw_1234")
    member = _login(http_client, "fwg_member", "fwg_member_pw_1234")

    for who, headers in (("owner", owner), ("member", member)):
        r = http_client.get("/api/factors/weights", headers=headers)
        assert r.status_code == 200, f"{who}: {r.text}"
        assert len(r.json()["data"]["items"]) == 15

    r = http_client.get("/api/factors/calibration?market=CN", headers=member)
    assert r.status_code == 403, r.text


# ── 3. 读旧值必须来自库: FOR UPDATE + populate_existing ────────────────────
def test_load_row_for_update_overwrites_stale_identity_map(tmp_path):
    """会话里已有 weight=1.0 的对象, 库里实际是 1.6 ⇒ 读到的必须是 1.6。"""
    from src.core import factor_weights as fw
    from src.web.models import FactorWeight

    session = _mk_session(tmp_path / "guard.db")
    try:
        session.add(FactorWeight(factor_code="alpha_score", market="CN", weight=1.0))
        session.commit()
        cached = (
            session.query(FactorWeight)
            .filter(FactorWeight.factor_code == "alpha_score")
            .one()
        )
        assert float(cached.weight) == 1.0  # 身份映射里留着旧值

        # 另一条连接把库里的值改掉并提交(模拟另一个 worker 已写入)
        other = Session(bind=session.get_bind())
        other.execute(
            text(
                "UPDATE factor_weights SET weight = 1.6 "
                "WHERE factor_code = 'alpha_score' AND market = 'CN'"
            )
        )
        other.commit()
        other.close()

        row = fw._load_row_for_update(session, "alpha_score", "CN")
        assert float(row.weight) == 1.6, "读到的是身份映射旧值, 不是库里真实值"
        assert float(cached.weight) == 1.6, "populate_existing 必须用 SELECT 结果覆盖对象"
    finally:
        session.close()


def test_set_weight_history_before_is_db_truth(tmp_path):
    """历史里 before 必须是库里真实旧值(1.6), 不是会话缓存(1.0)。

    这里**把 5 个因子行都补齐**: 稳态下 `_ensure_rows` 直接返回、不会再开
    savepoint(SAVEPOINT 提交会顺带 expire 会话对象), 所以旧值是否真实完全取决于
    `_load_row_for_update` 的 `populate_existing()` —— 这也正是生产里最常见的形态。
    """
    from src.core.factor_weights import CALIBRATABLE_FACTORS, set_factor_weight
    from src.web.models import FactorWeight, FactorWeightHistory

    session = _mk_session(tmp_path / "hist.db")
    try:
        for factor in CALIBRATABLE_FACTORS:
            session.add(
                FactorWeight(
                    factor_code=factor,
                    market="CN",
                    weight=1.0,
                )
            )
        session.commit()
        cached = (
            session.query(FactorWeight)
            .filter(FactorWeight.factor_code == "alpha_score")
            .one()
        )
        assert float(cached.weight) == 1.0  # 身份映射里的旧值

        # 另一条连接把库里的值改成 1.6 并提交(模拟另一个 worker 已写入)
        other = Session(bind=session.get_bind())
        other.execute(
            text(
                "UPDATE factor_weights SET weight = 1.6 "
                "WHERE factor_code = 'alpha_score' AND market = 'CN'"
            )
        )
        other.commit()
        other.close()

        res = set_factor_weight(
            "alpha_score", "CN", weight=1.2, db=session, actor="unit-owner"
        )
        assert res["weight"] == 1.2

        hist = (
            session.query(FactorWeightHistory)
            .filter(FactorWeightHistory.factor_code == "alpha_score")
            .order_by(FactorWeightHistory.id.desc())
            .first()
        )
        assert hist is not None, "权重变化必须写历史"
        assert float(hist.old_weight) == 1.6, f"before 失真: {hist.old_weight}"
        assert float(hist.new_weight) == 1.2
    finally:
        session.close()


# ── 4. 首次补行并发冲突: 可恢复, 但不吞错 ──────────────────────────────────
def test_ensure_rows_recovers_from_concurrent_first_fill(tmp_path, monkeypatch):
    """本会话看到「缺行」, 而别的进程已经补上 ⇒ 撞唯一键 → 按真实行恢复。"""
    from src.core import factor_weights as fw
    from src.web.models import FactorWeight

    engine = create_engine(f"sqlite:///{tmp_path / 'fill.db'}")
    Base.metadata.create_all(engine)

    other = Session(bind=engine)
    for factor in fw.CALIBRATABLE_FACTORS:
        other.add(FactorWeight(factor_code=factor, market="CN", weight=1.0))
    other.commit()
    other.close()

    session = Session(bind=engine)
    real = fw._rows_for_market

    def _stale(db_, market_, *, refresh=False):
        # 第一次(缺失扫描)假装还没看见别人刚提交的行; 恢复路径必须回真实库里读
        if not refresh:
            return {}
        return real(db_, market_, refresh=refresh)

    monkeypatch.setattr(fw, "_rows_for_market", _stale)
    try:
        out = fw._ensure_rows(session, "CN")
        assert out == {f: 1.0 for f in fw.CALIBRATABLE_FACTORS}
        rows = session.query(FactorWeight).filter(FactorWeight.market == "CN").all()
        assert len(rows) == len(fw.CALIBRATABLE_FACTORS), "重试后不得留下重复/半截行"
        # 外层事务仍然可用(savepoint 只回滚了补行那一段)
        assert session.query(FactorWeight).count() == len(fw.CALIBRATABLE_FACTORS)
    finally:
        session.close()


def test_ensure_rows_does_not_swallow_unrelated_integrity_error(tmp_path, monkeypatch):
    """重读后仍然缺行 ⇒ 说明不是补行竞态, 必须原样上抛(不许静默当作已补齐)。

    构造: 库里**已有**这些行(所以 INSERT 必撞唯一键), 但读取路径看不到它们 ——
    冲突不是「别人刚补上」, 恢复后仍缺行, 此时吞掉异常就会让调用方拿着
    不存在的行继续写。
    """
    from src.core import factor_weights as fw
    from src.web.models import FactorWeight

    engine = create_engine(f"sqlite:///{tmp_path / 'fill2.db'}")
    Base.metadata.create_all(engine)
    seed = Session(bind=engine)
    for factor in fw.CALIBRATABLE_FACTORS:
        seed.add(FactorWeight(factor_code=factor, market="CN", weight=1.0))
    seed.commit()
    seed.close()

    session = Session(bind=engine)
    monkeypatch.setattr(fw, "_rows_for_market", lambda db_, market_, *, refresh=False: {})
    try:
        with pytest.raises(IntegrityError):
            fw._ensure_rows(session, "CN")
    finally:
        session.rollback()
        session.close()


def test_concurrent_first_time_writes_all_succeed(tmp_path):
    """冷启动并发(同进程两线程): 都成功, 行不重复, 历史链 before 都是 1.0。"""
    from src.core.factor_weights import CALIBRATABLE_FACTORS, set_factor_weight
    from src.web.models import FactorWeight, FactorWeightHistory

    engine = create_engine(
        f"sqlite:///{tmp_path / 'conc.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)

    errors: list[BaseException] = []

    def _write(weight: float):
        s = Session(bind=engine)
        try:
            set_factor_weight(
                "alpha_score", "CN", weight=weight, db=s, actor=f"unit-{weight}"
            )
        except BaseException as exc:  # pragma: no cover - 失败时给断言看
            errors.append(exc)
        finally:
            s.close()

    threads = [threading.Thread(target=_write, args=(w,)) for w in (1.2, 1.8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)

    assert not errors, errors
    s = Session(bind=engine)
    try:
        rows = s.query(FactorWeight).filter(FactorWeight.market == "CN").all()
        assert len(rows) == len(CALIBRATABLE_FACTORS), [r.factor_code for r in rows]
        hist = (
            s.query(FactorWeightHistory)
            .filter(FactorWeightHistory.factor_code == "alpha_score")
            .order_by(FactorWeightHistory.id.asc())
            .all()
        )
        assert len(hist) == 2, [h.old_weight for h in hist]
        assert [float(h.old_weight) for h in hist] == [1.0, 1.2] or [
            float(h.old_weight) for h in hist
        ] == [1.0, 1.8], [float(h.old_weight) for h in hist]
    finally:
        s.close()


# ── 5. PG 隔离库: 真实行锁 + 真实唯一键冲突(无 PG 时跳过) ─────────────────
def _pg_sessions():
    from src.db.dialect import is_postgres

    if not is_postgres():
        pytest.skip("行锁/唯一键竞态需要真实 PG 隔离库")
    from src.db.session import WriteSessionLocal

    return WriteSessionLocal(), WriteSessionLocal()


def _pg_require_schema() -> None:
    """PG 测试库没建表时跳过, 不把「环境没准备好」记成失败。"""
    from sqlalchemy.exc import OperationalError, ProgrammingError

    from src.db.session import WriteSessionLocal
    from src.web.models import FactorWeight

    s = WriteSessionLocal()
    try:
        s.query(FactorWeight.factor_code).limit(1).all()
    except (OperationalError, ProgrammingError) as exc:
        s.rollback()
        pytest.skip(f"PG 测试库未建表(跳过): {type(exc).__name__}")
    finally:
        s.close()


def _pg_cleanup(market: str) -> None:
    from src.db.session import WriteSessionLocal
    from src.web.models import FactorWeight

    s = WriteSessionLocal()
    try:
        s.query(FactorWeight).filter(FactorWeight.market == market).delete(
            synchronize_session=False
        )
        s.commit()
    finally:
        s.close()


def test_pg_load_row_for_update_reads_committed_value():
    """真实 PG: 另一个事务已提交 1.7, 本事务 FOR UPDATE 读到的必须是 1.7。"""
    from src.core import factor_weights as fw
    from src.web.models import FactorWeight

    _pg_require_schema()
    market = "PGT"
    _pg_cleanup(market)
    a, b = _pg_sessions()
    try:
        a.add(FactorWeight(factor_code="alpha_score", market=market, weight=1.0))
        a.commit()
        stale = a.query(FactorWeight).filter(
            FactorWeight.market == market
        ).one()
        assert float(stale.weight) == 1.0

        row = b.query(FactorWeight).filter(FactorWeight.market == market).one()
        row.weight = 1.7
        b.commit()

        got = fw._load_row_for_update(a, "alpha_score", market)
        assert float(got.weight) == 1.7, "行锁读了, 但属性还是会话里的旧值"
    finally:
        a.rollback()
        b.rollback()
        a.close()
        b.close()
        _pg_cleanup(market)


def test_pg_concurrent_first_fill_recovers(monkeypatch):
    """真实 PG: 两事务抢着补同一批行 → 后者撞唯一键, 必须恢复而不是 500。"""
    from src.core import factor_weights as fw
    from src.web.models import FactorWeight

    _pg_require_schema()
    market = "PGU"
    _pg_cleanup(market)
    a, b = _pg_sessions()
    try:
        # A 先看到「无行」, 随后 B 补完并提交 → A 的 INSERT 撞唯一键
        assert fw._rows_for_market(a, market) == {}
        for factor in fw.CALIBRATABLE_FACTORS:
            b.add(FactorWeight(factor_code=factor, market=market, weight=1.0))
        b.commit()

        real = fw._rows_for_market
        monkeypatch.setattr(
            fw,
            "_rows_for_market",
            lambda db_, m_, *, refresh=False: (
                real(db_, m_, refresh=refresh) if refresh else {}
            ),
        )
        out = fw._ensure_rows(a, market)
        assert out == {f: 1.0 for f in fw.CALIBRATABLE_FACTORS}
        # 外层事务在 savepoint 回滚后仍可用, 且库里正好 5 行
        rows = a.query(FactorWeight).filter(FactorWeight.market == market).all()
        assert len(rows) == len(fw.CALIBRATABLE_FACTORS)
        a.commit()
    finally:
        a.rollback()
        b.rollback()
        a.close()
        b.close()
        _pg_cleanup(market)
