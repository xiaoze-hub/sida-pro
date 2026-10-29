"""五审回归(2026-10-29): 运行/分析历史 NULL 归属、私有报告严格归属、报告文件唯一。

复现的阻断(修前实测):

1. `GET /api/agents/{name}/history` 把 `user_id IS NULL` 的运行记录发给所有 member,
   result 里就是某人的成本; AnalysisHistory 的 list/detail/brief/chat/context/TA
   等读路径同样 `or_(uid, IS NULL)` —— NULL 被当成"公共数据"。
2. `report_generator._collect_positions(user_id)` / `premarket_evidence` 把
   `Account.user_id IS NULL` 或 `Position.user_id IS NULL` 的历史桶并进当前用户的
   **私有报告**, 正文与 LLM 输入都出现别人的成本。
3. 报告文件名只精确到秒: 两个账号同秒生成私有报告 → 同路径互相覆盖, 出现
   "A 的正文 + B 的 meta"(B 能读 A 的成本)。

修复契约(本文件钉住, 不再依赖内容正则):

- NULL 归属历史: member 一律不可读, owner 可读(本人 + NULL), 无会话不可读;
- 私有报告/证据: 只认 `Account.user_id == uid AND Position.user_id == uid`;
  NULL 旧数据不猜归属; 共享市场报告仍对所有登录用户开放且不含任何持仓;
- 报告文件名 = 时间戳前缀 + 随机尾, `O_EXCL` 创建, 绝不覆盖; 并发/同秒必然不同;
- 多账户持仓逐账户出行; 非有限/缺失成本标"未知", 不装 0; 缺行情不写 0。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import false

import src.core.report_generator as rg
import src.web.api.reports as reports_api
from src.core import premarket_evidence as pe
from src.core import report_scope
from src.core.analysis_history import (
    analysis_history_scope_clause,
    analysis_history_visible,
    get_analysis,
    get_latest_analysis,
)
from src.core.scheduler import _agent_run_result_text, _sole_bucket_user
from src.db.models import (
    Account,
    AgentRun,
    AnalysisHistory,
    Position,
    Stock,
    User,
)
from src.web.api.auth import create_token
from src.web.database import SessionLocal, init_db


@pytest.fixture(scope="module", autouse=True)
def _db_ready():
    init_db()


@pytest.fixture()
def env():
    s = SessionLocal()
    tag = uuid.uuid4().hex[:6]
    created = {"users": [], "accounts": [], "positions": [], "stocks": [],
               "runs": [], "histories": []}

    def user(role="member", name="u"):
        u = User(id=str(uuid.uuid4()), username=f"r5_{name}_{tag}_{uuid.uuid4().hex[:4]}",
                 password_hash="x", role=role, is_active=True, token_version=1)
        s.add(u)
        s.commit()
        created["users"].append(u.id)
        return u

    def account(uid, name):
        a = Account(user_id=uid, name=name, available_funds=0.0, enabled=True)
        s.add(a)
        s.commit()
        created["accounts"].append(a.id)
        return a

    def stock(uid, symbol, name="测试票"):
        st = Stock(user_id=uid, symbol=symbol, name=name, market="CN")
        s.add(st)
        s.commit()
        created["stocks"].append(st.id)
        return st

    def position(acc, st, uid, cost=10.0, qty=100):
        p = Position(user_id=uid, account_id=acc.id, stock_id=st.id,
                     cost_price=cost, quantity=qty, trading_style="swing")
        s.add(p)
        s.commit()
        created["positions"].append(p.id)
        return p

    def run(uid, agent, result, trace):
        r = AgentRun(user_id=uid, agent_name=agent, status="success",
                     trace_id=trace, result=result, trigger_source="schedule")
        s.add(r)
        s.commit()
        created["runs"].append(r.id)
        return r

    def hist(uid, agent, date, content, raw=None):
        h = AnalysisHistory(user_id=uid, agent_name=agent, stock_symbol="*",
                            analysis_date=date, title="t", content=content,
                            raw_data=raw or {}, status="success")
        s.add(h)
        s.commit()
        created["histories"].append(h.id)
        return h

    ctx = SimpleNamespace(s=s, tag=tag, created=created, user=user, account=account,
                          stock=stock, position=position, run=run, hist=hist)
    try:
        yield ctx
    finally:
        s.rollback()
        if created["histories"]:
            s.query(AnalysisHistory).filter(
                AnalysisHistory.id.in_(created["histories"])
            ).delete(synchronize_session=False)
        if created["runs"]:
            s.query(AgentRun).filter(AgentRun.id.in_(created["runs"])).delete(
                synchronize_session=False
            )
        if created["positions"]:
            s.query(Position).filter(Position.id.in_(created["positions"])).delete(
                synchronize_session=False
            )
        if created["accounts"]:
            s.query(Account).filter(Account.id.in_(created["accounts"])).delete(
                synchronize_session=False
            )
        if created["stocks"]:
            s.query(Stock).filter(Stock.id.in_(created["stocks"])).delete(
                synchronize_session=False
            )
        from tests.conftest import purge_users

        s.commit()
        purge_users(s, ids=created["users"])
        s.commit()
        s.close()


def _token(user: User) -> dict:
    tok, _ = create_token(user)
    return {"Authorization": f"Bearer {tok}"}


# ─────────────────────── 1. NULL 归属: 纯口径 ───────────────────────


def test_scope_clause_member_excludes_null_and_anonymous_reads_nothing(env):
    member_clause = str(analysis_history_scope_clause("u1", "member"))
    owner_clause = str(analysis_history_scope_clause("u1", "owner"))
    anon_clause = str(analysis_history_scope_clause(None, None))
    assert "IS NULL" not in member_clause
    assert "IS NULL" in owner_clause
    assert anon_clause.strip().lower() in ("false", "0 = 1", "0=1") or "false" in anon_clause.lower()
    # 与 SQLAlchemy false() 同义(执行层不可读)
    assert str(false()) in anon_clause or "false" in anon_clause.lower()


def test_object_level_visibility_matches_scope(env):
    u = env.user("member", "vis")
    own = SimpleNamespace(user_id=u.id)
    legacy = SimpleNamespace(user_id=None)
    foreign = SimpleNamespace(user_id=str(uuid.uuid4()))
    assert analysis_history_visible(own, u.id, "member") is True
    assert analysis_history_visible(legacy, u.id, "member") is False
    assert analysis_history_visible(legacy, u.id, "owner") is True
    assert analysis_history_visible(foreign, u.id, "owner") is False
    assert analysis_history_visible(own, None, None) is False


# ─────────────────────── 2. AgentRun 历史真实 HTTP ───────────────────────


def test_agent_history_http_hides_null_run_from_member(env):
    member = env.user("member", "hist")
    owner = env.user("owner", "hist")
    tag = uuid.uuid4().hex[:6]
    null_trace = f"sch-r5-null-{tag}"
    own_trace = f"sch-r5-own-{tag}"
    env.run(None, "premarket_outlook", "NULL-OWNER-SECRET 600519 成本 1234.56", null_trace)
    env.run(member.id, "premarket_outlook", "MEMBER-OWN-BODY", own_trace)

    from src.web.app import app

    client = TestClient(app)
    r = client.get("/api/agents/premarket_outlook/history", headers=_token(member))
    assert r.status_code == 200, r.text
    rows = r.json().get("data") or []
    traces = {row.get("trace_id") for row in rows}
    assert own_trace in traces
    assert null_trace not in traces
    assert all("NULL-OWNER-SECRET" not in (row.get("result") or "") for row in rows)

    r_owner = client.get("/api/agents/premarket_outlook/history", headers=_token(owner))
    assert r_owner.status_code == 200, r_owner.text
    owner_traces = {row.get("trace_id") for row in (r_owner.json().get("data") or [])}
    assert null_trace in owner_traces


# ─────────────────────── 3. AnalysisHistory 各读路径 ───────────────────────


def test_analysis_history_http_and_core_reads_are_owner_scoped(env):
    member = env.user("member", "ah")
    owner = env.user("owner", "ah")
    other = env.user("member", "ahother")
    tag = env.tag
    own = env.hist(member.id, "premarket_outlook", "2026-10-20", "MEMBER-ONLY-BODY",
                   raw={"prompt_context": "MEMBER-PROMPT", "news": [{"title": "m"}]})
    legacy = env.hist(None, "premarket_outlook", "2026-10-29", "NULL-SECRET-BODY 600519 成本 1234.56",
                      raw={"prompt_context": "NULL-SECRET-PROMPT"})
    foreign = env.hist(other.id, "premarket_outlook", "2026-10-28", "FOREIGN-SECRET-BODY")

    from src.web.app import app

    client = TestClient(app)
    r = client.get("/api/history?kind=workflow&limit=100", headers=_token(member))
    assert r.status_code == 200, r.text
    ids = {row["id"] for row in (r.json().get("data") or [])}
    assert own.id in ids
    assert legacy.id not in ids, "NULL 历史行不得出现在 member 的列表"
    assert foreign.id not in ids

    assert client.get(f"/api/history/{legacy.id}", headers=_token(member)).status_code == 404
    assert client.get(f"/api/history/{foreign.id}", headers=_token(member)).status_code == 404
    assert client.get(f"/api/history/{own.id}", headers=_token(member)).status_code == 200
    # owner 可以读 NULL 历史(数据责任人), 但仍不能读 member 的私有行。
    r_owner = client.get(f"/api/history/{legacy.id}", headers=_token(owner))
    assert r_owner.status_code == 200, r_owner.text
    assert "1234.56" in r_owner.text
    assert client.get(f"/api/history/{own.id}", headers=_token(owner)).status_code == 404

    # 核心读函数也必须显式给用户; 不给用户 = 一条不读(fail-closed)。
    assert get_analysis("premarket_outlook", "*", dt.date(2026, 10, 29),
                        user_id=member.id) is None
    assert get_analysis("premarket_outlook", "*", dt.date(2026, 10, 29)) is None
    latest = get_latest_analysis("premarket_outlook", "*", before_date=dt.date(2026, 10, 30),
                                 user_id=member.id)
    assert latest is not None and latest.id == own.id
    assert get_latest_analysis("premarket_outlook", "*", before_date=dt.date(2026, 10, 30)) is None

    # dashboard brief / 最新洞察: 另一个完全没有记录的用户不能借 NULL 行拿到内容。
    carol = env.user("member", "ahcarol")
    r_brief = client.get("/api/dashboard/brief?type=premarket", headers=_token(carol))
    assert r_brief.status_code == 200, r_brief.text
    payload = r_brief.json().get("data") or {}
    assert payload.get("empty") is True or "NULL-SECRET-BODY" not in (payload.get("content") or "")


def test_harness_premarket_brief_ignores_null_history(env):
    from src.harness.context import inject_premarket_brief

    member = env.user("member", "harness")
    env.hist(None, "premarket_outlook", "2026-10-29", "NULL-HARNESS-SECRET 600519 成本 1234.56")
    messages = [{"role": "system", "content": "base"}]
    db = SessionLocal()
    try:
        m = db.query(User).filter(User.id == member.id).first()
        injected = inject_premarket_brief(messages, db, m)
    finally:
        db.close()
    assert injected is False
    assert "NULL-HARNESS-SECRET" not in str(messages)


# ─────────────────────── 4. 私有报告: 严格双层归属 + LLM/正文 ───────────────────────


def _stub_report_collectors(monkeypatch, root):
    monkeypatch.setattr(rg, "_report_root", lambda: root)

    async def _idx(*a, **k):
        return [{"symbol": "sh000001", "name": "上证指数", "price": 3300.0,
                 "change_pct": 0.5, "amount": 4.1e11}]

    async def _flow(*a, **k):
        return {"total_main_flow": 1.0, "sh_flow": 1.0, "sz_flow": 0.0, "source": "test"}

    async def _lu(*a, **k):
        return {"limit_up_count": 10, "max_streak": 2, "ladder": {"2": 1}}

    async def _ld(*a, **k):
        return []

    async def _sig(*a, **k):
        return []

    async def _none(*a, **k):
        return None

    monkeypatch.setattr(rg, "_collect_indices", _idx)
    monkeypatch.setattr(rg, "_collect_market_flow", _flow)
    monkeypatch.setattr(rg, "_collect_limit_up_summary", _lu)
    monkeypatch.setattr(rg, "_collect_limit_down", _ld)
    monkeypatch.setattr(rg, "_collect_strategy_signals", _sig)
    monkeypatch.setattr(rg, "_llm_generate", _none)
    monkeypatch.setattr("src.core.marketdata_client.md_quote_rows", lambda *a, **k: [])


def test_private_report_never_reads_legacy_null_bucket(env, tmp_path, monkeypatch):
    alice = env.user("member", "rp")
    bob = env.user("member", "rp2")
    own_acc = env.account(alice.id, "alice主账户")
    own_st = env.stock(alice.id, f"OWN{env.tag.upper()}", "自己的票")
    env.position(own_acc, own_st, alice.id, cost=9.87, qty=200)
    legacy_acc = env.account(None, "历史共享账户")
    legacy_st = env.stock(None, f"LEG{env.tag.upper()}", "历史桶票")
    env.position(legacy_acc, legacy_st, None, cost=1234.56, qty=100)
    bob_acc = env.account(bob.id, "bob账户")
    bob_st = env.stock(bob.id, f"BOB{env.tag.upper()}", "bob的票")
    env.position(bob_acc, bob_st, bob.id, cost=55.55, qty=10)

    _stub_report_collectors(monkeypatch, tmp_path / "cron" / "output")
    db = SessionLocal()
    try:
        data = asyncio.run(rg._collect_data("postmarket", db, user_id=alice.id))
        prompt = rg._build_user_content("postmarket", data)
        out = asyncio.run(rg.generate_market_report("postmarket", db=db, user_id=alice.id))
    finally:
        db.close()
    body = Path(out["path"]).read_text(encoding="utf-8")

    for text, label in ((prompt, "LLM 输入"), (body, "报告正文")):
        assert own_st.symbol in text, f"{label} 应含本人持仓"
        assert legacy_st.symbol not in text, f"{label} 不得含 NULL 共享桶(他人历史)"
        assert "1234.56" not in text
        assert bob_st.symbol not in text and "55.55" not in text

    meta = report_scope.read_report_meta(Path(out["path"]))
    assert meta["scope"] == "private" and meta["owner_user_id"] == alice.id
    assert report_scope.report_visible(meta, user_id=bob.id, role="member") is False

    # 共享市场报告仍然开放, 且依然不含任何人的持仓。
    shared = asyncio.run(rg.generate_market_report("postmarket", user_id=None))
    shared_body = Path(shared["path"]).read_text(encoding="utf-8")
    shared_meta = report_scope.read_report_meta(Path(shared["path"]))
    assert shared_meta["scope"] == "shared"
    assert shared_meta["contains_private_holdings"] is False
    assert report_scope.report_visible(shared_meta, user_id=bob.id, role="member") is True
    for sym in (own_st.symbol, legacy_st.symbol, bob_st.symbol):
        assert sym not in shared_body
    assert "1234.56" not in shared_body


def test_positions_are_per_account_and_missing_cost_is_unknown(env, monkeypatch):
    alice = env.user("member", "multi")
    acc1 = env.account(alice.id, "券商A")
    acc2 = env.account(alice.id, "券商B")
    st = env.stock(alice.id, f"MULTI{env.tag.upper()}", "同一标的")
    env.position(acc1, st, alice.id, cost=10.0, qty=100)
    # SQLite 的 NOT NULL 列不能存 NaN(驱动会拒绝); ±Inf 可存, 走同一条 non-finite 分支。
    env.position(acc2, st, alice.id, cost=float("inf"), qty=300)
    monkeypatch.setattr("src.core.marketdata_client.md_quote_rows", lambda *a, **k: [])

    db = SessionLocal()
    try:
        rows = asyncio.run(rg._collect_positions(db, with_quotes=False, user_id=alice.id))
    finally:
        db.close()
    mine = [r for r in rows if r["symbol"] == st.symbol]
    assert len(mine) == 2, f"同一标的多账户必须逐账户出行(实际 {mine})"
    assert {r["account_id"] for r in mine} == {acc1.id, acc2.id}
    assert {r["account_name"] for r in mine} == {"券商A", "券商B"}
    nan_row = [r for r in mine if r["account_id"] == acc2.id][0]
    assert nan_row["cost_price"] is None and nan_row["cost_unknown"] is True
    assert "pnl_pct" not in nan_row

    report = rg._build_template_report("postmarket", {
        "user_id": alice.id, "positions": mine, "market_date": "2026-10-29",
        "generated_at": "2026-10-29 15:30:00", "indices": [], "market_flow": {},
        "limit_up": {}, "limit_down": [], "strategy_signals": [],
    })
    assert "券商A" in report and "券商B" in report
    assert "成本 未知" in report
    assert "成本 0" not in report and "成本 0.0" not in report


def test_premarket_evidence_per_account_and_finite_cost(env):
    alice = env.user("member", "pe")
    acc1 = env.account(alice.id, "账户甲")
    acc2 = env.account(alice.id, "账户乙")
    st = env.stock(alice.id, f"PE{env.tag.upper()}", "证据票")
    env.position(acc1, st, alice.id, cost=8.5, qty=100)
    env.position(acc2, st, alice.id, cost=float("inf"), qty=50)
    block = pe._collect_portfolio_block(env.s, alice.id, "2026-10-29", with_quotes=False)
    mine = [it for it in (block["items"] or []) if it["symbol"] == st.symbol]
    assert len(mine) == 2
    assert {it["account_id"] for it in mine} == {acc1.id, acc2.id}
    unknown = [it for it in mine if it["account_id"] == acc2.id][0]
    assert unknown["cost_price"] is None and unknown["cost_unknown"] is True


# ─────────────────────── 5. 报告文件唯一 + 并发 + 跨账号 HTTP ───────────────────────


def test_write_report_same_second_produces_distinct_files(tmp_path, monkeypatch):
    monkeypatch.setattr(rg, "_report_root", lambda: tmp_path)
    run_at = dt.datetime(2026, 10, 29, 15, 30, 0)
    a = rg._write_report("postmarket-review", "# A", "ALICE-BODY 成本 9.87", run_at,
                         scope="private", owner_user_id="alice", contains_private_holdings=True)
    b = rg._write_report("postmarket-review", "# B", "BOB-BODY 成本 7.65", run_at,
                         scope="private", owner_user_id="bob", contains_private_holdings=True)
    assert a["path"] != b["path"]
    assert Path(a["path"]).name.startswith("2026-10-29_15-30-00-")
    assert "ALICE-BODY" in Path(a["path"]).read_text(encoding="utf-8")
    assert "BOB-BODY" in Path(b["path"]).read_text(encoding="utf-8")
    assert report_scope.read_report_meta(Path(a["path"]))["owner_user_id"] == "alice"
    assert report_scope.read_report_meta(Path(b["path"]))["owner_user_id"] == "bob"


def test_write_report_concurrent_same_second_never_overwrites(tmp_path, monkeypatch):
    monkeypatch.setattr(rg, "_report_root", lambda: tmp_path)
    run_at = dt.datetime(2026, 10, 29, 15, 30, 0)
    barrier = threading.Barrier(16)

    def one(i):
        barrier.wait()
        return rg._write_report("postmarket-review", f"# {i}", f"BODY-{i}",
                                run_at, scope="private", owner_user_id=f"u{i}",
                                contains_private_holdings=True)

    with ThreadPoolExecutor(max_workers=16) as pool:
        outs = list(pool.map(one, range(16)))
    paths = [o["path"] for o in outs]
    assert len(set(paths)) == 16, "并发同秒必须产生 16 个不同文件"
    for i, o in enumerate(outs):
        p = Path(o["path"])
        assert p.read_text(encoding="utf-8").strip() == f"BODY-{i}"
        meta = report_scope.read_report_meta(p)
        assert meta["owner_user_id"] == f"u{i}"


def test_write_report_refuses_to_overwrite_existing_and_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(rg, "_report_root", lambda: tmp_path)
    monkeypatch.setattr(rg.secrets, "token_hex", lambda n: "deadbeef")
    run_at = dt.datetime(2026, 10, 29, 15, 30, 0)
    job_dir = tmp_path / "postmarket-review"
    job_dir.mkdir(parents=True)
    existing = job_dir / "2026-10-29_15-30-00-deadbeef.md"
    existing.write_text("OLD-SAFE-CONTENT", encoding="utf-8")

    with pytest.raises(RuntimeError):
        rg._write_report("postmarket-review", "# X", "NEW-PRIVATE-BODY", run_at,
                         scope="private", owner_user_id="alice",
                         contains_private_holdings=True)
    assert existing.read_text(encoding="utf-8") == "OLD-SAFE-CONTENT", "绝不覆盖已存在报告"
    assert report_scope.read_report_meta(existing) is None


def test_cross_account_report_http_is_isolated(env, tmp_path, monkeypatch):
    alice = env.user("member", "rphttp")
    bob = env.user("member", "rphttp2")
    monkeypatch.setattr(rg, "_report_root", lambda: tmp_path / "cron" / "output")
    monkeypatch.setattr(reports_api, "CRON_OUTPUT_DIR", tmp_path / "cron" / "output")
    reports_api._clear_cache()
    run_at = dt.datetime(2026, 10, 29, 15, 30, 0)
    a = rg._write_report("postmarket-review", "# A", "ALICE-PRIVATE 成本 9.87", run_at,
                         scope="private", owner_user_id=alice.id,
                         contains_private_holdings=True)
    b = rg._write_report("postmarket-review", "# B", "BOB-PRIVATE 成本 7.65", run_at,
                         scope="private", owner_user_id=bob.id,
                         contains_private_holdings=True)
    a_name, b_name = Path(a["path"]).name, Path(b["path"]).name

    from src.web.app import app

    client = TestClient(app)
    r = client.get("/api/reports/content", params={"job_id": "postmarket-review", "file": a_name},
                   headers=_token(alice))
    assert r.status_code == 200 and "ALICE-PRIVATE" in r.json()["data"]["content"]
    assert client.get("/api/reports/content",
                      params={"job_id": "postmarket-review", "file": b_name},
                      headers=_token(alice)).status_code == 404
    r_bob = client.get("/api/reports/content",
                       params={"job_id": "postmarket-review", "file": b_name},
                       headers=_token(bob))
    assert r_bob.status_code == 200 and "BOB-PRIVATE" in r_bob.json()["data"]["content"]

    r_list = client.get("/api/reports/list?job_id=postmarket-review&limit=50",
                        headers=_token(alice))
    files = {it["file"] for it in (r_list.json().get("data") or {}).get("items", [])}
    assert a_name in files and b_name not in files


# ─────────────────────── 6. 调度混合桶 / 防守线渠道选择集 ───────────────────────


def test_scheduler_mixed_null_bucket_never_attributes_body(env):
    u1 = str(uuid.uuid4())
    ctx = lambda uid: SimpleNamespace(user=SimpleNamespace(id=uid) if uid else None)
    res = lambda text: SimpleNamespace(content=text)

    assert _sole_bucket_user([ctx(u1)]) == u1
    assert _sole_bucket_user([ctx(u1), ctx(None)]) is None
    assert _sole_bucket_user([ctx(None), ctx(u1)]) is None
    assert _sole_bucket_user([ctx(None)]) is None
    for mixed in (
        [(ctx(u1), res("U1-SECRET")), (ctx(None), res("NULL-SECRET"))],
        [(ctx(None), res("NULL-SECRET")), (ctx(u1), res("U1-SECRET"))],
    ):
        text = _agent_run_result_text(mixed)
        assert "SECRET" not in text and "buckets=2" in text
    # 同一用户多桶: 只取该用户桶的正文, 不取"最后一桶"。
    assert _agent_run_result_text([(ctx(u1), res("FIRST")), (ctx(u1), res("SECOND"))]) == "FIRST"


def test_intraday_defense_push_uses_channel_selection_and_never_zero_cost(env, monkeypatch):
    import src.agents.intraday_monitor as im
    import src.core.notify_center as nc

    monkeypatch.setattr(im, "load_defense_lines",
                        lambda symbol, user_id=None: [
                            {"defense_price": 10.0, "cost_price": None}
                        ])
    # 屏蔽与本用例无关的外部取数
    monkeypatch.setattr(im, "_main_intent_summary", lambda *a, **k: "", raising=False)
    sent: list[dict] = []
    monkeypatch.setattr(nc, "push_notification",
                        lambda title, body="", **kw: sent.append({"body": body, **kw}) or 1)

    agent = im.IntradayMonitorAgent.__new__(im.IntradayMonitorAgent)
    stock = SimpleNamespace(name="测试票", symbol="600000", current_price=9.0, change_pct=-2.0)
    suggestion = {"action_label": "减仓", "signal": "跌破防线", "reason": "纪律"}
    content = agent._format_human_readable_content(
        stock, suggestion, "{}",
        data={"user_id": "u-1", "notify_channel_ids": [7, 8]},
    )
    assert sent, "防守线击穿必须尝试外发(站内+渠道)"
    assert sent[-1]["channel_ids"] == (7, 8)
    assert "成本未知" in content and "成本 0.00" not in content
    assert "成本 0.00" not in sent[-1]["body"]

    # 没有解析到渠道选择集 → 传空元组, notify_center 按 fail-closed 不外发。
    sent.clear()
    agent._format_human_readable_content(
        stock, suggestion, "{}", data={"user_id": "u-1"},
    )
    assert sent and sent[-1]["channel_ids"] == ()
