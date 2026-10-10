"""决策合成预落库 + TTL 缓存(2026-10-10 冗余设计)回归。

覆盖:
- 批算落库幂等(同键 upsert, 不新增行);
- 缓存命中**不再调算路**(mock 断言 compute_base 调用次数);
- TTL 过期 → 视为 miss → 重算;
- 个性化叠加在缓存之上, **不污染全局缓存行**;
- cached / computed_at 字段契约(hit 与 miss);
- 缺数据显式(不编造);
- 批算单标的失败不拖垮整批; 目标集去重.

禁真网络: `decision_cache.compute_base` 一律以 stub 替换(不触 K 线/资金源)。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from src.core import decision_cache as dc


@pytest.fixture(autouse=True)
def _clean_cache():
    dc.clear_decision_cache()
    yield
    dc.clear_decision_cache()


def _base(**over):
    b = {
        "symbol": "600519", "verdict": "动手", "reason": "动手: 趋势G信号",
        "phase": "向好", "row": 1,
        "parts": {"trend": "G信号", "activity": 5.0, "fund_net": 1e8},
        "last_close": 11.0,
    }
    b.update(over)
    return b


def _client(user):
    """带鉴权替身的 TestClient(真实 SessionLocal get_db)。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.web.api import decision as decision_api
    from src.web.api.auth import get_current_user
    from src.web.database import SessionLocal, get_db

    app = FastAPI()
    app.include_router(decision_api.router, prefix="/d")
    app.dependency_overrides[get_current_user] = lambda: user

    def _db():
        s = SessionLocal()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _db
    return TestClient(app, raise_server_exceptions=False)


def _owner():
    from factories import make_user

    return make_user(username=f"dc_{uuid.uuid4().hex[:8]}", role="owner")


# ─────────────────────────── A. 落库/读库基础 ───────────────────────────
def test_put_get_roundtrip_and_idempotent():
    assert dc.put_cached_decision("600519", "CN", _base(), ttl_s=300, source=dc.SOURCE_TTL)
    rec = dc.get_cached_decision("600519", "CN")
    assert rec and rec["payload"]["verdict"] == "动手"
    assert rec["source"] == dc.SOURCE_TTL
    assert rec["computed_at"].endswith("Z")
    assert rec["ttl_s"] == 300

    # 幂等: 同 (symbol, market) 再写只 UPSERT, 不新增行
    dc.put_cached_decision("600519", "CN", _base(verdict="看看"), ttl_s=300)
    assert dc.get_cached_decision("600519", "CN")["payload"]["verdict"] == "看看"
    assert dc.clear_decision_cache() == 1


def test_get_unknown_symbol_is_miss():
    assert dc.get_cached_decision("999999", "CN") is None


def test_get_expired_is_miss(monkeypatch):
    """过期行视为 miss(不返回陈旧结论)。"""
    past = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=2)
    monkeypatch.setattr(dc, "now_utc_naive", lambda: past)
    dc.put_cached_decision("600519", "CN", _base(), ttl_s=300)
    monkeypatch.undo()  # 恢复真实 now
    assert dc.get_cached_decision("600519", "CN") is None


def test_market_is_part_of_key():
    dc.put_cached_decision("00700", "HK", _base(symbol="00700"), ttl_s=300)
    assert dc.get_cached_decision("00700", "HK") is not None
    assert dc.get_cached_decision("00700", "US") is None


# ─────────────────────────── B. 端点: 命中/字段契约 ───────────────────────────
def test_cache_hit_skips_recompute(monkeypatch):
    calls = {"n": 0}

    def _compute(symbol, market="CN", days=120):
        calls["n"] += 1
        return _base(symbol=symbol)

    monkeypatch.setattr(dc, "compute_base", _compute)
    c = _client(_owner())

    r1 = c.get("/d/600519?market=CN")
    assert r1.status_code == 200, r1.text
    b1 = r1.json()
    assert b1["cached"] is False and b1["computed_at"].endswith("Z")
    assert calls["n"] == 1

    r2 = c.get("/d/600519?market=CN")
    b2 = r2.json()
    assert b2["cached"] is True
    assert calls["n"] == 1, "缓存命中不得再次调算路"
    assert b2["computed_at"] == b1["computed_at"]  # computed_at = 计算时刻, 命中不变


def test_miss_then_hit_fields_contract(monkeypatch):
    monkeypatch.setattr(dc, "compute_base", lambda s, m="CN", d=120: _base(symbol=s))
    c = _client(_owner())
    miss = c.get("/d/600519").json()
    assert set(["cached", "computed_at", "cache_source"]).issubset(miss.keys())
    assert miss["cached"] is False and miss["cache_source"] == dc.SOURCE_TTL
    hit = c.get("/d/600519").json()
    assert hit["cached"] is True
    assert hit["cache_source"] == dc.SOURCE_TTL


def test_endpoint_500_on_compute_failure(monkeypatch):
    def _boom(symbol, market="CN", days=120):
        raise RuntimeError("nope")

    monkeypatch.setattr(dc, "compute_base", _boom)
    c = _client(_owner())
    r = c.get("/d/600519")
    assert r.status_code == 500 and "决策计算失败" in r.text


# ─────────────────────────── C. 个性化叠加不污染全局 ───────────────────────────
def test_overlay_on_cache_hit_does_not_pollute_global(monkeypatch):
    from types import SimpleNamespace

    # 全局基底先落库(无用户维度, 动手 row3 —— 保守型会收紧为非共振『看看』)
    dc.put_cached_decision("600519", "CN", _base(verdict="动手", row=3), ttl_s=300)

    def _no_compute(*a, **k):  # 命中不该重算
        raise AssertionError("缓存命中不得重算")

    monkeypatch.setattr(dc, "compute_base", _no_compute)

    # 用户 A: 全持仓 long → 保守型
    uid = str(uuid.uuid4())
    user = SimpleNamespace(id=uid, username="c-owner", role="owner")
    from src.web.database import SessionLocal

    with SessionLocal() as s:
        from factories import make_user
        from src.web.models import Account, Position, Stock

        s.add(make_user(id=uid, username="c-owner", role="owner"))
        s.flush()
        stk = Stock(user_id=uid, symbol="600519", name="x", market="CN")
        s.add(stk)
        s.flush()
        acc = Account(user_id=uid, name="a", available_funds=0.0)
        s.add(acc)
        s.flush()
        s.add(Position(user_id=uid, account_id=acc.id, stock_id=stk.id,
                       cost_price=10.0, quantity=100, trading_style="long"))
        s.commit()

    c = _client(user)
    body = c.get("/d/600519?market=CN").json()
    assert body["cached"] is True
    assert body["risk_profile"] == "conservative"
    assert body["verdict"] == "看看"  # 保守型把非共振行3的『动手』收紧
    assert body["position"]["cost_price"] == 10.0
    assert body["position"]["last_close"] == 11.0
    assert body["position"]["pnl_pct"] == pytest.approx(10.0)

    # 全局缓存行**未被个性化污染**(仍是动手、无 personalized 字段)
    rec = dc.get_cached_decision("600519", "CN")
    assert rec["payload"]["verdict"] == "动手"
    assert "personalized" not in rec["payload"]
    assert "risk_profile" not in rec["payload"]


def test_overlay_without_context_is_field_identical_to_base():
    base = _base()
    out = dc.apply_user_overlay(base, None)
    assert out == base
    assert out is not base  # 深拷贝, 不共享引用


# ─────────────────────────── D. 缺数据显式(不编造) ───────────────────────────
def test_missing_data_is_explicit_not_fabricated(monkeypatch):
    def _compute(symbol, market="CN", days=120):
        return {
            "symbol": symbol, "verdict": "看看",
            "reason": "看看: 无 K 线数据, 先别动手", "phase": "无", "row": 0,
            "parts": {"trend": "无数据", "activity": None, "fund_net": None},
            "last_close": None,
        }

    monkeypatch.setattr(dc, "compute_base", _compute)
    c = _client(_owner())
    body = c.get("/d/600519").json()
    assert body["verdict"] == "看看"
    assert "无 K 线数据" in body["reason"]
    # 缺数据也落库(带上"无数据"语义), 命中时仍显式
    rec = dc.get_cached_decision("600519", "CN")
    assert "无 K 线数据" in rec["payload"]["reason"]


# ─────────────────────────── E. 批算 ───────────────────────────
def test_precompute_batch_idempotent(monkeypatch):
    from src.core import decision_precompute as dp

    monkeypatch.setattr(
        dc, "compute_base",
        lambda s, m="CN", d=120: {"symbol": s, "verdict": "看看", "reason": "r", "last_close": 10.0},
    )
    syms = [("600519", "CN"), ("000001", "CN")]
    out1 = dp.precompute(symbols=syms)
    assert out1["computed"] == 2 and out1["failed"] == 0
    assert out1["ttl_s"] == dc.precompute_ttl_s()
    # 再跑一次: 幂等 upsert, 仍只有两行
    out2 = dp.precompute(symbols=syms)
    assert out2["computed"] == 2
    assert dc.clear_decision_cache() == 2


def test_precompute_isolates_single_failure(monkeypatch):
    from src.core import decision_precompute as dp

    def _c(s, m="CN", d=120):
        if s == "BAD":
            raise RuntimeError("boom")
        return {"symbol": s, "verdict": "看看"}

    monkeypatch.setattr(dc, "compute_base", _c)
    out = dp.precompute(symbols=[("OK", "CN"), ("BAD", "CN")])
    assert out["computed"] == 1 and out["failed"] == 1
    assert out["errors"] and out["errors"][0].startswith("BAD")


def test_precompute_source_marked(monkeypatch):
    from src.core import decision_precompute as dp

    monkeypatch.setattr(dc, "compute_base", lambda s, m="CN", d=120: {"symbol": s, "verdict": "看看"})
    dp.precompute(symbols=[("600519", "CN")])
    rec = dc.get_cached_decision("600519", "CN")
    assert rec["source"] == dc.SOURCE_PRECOMPUTE


class _FakeQ:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeDB:
    def __init__(self, rows):
        self._rows = rows

    def query(self, *a, **k):
        return _FakeQ(self._rows)


def test_collect_targets_dedup_and_normalize():
    from src.core import decision_precompute as dp

    db = _FakeDB([("600519", "CN"), ("600519", "cn"), ("000001", None), ("", "CN"), ("AAPL", "US")])
    assert dp.collect_targets(db) == [("000001", "CN"), ("600519", "CN"), ("AAPL", "US")]


def test_precompute_trigger_requires_owner(monkeypatch):
    from factories import make_user

    calls = {"n": 0}
    import src.core.decision_precompute as dp

    monkeypatch.setattr(dp, "run_precompute_job", lambda: calls.__setitem__("n", calls["n"] + 1))

    member = make_user(username=f"m_{uuid.uuid4().hex[:8]}", role="member")
    r = _client(member).post("/d/precompute")
    assert r.status_code == 403
    assert calls["n"] == 0

    owner = _owner()
    r2 = _client(owner).post("/d/precompute")
    assert r2.status_code == 200 and r2.json()["started"] is True
