# -*- coding: utf-8 -*-
"""`/api/stocks/{symbol}/l2` 无数据降级契约回归(P1, 2026-10-10)。

真 bug: 上游(TQ 网关)不可用时 `fetch_stock_l2` 未捕获异常 → 端点裸 500
(body='Internal Server Error', 连 JSON envelope 都没走), 拖垮工作台/个股页/
行情页共 4 个路由(页面 L2 区全 `--` + spinner 常转)。

铁律『数据缺失显式降级、永不 500』: 三态(无数据 / 非交易日空载荷 / 上游异常)
都必须 200 + available:false + note(真实原因); 成功 → available:true。
**不吞异常返假数据**。

全 mock, 禁真实网络(不触碰 TQ 网关 / 不发任何请求)。
"""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.core import stock_l2 as l2mod  # noqa: E402


# ─────────────── ① 核心层: 三态永不抛, 返回 (data, reason) ───────────────

@pytest.mark.parametrize(
    "snap_val,more_val,label",
    [
        ({}, {}, "no_data"),                      # 无数据: 两源皆空
        ([], [], "non_trading_empty_payload"),    # 非交易日: 网关回空载荷(非 dict)
    ],
)
def test_empty_source_states_degrade(monkeypatch, snap_val, more_val, label):
    """无数据 / 非交易日空载荷 → ({}, reason), 不抛。"""
    monkeypatch.setattr(l2mod, "_rpc_snap", lambda *a, **k: snap_val)
    monkeypatch.setattr(l2mod, "_rpc_more", lambda *a, **k: more_val)

    data, reason = l2mod.fetch_stock_l2_ex("002361")

    assert data == {}, label
    assert reason, label
    # 批量入口依赖的 fetch_stock_l2 同样不抛、返回 {}
    assert l2mod.fetch_stock_l2("002361") == {}


def test_upstream_exception_degrades_no_raise(monkeypatch):
    """上游异常(TQ 网关不可用) → 不抛, 带回真实原因(曾经是裸 500 的根因)。"""
    def _boom(*a, **k):
        raise RuntimeError("TQ 网关不可用: 候选地址探测全灭")

    monkeypatch.setattr(l2mod, "_rpc_snap", _boom)
    monkeypatch.setattr(l2mod, "_rpc_more", _boom)

    data, reason = l2mod.fetch_stock_l2_ex("002361")

    assert data == {}
    assert reason
    assert "源不可用" in reason and "RuntimeError" in reason
    assert l2mod.fetch_stock_l2("002361") == {}


def test_more_source_exception_degrades(monkeypatch):
    """仅 more_info 源抛异常 → 仍降级不抛, reason 指明 more_info。"""
    monkeypatch.setattr(l2mod, "_rpc_snap", lambda *a, **k: {})
    monkeypatch.setattr(
        l2mod, "_rpc_more", lambda *a, **k: (_ for _ in ()).throw(OSError("more down"))
    )

    data, reason = l2mod.fetch_stock_l2_ex("002361")

    assert data == {}
    assert reason
    assert "more_info" in reason


def test_partial_snapshot_ok_more_fails(monkeypatch):
    """snapshot 有值 + more 源异常 → 返回部分数据并标注原因(不编造 more)。"""
    monkeypatch.setattr(
        l2mod, "_rpc_snap", lambda *a, **k: {"Now": "10.0", "LastClose": "9.9"}
    )
    monkeypatch.setattr(
        l2mod, "_rpc_more", lambda *a, **k: (_ for _ in ()).throw(OSError("more down"))
    )

    data, reason = l2mod.fetch_stock_l2_ex("002361")

    assert data["snapshot"]["now"] == 10.0
    assert data["more"] == {}
    assert reason


def test_batch_never_raises(monkeypatch):
    """候选池批量入口: 上游全灭也不抛(ex.map 曾会把单股异常反抛主线程)。"""
    def _boom(*a, **k):
        raise RuntimeError("down")

    monkeypatch.setattr(l2mod, "_rpc_snap", _boom)
    monkeypatch.setattr(l2mod, "_rpc_more", _boom)

    assert l2mod.fetch_stock_l2_batch(["002361", "600519"]) == {}


# ─────────────── ② 端点级: 三态返回 200(不是裸 500) ───────────────

def _build_client(monkeypatch):
    """搭最小 FastAPI app + 覆盖鉴权/DB 依赖 + 停用权限/调用日志副作用。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.core import hv_api_log, permissions
    from src.web.api import stocks as stocks_api
    from src.web.api.auth import get_current_user
    from src.web.database import get_db

    monkeypatch.setattr(permissions, "enforce_perm", lambda *a, **k: None)
    monkeypatch.setattr(hv_api_log, "log_high_value_call", lambda *a, **k: None)

    app = FastAPI()
    app.include_router(stocks_api.router, prefix="/api/stocks")
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=1)
    app.dependency_overrides[get_db] = lambda: None
    return TestClient(app)


def test_endpoint_upstream_exception_200_not_500(monkeypatch):
    """回归针: 上游异常时 HTTP 200 + available:false(不是裸 500)。"""
    def _boom(*a, **k):
        raise RuntimeError("TQ 网关不可用")

    monkeypatch.setattr(l2mod, "_rpc_snap", _boom)
    monkeypatch.setattr(l2mod, "_rpc_more", _boom)

    resp = _build_client(monkeypatch).get("/api/stocks/002361/l2")

    assert resp.status_code == 200, resp.text          # 曾为 500
    body = resp.json()                                  # 是 JSON envelope, 不是裸文本
    assert body["available"] is False
    assert body["note"]
    assert body["symbol"] == "002361"


def test_endpoint_no_data_200_available_false(monkeypatch):
    """无数据 → HTTP 200 + available:false + note。"""
    monkeypatch.setattr(l2mod, "_rpc_snap", lambda *a, **k: {})
    monkeypatch.setattr(l2mod, "_rpc_more", lambda *a, **k: {})

    resp = _build_client(monkeypatch).get("/api/stocks/002361/l2")

    assert resp.status_code == 200
    assert resp.json()["available"] is False


def test_endpoint_success_available_true(monkeypatch):
    """成功路径 → HTTP 200 + available:true, 字段不变(仅加 available)。"""
    monkeypatch.setattr(
        l2mod, "_rpc_snap", lambda *a, **k: {"Now": "10.0", "LastClose": "9.9"}
    )
    monkeypatch.setattr(l2mod, "_rpc_more", lambda *a, **k: {"ZTPrice": "11.0"})

    resp = _build_client(monkeypatch).get("/api/stocks/002361/l2")

    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is True
    assert body["snapshot"]["now"] == 10.0
    assert body["more"]["zt_price"] == 11.0


def test_endpoint_partial_source_note_surfaces_reason(monkeypatch):
    """snapshot 有值 + more 源异常 → 200 + available:true + note 带真实原因(不静默)。"""
    monkeypatch.setattr(
        l2mod, "_rpc_snap", lambda *a, **k: {"Now": "10.0", "LastClose": "9.9"}
    )
    monkeypatch.setattr(
        l2mod, "_rpc_more", lambda *a, **k: (_ for _ in ()).throw(OSError("more down"))
    )

    resp = _build_client(monkeypatch).get("/api/stocks/002361/l2")

    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is True
    assert body["note"] and "more_info" in body["note"]
