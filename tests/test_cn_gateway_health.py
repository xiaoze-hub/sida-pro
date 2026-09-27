"""国内数据网关存活监控(2026-09-27)。

钉住两件事:
  1. 探针必须探**数据路径** —— 网关"进程活着但上游被东财风控拒"时也要判失败(本次真实故障形态)
  2. 告警只在"坏了"和"恢复"各响一次 —— 连续失败不刷屏, 恢复要报
"""
from __future__ import annotations

import json

from src.core import cn_gateway_health as h


class _Resp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode("utf-8") if not isinstance(payload, bytes) else payload

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _patch_urlopen(monkeypatch, fn):
    monkeypatch.setattr(h.urllib.request, "urlopen", fn)


def test_probe_ok(monkeypatch):
    _patch_urlopen(monkeypatch, lambda url, timeout=None: _Resp(
        {"total_main_flow": -652.1, "up_count": 1098, "source": "eastmoney_push2delay_cn"}))
    r = h.probe()
    assert r["ok"] is True
    assert r["total_main_flow"] == -652.1


def test_probe_gateway_degraded_body_is_failure(monkeypatch):
    """网关自己 502 的 body 形如 {"error": ...} —— 必须判失败(本次故障就是这个形态)。"""
    _patch_urlopen(monkeypatch, lambda url, timeout=None: _Resp({"error": "RemoteDisconnected"}))
    r = h.probe()
    assert r["ok"] is False
    assert "网关降级" in r["detail"]


def test_probe_missing_flow_is_failure(monkeypatch):
    """有 200 但资金字段缺失 —— 同样算失败(降级态不能当成健康)。"""
    _patch_urlopen(monkeypatch, lambda url, timeout=None: _Resp({"up_count": 100}))
    r = h.probe()
    assert r["ok"] is False
    assert "total_main_flow" in r["detail"]


def test_probe_network_error_is_failure(monkeypatch):
    def boom(url, timeout=None):
        raise OSError("timed out")

    _patch_urlopen(monkeypatch, boom)
    r = h.probe()
    assert r["ok"] is False
    assert "timed out" in r["detail"]


def test_watch_alerts_on_second_consecutive_failure_only():
    w = h.GatewayWatch()
    bad = {"ok": False, "url": "u", "detail": "东财风控"}
    assert w.observe(bad) is None, "第一次失败不打扰"
    msg = w.observe(bad)
    assert msg and "连续 2 次" in msg
    assert w.observe(bad) is None, "仍坏着不重复告警"


def test_watch_reports_recovery_once():
    w = h.GatewayWatch()
    bad = {"ok": False, "url": "u", "detail": "x"}
    good = {"ok": True, "url": "u", "detail": "两市主力净流入 -652.1 亿"}
    w.observe(bad)
    w.observe(bad)
    msg = w.observe(good)
    assert msg and "已恢复" in msg
    assert w.observe(good) is None, "恢复只报一次"


def test_watch_single_blip_does_not_alarm_then_reset():
    """单次抖动后恢复 -> 全程无告警。"""
    w = h.GatewayWatch()
    assert w.observe({"ok": False, "url": "u", "detail": "抖一下"}) is None
    assert w.observe({"ok": True, "url": "u", "detail": "ok"}) is None
    assert w.alarmed is False
