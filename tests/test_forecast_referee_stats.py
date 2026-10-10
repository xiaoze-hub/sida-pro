"""P0-2 回归: AI 裁判战绩端点(engine /referee/stats + 8000 代理 /forecast/referee-stats)。

背景(全链路审计 P0-2「AI 裁判零消费」): 预测引擎裁判结论此前无任何 UI 消费;
本任务新增引擎统计端点 `referee_impact_stats` 的 HTTP 出口与 8000 代理, 供前端
「裁判战绩」卡片读取介入前后命中率。

验收(禁真网络):
  ① 引擎 `/referee/stats`: 透传 referee_impact_stats 结果 + symbol 过滤;
  ② 引擎统计异常 → 显式 no-data(total=0 + message), **200 永不 500**;
  ③ 8000 代理: 引擎有数据 → 原样代理;
  ④ 8000 代理: 引擎停机(ConnectError)/异常 → **200 + 显式 no-data**, 绝不 500
     (契约: 统计是增量信息, 缺数据显式, 不伪造命中率)。
"""
from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "forecast_lib") not in sys.path:
    sys.path.insert(0, str(ROOT / "forecast_lib"))


# ── 引擎侧 /referee/stats ────────────────────────────────────────────────

@pytest.fixture
def engine_client():
    import forecast_server
    from fastapi.testclient import TestClient

    return TestClient(forecast_server.app)


def test_engine_referee_stats_passes_through(engine_client, monkeypatch):
    """引擎透传 referee_impact_stats 结果 + symbol 过滤。"""
    import forecast_lib.ai_referee as ar

    seen: dict = {}

    def _fake(symbol: str = ""):
        seen["symbol"] = symbol
        return {
            "total": 5,
            "symbol": symbol or "all",
            "confirm_count": 3,
            "adjust_count": 2,
            "baseline_accuracy": 40.0,
            "referee_accuracy": 60.0,
            "delta_accuracy_pct": 20.0,
            "confirm_accuracy": 33.3,
            "adjust_accuracy": 100.0,
            "samples": [],
        }

    monkeypatch.setattr(ar, "referee_impact_stats", _fake)
    resp = engine_client.get("/referee/stats", params={"symbol": "600519"})

    assert resp.status_code == 200
    body = resp.json()
    assert seen["symbol"] == "600519"
    assert body["total"] == 5
    assert body["referee_accuracy"] == 60.0


def test_engine_referee_stats_never_500_on_error(engine_client, monkeypatch):
    """统计异常 → 显式 no-data(total=0 + message), 200 永不 500。"""
    import forecast_lib.ai_referee as ar

    def _boom(symbol: str = ""):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(ar, "referee_impact_stats", _boom)
    resp = engine_client.get("/referee/stats", params={"symbol": "000001"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 0
    assert body["symbol"] == "000001"
    assert "db exploded" in body["message"]


# ── 8000 代理 /forecast/referee-stats ────────────────────────────────────

@pytest.fixture
def main_client():
    """8000 forecast router 隔离 app(不 import 整个 src.web.app)。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.web.api import forecast as forecast_api
    from src.web.database import get_db

    app = FastAPI()
    app.include_router(forecast_api.router, prefix="/api")
    app.dependency_overrides[get_db] = lambda: object()
    return TestClient(app)


class _FakeResp:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"engine HTTP {self.status_code}")

    def json(self):
        return self._payload


class _FakeAsyncClient:
    def __init__(self, payload=None, exc=None, status_code=200, **kwargs):
        self._payload = payload
        self._exc = exc
        self._status_code = status_code
        self.calls: list = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, params=None):
        self.calls.append({"url": url, "params": params})
        if self._exc:
            raise self._exc
        return _FakeResp(self._payload, self._status_code)


def test_proxy_referee_stats_passes_through(monkeypatch, main_client):
    """引擎有数据 → 8000 原样代理(命中率/样本数透传)。"""
    from src.web.api import forecast as forecast_api

    engine_payload = {
        "total": 12,
        "symbol": "600519",
        "confirm_count": 10,
        "adjust_count": 2,
        "baseline_accuracy": 50.0,
        "referee_accuracy": 66.7,
        "delta_accuracy_pct": 16.7,
        "adjust_accuracy": None,
        "confirm_accuracy": 60.0,
        "samples": [],
    }
    holder: dict = {}

    def _factory(**kw):
        c = _FakeAsyncClient(payload=engine_payload)
        holder["client"] = c
        return c

    monkeypatch.setattr(forecast_api.httpx, "AsyncClient", _factory)
    resp = main_client.get("/api/forecast/referee-stats", params={"symbol": "600519"})

    assert resp.status_code == 200
    assert resp.json()["referee_accuracy"] == 66.7
    # 代理确实带上了 symbol 查询参数
    assert holder["client"].calls[0]["params"] == {"symbol": "600519"}
    assert holder["client"].calls[0]["url"].endswith("/referee/stats")


def test_proxy_referee_stats_never_500_on_engine_down(monkeypatch, main_client):
    """引擎停机(ConnectError) → 200 + 显式 no-data message, 绝不 500。"""
    from src.web.api import forecast as forecast_api

    monkeypatch.setattr(
        forecast_api.httpx,
        "AsyncClient",
        lambda **kw: _FakeAsyncClient(exc=httpx.ConnectError("connection refused")),
    )
    resp = main_client.get("/api/forecast/referee-stats", params={"symbol": "600519"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 0
    assert body["symbol"] == "600519"
    assert "预测引擎不可用" in body["message"]


def test_proxy_referee_stats_never_500_on_unexpected_error(monkeypatch, main_client):
    """代理层意外异常 → 200 + 显式 message, 绝不 500。"""
    from src.web.api import forecast as forecast_api

    monkeypatch.setattr(
        forecast_api.httpx,
        "AsyncClient",
        lambda **kw: _FakeAsyncClient(exc=ValueError("bad gateway")),
    )
    resp = main_client.get("/api/forecast/referee-stats")

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 0
    assert body["symbol"] == "all"
    assert "bad gateway" in body["message"]
