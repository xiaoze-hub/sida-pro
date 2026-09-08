"""W2.1/C3 (2026-09-09): 多租户数据访问隔离回归测试

覆盖本轮 C3 修复的新面(S1-S4 旧面见 test_user_isolation_api.py):
- price-alerts: create 按归属校验 stock(不能借他人 stock_id 建提醒) + /hits/today 跨规则聚合按 user 过滤
- abnormal-moves: 自选池按归属过滤 + 缓存键按 user 隔离(此前 am:all:{threshold} 全用户共享)
- stocks PUT: member 不可改全局共享自选(403) / owner 可改(200) / 他人私有 403
- feedback: 他人建议 404 防探测(owned_or_404)
- strategies/scan: watchlist 池按归属过滤
- templates: export/import 仅 owner(member 403)
- 静态门禁: check_scoped_queries.check_source 红/绿样例

策略与 test_user_isolation_api.py 一致: SessionLocal 直写数据 + TestClient 模拟 JWT。
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from src.web.database import SessionLocal, init_db

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))


@pytest.fixture(scope="module", autouse=True)
def _ensure_db():
    init_db()


@pytest.fixture(scope="module", autouse=True)
def _cleanup_module_owner():
    """模块结束清理 _OWNER_USERNAME (owner), 避免污染持久化库。

    该 owner 是 session 级单例(_get_or_create_owner_token), 若不清理, 后续
    test_permissions_rbac 删 admin 后走 env 重新引导时会被"已存在 owner"短路,
    admin 不再重建 → 登录 401(与 test_user_isolation_api 同教训)。
    """
    yield
    from src.web.database import SessionLocal
    from src.web.models import User

    db = SessionLocal()
    try:
        owner = db.query(User).filter(User.username == _OWNER_USERNAME).first()
        if owner:
            _cleanup_user_data([str(owner.id)])
    finally:
        db.close()


_OWNER_USERNAME = "mt_test_owner_v1"
_OWNER_PASSWORD = "mt_test_owner_pw_2026_v1"

_SESSION_OWNER_TOKEN: str | None = None


@pytest.fixture()
def client():
    from src.web.app import app

    return TestClient(app)


def _login_owner(client: TestClient) -> str:
    from src.web.database import SessionLocal
    from src.web.models import User
    from src.web.api.auth import hash_password, create_user

    db = SessionLocal()
    try:
        owner = db.query(User).filter(User.username == _OWNER_USERNAME).first()
        if not owner:
            owner = create_user(db, _OWNER_USERNAME, _OWNER_PASSWORD, "owner")
        else:
            owner.password_hash = hash_password(_OWNER_PASSWORD)
            db.commit()
    finally:
        db.close()

    r = client.post(
        "/api/auth/login",
        json={"username": _OWNER_USERNAME, "password": _OWNER_PASSWORD},
    )
    if r.status_code != 200:
        pytest.skip(f"无法登录 owner ({_OWNER_USERNAME}): {r.status_code} {r.text[:200]}")
    return r.json()["data"]["token"]


def _get_or_create_owner_token(client: TestClient) -> str:
    global _SESSION_OWNER_TOKEN
    if _SESSION_OWNER_TOKEN:
        return _SESSION_OWNER_TOKEN
    _SESSION_OWNER_TOKEN = _login_owner(client)
    return _SESSION_OWNER_TOKEN


def _create_two_members(client: TestClient, owner_token: str) -> dict:
    from src.web.database import SessionLocal
    from src.web.api.auth import create_token
    from src.web.models import User

    a_name = f"mt_a_{uuid.uuid4().hex[:8]}"
    b_name = f"mt_b_{uuid.uuid4().hex[:8]}"
    a_pass = "pass1234abcd"

    r1 = client.post(
        "/api/auth/users",
        headers={"Authorization": f"Bearer {owner_token}"},
        json={"username": a_name, "password": a_pass, "role": "member"},
    )
    assert r1.status_code == 200, r1.text
    a_id = r1.json()["data"]["user"]["id"]

    r2 = client.post(
        "/api/auth/users",
        headers={"Authorization": f"Bearer {owner_token}"},
        json={"username": b_name, "password": a_pass, "role": "member"},
    )
    assert r2.status_code == 200, r2.text
    b_id = r2.json()["data"]["user"]["id"]

    db = SessionLocal()
    try:
        ua = db.query(User).filter(User.id == a_id).first()
        ub = db.query(User).filter(User.id == b_id).first()
        a_token, _ = create_token(ua)
        b_token, _ = create_token(ub)
    finally:
        db.close()

    return {"a_id": a_id, "b_id": b_id, "a_name": a_name, "b_name": b_name,
            "a_token": a_token, "b_token": b_token}


@pytest.fixture()
def two_users(client):
    token = _get_or_create_owner_token(client)
    users = _create_two_members(client, token)
    users["owner_token"] = token
    yield users
    _cleanup_user_data([users["a_id"], users["b_id"]])


def _cleanup_user_data(user_ids: list[str]):
    db = SessionLocal()
    try:
        for uid in user_ids:
            db.execute(text(
                "DELETE FROM price_alert_hits WHERE rule_id IN "
                "(SELECT id FROM price_alert_rules WHERE user_id = :uid)"
            ), {"uid": uid})
            db.execute(text("DELETE FROM price_alert_rules WHERE user_id = :uid"), {"uid": uid})
            db.execute(text(
                "DELETE FROM suggestion_feedback WHERE suggestion_id IN "
                "(SELECT id FROM stock_suggestions WHERE user_id = :uid)"
            ), {"uid": uid})
            db.execute(text("DELETE FROM stock_suggestions WHERE user_id = :uid"), {"uid": uid})
            db.execute(text("DELETE FROM stocks WHERE user_id = :uid"), {"uid": uid})
            db.execute(text("DELETE FROM users WHERE id = :uid"), {"uid": uid})
        db.commit()
    finally:
        db.close()


def _insert_stock(symbol: str, market: str = "CN", name: str = "测试股",
                  user_id: str | None = None) -> int:
    """插入自选股; user_id=None 为全局共享项。"""
    db = SessionLocal()
    try:
        db.execute(
            text(
                "INSERT OR IGNORE INTO stocks (symbol, market, name, sort_order, user_id) "
                "VALUES (:symbol, :market, :name, :sort, :uid)"
            ),
            {"symbol": symbol, "market": market, "name": name, "sort": 9901, "uid": user_id},
        )
        db.commit()
        sid = db.execute(
            text("SELECT id FROM stocks WHERE symbol = :symbol AND market = :market"),
            {"symbol": symbol, "market": market},
        ).scalar()
        return int(sid)
    finally:
        db.close()


def _create_alert_rule(client: TestClient, token: str, stock_id: int, name: str) -> int:
    r = client.post(
        "/api/price-alerts",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "stock_id": stock_id,
            "name": name,
            "enabled": True,
            "condition_group": {
                "op": "and",
                "items": [{"type": "price", "op": ">", "value": 10.0}],
            },
            "market_hours_mode": "trading_only",
            "cooldown_minutes": 30,
            "max_triggers_per_day": 3,
            "repeat_mode": "repeat",
            "expire_at": None,
            "notify_channel_ids": [],
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    if isinstance(body, dict) and "id" in body:
        return body["id"]
    return body["data"]["id"]


def _insert_hit(rule_id: int, stock_id: int) -> int:
    import datetime as _dt

    db = SessionLocal()
    try:
        db.execute(
            text(
                "INSERT INTO price_alert_hits (rule_id, stock_id, trigger_time, trigger_bucket, "
                "trigger_snapshot, notify_success, notify_error) "
                "VALUES (:rid, :sid, :ts, :bucket, '{}', 0, '')"
            ),
            {
                "rid": rule_id, "sid": stock_id,
                "ts": _dt.datetime.now().replace(tzinfo=None),
                "bucket": _dt.datetime.now().strftime("%Y%m%d%H%M%S") + rule_id.__str__(),
            },
        )
        db.commit()
        hid = db.execute(
            text("SELECT id FROM price_alert_hits WHERE rule_id = :rid ORDER BY id DESC LIMIT 1"),
            {"rid": rule_id},
        ).scalar()
        return int(hid)
    finally:
        db.close()


def _json_items(r) -> list:
    """兼容裸 list 与标准包装 {code,success,data}。"""
    body = r.json()
    if isinstance(body, list):
        return body
    return body.get("data") or []


# ============================================================================
# price-alerts: 归属建单 + hits/today 聚合隔离
# ============================================================================

class TestPriceAlertsC3:
    def test_create_rule_rejects_other_users_stock(self, two_users, client):
        # B 的私有自选: A 不能借它建提醒(scoped 查询 404)
        b_sid = _insert_stock("600771", name="B私有", user_id=two_users["b_id"])
        r = client.post(
            "/api/price-alerts",
            headers={"Authorization": f"Bearer {two_users['a_token']}"},
            json={
                "stock_id": b_sid,
                "name": "越权提醒",
                "enabled": True,
                "condition_group": {
                    "op": "and",
                    "items": [{"type": "price", "op": ">", "value": 10.0}],
                },
                "market_hours_mode": "trading_only",
                "cooldown_minutes": 30,
                "max_triggers_per_day": 3,
                "repeat_mode": "repeat",
                "expire_at": None,
                "notify_channel_ids": [],
            },
        )
        assert r.status_code == 404, f"借他人 stock_id 建提醒应 404, 实际 {r.status_code}"
        db = SessionLocal()
        try:
            cnt = db.execute(
                text("SELECT COUNT(*) FROM price_alert_rules WHERE stock_id = :sid"),
                {"sid": b_sid},
            ).scalar()
            assert cnt == 0
        finally:
            db.close()

    def test_hits_today_isolation(self, two_users, client):
        sid_a = _insert_stock("600772", name="甲股", user_id=two_users["a_id"])
        sid_b = _insert_stock("600773", name="乙股", user_id=two_users["b_id"])
        rule_a = _create_alert_rule(client, two_users["a_token"], sid_a, "A规则")
        rule_b = _create_alert_rule(client, two_users["b_token"], sid_b, "B规则")
        _insert_hit(rule_a, sid_a)
        _insert_hit(rule_b, sid_b)

        r_a = client.get(
            "/api/price-alerts/hits/today",
            headers={"Authorization": f"Bearer {two_users['a_token']}"},
        )
        r_b = client.get(
            "/api/price-alerts/hits/today",
            headers={"Authorization": f"Bearer {two_users['b_token']}"},
        )
        assert r_a.status_code == 200 and r_b.status_code == 200
        a_rule_ids = {it["rule_id"] for it in _json_items(r_a)}
        b_rule_ids = {it["rule_id"] for it in _json_items(r_b)}
        assert rule_a in a_rule_ids and rule_b not in a_rule_ids
        assert rule_b in b_rule_ids and rule_a not in b_rule_ids

    def test_rule_hits_404_on_other_user(self, two_users, client):
        sid_a = _insert_stock("600774", name="甲股2", user_id=two_users["a_id"])
        rule_a = _create_alert_rule(client, two_users["a_token"], sid_a, "A规则2")
        _insert_hit(rule_a, sid_a)

        r_b = client.get(
            f"/api/price-alerts/{rule_a}/hits",
            headers={"Authorization": f"Bearer {two_users['b_token']}"},
        )
        assert r_b.status_code == 404

        r_a = client.get(
            f"/api/price-alerts/{rule_a}/hits",
            headers={"Authorization": f"Bearer {two_users['a_token']}"},
        )
        assert r_a.status_code == 200


# ============================================================================
# abnormal-moves: 池子按归属过滤 + 缓存键按 user 隔离
# ============================================================================

class TestAbnormalMovesC3:
    def test_scan_watchlist_scoped_by_user(self, two_users, client, monkeypatch):
        import src.web.api.abnormal_moves as am

        _insert_stock("600781", name="A自选", user_id=two_users["a_id"])
        _insert_stock("600782", name="B自选", user_id=two_users["b_id"])
        am.clear_cache()

        seen: list[list[str]] = []

        def _fake_analyze(symbols, min_proximity=0.5):
            seen.append(list(symbols))
            return []

        monkeypatch.setattr(am, "analyze_for_symbols", _fake_analyze)

        r_a = client.get(
            "/api/abnormal-moves?min_proximity=0.61",
            headers={"Authorization": f"Bearer {two_users['a_token']}"},
        )
        assert r_a.status_code == 200
        r_b = client.get(
            "/api/abnormal-moves?min_proximity=0.61",
            headers={"Authorization": f"Bearer {two_users['b_token']}"},
        )
        assert r_b.status_code == 200
        assert len(seen) == 2, f"A/B 应各触发一次真实扫描(缓存键隔离), 实际 {len(seen)} 次"
        a_syms, b_syms = set(seen[0]), set(seen[1])
        assert "600781" in a_syms and "600782" not in a_syms
        assert "600782" in b_syms and "600781" not in b_syms

    def test_user_scoped_cache_key(self, two_users):
        from types import SimpleNamespace

        from src.web.cache.biz_cache import user_scoped_key

        a = SimpleNamespace(id=two_users["a_id"])
        b = SimpleNamespace(id=two_users["b_id"])
        ka1 = user_scoped_key("am", a, min_proximity="0.50")
        ka2 = user_scoped_key("am", a, min_proximity="0.50")
        kb = user_scoped_key("am", b, min_proximity="0.50")
        assert ka1 == ka2, "同用户同参数应得到同一缓存键"
        assert ka1 != kb, "不同用户必须得到不同缓存键"


# ============================================================================
# stocks PUT: 全局共享项 owner 可改 / member 403 / 他人私有 403
# ============================================================================

class TestStocksOwnershipC3:
    def test_member_cannot_edit_global_owner_can(self, two_users, client):
        gid = _insert_stock("600791", name="全局共享", user_id=None)

        r_member = client.put(
            f"/api/stocks/{gid}",
            headers={"Authorization": f"Bearer {two_users['a_token']}"},
            json={"name": "member 改名"},
        )
        assert r_member.status_code == 403, \
            f"member 改全局自选应 403, 实际 {r_member.status_code}"

        r_owner = client.put(
            f"/api/stocks/{gid}",
            headers={"Authorization": f"Bearer {two_users['owner_token']}"},
            json={"name": "owner 改名"},
        )
        assert r_owner.status_code == 200, r_owner.text

        db = SessionLocal()
        try:
            name = db.execute(
                text("SELECT name FROM stocks WHERE id = :id"), {"id": gid}
            ).scalar()
            assert name == "owner 改名"
            db.execute(text("DELETE FROM stocks WHERE id = :id"), {"id": gid})
            db.commit()
        finally:
            db.close()

    def test_member_cannot_edit_others_private(self, two_users, client):
        a_sid = _insert_stock("600792", name="A私有", user_id=two_users["a_id"])
        r_b = client.put(
            f"/api/stocks/{a_sid}",
            headers={"Authorization": f"Bearer {two_users['b_token']}"},
            json={"name": "B 越权改名"},
        )
        assert r_b.status_code == 403

        db = SessionLocal()
        try:
            name = db.execute(
                text("SELECT name FROM stocks WHERE id = :id"), {"id": a_sid}
            ).scalar()
            assert name == "A私有", "B 的越权修改不应生效"
        finally:
            db.close()


# ============================================================================
# feedback: 他人建议 404(owned_or_404)
# ============================================================================

class TestFeedbackC3:
    def _insert_suggestion(self, user_id: str) -> int:
        db = SessionLocal()
        try:
            db.execute(
                text(
                    "INSERT INTO stock_suggestions (user_id, stock_symbol, stock_market, "
                    "stock_name, agent_name, action, action_label) "
                    "VALUES (:uid, '600795', 'CN', '测股', 'daily_report', 'watch', '观望')"
                ),
                {"uid": user_id},
            )
            db.commit()
            sid = db.execute(
                text("SELECT id FROM stock_suggestions WHERE user_id = :uid ORDER BY id DESC LIMIT 1"),
                {"uid": user_id},
            ).scalar()
            return int(sid)
        finally:
            db.close()

    def test_feedback_404_on_other_user(self, two_users, client):
        sug_a = self._insert_suggestion(two_users["a_id"])

        r_b = client.post(
            "/api/feedback",
            headers={"Authorization": f"Bearer {two_users['b_token']}"},
            json={"suggestion_id": sug_a, "useful": True},
        )
        assert r_b.status_code == 404, f"对他人建议反馈应 404, 实际 {r_b.status_code}"

        r_a = client.post(
            "/api/feedback",
            headers={"Authorization": f"Bearer {two_users['a_token']}"},
            json={"suggestion_id": sug_a, "useful": True},
        )
        assert r_a.status_code == 200


# ============================================================================
# strategies/scan: watchlist 池按归属过滤
# ============================================================================

class TestStrategiesScanC3:
    def test_scan_watchlist_excludes_other_users(self, two_users, client, monkeypatch):
        _insert_stock("600796", name="A池", user_id=two_users["a_id"])
        _insert_stock("600797", name="B池", user_id=two_users["b_id"])

        # 离线化: 假行情 vendor(全部有价) + 记录型评估函数
        import marketdata.vendors.tencent as tencent_mod
        import src.web.api.strategies as strat_mod

        class _FakeQuote:
            def __init__(self, code: str):
                self.symbol = code
                self.current_price = 10.0

        class _FakeVendor:
            def fetch(self, syms, opts):
                return [_FakeQuote(getattr(s, "code", None) or str(s)) for s in syms]

        seen: list[str] = []

        def _fake_eval(cfg, q, strategy_id, symbol, market):
            seen.append(symbol)
            return {
                "strategy_id": strategy_id, "symbol": symbol, "market": market,
                "passed": True, "score": 99.0, "score_breakdown": [],
                "failed_filters": [], "missing_fields": [], "current_data": {},
            }

        monkeypatch.setattr(tencent_mod, "TencentQuoteVendor", _FakeVendor)
        monkeypatch.setattr(strat_mod, "_evaluate_strategy", _fake_eval)

        r = client.post(
            "/api/strategies/scan",
            headers={"Authorization": f"Bearer {two_users['a_token']}"},
            json={"strategy_id": "dual_low", "universe": "watchlist", "market": "CN"},
        )
        assert r.status_code == 200, r.text
        assert "600797" not in r.text, "B 的自选不应出现在 A 的 watchlist 扫描池"
        assert "600796" in seen, "A 自己的自选应进入扫描池"
        assert "600797" not in seen, "B 的自选不应进入 A 的扫描池"


# ============================================================================
# templates: export/import 仅 owner
# ============================================================================

class TestTemplatesOwnerOnlyC3:
    def test_export_member_403_owner_200(self, two_users, client):
        r_member = client.get(
            "/api/templates/export",
            headers={"Authorization": f"Bearer {two_users['a_token']}"},
        )
        assert r_member.status_code == 403, \
            f"member 导出全库配置应 403, 实际 {r_member.status_code}"

        r_owner = client.get(
            "/api/templates/export",
            headers={"Authorization": f"Bearer {two_users['owner_token']}"},
        )
        assert r_owner.status_code == 200, r_owner.text

    def test_import_member_403(self, two_users, client):
        r = client.post(
            "/api/templates/import?mode=merge",
            headers={"Authorization": f"Bearer {two_users['a_token']}"},
            json={"version": 1, "settings": {}, "agents": [], "stocks": []},
        )
        assert r.status_code == 403


# ============================================================================
# 静态门禁: check_scoped_queries 红/绿样例
# ============================================================================

class TestScopeCheckerC3:
    def _checker(self):
        from check_scoped_queries import check_source

        return check_source

    def test_naked_query_flagged(self):
        check_source = self._checker()
        src = (
            "def bad(db):\n"
            "    return db.query(Stock).all()\n"
        )
        violations = check_source(src, {"Stock"})
        assert len(violations) == 1
        assert violations[0]["model"] == "Stock"
        assert violations[0]["func"] == "bad"

    def test_scoped_query_passes(self):
        check_source = self._checker()
        src = (
            "from src.web.api._scope import scoped\n"
            "def good(db, user):\n"
            "    return scoped(db.query(Stock), user).all()\n"
        )
        assert check_source(src, {"Stock"}) == []

    def test_owned_or_404_pattern_passes(self):
        check_source = self._checker()
        src = (
            "from src.web.api._scope import owned_or_404\n"
            "def good(db, user):\n"
            "    row = db.query(Stock).filter(Stock.id == 1).first()\n"
            "    return owned_or_404(row, user, '自选')\n"
        )
        assert check_source(src, {"Stock"}) == []

    def test_wrong_decorator_still_flagged(self):
        check_source = self._checker()
        # 普通装饰器不构成豁免 —— 只有 @allow_cross_user 算
        src = (
            "def deco(f):\n"
            "    return f\n"
            "@deco\n"
            "def exempt(db):\n"
            "    return db.query(Stock).all()\n"
        )
        assert len(check_source(src, {"Stock"})) == 1

    def test_allow_cross_user_decorator_passes(self):
        check_source = self._checker()
        src = (
            "import src.web.api._scope as scope_mod\n"
            "@scope_mod.allow_cross_user\n"
            "def exempt(db):\n"
            "    return db.query(Stock).all()\n"
        )
        assert check_source(src, {"Stock"}) == []
