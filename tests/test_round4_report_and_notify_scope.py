"""四审回归(2026-10-29): 报告/推送跨租户、误删误发与数值类型。

这一轮修的都是"上一轮修复的残留"或"修复引入的新回归"，每条都能用真实链路复现:

R1 共享报告假共享: `user_id=None` 仍采共享桶(历史某人的成本), meta 却按
   `bool(user_id)` 写 `contains_private_holdings=false` → member 可读。
   现在共享报告纯市场(不采任何持仓/自选), 标记按**实际内容**判定, 读侧再加内容绊线。
R2 订阅推送: `results[-1]` 的正文推给所有未投递订阅者(真实跨租户推送)。
R3 通知归属: agents.py 异步任务通知缺 user_id; report_scheduler 通知缺 user_id;
   `notify_task_done` 默认兜底 owner。
R4 渠道: 解析层给了选择集, 但 notify_center 最终外发只看 user_id → 全渠道超发。
R5 删自选: 按 stock_id 无条件删 PriceAlertRule/StockAgent → 删掉别人的提醒。
R6 情景锚定: 已清仓(qty=0)/缺数量被当权重 1; 不同市场/币种混算。
   decision 数量被转成 float 后丢失; 缓存 NaN 导致响应不是合法 JSON。
R7 agent_runs 全为 NULL user_id + 可猜 trace_id → 任何 member 能读最后一份报告正文。
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import src.core.report_generator as rg
import src.core.report_scheduler as rs
import src.core.scheduler as sched_mod
import src.core.scenario_compose as sc
from src.core import decision_cache, notify_center, report_scope
from src.core.agent_runs import record_agent_run
from src.core.decision import personalize
from src.db.models import (
    Account,
    AgentRun,
    Notification,
    NotifyChannel,
    Position,
    PriceAlertRule,
    ReportSubscription,
    Stock,
    User,
)
from src.web.database import SessionLocal, init_db


PREFIX = "r4"


@pytest.fixture(scope="module", autouse=True)
def _db_ready():
    init_db()


@pytest.fixture()
def db():
    s = SessionLocal()
    tag = uuid.uuid4().hex[:6]
    created = {"users": [], "stocks": [], "accounts": [], "positions": [],
               "channels": [], "notifications": [], "runs": [], "rules": []}

    def user(role="member", name="u"):
        u = User(
            id=str(uuid.uuid4()),
            username=f"{PREFIX}_{name}_{tag}_{uuid.uuid4().hex[:4]}",
            password_hash="x",
            role=role,
            is_active=True,
            token_version=1,
        )
        s.add(u)
        s.commit()
        created["users"].append(u.id)
        return u

    def stock(uid=None, symbol=None, market="CN", name=None):
        st = Stock(
            symbol=symbol or f"S{uuid.uuid4().hex[:8].upper()}",
            name=name or "测试票",
            market=market,
            user_id=uid,
        )
        s.add(st)
        s.commit()
        created["stocks"].append(st.id)
        return st

    def account(uid=None, name=None):
        a = Account(name=name or f"acc-{uuid.uuid4().hex[:6]}", available_funds=0.0,
                    enabled=True, user_id=uid)
        s.add(a)
        s.commit()
        created["accounts"].append(a.id)
        return a

    def position(acc, st, cost=10.0, qty=100, uid=None):
        p = Position(account_id=acc.id, stock_id=st.id, cost_price=cost,
                     quantity=qty, user_id=uid)
        s.add(p)
        s.commit()
        created["positions"].append(p.id)
        return p

    ctx = SimpleNamespace(s=s, tag=tag, created=created, user=user, stock=stock,
                          account=account, position=position)
    try:
        yield ctx
    finally:
        s.rollback()
        if created["rules"]:
            s.query(PriceAlertRule).filter(PriceAlertRule.id.in_(created["rules"])).delete(
                synchronize_session=False
            )
        s.query(PriceAlertRule).filter(PriceAlertRule.stock_id.in_(created["stocks"])).delete(
            synchronize_session=False
        )
        s.query(Position).filter(Position.id.in_(created["positions"])).delete(
            synchronize_session=False
        )
        s.query(Account).filter(Account.id.in_(created["accounts"])).delete(
            synchronize_session=False
        )
        s.query(Stock).filter(Stock.id.in_(created["stocks"])).delete(synchronize_session=False)
        if created["channels"]:
            s.query(NotifyChannel).filter(NotifyChannel.id.in_(created["channels"])).delete(
                synchronize_session=False
            )
        if created["users"]:
            s.query(Notification).filter(Notification.user_id.in_(created["users"])).delete(
                synchronize_session=False
            )
            s.query(AgentRun).filter(AgentRun.user_id.in_(created["users"])).delete(
                synchronize_session=False
            )
            s.query(ReportSubscription).filter(
                ReportSubscription.user_id.in_(created["users"])
            ).delete(synchronize_session=False)
        if created["runs"]:
            s.query(AgentRun).filter(AgentRun.id.in_(created["runs"])).delete(
                synchronize_session=False
            )
        from tests.conftest import purge_users

        s.commit()
        purge_users(s, ids=created["users"])
        s.commit()
        s.close()


def _token(u: User) -> dict:
    from src.web.api.auth import create_token

    tok, _ = create_token(u)
    return {"Authorization": f"Bearer {tok}"}


# ─────────────────────── R1 共享报告: 纯市场 + 内容判定 ───────────────────────


def test_shared_report_is_market_only_even_with_legacy_shared_position(db, tmp_path, monkeypatch):
    """历史共享桶(user_id NULL)里有成本时, 共享报告也不得采集它。"""
    legacy_acc = db.account(None)
    legacy_st = db.stock(None, symbol=f"LEG{uuid.uuid4().hex[:5].upper()}")
    legacy_pos = db.position(legacy_acc, legacy_st, cost=12.34, qty=500, uid=None)
    home = tmp_path / "home"
    monkeypatch.setattr(rg, "_report_root", lambda: home / "cron" / "output")
    monkeypatch.setattr(rg, "_collect_indices", _async(lambda *a, **k: []))
    monkeypatch.setattr(rg, "_collect_market_flow", _async(lambda *a, **k: {"total_main_flow": 1.0}))
    monkeypatch.setattr(rg, "_collect_limit_up_summary", _async(lambda *a, **k: {"limit_up_count": 10, "max_streak": 2}))
    monkeypatch.setattr(rg, "_collect_limit_down", _async(lambda *a, **k: []))
    monkeypatch.setattr(rg, "_collect_strategy_signals", _async(lambda *a, **k: []))
    monkeypatch.setattr(rg, "_llm_generate", _async(lambda *a, **k: None))

    out = asyncio.run(rg.generate_market_report("postmarket", db=db.s, user_id=None))
    body = Path(out["path"]).read_text(encoding="utf-8")
    meta = report_scope.read_report_meta(Path(out["path"]))

    assert legacy_st.symbol not in body, "共享报告不得出现共享桶持仓"
    assert "12.34" not in body
    assert meta["scope"] == "shared"
    assert meta["contains_private_holdings"] is False
    assert out["contains_private_holdings"] is False
    # 成员的报告中心不会因为共享报告而永远为空: 它是全员可读的市场报告。
    assert report_scope.report_visible(meta, user_id="any-member", role="member") is True
    # 但共享报告确实没有私有持仓(legacy 仍在库里, 只是不进共享报告)。
    assert db.s.query(Position).filter(Position.id == legacy_pos.id).first() is not None

    # 真实读链: member 能从报告中心读到这份共享市场报告(成员不会永久空白)。
    import src.web.api.reports as reports_api

    monkeypatch.setattr(reports_api, "CRON_OUTPUT_DIR", home / "cron" / "output")
    reports_api._clear_cache()
    member = db.user("member", "sharedread")
    got = reports_api._report_content_for(
        job_id="postmarket-review",
        file=Path(out["path"]).name,
        viewer=(member.id, "member"),
    )
    assert got["file"] == Path(out["path"]).name
    assert "持仓" in got["content"] and legacy_st.symbol not in got["content"]


def test_private_report_still_contains_own_positions_only(db, tmp_path, monkeypatch):
    owner_u = db.user("member", "priv")
    acc = db.account(owner_u.id)
    st = db.stock(None, symbol=f"P{uuid.uuid4().hex[:5].upper()}")
    db.position(acc, st, cost=20.0, qty=100, uid=owner_u.id)
    other = db.user("member", "other")
    other_acc = db.account(other.id)
    other_st = db.stock(None, symbol=f"Q{uuid.uuid4().hex[:5].upper()}")
    db.position(other_acc, other_st, cost=99.0, qty=100, uid=other.id)

    monkeypatch.setattr(rg, "_report_root", lambda: tmp_path / "cron" / "output")
    monkeypatch.setattr(rg, "_collect_indices", _async(lambda *a, **k: []))
    monkeypatch.setattr(rg, "_collect_market_flow", _async(lambda *a, **k: {"total_main_flow": 1.0}))
    monkeypatch.setattr(rg, "_collect_limit_up_summary", _async(lambda *a, **k: {"limit_up_count": 10, "max_streak": 2}))
    monkeypatch.setattr(rg, "_collect_limit_down", _async(lambda *a, **k: []))
    monkeypatch.setattr(rg, "_collect_strategy_signals", _async(lambda *a, **k: []))
    monkeypatch.setattr(rg, "_llm_generate", _async(lambda *a, **k: None))
    monkeypatch.setattr(
        "src.core.marketdata_client.md_quote_rows", lambda *a, **k: [], raising=False
    )

    out = asyncio.run(rg.generate_market_report("postmarket", db=db.s, user_id=owner_u.id))
    body = Path(out["path"]).read_text(encoding="utf-8")
    meta = report_scope.read_report_meta(Path(out["path"]))
    assert st.symbol in body and "20.0" in body.replace("20.00", "20.0")
    assert other_st.symbol not in body and "99.0" not in body
    assert meta["scope"] == "private" and meta["owner_user_id"] == owner_u.id
    assert meta["contains_private_holdings"] is True
    assert report_scope.report_visible(meta, user_id=other.id, role="member") is False


def _async(fn):
    async def _inner(*a, **k):
        return fn(*a, **k)
    return _inner


def test_shared_report_from_legacy_writer_is_blocked_by_content_tripwire(db, tmp_path, monkeypatch):
    """meta 说 shared/不含私有, 正文却有持仓行 → member 读不到(内容判定, 不假推断)。"""
    import src.web.api.reports as reports

    root = tmp_path / "cron" / "output"
    (root / "premarket-daily").mkdir(parents=True)
    monkeypatch.setattr(reports, "CRON_OUTPUT_DIR", root)
    reports._clear_cache()
    f = root / "premarket-daily" / "legacy_shared.md"
    f.write_text("# 盘前\n\n- 贵州茅台(600519) 500股 成本 1680.5\n", encoding="utf-8")
    report_scope.write_report_meta(f, scope="shared", contains_private_holdings=False)

    member = db.user("member", "read")
    owner = db.user("owner", "read")
    from src.web.app import app

    client = TestClient(app)
    r = client.get("/api/reports/content",
                   params={"job_id": "premarket-daily", "file": "legacy_shared.md"},
                   headers=_token(member))
    assert r.status_code == 404 and "1680.5" not in r.text
    assert "legacy_shared.md" not in {
        i["file"] for i in client.get("/api/reports/list", headers=_token(member)).json()["data"]["items"]
    }
    r_owner = client.get("/api/reports/content",
                         params={"job_id": "premarket-daily", "file": "legacy_shared.md"},
                         headers=_token(owner))
    assert r_owner.status_code == 200


def test_notification_detail_does_not_attach_foreign_run_result(db):
    """通知详情按 trace 关联运行时必须按归属过滤: 否则通知主人能读到别人的正文。"""
    member = db.user("member", "notif")
    trace = f"sch-x-{uuid.uuid4().hex[:8]}"
    record_agent_run(agent_name="afterhours_review", status="success",
                     result="OTHER-USER-SECRET", trace_id=trace, user_id=None)
    db.s.commit()
    run = db.s.query(AgentRun).filter(AgentRun.trace_id == trace).order_by(AgentRun.id.desc()).first()
    db.created["runs"].append(run.id)
    n = Notification(user_id=member.id, category="report", level="info",
                     title="报告已生成", body="x", trace_id=trace, push_status="skipped")
    db.s.add(n)
    db.s.commit()
    db.created["notifications"].append(n.id)

    from src.web.app import app

    client = TestClient(app)
    r = client.get(f"/api/notifications/{n.id}", headers=_token(member))
    assert r.status_code == 200
    assert (r.json()["data"].get("task") or {}) == {} or r.json()["data"]["task"] is None
    assert "OTHER-USER-SECRET" not in r.text


# ─────────────────────── R2 订阅推送按接收用户匹配 ───────────────────────


def _ctx(user_id, name="u"):
    return SimpleNamespace(user=SimpleNamespace(id=user_id, username=name), watchlist=[])


def _res(content, title="复盘"):
    return SimpleNamespace(content=content, notify_content=None, title=title, raw_data={})


def test_subscriber_push_never_reuses_last_bucket_content(db, monkeypatch):
    """u1 有执行桶、u2 只有订阅: u2 不得收到 u1 的正文(修前正是 results[-1])。"""
    u1 = db.user("member", "sub1")
    u2 = db.user("member", "sub2")
    db.s.add(ReportSubscription(user_id=u1.id, report_type="review", enabled=True))
    db.s.add(ReportSubscription(user_id=u2.id, report_type="review", enabled=True))
    db.s.commit()
    db.created["users"] += [u1.id, u2.id]

    pushed: list[dict] = []
    monkeypatch.setattr(
        notify_center, "push_notification",
        lambda title, body="", **kw: pushed.append({"title": title, "body": body, **kw}) or 1,
    )
    # 渠道解析层: 只给测试用空集合(通知中心会据此不外发, 但推送调用本身可观测)。
    import src.bootstrap.runtime as runtime

    monkeypatch.setattr(
        runtime, "resolve_notify_channels_ex",
        lambda agent, user_id=None, **kw: SimpleNamespace(channels=[], reason="test-none"),
    )

    s = sched_mod.AgentScheduler()
    s._push_to_subscribers(
        "afterhours_review",
        [(_ctx(u1.id), _res("U1 私有持仓 500股@12.34"))],
        set(),
    )
    recipients = [c.get("user_id") for c in pushed]
    assert recipients == [u1.id], f"只应推给有本轮内容的 u1, 实际 {recipients}"
    assert all("U1 私有持仓" in c["body"] for c in pushed)
    assert not any(c.get("user_id") == u2.id for c in pushed)


def test_subscriber_push_matches_each_users_own_bucket(db, monkeypatch):
    u1 = db.user("member", "map1")
    u2 = db.user("member", "map2")
    db.s.add_all([
        ReportSubscription(user_id=u1.id, report_type="review", enabled=True),
        ReportSubscription(user_id=u2.id, report_type="review", enabled=True),
    ])
    db.s.commit()
    db.created["users"] += [u1.id, u2.id]
    pushed: list[dict] = []
    monkeypatch.setattr(
        notify_center, "push_notification",
        lambda title, body="", **kw: pushed.append({"body": body, **kw}) or 1,
    )
    import src.bootstrap.runtime as runtime

    monkeypatch.setattr(
        runtime, "resolve_notify_channels_ex",
        lambda agent, user_id=None, **kw: SimpleNamespace(channels=[], reason="test-none"),
    )
    s = sched_mod.AgentScheduler()
    s._push_to_subscribers(
        "afterhours_review",
        [(_ctx(u1.id), _res("U1-only")), (_ctx(u2.id), _res("U2-only"))],
        set(),
    )
    by_user = {c["user_id"]: c["body"] for c in pushed}
    assert by_user == {u1.id: "U1-only", u2.id: "U2-only"}, by_user


def test_agent_run_record_drops_body_when_buckets_are_mixed():
    u1, u2 = "uid-1", "uid-2"
    text = sched_mod._agent_run_result_text(
        [(_ctx(u1), _res("秘密一")), (_ctx(u2), _res("秘密二"))]
    )
    assert "秘密" not in text
    assert "buckets=2" in text
    assert sched_mod._sole_bucket_user([_ctx(u1), _ctx(u2)]) is None
    assert sched_mod._sole_bucket_user([_ctx(u1)]) == u1
    assert sched_mod._agent_run_result_text([(_ctx(u1), _res("我的报告"))]) == "我的报告"


# ─────────────────────── R7 trace_id + NULL 归属 fail closed ───────────────────────


def test_progress_endpoint_fails_closed_for_null_user_run(db):
    """可猜 trace_id + NULL 归属的历史运行记录: member 读不到正文, owner 可复核。"""
    trace = f"sch-afterhours_review-{int(time.time() * 1000)}"
    record_agent_run(
        agent_name="afterhours_review",
        status="success",
        result="SECRET-LAST-USER-REPORT 500股@12.34",
        trace_id=trace,
        trigger_source="schedule",
        user_id=None,
    )
    db.s.commit()
    run = db.s.query(AgentRun).filter(AgentRun.trace_id == trace).order_by(AgentRun.id.desc()).first()
    db.created["runs"].append(run.id)

    member = db.user("member", "trace")
    owner = db.user("owner", "trace")
    from src.web.app import app

    client = TestClient(app)
    r = client.get(f"/api/agents/runs/{trace}/progress", headers=_token(member))
    assert r.status_code == 200
    assert r.json()["data"]["status"] == "not_found"
    assert "SECRET-LAST-USER-REPORT" not in r.text

    r_owner = client.get(f"/api/agents/runs/{trace}/progress", headers=_token(owner))
    assert r_owner.status_code == 200
    assert r_owner.json()["data"].get("run", {}).get("result", "").startswith("SECRET")

    # 本人归属的运行仍可读(不误伤正常链路)。
    trace2 = f"man-x-{uuid.uuid4().hex[:8]}"
    record_agent_run(
        agent_name="intraday_monitor", status="success", result="MINE",
        trace_id=trace2, trigger_source="manual", user_id=member.id,
    )
    db.s.commit()
    run2 = db.s.query(AgentRun).filter(AgentRun.trace_id == trace2).order_by(AgentRun.id.desc()).first()
    db.created["runs"].append(run2.id)
    r_self = client.get(f"/api/agents/runs/{trace2}/progress", headers=_token(member))
    assert r_self.json()["data"].get("run", {}).get("result") == "MINE"


# ─────────────────────── R5 删自选不删他人资源 ───────────────────────


def test_delete_shared_stock_never_deletes_members_alert_rule(db):
    owner = db.user("owner", "del")
    member = db.user("member", "del")
    st = db.stock(None, symbol=f"SH{uuid.uuid4().hex[:5].upper()}")
    rule = PriceAlertRule(user_id=member.id, stock_id=st.id, name="member rule",
                          condition_group={"conditions": []}, enabled=True)
    db.s.add(rule)
    db.s.commit()
    db.created["rules"].append(rule.id)

    from src.web.app import app

    client = TestClient(app)
    r = client.delete(f"/api/stocks/{st.id}", headers=_token(owner))
    assert r.status_code == 409, r.text
    assert db.s.query(PriceAlertRule).filter(PriceAlertRule.id == rule.id).first() is not None
    assert db.s.query(Stock).filter(Stock.id == st.id).first() is not None


def test_delete_own_private_stock_still_removes_own_rule(db):
    member = db.user("member", "delown")
    st = db.stock(member.id, symbol=f"PR{uuid.uuid4().hex[:5].upper()}")
    rule = PriceAlertRule(user_id=member.id, stock_id=st.id, name="own rule",
                          condition_group={"conditions": []}, enabled=True)
    db.s.add(rule)
    db.s.commit()
    db.created["rules"].append(rule.id)

    from src.web.app import app

    client = TestClient(app)
    r = client.delete(f"/api/stocks/{st.id}", headers=_token(member))
    assert r.status_code == 200, r.text
    assert db.s.query(PriceAlertRule).filter(PriceAlertRule.id == rule.id).first() is None


def test_member_cannot_delete_another_users_private_stock(db):
    a = db.user("member", "da")
    b = db.user("member", "db")
    st = db.stock(b.id, symbol=f"OT{uuid.uuid4().hex[:5].upper()}")
    from src.web.app import app

    client = TestClient(app)
    r = client.delete(f"/api/stocks/{st.id}", headers=_token(a))
    assert r.status_code == 403
    assert db.s.query(Stock).filter(Stock.id == st.id).first() is not None


# ─────────────────────── R6 情景成本锚定(真实库) ───────────────────────


def test_cost_anchor_ignores_closed_and_missing_quantity_and_other_market(db):
    u = db.user("member", "anchor")
    acc = db.account(u.id)
    sym = f"A{uuid.uuid4().hex[:5].upper()}"
    cn = db.stock(None, symbol=sym, market="CN")
    hk = db.stock(None, symbol=sym, market="HK")
    acc2 = db.account(u.id)
    acc3 = db.account(u.id)
    db.position(acc, cn, cost=10.0, qty=0, uid=u.id)     # 已清仓(另一账户)
    db.position(acc2, cn, cost=20.0, qty=100, uid=u.id)  # 有效正持仓
    db.position(acc3, hk, cost=100.0, qty=1000, uid=u.id)  # 同 symbol 另一市场/币种

    detail = sc.owner_cost_detail(db.s, u, sym, "CN")
    assert detail.cost == 20.0, f"不能把已清仓成本/港币成本混进来(实际 {detail})"
    assert detail.method == "single"
    assert all(a["cost"] != 10.0 and a["cost"] != 100.0 for a in detail.accounts)

    hk_detail = sc.owner_cost_detail(db.s, u, sym, "HK")
    assert hk_detail.cost == 100.0


def test_cost_anchor_without_quantity_is_unknown_not_weight_one(db):
    u = db.user("member", "anchor2")
    acc = db.account(u.id)
    st = db.stock(None, symbol=f"N{uuid.uuid4().hex[:5].upper()}", market="CN")
    db.position(acc, st, cost=15.0, qty=0, uid=u.id)
    detail = sc.owner_cost_detail(db.s, u, st.symbol, "CN")
    assert detail.cost is None and detail.method == "none"


# ─────────────────────── R6 决策数量/NaN ───────────────────────


def test_decision_quantity_keeps_integer_type(db):
    u = db.user("member", "dec")
    acc = db.account(u.id)
    st = db.stock(None, symbol=f"D{uuid.uuid4().hex[:5].upper()}", market="CN")
    db.position(acc, st, cost=12.0, qty=300, uid=u.id)
    from src.web.api.decision import _build_user_context

    ctx = _build_user_context(db.s, u, st.symbol, "CN")
    assert ctx["quantity"] == 300 and isinstance(ctx["quantity"], int)


def test_personalize_normalizes_float_quantity_and_drops_nan_cost():
    out = {
        "verdict": "看看", "row": 3, "phase": "分歧", "basis": "two",
        "personalization_notes": [],
    }
    got = personalize(dict(out), {"holds": True, "cost_price": 10.0,
                                  "quantity": 100.0, "last_close": 11.0})
    assert got["position"]["quantity"] == 100
    assert got["position"]["pnl_pct"] == 10.0

    got2 = personalize(dict(out), {"holds": True, "cost_price": float("nan"),
                                   "quantity": float("nan"), "last_close": 11.0})
    assert got2["position"]["cost_price"] is None
    assert got2["position"]["quantity"] is None
    assert got2["position"]["pnl_pct"] is None
    json.dumps(got2, allow_nan=False)


def test_decision_cache_payload_never_stores_nan(db):
    sym = f"NAN{uuid.uuid4().hex[:6].upper()}"
    written = decision_cache.put_cached_decision(
        sym, "CN", {"last_close": 10.0, "levels": [1.0, float("nan")], "verdict": "看看"}
    )
    assert written is not None
    try:
        rec = decision_cache.get_cached_decision(sym, "CN")
        assert rec is not None
        json.dumps(rec["payload"], allow_nan=False)
        assert rec["payload"]["levels"][1] is None
    finally:
        decision_cache.clear_decision_cache(sym, "CN")


# ─────────────────────── R4 最终外发交叉校验 ───────────────────────


def test_push_layer_honors_selection_set_and_refuses_empty(db):
    u = db.user("member", "ch")
    ch1 = NotifyChannel(user_id=u.id, name="ch1", type="webhook", config={"url": "http://127.0.0.1:9/x"}, enabled=True)
    ch2 = NotifyChannel(user_id=u.id, name="ch2", type="webhook", config={"url": "http://127.0.0.1:9/y"}, enabled=True)
    foreign = db.user("member", "chf")
    ch3 = NotifyChannel(user_id=foreign.id, name="foreign", type="webhook", config={"url": "http://127.0.0.1:9/z"}, enabled=True)
    db.s.add_all([ch1, ch2, ch3])
    db.s.commit()
    db.created["channels"] += [ch1.id, ch2.id, ch3.id]

    mgr, records = notify_center._build_notifier(user_id=u.id, channel_ids=[ch1.id])
    assert [r["id"] for r in records] == [ch1.id], "选择集必须收窄, 不能扩到该用户全部渠道"

    mgr2, records2 = notify_center._build_notifier(user_id=u.id, channel_ids=[])
    assert mgr2 is None and records2 == [], "空选择集 = 一条都不发, 不许退化"

    # 他人的渠道 id 进选择集 → 被剔除, 不会借他人渠道外发。
    mgr3, records3 = notify_center._build_notifier(user_id=u.id, channel_ids=[ch3.id])
    assert mgr3 is None and records3 == []

    nid = notify_center.push_notification(
        "t", "b", category="report", level="info", user_id=u.id, channel_ids=[]
    )
    assert nid is not None
    db.s.expire_all()
    n = db.s.query(Notification).filter(Notification.id == nid).first()
    assert n.push_status == "skipped" and "选择集" in (n.push_error or "")


def test_notify_task_done_does_not_fall_back_to_owner_by_default(db, monkeypatch):
    owner = db.user("owner", "fallback")
    captured = {}
    monkeypatch.setattr(
        notify_center, "push_notification",
        lambda title, body="", **kw: captured.update(kw, title=title) or 1,
    )
    notify_center.notify_task_done("某任务", ok=True, detail="x")
    assert "user_id" in captured and captured.get("user_id") is None, "未知归属不得兜底推 owner"
    notify_center.notify_task_done("系统任务", ok=True, detail="x", allow_owner_fallback=True)
    resolved = notify_center._resolve_owner_user_id()
    assert resolved is not None, "本库应存在 owner"
    assert captured.get("user_id") == resolved, "显式 opt-in 的系统告警才兜底 owner"
    assert owner.id  # 测试数据清理用


# ─────────────────────── R3 异步任务通知捕获触发者 ───────────────────────


def test_spawn_async_run_passes_triggering_user_to_notify(monkeypatch):
    from src.web.api import agents as agents_api

    captured = {}

    def fake_notify(label, **kw):
        captured.update(kw, label=label)
        return 1

    monkeypatch.setattr("src.core.notify_center.notify_task_done", fake_notify)

    async def noop():
        return None

    agents_api._spawn_async_run(
        noop, name="t-r4", notify_label="盘前", notify_user_id="user-42",
        notify_channel_ids=[11, 12],
    )
    for _ in range(100):
        if captured:
            break
        time.sleep(0.02)
    assert captured.get("user_id") == "user-42"
    assert captured.get("channel_ids") == [11, 12], "选择集要一路带到最终外发"


def test_report_scheduler_notifies_with_explicit_owner(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        notify_center, "push_notification",
        lambda title, body="", **kw: captured.update(kw) or 1,
    )
    ok = rs._push_report_notification(
        "premarket", {"title": "# t", "path": "/x/y.md", "size": 1}, user_id="owner-1"
    )
    assert ok is True and captured.get("user_id") == "owner-1"
