# -*- coding: utf-8 -*-
"""告警体系单测: src/core/alerting.py

覆盖:
  - AlertManager 冷却去重(同 key N 分钟内只发一次) + force 突破
  - 未配 webhook 时只打日志不抛异常
  - 5xx 滑动窗口: 未超阈值不告警 / 超阈值触发
  - LLM 429 风暴窗口
  - 数据源连续失败达阈值触发 data_source_down; 成功清零
  - check_disk_usage 返回结构 + 阈值边界
  - markdown 卡片含标题/级别/时间/详情
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from src.core import alerting as al  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_alert_state():
    al.set_alert_manager(None)
    al.reset_5xx_window()
    al.reset_llm_429_window()
    al.reset_data_source_streaks()
    yield
    al.set_alert_manager(None)
    al.reset_5xx_window()
    al.reset_llm_429_window()
    al.reset_data_source_streaks()


class _CaptureManager(al.AlertManager):
    """测试用: 记录 send 载荷, 不真发 HTTP。"""

    def __init__(self, **kwargs):
        kwargs.setdefault("webhook_url", "")
        kwargs.setdefault("cooldown_minutes", 30)
        super().__init__(**kwargs)
        self.sent: list[al.AlertMessage] = []

    def send(self, key, level, title, detail="", *, extra=None, force=False):
        # 复用父类去重逻辑, 但把投递换成捕获
        try:
            lvl = level if isinstance(level, al.AlertLevel) else al.AlertLevel(str(level))
        except ValueError:
            lvl = al.AlertLevel.WARNING
        now = __import__("time").monotonic()
        with self._lock:
            if not self._should_send(key, now, force):
                return False
            self._mark_sent(key, now)
        self.sent.append(
            al.AlertMessage(key=key, level=lvl, title=title, detail=detail or "", extra=dict(extra or {}))
        )
        return True


# ---------------------------------------------------------------------------
# 冷却去重
# ---------------------------------------------------------------------------


def test_cooldown_dedupes_same_key():
    m = _CaptureManager(cooldown_minutes=30)
    assert m.send("k1", al.AlertLevel.WARNING, "T1", "d") is True
    assert m.send("k1", al.AlertLevel.WARNING, "T1", "d") is False
    assert len(m.sent) == 1


def test_different_keys_are_independent():
    m = _CaptureManager(cooldown_minutes=30)
    assert m.send("a", al.AlertLevel.INFO, "A") is True
    assert m.send("b", al.AlertLevel.INFO, "B") is True
    assert len(m.sent) == 2


def test_force_bypasses_cooldown():
    m = _CaptureManager(cooldown_minutes=30)
    assert m.send("k", al.AlertLevel.CRITICAL, "T") is True
    assert m.send("k", al.AlertLevel.CRITICAL, "T", force=True) is True
    assert len(m.sent) == 2


def test_zero_cooldown_always_sends():
    m = _CaptureManager(cooldown_minutes=0)
    assert m.send("k", al.AlertLevel.INFO, "T") is True
    assert m.send("k", al.AlertLevel.INFO, "T") is True


def test_reset_cooldown_allows_resend():
    m = _CaptureManager(cooldown_minutes=30)
    m.send("k", al.AlertLevel.INFO, "T")
    m.reset_cooldown("k")
    assert m.send("k", al.AlertLevel.INFO, "T") is True


# ---------------------------------------------------------------------------
# markdown 卡片
# ---------------------------------------------------------------------------


def test_markdown_card_contains_required_fields():
    msg = al.AlertMessage(
        key="api_error_5xx",
        level=al.AlertLevel.CRITICAL,
        title="接口 5xx 突增",
        detail="5 分钟内 12 次",
        extra={"count": 12},
    )
    body = msg.format_markdown()
    assert "接口 5xx 突增" in body
    assert "critical" in body
    assert "api_error_5xx" in body
    assert "5 分钟内 12 次" in body
    assert "count" in body
    # 时间字段存在
    assert "**时间**" in body


def test_invalid_level_falls_back_to_warning():
    m = _CaptureManager()
    m.send("k", "not-a-level", "T")
    assert m.sent[0].level == al.AlertLevel.WARNING


# ---------------------------------------------------------------------------
# 5xx 窗口
# ---------------------------------------------------------------------------


def test_5xx_below_threshold_no_alert(monkeypatch):
    monkeypatch.setenv("ALERT_5XX_THRESHOLD", "10")
    cap = _CaptureManager()
    al.set_alert_manager(cap)
    for _ in range(10):
        al.record_http_status(500, "/api/x")
    assert cap.sent == []


def test_5xx_above_threshold_triggers(monkeypatch):
    monkeypatch.setenv("ALERT_5XX_THRESHOLD", "3")
    cap = _CaptureManager()
    al.set_alert_manager(cap)
    for _ in range(4):
        al.record_http_status(502, "/api/boom")
    keys = [s.key for s in cap.sent]
    assert al.ALERT_API_ERROR_5XX in keys


def test_2xx_ignored():
    cap = _CaptureManager()
    al.set_alert_manager(cap)
    al.record_http_status(200, "/api/ok")
    al.record_http_status(404, "/api/nope")
    assert cap.sent == []


# ---------------------------------------------------------------------------
# LLM 429
# ---------------------------------------------------------------------------


def test_llm_429_storm(monkeypatch):
    monkeypatch.setenv("ALERT_LLM_429_THRESHOLD", "3")
    cap = _CaptureManager()
    al.set_alert_manager(cap)
    for _ in range(3):
        al.record_llm_429("chat")
    keys = [s.key for s in cap.sent]
    assert any(k.startswith(al.ALERT_LLM_RATE_LIMIT) for k in keys)


def test_llm_429_below_threshold(monkeypatch):
    monkeypatch.setenv("ALERT_LLM_429_THRESHOLD", "10")
    cap = _CaptureManager()
    al.set_alert_manager(cap)
    al.record_llm_429("chat")
    assert cap.sent == []


# ---------------------------------------------------------------------------
# 数据源连续失败
# ---------------------------------------------------------------------------


def test_data_source_down_after_consecutive_failures(monkeypatch):
    monkeypatch.setenv("ALERT_DS_FAIL_THRESHOLD", "3")
    cap = _CaptureManager()
    al.set_alert_manager(cap)
    for i in range(3):
        al.record_data_source_failure("tencent", f"err{i}")
    keys = [s.key for s in cap.sent]
    assert f"{al.ALERT_DATA_SOURCE_DOWN}:tencent" in keys


def test_data_source_success_resets_streak(monkeypatch):
    monkeypatch.setenv("ALERT_DS_FAIL_THRESHOLD", "3")
    cap = _CaptureManager()
    al.set_alert_manager(cap)
    al.record_data_source_failure("tencent")
    al.record_data_source_failure("tencent")
    al.record_data_source_success("tencent")
    al.record_data_source_failure("tencent")
    al.record_data_source_failure("tencent")
    # 只有 2 次失败(成功后清零), 未达阈值 3
    assert cap.sent == []


def test_data_source_independent_providers(monkeypatch):
    monkeypatch.setenv("ALERT_DS_FAIL_THRESHOLD", "2")
    cap = _CaptureManager()
    al.set_alert_manager(cap)
    al.record_data_source_failure("a")
    al.record_data_source_failure("b")
    al.record_data_source_failure("a")
    keys = [s.key for s in cap.sent]
    assert f"{al.ALERT_DATA_SOURCE_DOWN}:a" in keys
    assert f"{al.ALERT_DATA_SOURCE_DOWN}:b" not in keys


# ---------------------------------------------------------------------------
# 磁盘检查
# ---------------------------------------------------------------------------


def test_check_disk_usage_returns_structure(monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(ROOT / "data"))
    cap = _CaptureManager()
    al.set_alert_manager(cap)
    # 阈值 101% 保证本地不会触发
    r = al.check_disk_usage(threshold_pct=101.0)
    assert "path" in r
    assert "used_pct" in r
    assert r["used_pct"] >= 0
    assert r["alerted"] is False


def test_check_disk_usage_triggers_when_threshold_low(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    cap = _CaptureManager()
    al.set_alert_manager(cap)
    r = al.check_disk_usage(threshold_pct=0.0)
    assert r["alerted"] is True
    assert any(s.key == al.ALERT_DISK_HIGH for s in cap.sent)


def test_db_connection_error_alert():
    cap = _CaptureManager()
    al.set_alert_manager(cap)
    ok = cap.send_db_connection_error("connection refused")
    assert ok is True
    assert cap.sent[0].key == al.ALERT_DB_CONNECTION
    assert cap.sent[0].level == al.AlertLevel.CRITICAL


# ---------------------------------------------------------------------------
# 无 webhook 时不抛异常
# ---------------------------------------------------------------------------


def test_send_without_webhook_only_logs():
    m = al.AlertManager(webhook_url="", cooldown_minutes=30)
    assert m.send("k", al.AlertLevel.INFO, "标题", "详情") is True
