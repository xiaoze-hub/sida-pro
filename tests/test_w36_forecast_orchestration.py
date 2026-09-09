"""W3.6/D5 编排行为测试: 8000→8010 单向推送 + 双侧优雅降级。

对应方案 3.6 验收:
- 停掉 8000 → 8010 的 POST /predict 仍能正常返回(llm_config 随请求体推送,
  不依赖任何对 8000 的回调);
- 停掉 8010 → 8000 的 forecast 代理端点优雅降级(503 明确文案, 不是 500 堆栈),
  且 /api/health components.forecast_engine 反映出来。

全部 mock 网络, 不触真实 8010/8000/LLM。
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
# forecast_server direct-run 语义: forecast_lib/ 亦在 sys.path, bare `forecast_sentiment`
# 与 `forecast_lib.forecast_sentiment` 是两个模块实例 —— 生产消费的是 bare 实例。
if str(ROOT / "forecast_lib") not in sys.path:
    sys.path.insert(0, str(ROOT / "forecast_lib"))


@pytest.fixture(autouse=True)
def _clear_runtime_llm_config():
    for name in ("forecast_sentiment", "forecast_lib.forecast_sentiment"):
        mod = sys.modules.get(name)
        if mod is not None and hasattr(mod, "RUNTIME_LLM_CONFIG"):
            mod.RUNTIME_LLM_CONFIG.clear()
    yield
    for name in ("forecast_sentiment", "forecast_lib.forecast_sentiment"):
        mod = sys.modules.get(name)
        if mod is not None and hasattr(mod, "RUNTIME_LLM_CONFIG"):
            mod.RUNTIME_LLM_CONFIG.clear()


@pytest.fixture
def engine_client(monkeypatch):
    """8010 forecast_server 的隔离 TestClient, _predict_with_guard 捕获入参。"""
    import forecast_server
    from fastapi.testclient import TestClient

    captured: dict = {}

    def fake_guard(symbol, days, task_id, target_date, force):
        captured["args"] = (symbol, days, task_id, target_date, force)
        return {"ok": True, "symbol": symbol}

    monkeypatch.setattr(forecast_server, "_predict_with_guard", fake_guard)
    return TestClient(forecast_server.app), captured


# ---------- 8010: POST /predict 编排入口 ----------

def test_post_predict_pushes_llm_config(engine_client):
    """llm_config 随 body 推送, 落入 RUNTIME_LLM_CONFIG(8010 不回调 8000 取配置)。"""
    import forecast_sentiment

    client, captured = engine_client
    resp = client.post(
        "/predict",
        json={
            "symbol": "600519",
            "days": 7,
            "task_id": "t-1",
            "target_date": "2026-09-10",
            "force": True,
            "llm_config": {"base_url": "https://pushed.example/v1", "model": "pushed-model", "api_key": "k-push"},
        },
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "symbol": "600519"}
    assert captured["args"] == ("600519", 7, "t-1", "2026-09-10", True)
    assert forecast_sentiment.RUNTIME_LLM_CONFIG == {
        "base_url": "https://pushed.example/v1",
        "model": "pushed-model",
        "api_key": "k-push",
    }


def test_post_predict_without_llm_config_clears_runtime(engine_client):
    """无 llm_config 时 runtime 配置清空(GET 独立进程语义: 走本地 env 兜底)。"""
    import forecast_sentiment

    forecast_sentiment.set_runtime_llm_config({"base_url": "stale", "model": "stale", "api_key": "stale"})
    client, captured = engine_client
    resp = client.post("/predict", json={"symbol": "600519", "days": 5})
    assert resp.status_code == 200
    assert captured["args"] == ("600519", 5, "", "", False)
    assert forecast_sentiment.RUNTIME_LLM_CONFIG == {}


def test_post_predict_invalid_days(engine_client):
    client, _ = engine_client
    resp = client.post("/predict", json={"symbol": "600519", "days": "abc"})
    assert resp.status_code == 400


def test_get_predict_still_works_and_keeps_runtime_config(engine_client):
    """GET /predict(systemd/手动) 仍可用, 且不触碰 runtime 配置。"""
    import forecast_sentiment

    forecast_sentiment.set_runtime_llm_config({"api_key": "kept"})
    client, captured = engine_client
    resp = client.get("/predict", params={"symbol": "600519", "days": 3})
    assert resp.status_code == 200
    assert captured["args"] == ("600519", 3, "", "", False)
    assert forecast_sentiment.RUNTIME_LLM_CONFIG == {"api_key": "kept"}


def test_pushed_llm_config_wins_over_local_env(monkeypatch):
    """推送配置优先级最高: RUNTIME_LLM_CONFIG 非空时 _load_llm_config 不读本地文件。"""
    import forecast_sentiment

    monkeypatch.setattr(
        "os.path.exists",
        lambda p: False if ("panwatch_forecast" in str(p) or "agnes_key" in str(p)) else bool(p),
    )
    forecast_sentiment.set_runtime_llm_config(
        {"base_url": "https://pushed.example/v1", "model": "pushed-model", "api_key": "k-push"}
    )
    cfg = forecast_sentiment._load_llm_config()
    assert cfg["base_url"] == "https://pushed.example/v1"
    assert cfg["model"] == "pushed-model"
    assert cfg["api_key"] == "k-push"


# ---------- 8000: 代理端点优雅降级 + 健康反映 ----------

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


def test_forecast_proxy_degrades_503_on_engine_down(monkeypatch, main_client):
    """8010 停机(ConnectError) → 503 + 明确文案, 不是 500 堆栈(验收项)。"""
    from src.web.api import forecast as forecast_api

    class _Exploding:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            raise httpx.ConnectError("connection refused")

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(forecast_api.httpx, "AsyncClient", _Exploding)
    resp = main_client.get("/api/forecast/predict", params={"symbol": "600519", "days": 5})
    assert resp.status_code == 503
    assert "预测引擎不可用(需在主机运行 forecast_server.py)" in resp.json()["detail"]


def test_forecast_llm_payload_from_settings(monkeypatch):
    """_forecast_llm_payload: forecast_llm_* 三键归一为 base_url/model/api_key。"""
    from src.web.api import forecast as forecast_api
    from src.web.models import AppSettings

    rows = [
        AppSettings(key="forecast_llm_base_url", value="https://s.example/v1"),
        AppSettings(key="forecast_llm_model", value="m1"),
        AppSettings(key="forecast_llm_unrelated", value="x"),
    ]

    class _Q:
        def filter(self, *a, **k):
            return self

        def all(self):
            return rows

    class _DB:
        def query(self, *a, **k):
            return _Q()

    payload = forecast_api._forecast_llm_payload(_DB())  # type: ignore[arg-type]
    assert payload == {"base_url": "https://s.example/v1", "model": "m1", "api_key": ""}


@pytest.fixture(autouse=True)
def _reset_probe_cache():
    from src.web.api import health as health_api

    health_api._FORECAST_PROBE_CACHE.update({"ts": 0.0, "detail": {"status": "unknown"}})
    yield
    health_api._FORECAST_PROBE_CACHE.update({"ts": 0.0, "detail": {"status": "unknown"}})


def test_health_forecast_engine_probe_down(monkeypatch):
    """8010 停机 → probe=down + record_component_status(False)(验收项 /api/health 反映)。"""
    from src.web.api import health as health_api

    calls: list = []
    monkeypatch.setattr(health_api, "record_component_status", lambda c, ok: calls.append((c, ok)))
    monkeypatch.setattr("httpx.get", lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectError("refused")))

    detail = health_api._forecast_engine_probe()
    assert detail["status"] == "down"
    assert "refused" in detail["error"]
    assert calls == [("forecast_engine", False)]


def test_health_forecast_engine_probe_ok(monkeypatch):
    from src.web.api import health as health_api

    calls: list = []
    monkeypatch.setattr(health_api, "record_component_status", lambda c, ok: calls.append((c, ok)))
    monkeypatch.setattr("httpx.get", lambda *a, **k: SimpleNamespace(status_code=200))

    detail = health_api._forecast_engine_probe()
    assert detail == {"status": "ok", "url": health_api._FORECAST_PROBE_CACHE["detail"]["url"]}
    assert calls == [("forecast_engine", True)]


def test_health_forecast_engine_probe_cached(monkeypatch):
    """30s 缓存窗口内不重复探测。"""
    import time as _t

    from src.web.api import health as health_api

    health_api._FORECAST_PROBE_CACHE.update({"ts": _t.time(), "detail": {"status": "ok", "url": "cached"}})
    monkeypatch.setattr(
        "httpx.get", lambda *a, **k: (_ for _ in ()).throw(AssertionError("缓存窗口内不得再探测"))
    )
    assert health_api._forecast_engine_probe() == {"status": "ok", "url": "cached"}
