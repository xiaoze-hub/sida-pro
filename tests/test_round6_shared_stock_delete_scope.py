"""六审回归(2026-10-29): 共享自选删除的跨账号副作用 + health 报错归属。

五审为了"不 dead-loop"引入了自动拆离, 实测有三个跨账号副作用:

1. 给他人复制私有 Stock + StockAgent 绑定 → 调度器随后真的跑别人的 Agent
   (LLM 调用 + 按绑定渠道外发), 用户没同意;
2. 只迁 `rule.stock_id`, 没迁 `price_alert_hits.stock_id` → 删原行时
   `ON DELETE CASCADE` 把他人的命中历史一起删掉;
3. NULL 归属规则被 `~own_filter` 的三值逻辑滤掉 → 不算他人的也不算本人的,
   最后被数据库级联删掉, 历史同样丢失。

六审口径: 不能证明"迁移覆盖全部关联 + 用户授权"时**禁止静默自动迁移**;
回通用 409, 共享票/规则/绑定/所有历史原样保留, 文案不报姓名/数量。
用户自己的提醒可用显式 `POST /api/price-alerts/{id}/adopt` 迁到私有自选
(不复制 Agent、不触发 LLM/外发), 迁空后共享行才可删。

另外 `GET /api/agents/health` 的 `last_run` 曾展示全库最近一次 AgentRun,
member 能读到别人的 error/result 正文 —— 现在 member 只看本人, NULL 不猜。
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

import src.core.report_generator as rg
from src.core.portfolio_scope import count_unattributed_positions
from src.db.models import (
    Account,
    AgentConfig,
    AgentRun,
    Notification,
    Position,
    PriceAlertHit,
    PriceAlertRule,
    Stock,
    StockAgent,
    User,
)
from src.web.database import SessionLocal, init_db

PREFIX = "r6"


@pytest.fixture(scope="module", autouse=True)
def _db_ready():
    init_db()


@pytest.fixture()
def db():
    s = SessionLocal()
    tag = uuid.uuid4().hex[:6]
    created = {
        "users": [], "stocks": [], "accounts": [], "positions": [],
        "rules": [], "hits": [], "agents": [], "configs": [], "runs": [],
    }

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

    def rule(st, uid, name="r"):
        r = PriceAlertRule(user_id=uid, stock_id=st.id, name=name,
                           condition_group={"conditions": []}, enabled=True)
        s.add(r)
        s.commit()
        created["rules"].append(r.id)
        return r

    def hit(r, st, bucket=None):
        h = PriceAlertHit(rule_id=r.id, stock_id=st.id,
                          trigger_bucket=bucket or uuid.uuid4().hex[:12])
        s.add(h)
        s.commit()
        created["hits"].append(h.id)
        return h

    def agent(st, name=None):
        a = StockAgent(stock_id=st.id, agent_name=name or f"wf_{uuid.uuid4().hex[:6]}")
        s.add(a)
        s.commit()
        created["agents"].append(a.id)
        return a

    def config(name=None):
        c = AgentConfig(name=name or f"{PREFIX}_cfg_{uuid.uuid4().hex[:8]}",
                        display_name="六审测试", kind="workflow", visible=True,
                        enabled=True, schedule="")
        s.add(c)
        s.commit()
        created["configs"].append(c.id)
        return c

    def run(uid, agent_name, status="success", error="", result=""):
        r = AgentRun(user_id=uid, agent_name=agent_name, status=status,
                     error=error, result=result, trace_id=uuid.uuid4().hex)
        s.add(r)
        s.commit()
        created["runs"].append(r.id)
        return r

    from types import SimpleNamespace

    ctx = SimpleNamespace(s=s, tag=tag, created=created, user=user, stock=stock,
                          account=account, position=position, rule=rule, hit=hit,
                          agent=agent, config=config, run=run)
    try:
        yield ctx
    finally:
        s.rollback()
        if created["hits"]:
            s.query(PriceAlertHit).filter(PriceAlertHit.id.in_(created["hits"])).delete(
                synchronize_session=False
            )
        if created["rules"]:
            s.query(PriceAlertRule).filter(PriceAlertRule.id.in_(created["rules"])).delete(
                synchronize_session=False
            )
        if created["agents"]:
            s.query(StockAgent).filter(StockAgent.id.in_(created["agents"])).delete(
                synchronize_session=False
            )
        # 端点可能自行创建/删除行; 再按测试 stock 兜底清一次。
        s.query(PriceAlertHit).filter(
            PriceAlertHit.stock_id.in_(created["stocks"])
        ).delete(synchronize_session=False)
        s.query(PriceAlertRule).filter(
            PriceAlertRule.stock_id.in_(created["stocks"])
        ).delete(synchronize_session=False)
        s.query(StockAgent).filter(
            StockAgent.stock_id.in_(created["stocks"])
        ).delete(synchronize_session=False)
        if created["positions"]:
            s.query(Position).filter(Position.id.in_(created["positions"])).delete(
                synchronize_session=False
            )
        if created["accounts"]:
            s.query(Account).filter(Account.id.in_(created["accounts"])).delete(
                synchronize_session=False
            )
        s.query(Stock).filter(Stock.id.in_(created["stocks"])).delete(
            synchronize_session=False
        )
        # 迁移端点给 member 建的私有自选(可能不在 created["stocks"] 里)。
        if created["users"]:
            own_stock_ids = [
                row[0]
                for row in s.query(Stock.id)
                .filter(Stock.user_id.in_(created["users"]))
                .all()
            ]
            if own_stock_ids:
                s.query(PriceAlertHit).filter(
                    PriceAlertHit.stock_id.in_(own_stock_ids)
                ).delete(synchronize_session=False)
                s.query(PriceAlertRule).filter(
                    PriceAlertRule.stock_id.in_(own_stock_ids)
                ).delete(synchronize_session=False)
                s.query(StockAgent).filter(
                    StockAgent.stock_id.in_(own_stock_ids)
                ).delete(synchronize_session=False)
                s.query(Stock).filter(Stock.id.in_(own_stock_ids)).delete(
                    synchronize_session=False
                )
            s.query(AgentRun).filter(AgentRun.user_id.in_(created["users"])).delete(
                synchronize_session=False
            )
        if created["configs"]:
            s.query(AgentConfig).filter(AgentConfig.id.in_(created["configs"])).delete(
                synchronize_session=False
            )
        if created["runs"]:
            s.query(AgentRun).filter(AgentRun.id.in_(created["runs"])).delete(
                synchronize_session=False
            )
        from tests.conftest import purge_users

        s.commit()
        purge_users(s, ids=created["users"])
        s.commit()
        s.close()


def _hdr(u: User) -> dict:
    from src.web.api.auth import create_token

    tok, _ = create_token(u)
    return {"Authorization": f"Bearer {tok}"}


def _detail(resp) -> str:
    payload = resp.json()
    return str(payload.get("message") or payload.get("detail") or "")


def _assert_generic(detail: str, *secrets: str) -> None:
    assert "引用" in detail
    assert "detached_users" not in detail
    for secret in secrets:
        if secret:
            assert secret not in detail


# ───────────── 1. 共享票上他人规则: 通用 409, 全量保留 ─────────────


def test_shared_stock_delete_refused_preserving_rules_hits_agents(db):
    owner = db.user("owner", "o")
    member = db.user("member", "m")
    st = db.stock(None, symbol=f"SH{uuid.uuid4().hex[:6].upper()}")
    r = db.rule(st, member.id, "member rule")
    h = db.hit(r, st)
    a = db.agent(st)

    client = TestClient(app())
    resp = client.delete(f"/api/stocks/{st.id}", headers=_hdr(owner))
    assert resp.status_code == 409, resp.text
    _assert_generic(_detail(resp), member.id, member.username)

    db.s.expire_all()
    assert db.s.query(Stock).filter(Stock.id == st.id).first() is not None
    kept = db.s.query(PriceAlertRule).filter(PriceAlertRule.id == r.id).first()
    assert kept is not None and kept.stock_id == st.id
    assert db.s.query(PriceAlertHit).filter(PriceAlertHit.id == h.id).first() is not None
    assert db.s.query(StockAgent).filter(StockAgent.id == a.id).first() is not None
    # 不替 member 建私有自选。
    assert db.s.query(Stock).filter(
        Stock.user_id == member.id, Stock.symbol == st.symbol
    ).count() == 0
    # 拒绝删除是纯读路径: 不给任何人新增通知(更不会外发)。
    assert db.s.query(Notification).filter(
        Notification.user_id.in_([owner.id, member.id])
    ).count() == 0


def app():
    from src.web.app import app as _app

    return _app


# ───────────── 2. NULL 归属规则: owner 也不能删(不猜归属, 不丢历史) ─────────────


def test_shared_stock_with_null_rule_refused_for_owner(db):
    owner = db.user("owner", "o")
    st = db.stock(None, symbol=f"SN{uuid.uuid4().hex[:6].upper()}")
    r = db.rule(st, None, "legacy rule")
    h = db.hit(r, st)

    client = TestClient(app())
    resp = client.delete(f"/api/stocks/{st.id}", headers=_hdr(owner))
    assert resp.status_code == 409, resp.text
    _assert_generic(_detail(resp))

    db.s.expire_all()
    assert db.s.query(Stock).filter(Stock.id == st.id).first() is not None
    assert db.s.query(PriceAlertRule).filter(PriceAlertRule.id == r.id).first() is not None
    assert db.s.query(PriceAlertHit).filter(PriceAlertHit.id == h.id).first() is not None


def test_private_stock_with_null_rule_refused_for_member(db):
    """自己私有 stock 上的 NULL 历史规则同样不猜、不删, 保住连带历史。"""
    member = db.user("member", "m")
    st = db.stock(member.id, symbol=f"PN{uuid.uuid4().hex[:6].upper()}")
    r = db.rule(st, None, "legacy on private")
    h = db.hit(r, st)

    client = TestClient(app())
    resp = client.delete(f"/api/stocks/{st.id}", headers=_hdr(member))
    assert resp.status_code == 409, resp.text
    db.s.expire_all()
    assert db.s.query(Stock).filter(Stock.id == st.id).first() is not None
    assert db.s.query(PriceAlertRule).filter(PriceAlertRule.id == r.id).first() is not None
    assert db.s.query(PriceAlertHit).filter(PriceAlertHit.id == h.id).first() is not None


def test_foreign_hit_without_foreign_rule_on_stock_refused(db):
    """命中记录指向他人规则(错位数据)时也不能删: 命中历史先于自选存活。"""
    owner = db.user("owner", "o")
    member = db.user("member", "m")
    other_stock = db.stock(member.id, symbol=f"OH{uuid.uuid4().hex[:6].upper()}")
    target = db.stock(None, symbol=f"TG{uuid.uuid4().hex[:6].upper()}")
    r = db.rule(other_stock, member.id, "foreign rule")
    h = db.hit(r, target)  # rule 不在 target 上, 但命中挂在 target

    client = TestClient(app())
    resp = client.delete(f"/api/stocks/{target.id}", headers=_hdr(owner))
    assert resp.status_code == 409, resp.text
    db.s.expire_all()
    assert db.s.query(Stock).filter(Stock.id == target.id).first() is not None
    assert db.s.query(PriceAlertHit).filter(PriceAlertHit.id == h.id).first() is not None


# ───────────── 3. 本人私有记录: 正常删除(不误伤) ─────────────


def test_own_private_stock_with_own_rule_hit_agent_deletes(db):
    member = db.user("member", "m")
    st = db.stock(member.id, symbol=f"PV{uuid.uuid4().hex[:6].upper()}")
    stock_id = st.id
    r = db.rule(st, member.id, "own rule")
    rule_id = r.id
    h = db.hit(r, st)
    hit_id = h.id
    a = db.agent(st)
    agent_id = a.id

    client = TestClient(app())
    resp = client.delete(f"/api/stocks/{stock_id}", headers=_hdr(member))
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"] == {"ok": True}
    db.s.expire_all()
    assert db.s.query(Stock).filter(Stock.id == stock_id).first() is None
    assert db.s.query(PriceAlertRule).filter(PriceAlertRule.id == rule_id).first() is None
    assert db.s.query(PriceAlertHit).filter(PriceAlertHit.id == hit_id).first() is None
    assert db.s.query(StockAgent).filter(StockAgent.id == agent_id).first() is None


def test_member_cannot_delete_own_stock_with_other_users_rule(db):
    member = db.user("member", "m")
    other = db.user("member", "x")
    st = db.stock(member.id, symbol=f"PX{uuid.uuid4().hex[:6].upper()}")
    r = db.rule(st, other.id, "other rule")
    assert db.s.query(PriceAlertRule).filter(PriceAlertRule.id == r.id).count() == 1
    from src.web.app import app as _app

    client = TestClient(_app)
    resp = client.delete(f"/api/stocks/{st.id}", headers=_hdr(member))
    assert resp.status_code == 409, resp.text
    db.s.expire_all()
    assert db.s.query(Stock).filter(Stock.id == st.id).first() is not None


# ───────────── 4. 显式迁出(用户可操作的安全路径) ─────────────


def test_member_adopts_own_rule_then_owner_can_delete_shared_stock(db):
    owner = db.user("owner", "o")
    member = db.user("member", "m")
    st = db.stock(None, symbol=f"AD{uuid.uuid4().hex[:6].upper()}")
    shared_id = st.id
    shared_symbol = st.symbol
    r = db.rule(st, member.id, "member rule")
    rule_id = r.id
    h = db.hit(r, st)
    hit_id = h.id
    shared_agent = db.agent(st)
    shared_agent_id = shared_agent.id

    client = TestClient(app())
    # 迁移前: owner 删不动。
    assert client.delete(f"/api/stocks/{st.id}", headers=_hdr(owner)).status_code == 409

    # member 自己显式迁移(带同意): 只迁规则 + 命中, 不复制 Agent。
    adv = client.post(f"/api/price-alerts/{r.id}/adopt", headers=_hdr(member))
    assert adv.status_code == 200, adv.text
    target_id = adv.json()["data"]["stock_id"]
    db.created["stocks"].append(target_id)
    db.s.expire_all()
    target = db.s.query(Stock).filter(Stock.id == target_id).first()
    assert target is not None and target.user_id == member.id
    assert target.symbol == shared_symbol and target.market == "CN"
    moved = db.s.query(PriceAlertRule).filter(PriceAlertRule.id == rule_id).first()
    assert moved.stock_id == target_id
    moved_hit = db.s.query(PriceAlertHit).filter(PriceAlertHit.id == hit_id).first()
    assert moved_hit.stock_id == target_id, "命中历史必须随规则迁走, 不能在原行级联被删"
    # 没有把共享票的 Agent 绑定复制给 member(否则会替他触发 LLM/外发)。
    assert db.s.query(StockAgent).filter(StockAgent.stock_id == target_id).count() == 0
    assert db.s.query(StockAgent).filter(StockAgent.id == shared_agent_id).first() is not None

    # 迁空后 owner 可删共享行, member 的数据仍在。
    gone = client.delete(f"/api/stocks/{shared_id}", headers=_hdr(owner))
    assert gone.status_code == 200, gone.text
    db.s.expire_all()
    assert db.s.query(Stock).filter(Stock.id == shared_id).first() is None
    assert db.s.query(PriceAlertRule).filter(PriceAlertRule.id == rule_id).first() is not None
    assert db.s.query(PriceAlertHit).filter(PriceAlertHit.id == hit_id).first() is not None


def test_adopt_rejects_other_users_rule_and_private_target(db):
    a = db.user("member", "a")
    b = db.user("member", "b")
    shared = db.stock(None, symbol=f"AR{uuid.uuid4().hex[:6].upper()}")
    rule_a = db.rule(shared, a.id, "a rule")
    own_st = db.stock(b.id, symbol=f"OB{uuid.uuid4().hex[:6].upper()}")
    rule_b = db.rule(own_st, b.id, "b private rule")

    client = TestClient(app())
    r1 = client.post(f"/api/price-alerts/{rule_a.id}/adopt", headers=_hdr(b))
    assert r1.status_code == 404, r1.text
    r2 = client.post(f"/api/price-alerts/{rule_b.id}/adopt", headers=_hdr(b))
    assert r2.status_code == 400, r2.text
    db.s.expire_all()
    assert db.s.query(PriceAlertRule).filter(PriceAlertRule.id == rule_a.id).one().user_id == a.id


# ───────────── 7. 并发双删 + 故障回滚(真实 PG) ─────────────


def test_concurrent_double_delete_never_500_and_leaves_no_orphans(db):
    """两个请求同时删同一自选: 恰好一个成功, 其余 404/409, 不留规则/命中孤儿。"""
    from src.db.dialect import is_postgres

    if not is_postgres():
        pytest.skip("竞态路径需真实 PG 隔离库")

    import concurrent.futures

    from src.web.app import app as _app

    member = db.user("member", "c")
    st = db.stock(member.id, symbol=f"CC{uuid.uuid4().hex[:6].upper()}")
    stock_id = st.id
    r = db.rule(st, member.id, "concurrent")
    rule_id = r.id
    h = db.hit(r, st)
    hit_id = h.id

    def _one(_i):
        c = TestClient(_app, raise_server_exceptions=False)
        return c.delete(f"/api/stocks/{stock_id}", headers=_hdr(member)).status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        codes = list(ex.map(_one, range(4)))
    assert 500 not in codes, codes
    assert codes.count(200) == 1, codes
    assert set(codes) <= {200, 404, 409}, codes

    db.s.expire_all()
    assert db.s.query(Stock).filter(Stock.id == stock_id).first() is None
    assert db.s.query(PriceAlertRule).filter(PriceAlertRule.id == rule_id).first() is None
    assert db.s.query(PriceAlertHit).filter(PriceAlertHit.id == hit_id).first() is None


def test_delete_failure_rolls_back_partial_cleanup(db, monkeypatch):
    """清理到一半提交失败: 整单回滚, 自选/规则/命中一条都不少(不半截删除)。"""
    from sqlalchemy.orm import Session as SASession

    member = db.user("member", "f")
    st = db.stock(member.id, symbol=f"RB{uuid.uuid4().hex[:6].upper()}")
    stock_id = st.id
    r = db.rule(st, member.id, "rollback")
    rule_id = r.id
    h = db.hit(r, st)
    hit_id = h.id

    original = SASession.commit

    def _boom(self, *args, **kwargs):
        raise RuntimeError("injected-commit-failure")

    monkeypatch.setattr(SASession, "commit", _boom)
    try:
        client = TestClient(app())
        resp = client.delete(f"/api/stocks/{stock_id}", headers=_hdr(member))
    finally:
        monkeypatch.setattr(SASession, "commit", original)

    assert resp.status_code == 409, resp.text
    db.s.expire_all()
    assert db.s.query(Stock).filter(Stock.id == stock_id).first() is not None
    assert db.s.query(PriceAlertRule).filter(PriceAlertRule.id == rule_id).first() is not None
    assert db.s.query(PriceAlertHit).filter(PriceAlertHit.id == hit_id).first() is not None


def test_adopt_respects_watchlist_quota(db):
    """迁移会新建私有自选 → 与 POST /api/stocks 同配额, 不能借迁移绕过限制。"""
    member = db.user("member", "quota")
    shared = db.stock(None, symbol=f"QT{uuid.uuid4().hex[:6].upper()}")
    rule = db.rule(shared, member.id, "quota rule")
    for _ in range(12):
        db.stock(member.id, symbol=f"QF{uuid.uuid4().hex[:6].upper()}")

    client = TestClient(app())
    resp = client.post(f"/api/price-alerts/{rule.id}/adopt", headers=_hdr(member))
    assert resp.status_code == 403, resp.text
    db.s.expire_all()
    assert db.s.query(PriceAlertRule).filter(PriceAlertRule.id == rule.id).one().stock_id == shared.id


# ───────────── 8. health 报错按用户归属隔离 ─────────────


def test_health_hides_other_users_agent_error_from_member(db):
    member = db.user("member", "h")
    other = db.user("member", "x")
    owner = db.user("owner", "o")
    cfg = db.config()
    db.run(member.id, cfg.name, status="success", result="MINE-OK")
    db.run(other.id, cfg.name, status="failed",
           error="SECRET-OTHER-ERROR 持有 600股@88.88", result="SECRET-OTHER-RESULT")

    client = TestClient(app())
    r = client.get("/api/agents/health", headers=_hdr(member))
    assert r.status_code == 200, r.text
    row = next(a for a in r.json()["data"]["agents"] if a["name"] == cfg.name)
    assert row["last_run"]["status"] == "success"
    assert "SECRET" not in r.text
    # 失败数也不能把别人的失败算给 member。
    assert r.json()["data"]["summary"]["recent_failed_count"] == 0

    r_owner = client.get("/api/agents/health", headers=_hdr(owner))
    row_o = next(a for a in r_owner.json()["data"]["agents"] if a["name"] == cfg.name)
    # owner 是运维视角: 能看到全局最近一次(这里是 other 的 failed), 用于排障。
    assert "SECRET-OTHER-ERROR" in (row_o["last_run"].get("error") or "")

    # 没有本人运行的 member: last_run=None, 不拿别人的运行充数。
    other2 = db.user("member", "y")
    r2 = client.get("/api/agents/health", headers=_hdr(other2))
    row2 = next(a for a in r2.json()["data"]["agents"] if a["name"] == cfg.name)
    assert row2["last_run"] is None


# ───────────── 6. 私有报告显式提示"未计入", 共享报告不承诺不存在的入口 ─────────────


def test_count_unattributed_positions_only_user_attributable_rows(db):
    member = db.user("member", "p")
    other = db.user("member", "q")
    acc_mine = db.account(member.id)
    acc_null = db.account(None)
    acc_null2 = db.account(None)
    st = db.stock(member.id, symbol=f"UA{uuid.uuid4().hex[:6].upper()}")

    # 1) 账户是我的, 持仓 NULL → 可关联到本人 → 计入
    db.position(acc_mine, st, qty=100, uid=None)
    # 2) 持仓是我的, 账户 NULL → 计入
    db.position(acc_null, st, qty=200, uid=member.id)
    # 3) 两侧都 NULL → 不猜给任何人 → 不计入
    db.position(acc_null2, st, qty=300, uid=None)
    # 4) 他人的行 → 不计入
    acc_other = db.account(other.id)
    db.position(acc_other, st, qty=400, uid=other.id)

    assert count_unattributed_positions(db.s, member.id) == 2
    assert count_unattributed_positions(db.s, other.id) == 0


def test_private_report_shows_unattributed_notice_without_other_records(db):
    member = db.user("member", "p")
    data = {
        "generated_at": "2026-10-29 19:00:00",
        "market_date": "2026-10-29",
        "user_id": member.id,
        "positions": [],
        "unattributed_positions": 2,
        "sources": {},
    }
    text = rg._build_template_report("postmarket", data)
    assert "2 条历史归属不完整的持仓未计入" in text
    payload = rg._build_user_content("postmarket", data)
    assert "unattributed_positions" in payload
    assert "必须在「持仓」小节明确写出" in payload


def test_shared_report_copy_does_not_promise_missing_private_entry(db):
    data = {
        "generated_at": "2026-10-29 19:00:00",
        "market_date": "2026-10-29",
        "user_id": None,
        "positions": [],
        "sources": {},
    }
    text = rg._build_template_report("premarket", data)
    assert "按用户生成的报告" not in text
    assert "「持仓」页" in text
