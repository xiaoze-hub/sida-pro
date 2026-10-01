# -*- coding: utf-8 -*-
"""`/api/orderbook-ob` 上游瞬时抖动治理(B1 P2 间歇 502)单测。

覆盖:
  ① 首次失败 → 有限次重试成功(成功路径返回值逐字段与修改前一致)
  ② 重试仍失败 → 显式降级(返回 dict, available:false + 原因), 不是 5xx
  ③ 硬超时(上游挂住) → 也走降级, 不阻塞
  ④ 降级/快照兜底时来源(source)与时间(as_of)已标注
  ⑤ 端点级: 上游失败时 HTTP 200(不是 502), 响应体 available:false

全 mock, 禁真实网络(不触碰 thsdk / 不发任何请求)。
"""
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from src.core import orderbook_engine as obe  # noqa: E402
from src.core import tdx_img_parser as tip  # noqa: E402
from src.web.api import orderbook as obmod  # noqa: E402


# 上游成功返回(与 orderbook_engine.run 真实返回结构一致)
_FAKE_RESULT = {
    "events": [
        {"type": "托单", "side": "bid", "price_level": 1, "price": 11.27,
         "delta_hands": 5000, "duration_s": 0.3, "ts": 1.0, "note": ""},
    ],
    "ob_series": [
        {"ts": 1.0, "dt": "2026-10-01T10:30:00", "bid_amt10": 123456.0,
         "ask_amt10": 23456.0, "ob": 0.6807, "label": "买压"},
    ],
    "ghost_ratio": 0.25,
    "summary": "symbol=USZA002361 快照数=1 用时=1.0s | 交易时段 | 事件数=1 | OB均值=0.6807",
}

# 修改前的成功路径期望值(逐字段钉死)
_EXPECTED_SUCCESS = {
    "available": True,
    "ob_series": _FAKE_RESULT["ob_series"],
    "events": _FAKE_RESULT["events"],
    "ghost_ratio": 0.25,
    "note": _FAKE_RESULT["summary"],
}


@pytest.fixture(autouse=True)
def _reset_thsdk_breaker():
    """隔离 thsdk 并发槽/熔断器状态(硬超时用例会占用槽位)。"""
    try:
        from src.core.thsdk_breaker import reset_for_tests

        reset_for_tests()
    except Exception:  # pragma: no cover
        pass
    yield


@pytest.fixture(autouse=True)
def _no_img_fallback(monkeypatch):
    """默认: 无本地 .img 兜底 + thsdk 视为已安装(测试环境本身不装 thsdk)。"""
    monkeypatch.setattr(obe, "find_img_file", lambda *a, **k: None)
    monkeypatch.setattr(obe, "THS", object())  # 占位哨兵, run 已被 mock 不会真调


# ────────────────────────── ① 成功路径逐字段一致 ──────────────────────────

def test_success_fields_unchanged(monkeypatch):
    """成功路径返回值与修改前逐字段一致(不新增 source/as_of)。"""
    monkeypatch.setattr(obe, "run", lambda *a, **k: _FAKE_RESULT)

    out = obmod.get_orderbook_ob("USZA002361")

    assert out == _EXPECTED_SUCCESS
    assert set(out.keys()) == {"available", "ob_series", "events", "ghost_ratio", "note"}


def test_success_6digit_symbol_normalized(monkeypatch):
    """裸 6 位代码 → USZA 前缀后调 run。"""
    seen = {}

    def _fake(sym, n_snapshots=None, interval=None):
        seen["sym"] = sym
        return _FAKE_RESULT

    monkeypatch.setattr(obe, "run", _fake)

    out = obmod.get_orderbook_ob("002361")

    assert seen["sym"] == "USZA002361"
    assert out["available"] is True


# ────────────── ② 首次失败重试成功 / 重试仍失败显式降级 ──────────────

def test_first_fail_then_success_retries(monkeypatch):
    """首次上游失败(异常→护栏返回 None) → 重试一次成功, 成功路径逐字段一致。"""
    calls = {"n": 0}

    def _flaky(sym, n_snapshots=None, interval=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("thsdk -6 请求超时")
        return _FAKE_RESULT

    monkeypatch.setattr(obe, "run", _flaky)
    monkeypatch.setattr(obmod, "_RETRY_BACKOFF_S", 0.0)  # 退避置 0, 测试不等待

    out = obmod.get_orderbook_ob("USZA002361")

    assert calls["n"] == 2                # 首次 + 1 次重试
    assert out == _EXPECTED_SUCCESS       # 重试成功后与成功路径逐字段一致


def test_persistent_fail_degrades_not_502(monkeypatch):
    """重试仍失败 → 显式降级(available:false + 原因), 绝不抛 5xx。"""
    calls = {"n": 0}

    def _down(sym, n_snapshots=None, interval=None):
        calls["n"] += 1
        raise RuntimeError("thsdk -6 请求超时")

    monkeypatch.setattr(obe, "run", _down)
    monkeypatch.setattr(obmod, "_RETRY_BACKOFF_S", 0.0)

    out = obmod.get_orderbook_ob("USZA002361")  # 不抛异常

    assert calls["n"] == obmod._RETRY_ATTEMPTS
    assert out["available"] is False
    assert out["ob_series"] == []
    assert out["events"] == []
    assert "上游 thsdk" in out["note"]
    assert str(obmod._RETRY_ATTEMPTS) in out["note"]


def test_hard_timeout_degrades_bounded(monkeypatch):
    """上游**挂住不返回**(硬超时) → 也走降级, 且总耗时有界。"""
    def _hang(sym, n_snapshots=None, interval=None):
        time.sleep(1.5)  # 模拟上游卡死
        return _FAKE_RESULT

    monkeypatch.setattr(obe, "run", _hang)
    monkeypatch.setattr(obmod, "_HARD_TIMEOUT_S", 0.15)
    monkeypatch.setattr(obmod, "_RETRY_BACKOFF_S", 0.0)

    t0 = time.monotonic()
    out = obmod.get_orderbook_ob("USZA002361")
    elapsed = time.monotonic() - t0

    assert out["available"] is False
    assert "上游 thsdk" in out["note"]
    # 两次尝试各 0.15s, 远小于挂死线程的 1.5s —— 证明没有等挂死线程
    assert elapsed < 1.2, f"硬超时未生效, 耗时 {elapsed:.2f}s"


def test_empty_series_degrades(monkeypatch):
    """run 成功但无 OB 序列(非交易时段) → 仍显式降级, 不编造。"""
    monkeypatch.setattr(obe, "run", lambda *a, **k: {
        "events": [], "ob_series": [], "ghost_ratio": 0.0, "summary": "x"
    })

    out = obmod.get_orderbook_ob("USZA002361")

    assert out["available"] is False
    assert "未产出 OB 序列" in out["note"]


# ───────────────────── ④ 降级/快照兜底 标注来源与时间 ─────────────────────

def test_degraded_annotates_source_and_time(monkeypatch):
    """降级响应必须带 source + as_of(不得拿陈旧值冒充实时)。"""
    monkeypatch.setattr(obe, "run", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    monkeypatch.setattr(obmod, "_RETRY_BACKOFF_S", 0.0)

    out = obmod.get_orderbook_ob("USZA002361")

    assert out["available"] is False
    assert out["source"] == "thsdk"
    assert out["as_of"]                       # 有真实时间标注
    assert "T" in out["as_of"]                # ISO 形式(上海时区)


def test_thsdk_missing_degrades_annotated(monkeypatch):
    """thsdk 未安装 → 显式降级 + 来源/时间标注。"""
    monkeypatch.setattr(obe, "THS", None)

    out = obmod.get_orderbook_ob("USZA002361")

    assert out["available"] is False
    assert "thsdk 未安装" in out["note"]
    assert out["source"] == "thsdk" and out["as_of"]


def test_img_snapshot_fallback_annotates_source_and_time(monkeypatch):
    """实时失败但有本地 .img 快照 → 用快照兜底, 显式标注 source='img' + as_of=快照时间。"""
    fr1 = tip.ImgSnapshot(t="10:30:00", bid_prices=[11.27, 11.26], bid_vols=[10000, 5000],
                          ask_prices=[11.28, 11.29], ask_vols=[4000, 2000])
    fr2 = tip.ImgSnapshot(t="10:30:01", bid_prices=[11.27, 11.26], bid_vols=[12000, 5000],
                          ask_prices=[11.28, 11.29], ask_vols=[4000, 2000])
    snaps = [
        obe.img_frame_to_snapshot(fr1, ts=1.0, dt_iso="10:30:00"),
        obe.img_frame_to_snapshot(fr2, ts=2.0, dt_iso="10:30:01"),
    ]

    monkeypatch.setattr(obe, "run", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    monkeypatch.setattr(obmod, "_RETRY_BACKOFF_S", 0.0)
    monkeypatch.setattr(obe, "find_img_file", lambda *a, **k: "/tmp/fake_002361.img")
    monkeypatch.setattr(obe, "load_snapshots_from_img", lambda *a, **k: snaps)

    out = obmod.get_orderbook_ob("USZA002361")

    assert out["available"] is True
    assert out["source"] == "img"
    assert out["as_of"] == "10:30:01"          # 快照时间, 不是 now
    assert "离线快照" in out["note"]
    assert "非实时" in out["note"]
    assert len(out["ob_series"]) == 2


# ───────────────────────── ⑤ 端点级: 失败返回 200 ─────────────────────────

def test_endpoint_returns_200_on_upstream_failure(monkeypatch):
    """端点级回归: 上游失败时 HTTP 200 + available:false(不是 502/500)。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    monkeypatch.setattr(obe, "run", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    monkeypatch.setattr(obmod, "_RETRY_BACKOFF_S", 0.0)

    app = FastAPI()
    app.include_router(obmod.router, prefix="/api/orderbook-ob")
    client = TestClient(app)

    resp = client.get("/api/orderbook-ob", params={"symbol": "002361"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is False
    assert body["source"] == "thsdk" and body["as_of"]


def test_endpoint_returns_data_on_success(monkeypatch):
    """端点级: 成功时 HTTP 200 且成功字段逐字段一致。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    monkeypatch.setattr(obe, "run", lambda *a, **k: _FAKE_RESULT)

    app = FastAPI()
    app.include_router(obmod.router, prefix="/api/orderbook-ob")
    client = TestClient(app)

    resp = client.get("/api/orderbook-ob/USZA002361")

    assert resp.status_code == 200
    assert resp.json() == _EXPECTED_SUCCESS
