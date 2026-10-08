"""tq.py URL 层 + 单位 + 公式早退 回归测试 (2026-10-08 断链审计 P0-3/P1-4/5/8/10/P2)。

全部 mock, 禁真网络:
- 候选全灭 → 显式 TqUnavailable, 绝不回退/缓存死地址
- 成功缓存 TTL 过期 → 重探(自愈); 连续连接失败 → 主动失效
- 探测总预算上限
- httpx connect 超时生效
- turnover 单位标定(万元×1e4, 恒等式 vol×price≈amt)
- formula_scan 早退带 date_has_data=False
"""

from __future__ import annotations

import time

import httpx
import pytest

from marketdata.symbol import Symbol
from marketdata.vendors import tq as tqmod


def _reset(monkeypatch):
    """把 URL 层状态清回"冷启动"。"""
    monkeypatch.setattr(tqmod, "_TQ_URL_CACHE", None)
    monkeypatch.setattr(tqmod, "_TQ_URL_CACHE_OK", False)
    monkeypatch.setattr(tqmod, "_TQ_URL_CACHE_TS", 0.0)
    monkeypatch.setattr(tqmod, "_TQ_LAST_GOOD_HOST", None)
    monkeypatch.setattr(tqmod, "_TQ_FAIL_STREAK", 0)
    monkeypatch.delenv("TDX_QUANT_URL", raising=False)
    monkeypatch.setattr(tqmod, "_host_gateway", lambda: "10.9.9.9")


# ---------------------------------------------------------------------------
# P0-3: 候选全灭 → 不回退死地址
# ---------------------------------------------------------------------------


def test_all_candidates_fail_returns_none_no_dead_address(monkeypatch):
    _reset(monkeypatch)
    calls: list[str] = []

    def fake_probe(url, timeout=1.5):
        calls.append(url)
        return False

    monkeypatch.setattr(tqmod, "_probe_tq", fake_probe)

    out = tqmod._resolve_tq_url()
    assert out is None, "候选全灭必须返回 None"
    assert tqmod._TQ_URL_CACHE is None, "不得缓存任何具体(死)地址"
    assert calls, "应当探测过候选"
    # 绝不出现旧代码的 docker 桥死地址兜底
    assert "172.18.0.1" not in (out or "")


def test_rpc_raises_tq_unavailable_when_all_dead(monkeypatch):
    _reset(monkeypatch)
    monkeypatch.setattr(tqmod, "_probe_tq", lambda url, timeout=1.5: False)

    with pytest.raises(tqmod.TqUnavailable):
        tqmod._rpc("get_market_snapshot", {"stock_code": "002361.SZ"})


def test_env_url_is_only_permitted_fallback(monkeypatch):
    """env TDX_QUANT_URL 是唯一允许的非探测兜底(ops 显式意图)。"""
    _reset(monkeypatch)
    monkeypatch.setenv("TDX_QUANT_URL", "http://10.1.2.3:17709")
    monkeypatch.setattr(tqmod, "_probe_tq", lambda url, timeout=1.5: False)

    out = tqmod._resolve_tq_url()
    assert out == "http://10.1.2.3:17709/"


def test_no_fallback_url_dead_code_removed():
    assert not hasattr(tqmod, "_FALLBACK_URL"), "死代码 _FALLBACK_URL 应已删除"


# ---------------------------------------------------------------------------
# P1-5: 成功缓存自愈
# ---------------------------------------------------------------------------


def test_ok_cache_reused_within_ttl(monkeypatch):
    _reset(monkeypatch)
    calls: list[str] = []
    monkeypatch.setattr(tqmod, "_probe_tq", lambda url, timeout=1.5: calls.append(url) or False)
    monkeypatch.setattr(tqmod, "_TQ_URL_CACHE", "http://10.9.9.9:17709/")
    monkeypatch.setattr(tqmod, "_TQ_URL_CACHE_OK", True)
    monkeypatch.setattr(tqmod, "_TQ_URL_CACHE_TS", time.time())

    out = tqmod._resolve_tq_url()
    assert out == "http://10.9.9.9:17709/"
    assert calls == [], "成功缓存 TTL 内不应重探"


def test_ok_cache_reexpires_after_ttl(monkeypatch):
    """P1-5: 旧实现成功后永久不重探; 现 TTL 过期必须重探。"""
    _reset(monkeypatch)
    monkeypatch.setattr(tqmod, "_TQ_URL_CACHE", "http://10.0.0.1:17709/")  # 旧地址
    monkeypatch.setattr(tqmod, "_TQ_URL_CACHE_OK", True)
    monkeypatch.setattr(tqmod, "_TQ_URL_CACHE_TS", time.time() - tqmod._TQ_OK_TTL - 1.0)

    hit_url = "http://172.27.16.1:17709/"
    calls: list[str] = []

    def probe(url, timeout=1.5):
        calls.append(url)
        return url == hit_url

    monkeypatch.setattr(tqmod, "_probe_tq", probe)
    out = tqmod._resolve_tq_url()
    assert calls, "TTL 过期后必须重探"
    assert out == hit_url, "应自愈到新命中地址"


def test_connect_fail_streak_invalidates_ok_cache(monkeypatch):
    _reset(monkeypatch)
    monkeypatch.setattr(tqmod, "_TQ_URL_CACHE", "http://10.0.0.1:17709/")
    monkeypatch.setattr(tqmod, "_TQ_URL_CACHE_OK", True)
    monkeypatch.setattr(tqmod, "_TQ_URL_CACHE_TS", time.time())

    for _ in range(tqmod._TQ_FAIL_STREAK_THRESHOLD):
        tqmod._note_rpc_conn_failure()
    assert tqmod._TQ_URL_CACHE_OK is False, "连续连接失败应失效成功缓存"
    assert tqmod._TQ_URL_CACHE_TS == 0.0, "应强制下次重探"


# ---------------------------------------------------------------------------
# P1-4: 探测预算上限
# ---------------------------------------------------------------------------


def test_probe_budget_capped(monkeypatch):
    _reset(monkeypatch)
    monkeypatch.setattr(tqmod, "_PROBE_BUDGET_S", 0.5)
    monkeypatch.setattr(tqmod, "_PROBE_SINGLE_TIMEOUT_S", 0.05)
    calls: list[str] = []

    def slow_probe(url, timeout=1.5):
        calls.append(url)
        time.sleep(0.4)
        return False

    monkeypatch.setattr(tqmod, "_probe_tq", slow_probe)
    t0 = time.monotonic()
    out = tqmod._resolve_tq_url()
    elapsed = time.monotonic() - t0

    assert out is None
    assert elapsed < 1.2, f"探测总耗时 {elapsed:.2f}s 应受预算约束(≤~0.5s+一次探测)"
    assert 0 < len(calls) <= 3, f"不应扫完全部候选({len(calls)} 次)"


# ---------------------------------------------------------------------------
# P1-10: connect 超时生效
# ---------------------------------------------------------------------------


class _FakeResp:
    status_code = 200

    def __init__(self, payload=b'{"result":{"Value":[1]}}'):
        self.content = payload

    def raise_for_status(self):
        pass


def _install_fake_client(monkeypatch, seen: dict):
    class _FakeClient:
        def __init__(self, *a, **kw):
            seen["timeout"] = kw.get("timeout")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, *a, **kw):
            return _FakeResp()

    monkeypatch.setattr(tqmod.httpx, "Client", _FakeClient)


def test_rpc_uses_connect_timeout(monkeypatch):
    _reset(monkeypatch)
    monkeypatch.setattr(tqmod, "_TQ_URL_CACHE", "http://10.9.9.9:17709/")
    monkeypatch.setattr(tqmod, "_TQ_URL_CACHE_OK", True)
    monkeypatch.setattr(tqmod, "_TQ_URL_CACHE_TS", time.time())
    seen: dict = {}
    _install_fake_client(monkeypatch, seen)

    tqmod._rpc("get_stock_list", {"market": "5", "list_type": 0})
    to = seen["timeout"]
    assert isinstance(to, httpx.Timeout)
    assert to.connect == tqmod._TIMEOUT_CONNECT_S
    assert to.connect <= 2.0, "connect 超时必须显式且 ≤2s"


def test_probe_uses_connect_timeout(monkeypatch):
    seen: dict = {}
    _install_fake_client(monkeypatch, seen)
    tqmod._probe_tq("http://10.9.9.9:17709/", timeout=1.5)
    to = seen["timeout"]
    assert isinstance(to, httpx.Timeout)
    assert to.connect <= 2.0


# ---------------------------------------------------------------------------
# P1-8: turnover 单位标定 (002361.SZ 实盘证据) + 恒等式
# ---------------------------------------------------------------------------

# 2026-10-08 收盘: TQ Amount=182560.53(万元) / Volume=1784413(手) / Average=10.23;
# 腾讯同日 元额=1,825,605,387 ≈ 182560.53×1e4。
_SNAP = {
    "Now": "10.10", "LastClose": "9.58", "Open": "9.77", "Max": "10.54", "Min": "9.59",
    "Volume": "1784413", "Amount": "182560.53", "Average": "10.23",
    "Inside": "778965", "Outside": "1005449", "ErrorId": "0",
}


def test_quote_turnover_is_yuan_calibrated(monkeypatch):
    def fake_rpc(method, params, timeout=None, **kw):
        if method == "get_market_snapshot":
            return dict(_SNAP)
        return {}

    monkeypatch.setattr(tqmod, "_rpc", fake_rpc)
    quotes = tqmod.TqQuoteVendor().fetch([Symbol.parse("002361", "CN")], {})
    assert len(quotes) == 1
    q = quotes[0]
    assert q.turnover == pytest.approx(182560.53 * 1e4, rel=1e-9)
    # P2 Volume 单位标定: 快照 Volume 原样落 手(与腾讯 parts[6] 同口径), **不 ×100**。
    assert q.volume == pytest.approx(1784413)
    # 恒等式 turnover(元) / (volume(手)×100) ≈ VWAP
    assert q.turnover / (q.volume * 100) == pytest.approx(10.23, abs=0.02)
    # 与腾讯同日元额一致(±1%)
    assert q.turnover == pytest.approx(1_825_605_387, rel=0.01)


# ---------------------------------------------------------------------------
# P2: formula_scan 早退带 date_has_data=False
# ---------------------------------------------------------------------------


def test_formula_scan_empty_pool_marks_no_data(monkeypatch):
    monkeypatch.setattr(tqmod, "stock_list", lambda market, list_type: [])
    res = tqmod.formula_scan("MACD买入", date="20261008")
    assert res["hit_count"] == 0
    assert res["date_has_data"] is False
    assert res["complete"] is False
