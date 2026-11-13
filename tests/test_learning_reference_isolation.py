"""历史无归属经验参考 + system 桶独立对账 —— 真实 HTTP 隔离契约(2026-11-13)。

生产存量 4.4 万+ 条已评估后验是加 `user_id` 之前的记录: 无法证明属于哪个账号,
因此**不能**进入任何人的个人命中率, 也不能被清洗/回填成"某个人的成绩"。它们仍有
"整体系统经验"的参考价值 —— 但只能在**全局汇总**层面出现, 并且必须同时说清:

  * 历史无归属 / 未验证 / 仅经验参考 / 非个人成绩;
  * 来源时点不可证 ⇒ 不训练、不自动调权、不宣称回测;
  * 真实分母(方向性动作)与重复键潜在风险。

system 桶(无会话的系统调度运行)是第三条独立数据路: 与个人结果互不合并,
只做全局聚合; 成熟时只能做**只读行情对账预览**, 不写回、不改权重、不下单。

本文件用真实 HTTP(TestClient + 真依赖覆盖)钉死: 两个用户 + system + 旧记录
同时存在时, 三路权限/计分严格隔离; 故障态 fail-closed; 对账预览零写入。
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.web import models as M  # noqa: F401
from src.web.api import learning_reference as lr_api
from src.web.api import profile as profile_api
from src.web.api.auth import get_current_user, require_owner
from src.web.database import Base, get_db
from src.web.models import AgentPredictionOutcome, User

SYMBOL_A = "600519"
SYMBOL_B = "000001"
SYMBOL_SYS = "300750"
SYMBOL_LEGACY = "688981"


class _User:
    def __init__(self, uid: str, role: str = "member"):
        self.id = uid
        self.username = f"user-{uid}"
        self.role = role
        self.nickname = None
        self.avatar = None
        self.shadow_profile_json = None


def _client(monkeypatch, *, user: _User | None, owner: bool = False,
            engine=None) -> TestClient:
    if engine is None:
        engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
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


def _seed(s, uid: str, agent: str, action: str, ret: float, day: str,
          *, symbol: str, owner_scope: str | None, status: str = "evaluated",
          horizon: int = 1) -> None:
    s.add(AgentPredictionOutcome(
        user_id=uid, owner_scope=owner_scope, agent_name=agent,
        stock_symbol=symbol, stock_market="CN", prediction_date=day,
        horizon_days=horizon, action=action, action_label=action,
        outcome_return_pct=ret, outcome_status=status,
    ))


def _today() -> str:
    return date.today().isoformat()


def _days_ago(n: int) -> str:
    return (date.today() - timedelta(days=n)).isoformat()


def _assert_no_sensitive(payload, *, symbols=(), user_ids=()):
    """响应里不得出现个股代码 / 用户标识 / 明细键。"""
    blob = json.dumps(payload, ensure_ascii=False)
    for value in list(symbols) + list(user_ids):
        assert value not in blob, f"响应泄露了敏感值 {value!r}"
    forbidden_keys = {"user_id", "stock_symbol", "symbol", "username", "nickname", "owner_id"}

    def _walk(node, path="$"):
        if isinstance(node, dict):
            for k, v in node.items():
                assert k not in forbidden_keys, f"响应出现明细键 {path}.{k}"
                _walk(v, f"{path}.{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                _walk(v, f"{path}[{i}]")

    _walk(payload)


# ── 旧数据: 仅全局汇总, 明确标注, 不暴露任何人/个股 ──────────────────────

def test_legacy_summary_is_global_only_and_never_personal(monkeypatch):
    """两个真实用户 + system + 旧记录同时存在: 旧记录只按全局汇总, 三方互不混算。"""
    c = _client(monkeypatch, user=_User("u-a"))
    s = c._session_factory()
    day = _days_ago(10)
    # 个人 u-a: 1 中 1; 个人 u-b: 1 中 0
    _seed(s, "u-a", "premarket", "buy", 3.0, day, symbol=SYMBOL_A, owner_scope="user")
    _seed(s, "u-b", "premarket", "buy", -3.0, day, symbol=SYMBOL_B, owner_scope="user")
    # 历史无归属(加列前): 2 中 1, 含一条中性不计分母
    _seed(s, None, "premarket", "buy", 4.0, day, symbol=SYMBOL_LEGACY, owner_scope=None)
    _seed(s, None, "premarket", "sell", 2.0, day, symbol="688982", owner_scope=None)
    _seed(s, None, "premarket", "watch", 9.0, day, symbol="688983", owner_scope=None)
    # system 桶: 独立
    _seed(s, None, "premarket", "buy", -2.0, day, symbol=SYMBOL_SYS, owner_scope="system")
    s.commit()
    s.close()

    r = c.get("/lr/legacy", params={"min_n": 1})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["scope"] == "legacy_unowned"
    assert body["global_only"] is True
    assert body["attributable_to_user"] is False
    assert body["personal_scores_affected"] is False
    assert body["provenance_verified"] is False
    assert body["trainable"] is False
    assert body["auto_weighting"] is False
    assert body["backtest_claim_allowed"] is False
    # 分母: 2 条方向性 + 1 条中性被排除; system / 个人行绝不混入
    assert body["sample"]["evaluated_rows"] == 3
    assert body["sample"]["directional_sample"] == 2
    assert body["sample"]["excluded_neutral_rows"] == 1
    assert body["overall"]["hit_count"] == 1
    assert body["overall"]["total"] == 2
    assert body["overall"]["hit_rate"] == 50.0
    for label in ("历史无归属", "未验证", "仅经验参考", "非个人成绩"):
        assert label in body["labels"]
    _assert_no_sensitive(body, symbols=(SYMBOL_A, SYMBOL_B, SYMBOL_LEGACY, SYMBOL_SYS,
                                        "688982", "688983"), user_ids=("u-a", "u-b"))

    # 另一账号看到的是同一份全局参考(它不是任何人的成绩)
    c_b = _client(monkeypatch, user=_User("u-b"), engine=c._engine)
    body_b = c_b.get("/lr/legacy", params={"min_n": 1}).json()
    assert body_b["overall"] == body["overall"]

    # 两个账号的个人榜各自独立, 旧记录/system 都不在里面
    pa = c.get("/p/stats/accuracy", params={"days": 30, "min_n": 1}).json()
    pb = c_b.get("/p/stats/accuracy", params={"days": 30, "min_n": 1}).json()
    assert pa["overall"]["total"] == 1 and pa["overall"]["hit_count"] == 1
    assert pb["overall"]["total"] == 1 and pb["overall"]["hit_count"] == 0
    assert pa["legacy_unscoped_rows"] == 3
    assert pa["system_scoped_rows"] == 1


def test_legacy_duplicate_risk_and_real_denominator_are_reported(monkeypatch):
    """旧表没有部分唯一索引: 同键重复必须如实报出, 汇总按最早一行去重, 不算两次。"""
    c = _client(monkeypatch, user=_User("u-a"))
    s = c._session_factory()
    day = _days_ago(5)
    _seed(s, None, "premarket", "buy", 5.0, day, symbol=SYMBOL_LEGACY, owner_scope=None)
    _seed(s, None, "premarket", "buy", -5.0, day, symbol=SYMBOL_LEGACY, owner_scope=None)
    _seed(s, None, "premarket", "buy", 1.0, day, symbol="688982", owner_scope=None)
    _seed(s, None, "premarket", "buy", 1.0, day, symbol="688983", owner_scope=None,
          status="pending")
    s.commit()
    s.close()

    body = c.get("/lr/legacy", params={"min_n": 1}).json()
    assert body["sample"]["evaluated_rows"] == 3
    assert body["sample"]["pending_rows"] == 1
    assert body["duplicate_risk"]["key_groups_with_duplicates"] == 1
    assert body["duplicate_risk"]["rows_in_duplicate_groups"] == 2
    assert body["duplicate_risk"]["deduped_sample"] == 2
    assert body["overall"]["raw_total"] == 3
    assert body["overall"]["total"] == 2          # 去重后的真实分母
    assert body["overall"]["basis"] == "deduped_first_row_per_key"
    assert "重复" in body["duplicate_risk"]["note"]


def test_legacy_small_sample_not_ranked(monkeypatch):
    """样本不足不参评, 不给"看起来精准"的比例。"""
    c = _client(monkeypatch, user=_User("u-a"))
    s = c._session_factory()
    _seed(s, None, "rare", "buy", 3.0, _days_ago(3), symbol=SYMBOL_LEGACY, owner_scope=None)
    s.commit()
    s.close()

    body = c.get("/lr/legacy", params={"min_n": 30}).json()
    rare = body["agents"][0]
    assert rare["agent"] == "rare"
    assert rare["total"] == 1
    assert rare["qualified"] is False
    assert rare["small_sample"] is True
    assert rare["hit_rate"] is None


def test_legacy_fail_closed_when_scoring_health_blocked(monkeypatch):
    """故障态: schema 体检不可信时, 任何数字都不给(None), 而不是给 0 冒充"没有样本"。"""
    import src.core.learning_reference as lr

    c = _client(monkeypatch, user=_User("u-a"))
    s = c._session_factory()
    _seed(s, None, "premarket", "buy", 4.0, _days_ago(3), symbol=SYMBOL_LEGACY, owner_scope=None)
    s.commit()
    s.close()

    monkeypatch.setattr(lr, "_health_payload", lambda db: {
        "status": "blocked", "scoring_blocked": True,
        "reason_code": "missing", "reason": "四条后验计分唯一索引不可信",
    })
    body = c.get("/lr/legacy").json()
    assert body["scoring_blocked"] is True
    assert body["overall"]["total"] is None and body["overall"]["hit_rate"] is None
    assert body["sample"]["directional_sample"] is None
    assert "阻断" in body["note"]
    assert body["trainable"] is False and body["auto_weighting"] is False


def test_legacy_requires_authentication(monkeypatch):
    """未登录不得读取(即使是全局聚合, 也不做匿名开放)。"""
    c = _client(monkeypatch, user=None)
    assert c.get("/lr/legacy").status_code == 401
    assert c.get("/lr/system").status_code == 401


# ── system 桶: 独立聚合 + 只读对账预览 ─────────────────────────────────

def test_detached_and_ghost_rows_never_enter_global_reference(monkeypatch):
    """账号注销留档(detached)与悬空 user_id(ghost)既不是旧无归属也不是 system —— 不得混入。"""
    c = _client(monkeypatch, user=_User("u-a"))
    s = c._session_factory()
    day = _days_ago(7)
    _seed(s, None, "premarket", "buy", 4.0, day, symbol=SYMBOL_LEGACY, owner_scope=None)
    # 注销后 user_id 被置空, 但 owner_scope 仍是 user
    _seed(s, None, "premarket", "buy", 9.0, day, symbol="688982", owner_scope="user")
    # user_id 指向已不存在的账号
    _seed(s, "u-gone", "premarket", "buy", 9.0, day, symbol="688983", owner_scope="user")
    s.commit()
    s.close()

    legacy = c.get("/lr/legacy", params={"min_n": 1}).json()
    assert legacy["sample"]["evaluated_rows"] == 1, "detached/ghost 被混进了历史无归属"
    assert legacy["overall"]["total"] == 1
    assert legacy["excluded"]["detached_account_rows"] == 1
    assert legacy["excluded"]["ghost_user_rows"] == 1

    system = c.get("/lr/system", params={"min_n": 1}).json()
    assert system["sample"]["evaluated_rows"] == 0, "detached/ghost 被混进了 system 桶"
    assert system["excluded"]["detached_account_rows"] == 1
    assert system["excluded"]["ghost_user_rows"] == 1


def test_system_summary_is_independent_and_never_merged(monkeypatch):
    c = _client(monkeypatch, user=_User("u-a"))
    s = c._session_factory()
    day = _days_ago(8)
    _seed(s, "u-a", "premarket", "buy", 9.0, day, symbol=SYMBOL_A, owner_scope="user")
    _seed(s, None, "premarket", "buy", -4.0, day, symbol=SYMBOL_SYS, owner_scope="system")
    _seed(s, None, "premarket", "sell", -1.0, day, symbol="300751", owner_scope="system")
    _seed(s, None, "premarket", "buy", 2.0, day, symbol="300752", owner_scope="system",
          status="pending")
    s.commit()
    s.close()

    body = c.get("/lr/system", params={"min_n": 1}).json()
    assert body["scope"] == "system"
    assert body["independent_of_personal"] is True
    assert body["merged_into_personal"] is False
    assert body["global_only"] is True
    assert body["personal_scores_affected"] is False
    assert body["trainable"] is False and body["auto_weighting"] is False
    assert body["sample"]["evaluated_rows"] == 2
    assert body["sample"]["directional_sample"] == 2
    assert body["sample"]["pending_rows"] == 1
    assert body["sample"]["unknown_status_rows"] == 0
    assert body["unknown"]["pending_rows"] == 1
    assert body["unknown"]["status_rows"] == 0
    assert body["overall"]["total"] == 2
    # buy 跌 -4% = 未命中; sell 跌 -1% = 命中(方向性口径)
    assert body["overall"]["hit_count"] == 1
    _assert_no_sensitive(body, symbols=(SYMBOL_A, SYMBOL_SYS, "300751", "300752"),
                         user_ids=("u-a",))


def _fake_fetcher(rows_by_symbol):
    def _fetch(symbol, market):
        if symbol not in rows_by_symbol:
            raise RuntimeError("provider down")
        return rows_by_symbol[symbol]
    return _fetch


def test_reconcile_preview_is_read_only_and_never_touches_personal(monkeypatch):
    """只读行情对账: 算出"本来会评估多少", 但一行不写; 个人行完全不动。"""
    import src.core.learning_reference as lr
    import src.core.context_store as cs

    c = _client(monkeypatch, user=_User("admin", "owner"), owner=True)
    s = c._session_factory()
    day = _days_ago(20)
    _seed(s, None, "premarket", "buy", None, day, symbol=SYMBOL_SYS,
          owner_scope="system", status="pending")
    _seed(s, "u-a", "premarket", "buy", None, day, symbol=SYMBOL_A,
          owner_scope="user", status="pending")
    s.commit()

    def _boom(**kwargs):
        raise AssertionError("只读对账不得调用任何写入函数")

    from src.core.prediction_outcome import _add_trading_days

    target_day = _add_trading_days(date.fromisoformat(day), 1)
    monkeypatch.setattr(cs, "mark_agent_prediction_outcome", _boom)
    monkeypatch.setattr(cs, "save_agent_prediction_outcome", _boom)
    monkeypatch.setattr(lr, "_default_kline_fetcher", _fake_fetcher({
        SYMBOL_SYS: [SimpleNamespace(date=day, close=10.0),
                     SimpleNamespace(date=target_day.isoformat(), close=11.0)],
    }))

    r = c.post("/lr/system/reconcile", params={"max_horizon_days": 10, "limit": 50})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["dry_run"] is True and body["read_only"] is True
    assert body["writes"] == 0 and body["executed"] is False
    assert body["total_pending"] == 1, "对账只能看到 system 桶, 绝不能把个人 pending 行算进来"
    assert body["batch_size"] == 1 and body["has_more"] is False and body["truncated"] is False
    assert body["would_evaluate"] == 1
    assert body["weights_changed"] is False
    assert body["auto_weighting"] is False

    s2 = c._session_factory()
    sys_row = s2.query(AgentPredictionOutcome).filter(
        AgentPredictionOutcome.owner_scope == "system").one()
    user_row = s2.query(AgentPredictionOutcome).filter(
        AgentPredictionOutcome.user_id == "u-a").one()
    assert sys_row.outcome_status == "pending" and sys_row.outcome_price is None
    assert user_row.outcome_status == "pending" and user_row.outcome_price is None
    s2.close()


def test_reconcile_preview_skips_future_and_survives_provider_failure(monkeypatch):
    """未来时点不算到期; 行情故障逐行降级, 不外抛、不写入。"""
    import src.core.learning_reference as lr

    c = _client(monkeypatch, user=_User("admin", "owner"), owner=True)
    s = c._session_factory()
    _seed(s, None, "premarket", "buy", None, (date.today() + timedelta(days=3)).isoformat(),
          symbol=SYMBOL_SYS, owner_scope="system", status="pending")
    _seed(s, None, "premarket", "buy", None, _days_ago(20),
          symbol="300753", owner_scope="system", status="pending")
    s.commit()
    s.close()

    monkeypatch.setattr(lr, "_default_kline_fetcher", _fake_fetcher({}))
    body = c.post("/lr/system/reconcile", params={"max_horizon_days": 10, "limit": 50}).json()
    assert body["writes"] == 0
    assert body["total_pending"] == 1
    assert body["all_pending_rows"] == 2
    assert body["future_prediction_pending_rows"] == 1
    assert body["batch_size"] == 1
    assert body["skipped_not_due"] == 0
    assert body["skipped_no_price"] == 0
    assert body["skipped_no_quote"] == 0
    assert body["kline_error_rows"] == 1
    assert body["would_evaluate"] == 0


def test_reconcile_requires_owner(monkeypatch):
    """写路径(哪怕是只读预览)只对管理员开放; 普通用户 403, 匿名 401。"""
    c_member = _client(monkeypatch, user=_User("u-a", "member"), owner=False)
    assert c_member.post("/lr/system/reconcile").status_code == 403

    c_anon = _client(monkeypatch, user=None, owner=False)
    assert c_anon.post("/lr/system/reconcile").status_code == 401


def test_mark_guard_still_refuses_system_rows():
    """既有写入闸门本身也拒绝无归属行 —— 只读预览之外没有"顺手回填"的口子。"""
    from src.core.context_store import mark_agent_prediction_outcome

    class _Rec:
        id = 1
        user_id = None
        owner_scope = "system"
        outcome_status = "pending"
        agent_name = "a"
        stock_symbol = "300750"
        prediction_date = "2026-11-01"
        horizon_days = 1

    class _DB:
        def query(self, *_a, **_k):
            class _Q:
                def filter(self, *_a, **_k):
                    return self
                def first(self):
                    return _Rec()
            return _Q()
        def commit(self):
            raise AssertionError("不得提交")
        def rollback(self):
            pass

    class _Health:
        blocked = False

    assert mark_agent_prediction_outcome(
        record_id=1, outcome_price=10.0, outcome_return_pct=1.0,
        db=_DB(), health=_Health(),
    ) is False
