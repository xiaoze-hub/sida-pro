# -*- coding: utf-8 -*-
"""只读因子标定的**可消费性**与可观测性(2026-11-12 独立审计)。

背景: 标定已经收敛成「只出建议」, 但建议只能靠内部函数拿到, HTTP 上没有任何
入口; 权重列表里的 `last_ic` 只是历史 meta, 却长得跟当前值一样, 容易被当成
「现在算出来的 IC」。本文件锁住三件事:

1. `GET /api/factors/calibration` 实时计算只读建议, 并**显式**给出
   `computed_at` / `ok` / `error` / 每因子 `sample_size` / `rows_present` /
   `no_rows` —— 「没数据」「样本不足」「算失败」三者必须可区分。
2. 该端点零写入: 不建权重行(不 lazy seed)、不写 meta/历史、不改任何有效权重。
3. 旧列表里的 `last_ic` 必须标成历史观测(带观测时间 + 过期标记),
   不得冒充当前值。
4. 核心层出错时**不得回滚调用方传入的 session**(那会连带丢掉调用方未提交事务)。
"""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

ADMIN_USER = "fac_obs_admin"
ADMIN_PW = "fac_obs_admin_pw_1234"


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr("src.web.middleware.RATE_LIMIT_ENABLED", False)
    monkeypatch.setenv("AUTH_USERNAME", ADMIN_USER)
    monkeypatch.setenv("AUTH_PASSWORD", ADMIN_PW)
    from src.web.database import SessionLocal
    from tests.conftest import purge_users

    db = SessionLocal()
    try:
        # 只清本文件的账号, 不动别的测试模块已建好的 owner(跨文件污染会连坐)。
        purge_users(db, only_username=ADMIN_USER)
        from src.web.api.auth import create_user, hash_password
        from src.web.models import User

        row = db.query(User).filter(User.username == ADMIN_USER).first()
        if row is None:
            create_user(db, ADMIN_USER, ADMIN_PW, "owner")
        else:
            row.password_hash = hash_password(ADMIN_PW)
            row.role = "owner"
            row.is_active = True
            db.commit()
        _clear_factor_tables(db)
    finally:
        db.close()

    from src.web.api import factors as factors_api
    from src.web.app import app

    factors_api._reset_calibration_cache()
    c = TestClient(app)
    try:
        yield c
    finally:
        c.cookies.clear()
        factors_api._reset_calibration_cache()
        # 不能把自己的 owner 留在测试库里: test_permissions_rbac 依赖「无 owner 时
        # 由 env 首启建号」, 残留 owner 会让那条路径失效(实测跨文件连坐)。
        from src.web.database import SessionLocal
        from tests.conftest import purge_users

        db = SessionLocal()
        try:
            purge_users(db, only_username=ADMIN_USER)
        finally:
            db.close()


def _clear_factor_tables(db):
    from src.web.models import (
        FactorWeight,
        FactorWeightHistory,
        StrategyFactorSnapshot,
        StrategyOutcome,
        StrategySignalRun,
    )

    for model in (
        FactorWeightHistory,
        FactorWeight,
        StrategyOutcome,
        StrategyFactorSnapshot,
        StrategySignalRun,
    ):
        db.query(model).delete()
    db.commit()


def _login(c: TestClient) -> str:
    r = c.post("/api/auth/login", json={"username": ADMIN_USER, "password": ADMIN_PW})
    assert r.status_code == 200, r.text
    return r.json()["data"]["token"]


def _hdr(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _get(client, token, path="/api/factors/calibration?market=CN"):
    r = client.get(path, headers=_hdr(token))
    return r


def _count(model):
    from src.web.database import SessionLocal

    db = SessionLocal()
    try:
        return db.query(model).count()
    finally:
        db.close()


def _seed_pair(db, sid, *, market="CN", snapshot_date, alpha=0.0, ret=0.0, horizon=5):
    from src.web.models import StrategyFactorSnapshot, StrategyOutcome
    from src.web.models import StrategySignalRun

    # 快照/结果都对 strategy_signal_runs 有外键; 真库开着 FK, 父行必须先有。
    db.add(
        StrategySignalRun(
            id=sid,
            snapshot_date=snapshot_date,
            stock_symbol=f"S{sid}",
            stock_market=market,
            strategy_code="trend_follow",
        )
    )
    # 模型之间没有 relationship(), flush 顺序不保证父行在前 —— 显式先落父行。
    db.flush()
    db.add(
        StrategyFactorSnapshot(
            signal_run_id=sid,
            snapshot_date=snapshot_date,
            stock_symbol=f"S{sid}",
            stock_market=market,
            strategy_code="trend_follow",
            alpha_score=alpha,
            final_score=50.0,
        )
    )
    db.add(
        StrategyOutcome(
            signal_run_id=sid,
            strategy_code="trend_follow",
            stock_symbol=f"S{sid}",
            stock_market=market,
            snapshot_date=snapshot_date,
            horizon_days=horizon,
            target_date=snapshot_date,
            outcome_return_pct=ret,
            outcome_status="evaluated",
        )
    )


def _seed_snapshots(days=2, per_day=3, market="CN"):
    from src.web.database import SessionLocal

    db = SessionLocal()
    try:
        sid = 100
        for di in range(days):
            d = (date.today() - timedelta(days=30 + di)).strftime("%Y-%m-%d")
            for rank in range(1, per_day + 1):
                _seed_pair(db, sid, market=market, snapshot_date=d,
                           alpha=float(rank), ret=float(rank))
                sid += 1
        db.commit()
    finally:
        db.close()


# ── 1. 无行 / 无数据必须可区分, 且零写入 ────────────────────────────────
def test_calibration_endpoint_no_rows_no_seed(client):
    from src.web.models import FactorWeight, FactorWeightHistory

    token = _login(client)
    r = _get(client, token)
    assert r.status_code == 200, r.text
    data = r.json()["data"]

    assert data["mode"] == "readonly"
    assert data["ok"] is True
    assert data["write_back"] is False
    assert data["changed"] == 0
    assert data["rows_present"] == 0
    assert data["no_rows"] is True
    assert data["computed_at"], "必须给出实际计算时间"
    assert len(data["proposals"]) == 5
    assert all(p["status"] == "skipped_no_data" for p in data["proposals"])

    assert _count(FactorWeight) == 0, "只读端点不得 lazy seed 权重行"
    assert _count(FactorWeightHistory) == 0


def test_calibration_endpoint_does_not_touch_existing_rows(client):
    from src.web.database import SessionLocal
    from src.web.models import FactorWeight, FactorWeightHistory

    db = SessionLocal()
    try:
        db.add(FactorWeight(factor_code="alpha_score", market="CN", weight=1.25,
                            reason="manual", meta={"note": "keep-me"}))
        db.commit()
    finally:
        db.close()

    token = _login(client)
    r = _get(client, token)
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["rows_present"] == 1
    assert data["no_rows"] is False

    db = SessionLocal()
    try:
        row = db.query(FactorWeight).filter_by(factor_code="alpha_score", market="CN").one()
        assert row.weight == 1.25
        assert row.reason == "manual"
        assert dict(row.meta or {}) == {"note": "keep-me"}
        assert db.query(FactorWeightHistory).count() == 0
    finally:
        db.close()


def test_calibration_endpoint_reports_low_sample_with_sample_size(client):
    _seed_snapshots(days=2, per_day=3)
    token = _login(client)
    data = _get(client, token).json()["data"]
    by_code = {p["factor_code"]: p for p in data["proposals"]}
    alpha = by_code["alpha_score"]
    assert alpha["sample_size"] > 0, "有快照就必须报出真实样本量"
    assert alpha["status"] == "skipped_low_sample"
    assert data["skipped_low_sample"] >= 1


def test_calibration_endpoint_reports_error_explicitly(client, monkeypatch, caplog):
    from src.core import factor_calibration as fc

    def _boom(**kwargs):
        raise RuntimeError(
            "sqlite 读失败: SELECT weight, meta FROM factor_weights WHERE market='CN'; "
            "dsn=postgresql://sida:S1da-Pr0d-Pa55w0rd@db.internal:5432/sida; "
            "[parameters: ('622-CN-bound-param',)]"
        )

    monkeypatch.setattr(fc, "evaluate_factor_ic", _boom)
    token = _login(client)
    with caplog.at_level("ERROR"):
        r = _get(client, token)
    # 失败必须让 **HTTP 状态本身**失败: 200 + ok=false 会被监控当成功。
    assert r.status_code == 503, r.text
    body = r.json()
    assert body["success"] is False and body["data"] is None
    message = body["message"]
    # 失败必须显式, 但**不得把异常原文/SQL/DSN/绑定参数回给客户端**。
    for leak in ("sqlite 读失败", "SELECT weight", "postgresql://", "S1da-Pr0d-Pa55w0rd",
                 "622-CN-bound-param", "factor_weights", "Traceback"):
        assert leak not in r.text, f"响应泄漏: {leak!r}"
    assert "ERR-" in message

    # 服务端日志: 关联号 + 异常类型在, SQL/DSN/参数/表名不在(白名单口径)。
    logs = "\n".join(rec.getMessage() for rec in caplog.records)
    ref = next(
        (tok for tok in message.replace("(", " ").replace(")", " ").split()
         if tok.startswith("ERR-")),
        "",
    )
    assert ref and ref in logs, f"日志里没有同一个关联号: {logs!r}"
    assert "RuntimeError" in logs
    # 注意: 不能拿 "factor_weights" 当日志泄漏串 —— 帧位置里的函数名
    # `calibrate_factor_weights` 合法地含这个子串(代码标识符, 不是数据)。
    for leak in ("SELECT weight", "postgresql://", "S1da-Pr0d-Pa55w0rd",
                 "622-CN-bound-param"):
        assert leak not in logs, f"日志泄漏: {leak!r}"

    from src.web.models import FactorWeight, FactorWeightHistory

    assert _count(FactorWeight) == 0
    assert _count(FactorWeightHistory) == 0


def test_calibration_endpoint_rejects_unknown_market(client):
    token = _login(client)
    r = _get(client, token, "/api/factors/calibration?market=XX")
    assert r.status_code == 400, r.text


def test_calibration_endpoint_is_cached_to_avoid_expensive_repeat(client, monkeypatch):
    from src.core import factor_calibration as fc

    calls = {"n": 0}
    real = fc.evaluate_factor_ic

    def _counting(**kwargs):
        calls["n"] += 1
        return real(**kwargs)

    monkeypatch.setattr(fc, "evaluate_factor_ic", _counting)
    token = _login(client)

    first = _get(client, token).json()["data"]
    second = _get(client, token).json()["data"]

    assert calls["n"] == 1, f"同一市场重复请求不应重复重算, 实际 {calls['n']} 次"
    assert second["cached"] is True
    assert second["computed_at"] == first["computed_at"]
    assert second["cache_age_seconds"] >= 0


def test_calibration_endpoint_throttles_parameter_sweeps(client):
    """换参数绕开缓存也不行: 实际计算之间有全局节流, 返回 429 而不是硬算。"""
    token = _login(client)
    assert _get(client, token, "/api/factors/calibration?market=CN").status_code == 200

    r = _get(client, token, "/api/factors/calibration?market=HK")
    assert r.status_code == 429, f"未节流: {r.status_code} {r.text[:200]}"
    assert "频繁" in r.text

    # 但同参数再取命中缓存, 不受节流影响
    again = _get(client, token, "/api/factors/calibration?market=CN")
    assert again.status_code == 200


# ── 2. 旧 meta 观测必须标明历史 ───────────────────────────────────────────
def _put_meta(db, meta):
    from src.web.models import FactorWeight

    db.add(FactorWeight(factor_code="alpha_score", market="CN", weight=1.0, meta=meta))
    db.commit()


def test_legacy_ic_is_labelled_as_historical(client):
    from src.web.database import SessionLocal

    old_ts = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    db = SessionLocal()
    try:
        _put_meta(db, {"last_ic": 0.12, "last_ir": 0.8, "last_sample_size": 99,
                       "last_calibrated_at": old_ts})
    finally:
        db.close()

    token = _login(client)
    items = client.get("/api/factors/weights", headers=_hdr(token)).json()["data"]["items"]
    item = next(i for i in items if i["factor_code"] == "alpha_score" and i["market"] == "CN")

    assert item["last_ic"] == 0.12  # 兼容字段保留
    assert item["last_observation_at"] == old_ts
    assert item["last_observation_stale"] is True
    assert "历史" in item["observation_note"]


def test_fresh_meta_observation_is_not_marked_stale(client):
    from src.web.database import SessionLocal

    fresh_ts = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    db = SessionLocal()
    try:
        _put_meta(db, {"last_ic": 0.05, "last_calibrated_at": fresh_ts})
    finally:
        db.close()

    token = _login(client)
    items = client.get("/api/factors/weights", headers=_hdr(token)).json()["data"]["items"]
    item = next(i for i in items if i["factor_code"] == "alpha_score" and i["market"] == "CN")
    assert item["last_observation_stale"] is False
    assert item["last_observation_at"] == fresh_ts


def test_missing_meta_observation_is_stale_not_zero(client):
    from src.web.database import SessionLocal

    db = SessionLocal()
    try:
        _put_meta(db, {})
    finally:
        db.close()

    token = _login(client)
    items = client.get("/api/factors/weights", headers=_hdr(token)).json()["data"]["items"]
    item = next(i for i in items if i["factor_code"] == "alpha_score" and i["market"] == "CN")
    assert item["last_ic"] is None
    assert item["last_observation_at"] is None
    assert item["last_observation_stale"] is True


# ── 3. 核心层: 不得回滚调用方的未提交事务 ────────────────────────────────
def test_calibration_error_does_not_rollback_caller_session(monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import src.web.models  # noqa: F401
    from src.core import factor_calibration as fc
    from src.web.database import Base
    from src.web.models import FactorWeight

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()

    def _boom(**kwargs):
        raise RuntimeError("读失败")

    monkeypatch.setattr(fc, "evaluate_factor_ic", _boom)
    try:
        pending = FactorWeight(factor_code="alpha_score", market="CN", weight=1.4)
        db.add(pending)  # 调用方尚未提交

        res = fc.calibrate_factor_weights("CN", db=db)
        assert res["ok"] is False and res.get("error_ref", "").startswith("ERR-")
        assert "读失败" not in (res.get("error") or "")

        # 若核心层擅自 rollback, 这笔未提交事务会被无声丢弃
        db.commit()
        assert db.query(FactorWeight).filter_by(weight=1.4).count() == 1
    finally:
        db.close()


def test_calibration_error_on_owned_session_still_safe(monkeypatch):
    """自己开的 session 出错仍要回滚并关闭(不泄漏事务)。"""
    from src.core import factor_calibration as fc

    monkeypatch.setattr(fc, "SessionLocal", lambda: _ExplodingSession())
    res = fc.calibrate_factor_weights("CN")
    assert res["error"]
    assert res["write_back"] is False


class _ExplodingSession:
    """最小假 session: evaluate 前就炸, 只验证清理路径不抛。"""

    def rollback(self):
        return None

    def close(self):
        return None

    def query(self, *a, **kw):
        raise RuntimeError("boom")
