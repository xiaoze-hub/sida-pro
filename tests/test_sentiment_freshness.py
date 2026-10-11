# -*- coding: utf-8 -*-
"""市场情绪序列新鲜度自检 + 自愈 + 消费方陈旧标注(2026-10-11 断链修复)。

背景: market_sentiment_daily(TQ get_scjy_value SC01-35)在生产停在 2026-09-22 无人知。
回归目标:
- 期望最新交易日按真实交易日历判定(周末/节假日不算缺口);
- 缺失交易日枚举正确;
- 有缺口才补数(无缺口零副作用), 补完复检转绿;
- 补数**批量 + 重试/降级诚实**: 失败重试, 仍失败 → ok=False + 显式 error(不静默);
- 落后 → 作业判 failed(ok=False) + 写告警通知;
- 消费方(温度计/regime)数据停在过去 → 显式标注『数据停在 X 日』, 不据此条件化。

全 mock / 临时测试库, 禁真实网络。
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.core import sentiment_freshness as sf  # noqa: E402


_NOW_1011 = datetime(2026, 10, 11, 9, 0)  # 周日 → 期望上一交易日 20261009


# ─────────────── ① 期望交易日 / 缺失枚举 ───────────────

@pytest.mark.parametrize(
    "now,expected",
    [
        (datetime(2026, 10, 11, 9, 0), "20261009"),
        (datetime(2026, 10, 9, 16, 0), "20261009"),
        (datetime(2026, 10, 3, 9, 0), "20260930"),   # 国庆假期中 → 回退 9-30
        (datetime(2026, 9, 25, 9, 0), "20260924"),   # 中秋假期 → 回退
    ],
)
def test_expected_latest(now, expected):
    assert sf._expected_latest(now) == expected


def test_missing_days():
    miss = sf._missing_days("20260930", "20261009")
    assert miss == ["20261008", "20261009"]
    assert sf._missing_days("20261009", "20261009") == []
    assert sf._missing_days(None, "20261009") == []


# ─────────────── ② check: 有/无缺口 + 空表诚实态 ───────────────

def test_check_stale_then_fresh():
    stale = sf.check(_NOW_1011, latest_fn=lambda: "20260922")
    assert stale["ok"] is False and stale["stale"] is True
    assert "20260923" in stale["missing"] and stale["missing"][-1] == "20261009"
    assert "停在 20260922" in stale["reason"]

    fresh = sf.check(_NOW_1011, latest_fn=lambda: "20261009")
    assert fresh["ok"] is True and fresh["stale"] is False and fresh["missing"] == []


def test_check_empty_table_not_ok():
    r = sf.check(_NOW_1011, latest_fn=lambda: None)
    assert r["ok"] is False and r["latest"] is None and r["stale"] is True


# ─────────────── ③ guard: 有缺口才补 + 复检 ───────────────

def test_guard_noop_when_fresh():
    calls = []
    r = sf.guard(_NOW_1011, latest_fn=lambda: "20261009",
                 backfill_fn=lambda days: calls.append(days) or {"ok": True, "rows": 0})
    assert r["ok"] is True and r["backfilled"] == 0 and r["recovered"] is True
    assert calls == []


def test_guard_backfills_then_recovers():
    state = {"latest": "20260922"}
    seen = {}

    def _bf(days):
        seen["days"] = list(days)
        state["latest"] = "20261009"
        return {"ok": True, "rows": 12, "attempts": 1, "error": None}

    r = sf.guard(_NOW_1011, latest_fn=lambda: state["latest"], backfill_fn=_bf)
    assert seen["days"][0] == "20260923"
    assert r["ok"] is True and r["recovered"] is True
    assert r["latest_before"] == "20260922" and r["latest"] == "20261009"
    assert r["backfilled"] == 12


def test_guard_still_stale_reports_not_ok():
    r = sf.guard(_NOW_1011, latest_fn=lambda: "20260922",
                 backfill_fn=lambda days: {"ok": False, "rows": 0, "attempts": 3, "error": "TQ 网关不可达"})
    assert r["ok"] is False and r["recovered"] is False
    assert "仍缺" in (r["reason"] or "") and "TQ 网关不可达" in (r["reason"] or "")


# ─────────────── ④ backfill_missing: 批量 + 重试/降级诚实 ───────────────

_SENTINEL = object()  # 冒充 db, 假 sync_fn 忽略之


def test_backfill_missing_success():
    calls = []

    def _sync(db, *, start_time=""):
        calls.append(start_time)
        return {"rows": 5, "first": "20260923", "last": "20260930"}

    r = sf.backfill_missing(["20260923", "20260930"], sync_fn=_sync, db=_SENTINEL,
                            retries=2, sleep_fn=lambda s: None)
    assert r["ok"] is True and r["rows"] == 5 and r["attempts"] == 1
    # 窗口按最早缺失日回看 BACKFILL_LOOKBACK_DAYS
    assert calls and calls[0] <= "20260923"


def test_backfill_missing_retries_then_succeeds():
    seen = {"n": 0}

    def _sync(db, *, start_time=""):
        seen["n"] += 1
        if seen["n"] < 3:
            return {"error": "突发 reset"}
        return {"rows": 1}

    r = sf.backfill_missing(["20260923"], sync_fn=_sync, db=_SENTINEL,
                            retries=3, sleep_fn=lambda s: None)
    assert r["ok"] is True and r["attempts"] == 3 and seen["n"] == 3


def test_backfill_missing_exhausts_and_fails_explicitly():
    def _sync(db, *, start_time=""):
        raise RuntimeError("TQ 网关不可达")

    r = sf.backfill_missing(["20260923"], sync_fn=_sync, db=_SENTINEL,
                            retries=2, sleep_fn=lambda s: None)
    assert r["ok"] is False and r["attempts"] == 3 and r["rows"] == 0
    assert "TQ 网关不可达" in (r["error"] or "")


def test_backfill_missing_empty_return_is_failure():
    """TQ 返回空(客户端未开) → 显式失败, 不拿'无数据'冒充'补好了'。"""
    r = sf.backfill_missing(["20260923"], sync_fn=lambda db, *, start_time="": {"error": "TQ 无返回"},
                            db=_SENTINEL, retries=0, sleep_fn=lambda s: None)
    assert r["ok"] is False and r["error"]


def test_backfill_missing_no_days_noop():
    r = sf.backfill_missing([])
    assert r["ok"] is True and r["rows"] == 0 and r["attempts"] == 0


# ─────────────── ⑤ 作业入口: 落后 → ok=False + 告警 ───────────────

def test_run_job_stale_alerts(monkeypatch):
    alerted = {}
    monkeypatch.setattr(sf, "check", lambda now=None, latest_fn=sf.latest_sentiment_date:
                        {"ok": False, "latest": "20260922", "expected": "20261009",
                         "missing": ["20260923"], "stale": True, "reason": "停在 20260922"})
    monkeypatch.setattr(sf, "_notify_stale", lambda info, now: alerted.update(info))
    # 补数(仍缺) → guard 复检仍 stale
    monkeypatch.setattr(sf, "backfill_missing",
                        lambda days, **k: {"ok": False, "rows": 0, "attempts": 3, "error": "TQ 断链"})
    out = sf.run_sentiment_freshness_job(_NOW_1011)
    assert out["ok"] is False
    assert alerted.get("latest") == "20260922"


def test_run_job_skips_when_in_flight(monkeypatch):
    class _J:
        def create(self, kind, label, **k):
            return "job1", False
    monkeypatch.setattr("src.core.jobs.jobs", _J())
    out = sf.run_sentiment_freshness_job(_NOW_1011)
    assert out.get("skipped") == "in_flight"
