"""报告中心跨租户泄漏回归(2026-10-29 三审 R1)。

复现的越权(修前实测):

1. `report_generator._collect_positions()` 一条用户条件都没有 → 盘前/盘后报告里写着
   **全库所有人**的成本价/持仓量;
2. `premarket_evidence.collect(user_id=None)` 的 `if user_id:` 写法在 None 时不加过滤
   → 证据层(以及引用它的报告)同样读到全库;
3. `/api/reports/list` + `/content` 只要登录就能读 → 任一 member 都能下载别人的成本;
4. 报告是裸文件, 没有归属信息 → 存量报告的泄漏无法靠"修新生成"消除。

本文件锁定修复后的口径:

- 生成侧: 传 uid 只采该用户可见(账户可见 AND 持仓可见); 传 None = 历史共享桶, 不是全库;
- 读侧: 有 meta 按 meta; **无 meta(存量)= 来源不明 = 仅 owner**; member 统一 404;
- `/list` 不出现在不可读项里(不泄露存在性), 缓存键含查看者(不能串号);
- owner 可见全部(数据责任人, 明确裁决)—— 对 owner 的影响写在这里, 便于复核。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import src.core.report_generator as rg
import src.web.api.reports as reports
from src.core import report_scope
from src.db.models import Account, Position, Stock, User
from src.web.database import SessionLocal, init_db


# ──────────────────────────── 纯逻辑: 判定表 ────────────────────────────


def test_meta_absent_is_owner_only():
    """存量报告(无 meta)对 member 一律不可读, 对 owner 可读。"""
    assert report_scope.report_visible(None, user_id="u1", role="member") is False
    assert report_scope.report_visible(None, user_id="u1", role="owner") is True
    assert report_scope.report_visibility_reason(None, user_id="u1", role="member") == "legacy-unscoped"


def test_shared_without_private_holdings_visible_to_member():
    meta = {"scope": "shared", "owner_user_id": None, "contains_private_holdings": False}
    assert report_scope.report_visible(meta, user_id="u1", role="member") is True


def test_shared_with_private_holdings_hidden_from_member():
    """旧写法产物: 名义共享 + 含私有持仓 → member 不可读(二次把关)。"""
    meta = {"scope": "shared", "owner_user_id": None, "contains_private_holdings": True}
    assert report_scope.report_visible(meta, user_id="u1", role="member") is False
    assert report_scope.report_visible(meta, user_id="owner-x", role="owner") is True


def test_private_visible_only_to_its_owner():
    meta = {"scope": "private", "owner_user_id": "u1", "contains_private_holdings": True}
    assert report_scope.report_visible(meta, user_id="u1", role="member") is True
    assert report_scope.report_visible(meta, user_id="u2", role="member") is False
    assert report_scope.report_visible(meta, user_id=None, role=None) is False


def test_unknown_or_broken_meta_fails_closed():
    for meta in (
        {"scope": "public"},
        {"scope": ""},
        {"nope": 1},
    ):
        assert report_scope.report_visible(meta, user_id="u1", role="member") is False


def test_write_meta_roundtrip_and_private_requires_owner(tmp_path):
    f = tmp_path / "a.md"
    f.write_text("# t\n", encoding="utf-8")
    with pytest.raises(ValueError):
        report_scope.write_report_meta(f, scope="private", owner_user_id=None)
    mp = report_scope.write_report_meta(
        f, scope="private", owner_user_id="u1", contains_private_holdings=True
    )
    assert mp.name == "a.md.meta.json"
    got = report_scope.read_report_meta(f)
    assert got and got["scope"] == "private" and got["owner_user_id"] == "u1"


def test_broken_meta_json_is_none_not_crash(tmp_path):
    f = tmp_path / "b.md"
    f.write_text("# t\n", encoding="utf-8")
    report_scope.report_meta_path(f).write_text("{not json", encoding="utf-8")
    assert report_scope.read_report_meta(f) is None


# ──────────────────────────── 生成侧: 采集作用域 ────────────────────────────


PREFIX = "rsc"


@pytest.fixture(scope="module", autouse=True)
def _db_ready():
    init_db()


@pytest.fixture()
def tenancy():
    db = SessionLocal()
    tag = uuid.uuid4().hex[:6]
    created = {"users": [], "accounts": [], "positions": [], "stocks": []}
    try:
        a = User(
            id=str(uuid.uuid4()),
            username=f"{PREFIX}_a_{tag}",
            password_hash="x",
            role="member",
            is_active=True,
            token_version=1,
        )
        b = User(
            id=str(uuid.uuid4()),
            username=f"{PREFIX}_b_{tag}",
            password_hash="x",
            role="member",
            is_active=True,
            token_version=1,
        )
        db.add_all([a, b])
        db.commit()
        created["users"] = [a.id, b.id]

        acc_a = Account(name=f"{PREFIX}-A-{tag}", available_funds=1000.0, enabled=True, user_id=a.id)
        acc_b = Account(name=f"{PREFIX}-B-{tag}", available_funds=2000.0, enabled=True, user_id=b.id)
        db.add_all([acc_a, acc_b])
        db.commit()
        created["accounts"] = [acc_a.id, acc_b.id]

        def mk(acc, uid, sym, cost=10.0, qty=100):
            st = Stock(symbol=sym, name=sym, market="CN", user_id=None)
            db.add(st)
            db.flush()
            pos = Position(
                account_id=acc.id, stock_id=st.id, cost_price=cost, quantity=qty, user_id=uid
            )
            db.add(pos)
            db.commit()
            created["stocks"].append(st.id)
            created["positions"].append(pos.id)
            return pos

        sym_a, sym_b = f"RSA{tag}", f"RSB{tag}"
        yield {
            "users": {"a": a, "b": b},
            "sym_a": sym_a,
            "sym_b": sym_b,
            "pos_a": mk(acc_a, a.id, sym_a, cost=11.11),
            "pos_b": mk(acc_b, b.id, sym_b, cost=22.22),
        }
    finally:
        db.rollback()
        db.query(Position).filter(Position.id.in_(created["positions"])).delete(
            synchronize_session=False
        )
        db.query(Account).filter(Account.id.in_(created["accounts"])).delete(
            synchronize_session=False
        )
        db.query(Stock).filter(Stock.id.in_(created["stocks"])).delete(
            synchronize_session=False
        )
        from tests.conftest import purge_users

        purge_users(db, ids=created["users"])
        db.commit()
        db.close()


def _symbols(rows) -> set[str]:
    return {r["symbol"] for r in rows}


def test_collect_positions_scoped_per_user(tenancy):
    db = SessionLocal()
    try:
        a_id = tenancy["users"]["a"].id
        rows = asyncio.run(rg._collect_positions(db, with_quotes=False, user_id=a_id))
        syms = _symbols(rows)
        assert tenancy["sym_a"] in syms, "A 自己账户的持仓必须在"
        assert tenancy["sym_b"] not in syms, "A 的报告不得含 B 的持仓"
    finally:
        db.close()


def test_collect_positions_shared_bucket_is_not_full_table(tenancy):
    """user_id=None = 共享市场报告: 一条持仓都不采(四审)。

    三审只收敛到"共享桶"; 但共享桶里仍是某个人的历史成本, 标成 shared=false 反而
    把它贴成了公共数据。四审: 共享报告纯市场, 私有持仓只按用户生成。
    """
    db = SessionLocal()
    try:
        rows = asyncio.run(rg._collect_positions(db, with_quotes=False, user_id=None))
        assert rows == [], f"共享报告不得采集任何持仓(实际 {rows})"
    finally:
        db.close()


def test_shared_report_meta_flag_is_content_driven():
    """contains_private_holdings 按**实际采集内容**判定, 不是 bool(user_id) 猜的。"""
    assert rg._detect_private_holdings({"user_id": None, "positions": []}) is False
    assert rg._detect_private_holdings({"user_id": None, "positions": [{"symbol": "X"}]}) is True
    assert rg._detect_private_holdings({"user_id": "u1", "positions": []}) is True
    ev = {"portfolio": {"items": [{"symbol": "X"}]}}
    assert rg._detect_private_holdings({"user_id": None, "positions": [], "evidence": ev}) is True


def test_write_report_records_scope_meta(tmp_path, monkeypatch):
    monkeypatch.setattr(rg, "_report_root", lambda: tmp_path)
    from datetime import datetime

    out_priv = rg._write_report(
        "premarket-daily",
        "# t",
        "# t\nbody",
        datetime(2026, 10, 29, 8, 30, 0),
        scope="private",
        owner_user_id="u-1",
        contains_private_holdings=True,
    )
    meta = report_scope.read_report_meta(Path(out_priv["path"]))
    assert meta["scope"] == "private" and meta["owner_user_id"] == "u-1"

    out_shared = rg._write_report(
        "premarket-daily",
        "# t2",
        "# t2\nbody",
        datetime(2026, 10, 29, 8, 31, 0),
        scope="shared",
    )
    meta2 = report_scope.read_report_meta(Path(out_shared["path"]))
    assert meta2["scope"] == "shared" and meta2["contains_private_holdings"] is False


# ──────────────────────────── 读侧: HTTP ────────────────────────────


@pytest.fixture()
def root(tmp_path, monkeypatch):
    r = tmp_path / "cron" / "output"
    (r / "premarket-daily").mkdir(parents=True)
    monkeypatch.setattr(reports, "CRON_OUTPUT_DIR", r)
    monkeypatch.setattr(reports, "HERMES_HOME", tmp_path)
    reports._clear_cache()
    yield r
    reports._clear_cache()


@pytest.fixture()
def client(root):
    from src.web.app import app

    return TestClient(app)


@pytest.fixture()
def tokens():
    from src.web.api.auth import create_token

    db = SessionLocal()
    tag = uuid.uuid4().hex[:6]
    users = {}
    ids = []
    try:
        for name, role in (("a", "member"), ("b", "member"), ("o", "owner")):
            u = User(
                id=str(uuid.uuid4()),
                username=f"{PREFIX}_{name}_{tag}",
                password_hash="x",
                role=role,
                is_active=True,
                token_version=1,
            )
            db.add(u)
            users[name] = u
        db.commit()
        ids = [u.id for u in users.values()]
        out = {}
        for name, u in users.items():
            tok, _ = create_token(u)
            out[name] = {"token": tok, "id": u.id, "role": u.role}
        yield out
    finally:
        db.rollback()
        from tests.conftest import purge_users

        purge_users(db, ids=ids)
        db.commit()
        db.close()


def _hdr(tokens, who):
    return {"Authorization": f"Bearer {tokens[who]['token']}"}


def _write(root: Path, name: str, meta: dict | None, body: str = "# 报告\n正文") -> Path:
    f = root / "premarket-daily" / name
    f.write_text(body, encoding="utf-8")
    if meta is not None:
        report_scope.write_report_meta(f, scope=meta["scope"], owner_user_id=meta.get("owner_user_id"),
                                       contains_private_holdings=meta.get("contains_private_holdings", False))
    return f


def test_legacy_report_without_meta_is_owner_only(client, root, tokens):
    _write(root, "legacy.md", None, "# 旧报告\n成本 12.34")
    r_member = client.get(
        "/api/reports/content",
        params={"job_id": "premarket-daily", "file": "legacy.md"},
        headers=_hdr(tokens, "a"),
    )
    assert r_member.status_code == 404, "存量报告(无 meta)不得对 member 开放"
    assert "12.34" not in r_member.text

    r_owner = client.get(
        "/api/reports/content",
        params={"job_id": "premarket-daily", "file": "legacy.md"},
        headers=_hdr(tokens, "o"),
    )
    assert r_owner.status_code == 200
    assert "成本 12.34" in r_owner.json()["data"]["content"]


def test_private_report_hidden_from_other_member(client, root, tokens):
    _write(root, "b_private.md", {"scope": "private", "owner_user_id": tokens["b"]["id"],
                                  "contains_private_holdings": True}, "# B 的报告\n成本 9.9")
    r_a = client.get(
        "/api/reports/content",
        params={"job_id": "premarket-daily", "file": "b_private.md"},
        headers=_hdr(tokens, "a"),
    )
    assert r_a.status_code == 404
    assert "9.9" not in r_a.text

    r_b = client.get(
        "/api/reports/content",
        params={"job_id": "premarket-daily", "file": "b_private.md"},
        headers=_hdr(tokens, "b"),
    )
    assert r_b.status_code == 200
    assert "成本 9.9" in r_b.json()["data"]["content"]


def test_shared_market_report_visible_to_all(client, root, tokens):
    _write(root, "shared.md", {"scope": "shared"}, "# 市场\n指数 3200")
    for who in ("a", "b", "o"):
        r = client.get(
            "/api/reports/content",
            params={"job_id": "premarket-daily", "file": "shared.md"},
            headers=_hdr(tokens, who),
        )
        assert r.status_code == 200, who


def test_shared_report_whose_body_contains_holdings_is_hidden_from_member(client, root, tokens):
    """meta 自称 shared+不含私有, 正文却有「500股 成本 12.34」→ member 仍不可读(内容绊线)。

    这是历史生成器/外部 cron 写错 meta 的情形: 读侧不能把 meta 当事实, 必须按内容
    二次校验, 判不了就不给 member(fail-closed)。
    """
    _write(root, "lying_meta.md", {"scope": "shared", "contains_private_holdings": False},
           "# 盘前\n\n- 历史共享票(LEGACY1) 500股 成本 12.34\n")
    r_member = client.get(
        "/api/reports/content",
        params={"job_id": "premarket-daily", "file": "lying_meta.md"},
        headers=_hdr(tokens, "a"),
    )
    assert r_member.status_code == 404
    assert "12.34" not in r_member.text
    lst = client.get("/api/reports/list", headers=_hdr(tokens, "a")).json()["data"]
    assert "lying_meta.md" not in {i["file"] for i in lst["items"]}

    r_owner = client.get(
        "/api/reports/content",
        params={"job_id": "premarket-daily", "file": "lying_meta.md"},
        headers=_hdr(tokens, "o"),
    )
    assert r_owner.status_code == 200, "owner 是数据责任人, 仍可复核这份报告"


def test_content_holdings_tripwire_only_flags_holdings_lines():
    assert report_scope.content_looks_private_holdings("- 平安银行(000001) 1000股 成本 11.2") is True
    assert report_scope.content_looks_private_holdings("- 持仓成本: 1680.5") is True
    # 市场解读里的"成本下降 5%"不是持仓行, 不能误伤共享市场报告。
    assert report_scope.content_looks_private_holdings("> 行业成本下降 5%, 毛利改善") is False
    assert report_scope.content_looks_private_holdings("## 四、持仓\n- 共享市场报告不含个人持仓/成本") is False


def test_shared_report_marked_private_holdings_hidden_from_member(client, root, tokens):
    _write(root, "tainted.md", {"scope": "shared", "contains_private_holdings": True},
           "# 旧格式共享\n成本 7.7")
    r = client.get(
        "/api/reports/content",
        params={"job_id": "premarket-daily", "file": "tainted.md"},
        headers=_hdr(tokens, "a"),
    )
    assert r.status_code == 404


def test_list_only_shows_visible_and_does_not_leak_names(client, root, tokens):
    _write(root, "legacy.md", None, "# 旧报告")
    _write(root, "shared.md", {"scope": "shared"}, "# 市场")
    _write(root, "b_private.md", {"scope": "private", "owner_user_id": tokens["b"]["id"]}, "# B")

    r = client.get("/api/reports/list", headers=_hdr(tokens, "a"))
    assert r.status_code == 200
    files = {it["file"] for it in r.json()["data"]["items"]}
    assert files == {"shared.md"}, files

    r_b = client.get("/api/reports/list", headers=_hdr(tokens, "b"))
    files_b = {it["file"] for it in r_b.json()["data"]["items"]}
    assert files_b == {"shared.md", "b_private.md"}, files_b

    r_o = client.get("/api/reports/list", headers=_hdr(tokens, "o"))
    files_o = {it["file"] for it in r_o.json()["data"]["items"]}
    assert files_o == {"legacy.md", "shared.md", "b_private.md"}, files_o


def test_list_cache_is_per_viewer(client, root, tokens):
    """A 的 list 结果不能被 B 命中(缓存键必须含查看者)。"""
    _write(root, "b_only.md", {"scope": "private", "owner_user_id": tokens["b"]["id"]}, "# B")
    _a = client.get("/api/reports/list", headers=_hdr(tokens, "a")).json()["data"]
    _b = client.get("/api/reports/list", headers=_hdr(tokens, "b")).json()["data"]
    assert {i["file"] for i in _a["items"]} == set()
    assert {i["file"] for i in _b["items"]} == {"b_only.md"}


def test_path_traversal_via_job_id_is_rejected(client, root, tokens):
    r = client.get(
        "/api/reports/content",
        params={"job_id": "../../../etc", "file": "passwd.md"},
        headers=_hdr(tokens, "o"),
    )
    assert r.status_code == 400


def test_reports_require_login(client, root):
    r = client.get("/api/reports/list")
    assert r.status_code == 401


# ──────────────────────────── JSON 严格性 ────────────────────────────


def test_meta_json_is_strict(tmp_path):
    """meta 必须能被 strict JSON 解析(不允许 NaN/Infinity 字面量)。"""
    f = tmp_path / "x.md"
    f.write_text("# t", encoding="utf-8")
    report_scope.write_report_meta(f, scope="shared")
    raw = report_scope.report_meta_path(f).read_text(encoding="utf-8")
    json.loads(raw, parse_constant=lambda c: (_ for _ in ()).throw(ValueError(c)))
