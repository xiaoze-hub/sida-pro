"""可转债雷达 API 层单测(2026-10-18)。

守四件事:
① 可用载荷进 `biz_cache`, 第二次请求命中缓存且带 `cached/age_s`(不打数据源);
② **降级载荷不进缓存** —— 单次抖动不能被钉住整个 TTL;
③ 数据源异常时接口返回 200 + `available=false` + note, 绝不 500;
④ 读穿透下 `refresh` 仍能绕过缓存与读库(运维强制刷新)。

注: 2026-10-18 P0-a 起 API 的取数入口由 `build_radar` 换成 `get_radar`
(读库优先 + 回源落库), 本文件的桩随之改为 patch `get_radar`。
"""
from __future__ import annotations

import pytest

from src.web.api import cb_radar as api


@pytest.fixture(autouse=True)
def _clean_cache():
    from src.web.cache.biz_cache import biz_cache

    for limit in (5, 10, 60, 200):
        try:
            biz_cache.delete(f"cb_radar:snapshot:{limit}")
        except Exception:  # noqa: BLE001
            pass
    yield


def _payload(rows=1):
    rows_data = [
        {
            "code": "113550",
            "name": "转债一号",
            "stock_code": "600111",
            "bond_price": 118.5,
            "premium_pct": -1.5,
            "double_low": 117.0,
            "scenarios": [],
        }
    ][:rows]
    return {
        "available": True,
        "data_date": "2026-10-16",
        "trade_phase": "post_close",
        "is_live": False,
        "as_of": "2026-10-18T15:00:00+08:00",
        "rows": rows_data,
        "universe": 500,
        "evaluated": len(rows_data),
        "scenario_counts": {},
        "note": None,
    }


def test_snapshot_caches_available_payload(monkeypatch):
    calls = {"n": 0}

    def _build(limit=60, refresh=False):
        calls["n"] += 1
        return _payload()

    monkeypatch.setattr(api, "get_radar", _build)
    r1 = api.snapshot(limit=10, refresh=False)
    r2 = api.snapshot(limit=10, refresh=False)

    assert calls["n"] == 1
    assert r1["cached"] is False and r2["cached"] is True
    assert r2["rows"][0]["code"] == "113550"
    assert r2["age_s"] >= 0
    assert "_cached_at" not in r2  # 内部缓存键不外泄


def test_snapshot_refresh_bypasses_cache(monkeypatch):
    calls = {"n": 0}

    def _build(limit=60, refresh=False):
        calls["n"] += 1
        return _payload()

    monkeypatch.setattr(api, "get_radar", _build)
    api.snapshot(limit=10, refresh=False)
    api.snapshot(limit=10, refresh=True)
    assert calls["n"] == 2


def test_snapshot_does_not_cache_degraded_payload(monkeypatch):
    calls = {"n": 0}

    def _build(limit=60, refresh=False):
        calls["n"] += 1
        return {"available": False, "rows": [], "note": "转债代码表不可用", "scenario_counts": {}}

    monkeypatch.setattr(api, "get_radar", _build)
    r1 = api.snapshot(limit=10, refresh=False)
    r2 = api.snapshot(limit=10, refresh=False)
    assert calls["n"] == 2  # 没有被缓存钉死
    assert r1["available"] is False and r2["available"] is False


def test_snapshot_swallows_exceptions(monkeypatch):
    def _boom(limit=60, refresh=False):
        raise RuntimeError("tq down")

    monkeypatch.setattr(api, "get_radar", _boom)
    r = api.snapshot(limit=10, refresh=False)
    assert r["available"] is False
    assert "tq down" in r["note"]


def test_bond_detail_swallows_exceptions(monkeypatch):
    def _boom(code):
        raise RuntimeError("gateway timeout")

    monkeypatch.setattr(api, "build_one", _boom)
    r = api.bond_detail("113550")
    assert r["available"] is False and "gateway timeout" in r["note"]


def test_bond_detail_ok(monkeypatch):
    monkeypatch.setattr(api, "build_one", lambda code: {"available": True, "code": code, "row": {"code": code}})
    r = api.bond_detail("113550")
    assert r["available"] is True and r["row"]["code"] == "113550"
