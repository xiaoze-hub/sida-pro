"""读库异常不得冒充"无快照"(2026-11-09)。

真实隔离 HTTP + 缓存端到端反例(已实测复现):
  注入 `engine.connect()` 抛错时 `GET /api/market-data/dragon-tiger/{date}/quality`
  回 `HTTP 200 + status=absent`(无快照); 旧缓存里 `count>0` 时同样被报成"无快照",
  且 `quality_note` 里带着底层异常原文(连接串用户名/host 一并外泄)。

本文件锁定的契约(分层):
  ① 语义层: **查询成功且零行** 才是 `absent`; 读库失败是 `unavailable`(不可判定);
     定性整体没跑成是 `unknown`。三者互不冒充。
  ② 传输层: `/quality` 在 `unavailable` 时给 503, 不复用 200 载荷冒充"无快照";
     日榜缓存路径保住 `count/items/source` 旧键, 只把质量态改成 `unavailable`。
  ③ 泄露层: 任何响应字段(含 note/evidence/quality)都不得出现底层异常原文、
     连接串、用户名、口令、host —— 只允许异常类名级别的粗粒度原因码。
  ④ 只读门禁: 读库失败/成功两条路径都只发 SELECT。
  ⑤ 无定性注入仍显式报错, 不得静默按无快照处理。

全部禁真实网络(vendor 打桩, 库用内存 SQLite / 断连桩)。
"""
from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.pool import StaticPool

import src.web.api.market_data as mdm
import src.web.database as database
from src.core import lhb_quality as q
from src.web.migrations import _m160_lhb_detail_tables, _m192_dragon_tiger_daily_table

# 反例里的"凭据指纹": 响应出现任一即视为泄露(不打印原串, 避免二次落盘)。
_DSN_USER = "lhb_ro"
_DSN_PASSWORD = "test-only-canary"
_DSN_HOST = "db.internal"
_DSN_SCHEME = "postgresql://"
_LEAK_MARKERS = (_DSN_USER, _DSN_PASSWORD, _DSN_HOST, _DSN_SCHEME, "could not connect to server")


# ─────────────────────────── 夹具 ───────────────────────────


def _mk_engine(with_seats: bool = False) -> object:
    eng = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    with eng.begin() as conn:
        _m192_dragon_tiger_daily_table(conn)
    if with_seats:
        with eng.begin() as conn:
            _m160_lhb_detail_tables(conn)
    return eng


def _add_daily(eng, rows, trade_date: str = "20261019", market: str = "CN"):
    with eng.begin() as conn:
        for sym, ts in rows:
            conn.execute(
                text(
                    "INSERT INTO dragon_tiger_daily "
                    "(trade_date, symbol, market, name, reason, close, net_buy, source, updated_at) "
                    "VALUES (:d, :s, :mkt, '某票', '日涨幅偏离值达7%', 10.0, 1.0, "
                    "'em_ftshare', :ts)"
                ),
                {"d": trade_date, "s": sym, "mkt": market, "ts": ts},
            )


class _BrokenEngine:
    """形态合法但连不上: `connect()` 抛带真实连接串的 DB 异常。"""

    def connect(self):  # noqa: D401 - 模拟 SQLAlchemy engine 接口
        raise OperationalError(
            "SELECT symbol FROM dragon_tiger_daily",
            {},
            Exception(
                "could not connect to server: "
                f"{_DSN_SCHEME}{_DSN_USER}:{_DSN_PASSWORD}@{_DSN_HOST}:5432/sida"
            ),
        )


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(mdm.router, prefix="/api/market")
    return TestClient(app, raise_server_exceptions=False)


def _use_engine(monkeypatch, eng):
    monkeypatch.setattr(database, "engine", eng)


def _use_memory_cache(monkeypatch, seed: dict | None = None) -> dict:
    store: dict = dict(seed or {})
    monkeypatch.setattr(mdm.biz_cache, "get_json", lambda k: store.get(k))
    monkeypatch.setattr(
        mdm.biz_cache, "set_json", lambda k, v, ttl=None, **kw: store.__setitem__(k, v)
    )
    return store


def _walk_strings(node, path: str = "$"):
    """递归产出载荷里所有字符串(含键名), 供泄露扫描。"""
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _walk_strings(k, f"{path}.<key>")
            yield from _walk_strings(v, f"{path}.{k}")
    elif isinstance(node, (list, tuple)):
        for i, v in enumerate(node):
            yield from _walk_strings(v, f"{path}[{i}]")
    elif isinstance(node, str):
        yield path, node


def _assert_no_leak(payload, where: str) -> None:
    blob = json.dumps(payload, ensure_ascii=False, default=str)
    hits = [m for m in _LEAK_MARKERS if m in blob]
    assert not hits, f"{where} 泄露底层异常/连接串片段: {hits}"
    # 异常原文里的自由文本也不该整句透出
    for path, s in _walk_strings(payload):
        assert "Traceback" not in s, f"{where} {path} 透出堆栈"
        assert "\n" not in s or "evidence" in path, f"{where} {path} 含多行异常原文"


def _capture_sql(eng) -> list[str]:
    from sqlalchemy import event

    seen: list[str] = []
    event.listen(eng, "before_cursor_execute", lambda _c, _cur, stmt, *a: seen.append(stmt))
    return seen


# ───────── ① 传输层: /quality 读库失败不得 200 + absent ─────────


def test_HTTP_quality_读库失败_给503而不是无快照(monkeypatch):
    """真实 HTTP 反例: 注入断连 engine, 端点曾回 200 + status=absent。"""
    _use_engine(monkeypatch, _BrokenEngine())
    _use_memory_cache(monkeypatch)

    r = _client().get("/api/market/dragon-tiger/20261019/quality", params={"market": "CN"})
    body = r.json()

    assert r.status_code == 503, f"读库失败必须 503(不给出结论), 实际 {r.status_code}: {body}"
    assert "absent" not in json.dumps(body), "读库失败被报成 absent(无快照)"
    _assert_no_leak(body, "/quality 读库失败响应")


def test_HTTP_quality_查询成功零行_如实absent(monkeypatch):
    """健康库零行仍是 absent —— 修复不得把\"真的没数据\"也推成不可用。"""
    eng = _mk_engine()
    _use_engine(monkeypatch, eng)
    _use_memory_cache(monkeypatch)

    r = _client().get("/api/market/dragon-tiger/20260101/quality", params={"market": "CN"})
    body = r.json()

    assert r.status_code == 200
    assert body["status"] == "absent"
    assert body["severity"] == "missing" and body["complete"] is False


def test_HTTP_quality_健康库_仍照常给结论(monkeypatch):
    eng = _mk_engine(with_seats=True)
    _use_engine(monkeypatch, eng)
    _add_daily(eng, [("600000", "2026-10-19 09:31:00")])
    _use_memory_cache(monkeypatch)

    r = _client().get("/api/market/dragon-tiger/20261019/quality", params={"market": "CN"})
    body = r.json()
    assert r.status_code == 200
    assert body["status"] == "unverified" and body["actual_symbols"] == 1


# ───────── ② 传输层: 旧缓存 count>0 + 读库失败 ─────────


def _legacy_cached_payload() -> dict:
    """上线前的缓存载荷: 无 quality 键, 但 count>0。"""
    return {
        "trade_date": "20261019",
        "market": "CN",
        "count": 2,
        "items": [{"symbol": "600000"}, {"symbol": "000001"}],
        "pending": False,
        "source": "db",
        "data_origin": "dragon_tiger_daily",
    }


def test_HTTP_日榜_旧缓存有数据但读库失败_不得称无快照(monkeypatch):
    _use_engine(monkeypatch, _BrokenEngine())
    _use_memory_cache(monkeypatch, {"mkt:dragon_tiger:CN:20261019": _legacy_cached_payload()})

    r = _client().get("/api/market/dragon-tiger/20261019", params={"market": "CN"})
    body = r.json()

    assert r.status_code == 200
    # 旧键原样(缓存里确有数据, 不能因为定性失败就把数据丢掉)
    assert body["count"] == 2 and len(body["items"]) == 2
    assert body["source"] == "db" and body["pending"] is False
    # 质量态: 不是 absent, 不是 complete, 且明确降级
    assert body["quality_status"] == "unavailable"
    assert body["degraded"] is True and body["verified_complete"] is False
    assert "无快照" not in json.dumps(body), "缓存有数据时不得声称无快照"
    _assert_no_leak(body, "日榜缓存+读库失败响应")


def test_HTTP_日榜_健康库读库路径不受影响(monkeypatch):
    eng = _mk_engine(with_seats=True)
    _use_engine(monkeypatch, eng)
    _add_daily(eng, [("600000", "2026-10-19 09:31:00"), ("000001", "2026-10-19 09:31:00")])
    _use_memory_cache(monkeypatch)

    r = _client().get("/api/market/dragon-tiger/20261019", params={"market": "CN"})
    body = r.json()
    assert r.status_code == 200
    assert body["count"] == 2 and body["source"] == "db"
    assert body["quality_status"] == "unverified" and body["degraded"] is False


# ───────── ③ 语义层: core 三态互不冒充 ─────────


def test_core_读库失败_判unavailable且不带异常详情():
    r = q.assess_date("20261019", "CN", engine=_BrokenEngine())

    assert r["status"] == "unavailable"
    assert r["status"] != "absent"
    assert r["degraded"] is True and r["complete"] is False
    assert r["verification"] != "no_snapshot"
    assert r["severity"] != "missing"
    assert r["actual_symbols"] == 0
    assert "读库" in r["note"]
    _assert_no_leak(r, "core 读库失败定性")


def test_core_表缺失_判unavailable不是absent():
    """新库未迁移/表缺失 = 读不成, 不是"该日没有快照"。"""
    eng = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    r = q.assess_date("20261019", "CN", engine=eng)

    assert r["status"] == "unavailable"
    assert "读库" in r["note"]
    assert any(e.startswith("db_error=") for e in r["evidence"])


def test_core_查询成功零行_才是absent():
    eng = _mk_engine()
    r = q.assess_date("20260101", "CN", engine=eng)

    assert r["status"] == "absent"
    assert r["verification"] == "no_snapshot" and r["severity"] == "missing"
    assert not any(e.startswith("db_error=") for e in r["evidence"])


def test_core_非法交易日_不判无快照():
    eng = _mk_engine()
    r = q.assess_date("not-a-date", "CN", engine=eng)

    assert r["status"] != "absent", "非法入参不得被说成\"该日没有快照\""
    assert r["status"] in {"unknown", "unavailable"}
    assert r["snapshot_read"] == "unknown"


def test_定性整体未跑成_标unknown而不是absent(monkeypatch):
    payload = {
        "trade_date": "20261019",
        "count": 1,
        "items": [{"symbol": "600000"}],
        "pending": False,
        "source": "db",
    }
    monkeypatch.setattr(mdm, "_lhb_snapshot_quality", lambda *a, **k: {})
    out = mdm._with_lhb_quality(payload, "20261019", "CN")

    assert out["quality_status"] == "unknown"
    assert out["quality_status"] != "absent"
    assert out["degraded"] is True and out["verified_complete"] is False
    assert out["count"] == 1 and out["source"] == "db"


# ───────── ④ 只读 + 注入门禁 ─────────


def test_只读_失败路径不得发写SQL(monkeypatch):
    eng = _mk_engine()
    seen = _capture_sql(eng)
    _use_engine(monkeypatch, eng)
    _use_memory_cache(monkeypatch)

    _client().get("/api/market/dragon-tiger/20260101/quality", params={"market": "CN"})

    verbs = {s.lstrip().split(None, 1)[0].upper() for s in seen}
    assert verbs <= {"SELECT"}, f"定性只读, 实际发出 {sorted(verbs)}"


def test_未注入engine_仍显式报错不得静默无快照():
    with pytest.raises(TypeError):
        q.assess_date("20261019")
    with pytest.raises(ValueError, match="显式注入"):
        q.assess_date("20261019", engine=None)


def test_缓存路径不因定性失败改写原始数据(monkeypatch):
    """定性失败只加降级字段, items/count 必须是缓存里的原值(顺序也不动)。"""
    cached = _legacy_cached_payload()
    monkeypatch.setattr(mdm, "_lhb_snapshot_quality", lambda *a, **k: {})
    out = mdm._with_lhb_quality(cached, "20261019", "CN")

    assert out["items"] == cached["items"]
    assert out["count"] == cached["count"]


# ───────── ⑤ events / 单股视角: 同一条"没读到 ≠ 没有"纪律 ─────────


def test_core_events读库失败_判unavailable且不带详情():
    r = q.assess_events_date("20261019", "CN", event_symbols=[], db_error="OperationalError",
                             read_status="failed")

    assert r["status"] == "unavailable"
    assert r["degraded"] is True and r["complete"] is False
    assert r["completeness_claimed"] is False
    _assert_no_leak(r, "events 读库失败定性")


def test_纯函数_全日未读到_本股不可判定而非无记录():
    """全日范围没读到 → recorded=None; 说成 False 等于替调用方断言"本股没上榜"。"""
    r = q.assess_symbol_scope("600000", day_symbols=[], day_status="unavailable")

    assert r["recorded"] is None
    assert "未能读取" in r["note"]


def test_纯函数_全日确实无覆盖仍是不可判定():
    r = q.assess_symbol_scope("600000", day_symbols=[], day_status="absent")
    assert r["recorded"] is None


def test_原因码只留类名_连接串整段丢弃():
    leaky = (
        "could not connect to server: postgresql://lhb_ro:test-only-canary@db.internal:5432/sida"
    )

    class OperationalError(Exception):
        pass

    code = q.safe_error_code(OperationalError(leaky))
    assert code == "OperationalError"
    assert not any(m in code for m in _LEAK_MARKERS)

    assert q.sanitize_reason(leaky) == "read_failed"
    assert q.sanitize_reason("invalid_trade_date") == "invalid_trade_date"
