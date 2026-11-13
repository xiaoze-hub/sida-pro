"""system 桶**持久化**成熟对账 —— 端到端 / 幂等 / 隔离 / 未知 / 回滚(2026-11-13)。

上一版只有只读预览(`writes=0`), system 行永远不会成熟, 页面也永远没有可积累的系统
评估样本。本文件用真实 HTTP + 真实 SQLite 文件库钉死新的持久对账契约:

  * pending 由**真实系统写入路径** `save_agent_prediction_outcome(owner_scope='system')`
    产出, 不是手工 seed 一条 evaluated 冒充;
  * 成熟后写回 system 行: outcome_price/return/status=evaluated + meta.reconcile
    (数据日期/规则版本/采集时间/去重规则);
  * 只写 `user_id IS NULL AND owner_scope='system' AND status='pending'`; 个人两账号、
    历史无归属(双 NULL)、注销留档(detached)、悬空 user_id(ghost)一行不动;
  * 重复执行幂等(第二次 evaluated=0, evaluated_at 不变); 并发只有一个 runner 写入;
  * 未来/当日未收盘/停牌/缺当日行情/缺基准价/行情故障/超时 => unknown, 保持 pending,
    绝不写假结果;
  * total_pending 是未截断总数, batch_size/limit/has_more/truncated 明确标出截断;
  * 写入失败整批回滚; 管理员手动触发与自动调度均不调权/不下单。
"""
from __future__ import annotations

import asyncio
import threading
import uuid
from datetime import date, datetime, time as dtime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from src.web import models as M  # noqa: F401
from src.web.api import learning_reference as lr_api
from src.web.api import profile as profile_api
from src.web.api.auth import get_current_user, require_owner
from src.web.database import Base, get_db
from src.web.models import AgentPredictionOutcome, User

CN = timezone(timedelta(hours=8))


class _User:
    def __init__(self, uid: str, role: str = "member"):
        self.id = uid
        self.username = f"user-{uid}"
        self.role = role
        self.nickname = None
        self.avatar = None
        self.shadow_profile_json = None


def _engine(path: str):
    eng = create_engine(
        f"sqlite:///{path}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )

    @event.listens_for(eng, "connect")
    def _pragmas(dbapi_conn, _record):  # noqa: ANN001
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA busy_timeout=30000")
        cur.execute("PRAGMA journal_mode=WAL")
        cur.close()

    Base.metadata.create_all(eng)
    return eng


def _client(engine, *, user: _User | None, owner: bool = False):
    Session = sessionmaker(bind=engine)
    app = FastAPI()
    app.include_router(lr_api.router, prefix="/lr")
    app.include_router(profile_api.router, prefix="/p")

    def _db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _db
    if user is not None:
        async def _u():
            return user

        app.dependency_overrides[get_current_user] = _u

        async def _owner():
            if not owner:
                raise HTTPException(403, "仅管理员可操作")
            return user

        app.dependency_overrides[require_owner] = _owner
    c = TestClient(app, raise_server_exceptions=False)
    c._engine = engine
    c._session_factory = Session
    return c


def _prev_trading_day(d: date) -> date:
    from src.core.trading_calendar import prev_trading_day

    return prev_trading_day(d)


def _mature_pred_day(horizon: int = 1, target: date | None = None) -> date:
    """选出 pred, 使 add_trading_days(pred, horizon) == target(默认上一交易日)。"""
    cur = target or _prev_trading_day(date.today())
    for _ in range(max(1, horizon)):
        cur = _prev_trading_day(cur)
    return cur


def _bar(day: date, close: float):
    return SimpleNamespace(date=day.isoformat(), close=close)


def _fetcher(rows_by_symbol):
    def _fetch(symbol, market):
        if symbol not in rows_by_symbol:
            raise RuntimeError("provider down")
        return rows_by_symbol[symbol]
    return _fetch


def _seed_users(s, *ids: str) -> None:
    for uid in ids:
        s.add(User(
            id=uid, username=f"user-{uid}", password_hash="x", role="member",
            is_active=True, token_version=1,
        ))
    s.flush()


def _seed_personal(s, uid: str, symbol: str, day: str, *, status: str = "pending") -> None:
    s.add(AgentPredictionOutcome(
        user_id=uid, owner_scope="user", run_trace_id=uuid.uuid4().hex,
        agent_name="premarket", stock_symbol=symbol, stock_market="CN",
        prediction_date=day, horizon_days=1, action="buy", action_label="buy",
        outcome_status=status,
    ))


def _seed_legacy(s, symbol: str, day: str, *, status: str = "pending") -> None:
    s.add(AgentPredictionOutcome(
        user_id=None, owner_scope=None, run_trace_id=uuid.uuid4().hex,
        agent_name="legacy", stock_symbol=symbol, stock_market="CN",
        prediction_date=day, horizon_days=1, action="buy", action_label="buy",
        outcome_status=status, outcome_return_pct=1.0 if status == "evaluated" else None,
    ))


def _seed_system(cs, Session, *, agent: str, symbol: str, pred_day: date,
                 horizon: int = 1, trigger_price: float | None = None,
                 meta: dict | None = None) -> int:
    """走**真实系统写入路径**产出 pending(不是手工造 evaluated)。"""
    res = cs.save_agent_prediction_outcome(
        agent_name=agent,
        stock_symbol=symbol,
        stock_market="CN",
        prediction_date=pred_day.isoformat(),
        horizon_days=horizon,
        action="buy",
        action_label="买入",
        trigger_price=trigger_price,
        meta=meta or {"source": "scheduler_run"},
        owner_scope="system",
        run_trace_id=uuid.uuid4().hex,
    )
    assert res.written and res.scored, f"system pending 未写入: {res}"
    return int(res.record_id)


def _get(s, rid: int) -> AgentPredictionOutcome:
    return s.query(AgentPredictionOutcome).filter(AgentPredictionOutcome.id == rid).one()


# ── 端到端: 真实 system pending -> 成熟持久对账 -> 汇总/页面可见 ──────────────

def test_system_pending_to_persisted_evaluated_end_to_end(tmp_path, monkeypatch):
    import src.core.context_store as cs
    import src.core.learning_reference as lr

    eng = _engine(str(tmp_path / "e2e.db"))
    Session = sessionmaker(bind=eng)
    monkeypatch.setattr(cs, "SessionLocal", Session)

    target = _prev_trading_day(date.today())
    pred = _mature_pred_day(1, target)

    s = Session()
    _seed_users(s, "u-a", "u-b")
    # 两个真实账号 + 历史无归属 + detached + ghost: 都必须保持 pending、价格为空
    _seed_personal(s, "u-a", "600519", pred.isoformat())
    _seed_personal(s, "u-b", "000001", pred.isoformat())
    _seed_legacy(s, "688981", pred.isoformat())
    s.add(AgentPredictionOutcome(
        user_id=None, owner_scope="user", run_trace_id=uuid.uuid4().hex,
        agent_name="stale", stock_symbol="688982", stock_market="CN",
        prediction_date=pred.isoformat(), horizon_days=1, action="buy",
        action_label="buy", outcome_status="pending",
    ))
    s.add(AgentPredictionOutcome(
        user_id="u-gone", owner_scope="user", run_trace_id=uuid.uuid4().hex,
        agent_name="ghost", stock_symbol="688983", stock_market="CN",
        prediction_date=pred.isoformat(), horizon_days=1, action="buy",
        action_label="buy", outcome_status="pending",
    ))
    s.commit()
    s.close()

    sys_ids = [
        _seed_system(cs, Session, agent="premarket", symbol="300750", pred_day=pred),
        _seed_system(cs, Session, agent="premarket", symbol="300751", pred_day=pred),
        _seed_system(cs, Session, agent="intraday", symbol="300752", pred_day=pred),
    ]

    c = _client(eng, user=_User("admin", "owner"), owner=True)
    rows = {
        "300750": [_bar(pred, 100.0), _bar(target, 110.0)],
        "300751": [_bar(pred, 50.0), _bar(target, 45.0)],
        "300752": [_bar(pred, 20.0), _bar(target, 22.0)],
    }
    monkeypatch.setattr(lr, "_default_kline_fetcher", _fetcher(rows))

    r = c.post("/lr/system/reconcile/run", params={
        "limit": 50, "max_symbols": 10, "fetch_timeout_seconds": 2,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["scope"] == "system" and body["owner_scope"] == "system"
    assert body["executed"] is True and body["db_committed"] is True
    assert body["writes"] == 3 and body["evaluated"] == 3
    assert body["total_pending"] == 3 and body["batch_size"] == 3
    assert body["truncated"] is False and body["has_more"] is False
    assert body["personal_rows_touched"] == 0 and body["legacy_rows_touched"] == 0
    assert body["orders_placed"] == 0 and body["weights_changed"] is False
    assert body["trades_triggered"] == 0
    assert body["data_date_min"] == target.isoformat()
    assert body["data_date_max"] == target.isoformat()
    assert body["version"] == lr.SYSTEM_RECONCILE_VERSION
    assert body["authorization"]["role"] == "owner"
    assert body["authorization"]["owner_scope"] == "system"

    # 结果真的落库, 且带数据日期/版本/采集时间/去重规则
    s = Session()
    persisted = [_get(s, i) for i in sys_ids]
    assert all(x.outcome_status == "evaluated" for x in persisted)
    assert [round(x.outcome_return_pct, 4) for x in persisted] == [10.0, -10.0, 10.0]
    for x in persisted:
        audit = (x.meta or {}).get("reconcile")
        assert audit, "system 结果缺少 meta.reconcile 审计字段"
        assert audit["data_date"] == target.isoformat()
        assert audit["version"] == lr.SYSTEM_RECONCILE_VERSION
        assert audit["dedupe_rule"]
        assert audit["collected_at"]
        assert audit["run_id"] == body["run_id"]
        assert x.evaluated_at is not None
        assert x.user_id is None and x.owner_scope == "system"

    # 个人两账号 / 历史 / detached / ghost: 一行都没被改
    for q in (
        s.query(AgentPredictionOutcome).filter(AgentPredictionOutcome.user_id.isnot(None)),
        s.query(AgentPredictionOutcome).filter(
            AgentPredictionOutcome.user_id.is_(None),
            AgentPredictionOutcome.owner_scope.in_([None, "user"]),
        ),
    ):
        for row in q.all():
            assert row.outcome_status == "pending"
            assert row.outcome_price is None
    personal_a = s.query(AgentPredictionOutcome).filter(
        AgentPredictionOutcome.user_id == "u-a").one()
    assert personal_a.meta in (None, {})
    s.close()

    # 汇总/页面数据来自刚持久化的结果
    summary = c.get("/lr/system", params={"min_n": 1}).json()
    assert summary["sample"]["evaluated_rows"] == 3
    assert summary["sample"]["pending_rows"] == 0
    assert summary["overall"]["total"] == 3
    assert summary["overall"]["hit_count"] == 2  # buy: +10, -10, +10
    assert summary["result_provenance"]["source"] == "system_reconcile"
    assert summary["automation"]["configured"] is True
    assert summary["independent_of_personal"] is True

    # 个人命中率不受影响: 两账号仍是 pending, 个人统计没有样本
    pa = c.get("/p/stats/accuracy", params={"days": 30, "min_n": 1}).json()
    assert pa["overall"]["total"] == 0
    assert pa["system_scoped_rows"] == 3

    # 第二次执行: 幂等, 不再写、不改 evaluated_at
    s = Session()
    evaluated_at_before = {x.id: x.evaluated_at for x in persisted}
    s.close()
    r2 = c.post("/lr/system/reconcile/run", params={"limit": 50})
    assert r2.status_code == 200, r2.text
    body2 = r2.json()
    assert body2["evaluated"] == 0 and body2["writes"] == 0
    assert body2["write_failed"] is False
    assert body2["total_pending"] == 0
    s = Session()
    for rid, ts in evaluated_at_before.items():
        assert _get(s, rid).evaluated_at == ts
        assert _get(s, rid).outcome_status == "evaluated"
    assert s.query(AgentPredictionOutcome).filter(
        AgentPredictionOutcome.owner_scope == "system").count() == 3
    s.close()


# ── total_pending 不得等于 limit 后的批次 ─────────────────────────────────

def test_total_pending_is_not_the_truncated_batch(tmp_path, monkeypatch):
    import src.core.context_store as cs
    import src.core.learning_reference as lr

    eng = _engine(str(tmp_path / "batch.db"))
    Session = sessionmaker(bind=eng)
    monkeypatch.setattr(cs, "SessionLocal", Session)

    target = _prev_trading_day(date.today())
    pred = _mature_pred_day(1, target)
    s = Session()
    for i, sym in enumerate(["300750", "300751", "300752", "300753", "300754"]):
        _seed_system(cs, Session, agent="premarket", symbol=sym, pred_day=pred)
    # 超出 horizon 上限的 pending 不算候选, 但全量 pending 要单独报
    _seed_system(cs, Session, agent="premarket", symbol="300755", pred_day=pred,
                 horizon=20)
    s.close()

    c = _client(eng, user=_User("admin", "owner"), owner=True)
    rows = {sym: [_bar(pred, 10.0), _bar(target, 11.0)]
            for sym in ["300750", "300751", "300752", "300753", "300754"]}
    monkeypatch.setattr(lr, "_default_kline_fetcher", _fetcher(rows))

    body = c.post("/lr/system/reconcile/run", params={
        "limit": 2, "max_symbols": 10, "max_horizon_days": 10,
    }).json()
    assert body["total_pending"] == 5, "total_pending 被 limit 截成了批次长度"
    assert body["all_pending_rows"] == 6
    assert body["excluded_horizon_pending_rows"] == 1
    assert body["batch_size"] == 2 and body["limit"] == 2
    assert body["has_more"] is True and body["truncated"] is True
    assert body["batch_capped_by_limit"] is True
    assert body["evaluated"] == 2 and body["writes"] == 2
    assert body["pending_after"] == 3
    assert "未按批次截断" in body["note"]

    # 只读预览同样不能把批次当总数
    preview = c.post("/lr/system/reconcile", params={"limit": 1}).json()
    assert preview["total_pending"] == 3
    assert preview["batch_size"] == 1
    assert preview["has_more"] is True and preview["truncated"] is True
    assert preview["writes"] == 0 and preview["executed"] is False


# ── 未知: 未来/未收盘/停牌/缺基准价/故障/超时/坏日期 都不写假结果 ───────────

def test_unknown_cases_never_write_fake_results(tmp_path, monkeypatch):
    import src.core.context_store as cs
    import src.core.learning_reference as lr

    eng = _engine(str(tmp_path / "unknown.db"))
    Session = sessionmaker(bind=eng)
    monkeypatch.setattr(cs, "SessionLocal", Session)

    today = date.today()
    target = _prev_trading_day(today)
    pred = _mature_pred_day(1, target)

    s = Session()
    # A 未来: 预测日=今天, h=1 -> 目标日=下一交易日 > 今天
    id_future = _seed_system(cs, Session, agent="a", symbol="300760", pred_day=today)
    # B 当日未收盘: 目标日=今天
    pred_same_day = _mature_pred_day(1, today)
    id_not_mature = _seed_system(cs, Session, agent="a", symbol="300761",
                                 pred_day=pred_same_day)
    # C 停牌/缺当日行情
    id_no_quote = _seed_system(cs, Session, agent="a", symbol="300762", pred_day=pred)
    # D 行情源故障
    id_error = _seed_system(cs, Session, agent="a", symbol="300763", pred_day=pred)
    # E 缺基准价(无 trigger, 预测日无收盘)
    id_no_base = _seed_system(cs, Session, agent="a", symbol="300764", pred_day=pred)
    # F 坏日期(可排序到窗口内, 但不合法)
    id_bad_date = _seed_system(cs, Session, agent="a", symbol="300765", pred_day=pred)
    # 直接把 F 改成坏日期字符串(系统写入路径拒空但不校验 2 月 30)
    rec = _get(s, id_bad_date)
    rec.prediction_date = "2026-02-30"
    # G 超时
    id_timeout = _seed_system(cs, Session, agent="a", symbol="300766", pred_day=pred)
    s.commit()
    s.close()

    rows = {
        "300761": [_bar(pred_same_day, 10.0), _bar(today, 11.0)],
        "300762": [_bar(pred, 10.0)],            # 目标日缺失 = 停牌/未知
        "300764": [_bar(target, 12.0)],          # 预测日缺失 = 无基准价
        "300765": [_bar(pred, 10.0), _bar(target, 11.0)],
    }

    def _slow_fetcher(symbol, market):
        if symbol == "300766":
            import time as _t
            _t.sleep(0.8)
            return [_bar(pred, 10.0), _bar(target, 11.0)]
        if symbol == "300763":
            raise RuntimeError("provider down")
        return _fetcher(rows)(symbol, market)

    now = datetime.combine(today, dtime(14, 0), tzinfo=CN)  # 当日未收盘
    c = _client(eng, user=_User("admin", "owner"), owner=True)
    monkeypatch.setattr(lr, "_default_kline_fetcher", _slow_fetcher)
    # API 不接受 now; 固定"当前时间"为当日 14:00, 以验证当日目标日严格未成熟
    monkeypatch.setattr(lr, "_now_cn", lambda _now=None: now)

    body = c.post("/lr/system/reconcile/run", params={
        "limit": 50, "max_symbols": 10, "fetch_timeout_seconds": 0.5,
    }).json()
    assert body["evaluated"] == 0 and body["writes"] == 0
    assert body["db_committed"] is True
    assert body["skipped_not_due"] >= 1
    assert body["skipped_not_mature"] >= 1
    assert body["skipped_no_quote"] >= 1
    assert body["skipped_no_base_price"] >= 1
    assert body["kline_error_rows"] >= 1
    assert body["fetch_timeout_rows"] >= 1

    s = Session()
    for rid in (id_future, id_not_mature, id_no_quote, id_error, id_no_base,
                id_bad_date, id_timeout):
        row = _get(s, rid)
        assert row.outcome_status == "pending", f"unknown 行被写成了 {row.outcome_status}"
        assert row.outcome_price is None
        assert row.outcome_return_pct is None
        assert not ((row.meta or {}).get("reconcile")), "unknown 行不应有对账结果"
    s.close()


# ── 并发: 同一批 pending 只有一个 runner 写入成功 ──────────────────────────

def test_concurrent_reconcile_is_idempotent(tmp_path, monkeypatch):
    import src.core.context_store as cs
    import src.core.learning_reference as lr

    eng = _engine(str(tmp_path / "concurrent.db"))
    Session = sessionmaker(bind=eng)
    monkeypatch.setattr(cs, "SessionLocal", Session)

    target = _prev_trading_day(date.today())
    pred = _mature_pred_day(1, target)
    s = Session()
    rid = _seed_system(cs, Session, agent="premarket", symbol="300750", pred_day=pred)
    s.close()

    barrier = threading.Barrier(2)

    def _slow_fetch(symbol, market):
        barrier.wait(timeout=5)
        return [_bar(pred, 10.0), _bar(target, 12.0)]

    results: list[dict] = []
    errors: list[BaseException] = []

    def _run():
        try:
            db = Session()
            try:
                results.append(lr.reconcile_system_outcomes(
                    db, limit=10, max_symbols=5, fetch_timeout_seconds=2,
                    kline_fetcher=_slow_fetch, trigger="concurrent",
                ))
            finally:
                db.close()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=_run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    assert not errors, [type(e).__name__ for e in errors]
    assert len(results) == 2

    evaluated = sum(int(r.get("evaluated") or 0) for r in results)
    assert evaluated == 1, f"并发重复写入: {[r.get('evaluated') for r in results]}"
    assert sum(int(r.get("skipped_concurrent") or 0) for r in results) == 1
    assert all(r.get("db_committed") is True for r in results)

    s = Session()
    row = _get(s, rid)
    assert row.outcome_status == "evaluated"
    assert round(row.outcome_return_pct, 4) == 20.0
    audit = (row.meta or {}).get("reconcile")
    assert audit and audit["trigger"] == "concurrent"
    assert s.query(AgentPredictionOutcome).filter(
        AgentPredictionOutcome.owner_scope == "system").count() == 1
    s.close()


# ── 失败回滚: 半批结果绝不落库 ───────────────────────────────────────────

def test_write_failure_rolls_back_whole_batch(tmp_path, monkeypatch):
    import src.core.context_store as cs
    import src.core.learning_reference as lr

    eng = _engine(str(tmp_path / "rollback.db"))
    Session = sessionmaker(bind=eng)
    monkeypatch.setattr(cs, "SessionLocal", Session)

    target = _prev_trading_day(date.today())
    pred = _mature_pred_day(1, target)
    s = Session()
    id1 = _seed_system(cs, Session, agent="a", symbol="300750", pred_day=pred)
    id2 = _seed_system(cs, Session, agent="a", symbol="300751", pred_day=pred)
    s.close()

    real_mark = cs.mark_system_prediction_outcome
    calls = {"n": 0}

    def _flaky_mark(**kwargs):
        calls["n"] += 1
        ok = real_mark(**kwargs)  # 第一次先写入(未提交), 第二次抛错
        if calls["n"] >= 2:
            raise RuntimeError("simulated db failure")
        return ok

    monkeypatch.setattr(cs, "mark_system_prediction_outcome", _flaky_mark)
    rows = {
        "300750": [_bar(pred, 10.0), _bar(target, 11.0)],
        "300751": [_bar(pred, 10.0), _bar(target, 12.0)],
    }
    db = Session()
    result = lr.reconcile_system_outcomes(
        db, limit=10, max_symbols=5, kline_fetcher=_fetcher(rows),
    )
    db.close()

    assert result["evaluated"] == 0 and result["writes"] == 0
    assert result["write_failed"] is True
    assert result["db_committed"] is False
    assert "回滚" in result["note"]
    s = Session()
    for rid in (id1, id2):
        row = _get(s, rid)
        assert row.outcome_status == "pending"
        assert row.outcome_price is None
        assert not ((row.meta or {}).get("reconcile"))
    s.close()


# ── 归属硬隔离: 系统标记函数不可能命中个人/历史/detached 行 ────────────────

def test_system_mark_function_cannot_touch_non_system_rows(tmp_path):
    import src.core.context_store as cs

    eng = _engine(str(tmp_path / "scope.db"))
    Session = sessionmaker(bind=eng)
    s = Session()
    _seed_users(s, "u-a")
    _seed_personal(s, "u-a", "600519", date.today().isoformat())
    _seed_legacy(s, "688981", date.today().isoformat())
    s.add(AgentPredictionOutcome(
        user_id=None, owner_scope="user", run_trace_id=uuid.uuid4().hex,
        agent_name="stale", stock_symbol="688982", stock_market="CN",
        prediction_date=date.today().isoformat(), horizon_days=1, action="buy",
        action_label="buy", outcome_status="pending",
    ))
    s.commit()
    ids = [r.id for r in s.query(AgentPredictionOutcome).all()]
    s.close()

    db = Session()
    for rid in ids:
        assert cs.mark_system_prediction_outcome(
            record_id=rid, outcome_price=10.0, outcome_return_pct=1.0,
            audit_meta={"reconcile": {"x": 1}}, db=db,
        ) is False
    db.commit()
    db.close()

    s = Session()
    for rid in ids:
        row = _get(s, rid)
        assert row.outcome_status == "pending"
        assert row.outcome_price is None
    s.close()


# ── 授权: 手动持久对账仅 owner; 匿名不可读 ───────────────────────────────

def test_manual_reconcile_requires_owner(tmp_path):
    eng = _engine(str(tmp_path / "auth.db"))
    member = _client(eng, user=_User("u-a", "member"), owner=False)
    assert member.post("/lr/system/reconcile/run").status_code == 403
    anon = _client(eng, user=None)
    assert anon.post("/lr/system/reconcile/run").status_code == 401
    assert anon.get("/lr/system").status_code == 401


# ── 自动调度: 任务已注册 + 可执行(scheduler trigger) ─────────────────────

def test_system_reconcile_job_is_registered_and_uses_scheduler_trigger(monkeypatch):
    from src.core import context_scheduler as cs
    import src.core.scheduler_registry as registry

    captured: list[dict] = []

    def _fake_reconcile(**kwargs):
        captured.append(kwargs)
        return {"evaluated": 0, "total_pending": 0, "write_failed": False}

    monkeypatch.setattr(cs, "reconcile_system_outcomes", _fake_reconcile)

    class _FakeAPS:
        def __init__(self):
            self.jobs: list[str] = []
            self.running = False

        def add_job(self, *_a, **kw):
            self.jobs.append(str(kw.get("id")))

        def start(self):
            self.running = True

        def get_job(self, job_id):
            return None

    scheduler = cs.ContextMaintenanceScheduler(timezone="Asia/Shanghai")
    fake = _FakeAPS()
    scheduler.scheduler = fake
    monkeypatch.setattr(registry, "register", lambda *_a, **_k: None)
    scheduler.start()
    assert cs.SYSTEM_RECONCILE_JOB_ID in fake.jobs

    asyncio.run(scheduler._system_reconcile_job())
    assert captured and captured[0]["trigger"] == "scheduler"

    from src.core.learning_reference import (
        SYSTEM_RECONCILE_CRON,
        system_reconcile_automation,
    )

    automation = system_reconcile_automation()
    assert automation["configured"] is True
    assert automation["cron"] == SYSTEM_RECONCILE_CRON
    assert automation["auto_trading"] is False
    assert automation["auto_weighting"] is False
