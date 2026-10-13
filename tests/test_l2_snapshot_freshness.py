# -*- coding: utf-8 -*-
"""L2 成品资金快照(l2_fund_snapshots)新鲜度自检 + 自愈(2026-10-13 快照源空修复)。

背景: 昨天 15:05 快照 cron 撞 TQ 僵尸期失败后无自愈重试 → 快照表 0 行, 非交易时段
web 层回退也没东西可兜 → 主力净流入显『无数据』。

回归目标(全 mock, 禁真实网络):
- 期望最新交易日按真实交易日历判定(周末/节假日不算缺口);
- 缺失交易日枚举正确;
- 有缺口才重采(无缺口零副作用), 重采后复检转绿;
- 重采**复用现有落库口径**(capture_universe), TQ 不可用 → ok=False + 显式 error(不静默);
- 落后 → 作业判 failed(ok=False) + 写告警通知;
- 空表不以『无缺口』冒充(ok=False)。
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.core import l2_snapshot_freshness as lf  # noqa: E402


_NOW_1013 = datetime(2026, 10, 13, 15, 35)  # 周二交易日 → 期望 20261013
_NOW_1011 = datetime(2026, 10, 11, 9, 0)    # 周日 → 期望上一交易日 20261009


# ─────────────── ① 期望交易日 / 缺失枚举 ───────────────

@pytest.mark.parametrize(
    "now,expected",
    [
        (datetime(2026, 10, 13, 15, 35), "20261013"),
        (datetime(2026, 10, 11, 9, 0), "20261009"),   # 周日 → 回退周五
        (datetime(2026, 10, 9, 16, 0), "20261009"),
        (datetime(2026, 10, 3, 9, 0), "20260930"),    # 国庆假期中 → 回退 9-30
    ],
)
def test_expected_latest(now, expected):
    assert lf._expected_latest(now) == expected


def test_missing_days():
    assert lf._missing_days("20261009", "20261013") == ["20261012", "20261013"]
    assert lf._missing_days("20261013", "20261013") == []
    assert lf._missing_days(None, "20261013") == []


# ─────────────── ② check: 有/无缺口 + 空表诚实态 ───────────────

def test_check_stale_then_fresh():
    stale = lf.check(_NOW_1013, latest_fn=lambda: "20261009")
    assert stale["ok"] is False and stale["stale"] is True
    assert stale["missing"] == ["20261012", "20261013"]
    assert "停在 20261009" in stale["reason"]

    fresh = lf.check(_NOW_1013, latest_fn=lambda: "20261013")
    assert fresh["ok"] is True and fresh["stale"] is False and fresh["missing"] == []


def test_check_empty_table_not_ok():
    """空表(0 行)不以『无缺口』冒充 —— 显式 ok=False(这正是本次事故形态)。"""
    r = lf.check(_NOW_1013, latest_fn=lambda: None)
    assert r["ok"] is False and r["latest"] is None and r["stale"] is True
    assert "空表" in r["reason"]


# ─────────────── ③ guard: 有缺口才重采 + 复检 ───────────────

def test_guard_noop_when_fresh():
    calls = []
    r = lf.guard(_NOW_1013, latest_fn=lambda: "20261013",
                 backfill_fn=lambda days, **k: calls.append(days) or {"ok": True, "captured": 0})
    assert r["ok"] is True and r["backfilled"] == 0 and r["recovered"] is True
    assert calls == []


def test_guard_backfills_then_recovers():
    state = {"latest": None}          # 初始空表

    def _bf(days, **k):
        state["latest"] = "20261013"  # 重采补齐当日
        return {"ok": True, "captured": 4210, "error": None}

    r = lf.guard(_NOW_1013, latest_fn=lambda: state["latest"], backfill_fn=_bf)
    assert r["ok"] is True and r["recovered"] is True
    assert r["latest_before"] is None and r["latest"] == "20261013"
    assert r["backfilled"] == 4210


def test_guard_still_stale_reports_not_ok():
    r = lf.guard(_NOW_1013, latest_fn=lambda: "20261001",
                 backfill_fn=lambda days, **k: {"ok": False, "captured": 0,
                                                "error": "TQ 网关不可达"})
    assert r["ok"] is False and r["recovered"] is False
    assert "仍缺" in (r["reason"] or "") and "TQ 网关不可达" in (r["reason"] or "")


def test_guard_empty_table_attempts_backfill():
    """事故形态: 表 0 行(latest=None, missing 枚举为空)也**必须触发重采**, 不能静默跳过。"""
    seen = {}
    r = lf.guard(_NOW_1013, latest_fn=lambda: None,
                 backfill_fn=lambda days, **k: seen.update(days=list(days))
                 or {"ok": False, "captured": 0, "error": "TQ 网关不可达"})
    assert seen.get("days") == ["20261013"]     # 空表以期望日为目标重采
    assert r["ok"] is False and r["recovered"] is False
    assert "仍空" in (r["reason"] or "") and "TQ 网关不可达" in (r["reason"] or "")



# ─────────────── ④ backfill_missing: 复用现有口径 + 降级诚实 ───────────────

def test_backfill_missing_success():
    seen = {}
    import src.core.l2_fund_snapshot as snap_mod

    def _cap(*, symbols=None, now=None):
        seen["called"] = True
        return {"ok": True, "captured": 3800, "stocks": 4000}

    # capture_fn 显式注入 → 不改模块; 验调用语义
    r = lf.backfill_missing(["20261013"], capture_fn=_cap, now=_NOW_1013)
    assert seen["called"] is True
    assert r["ok"] is True and r["captured"] == 3800 and r["error"] is None
    assert snap_mod  # 引用占位, 表明默认路径即该模块(见下条断言)


def test_backfill_missing_uses_existing_capture_by_default(monkeypatch):
    """默认重采必须复用 `l2_fund_snapshot.capture_universe`(禁自创第二套落库口径)。"""
    import src.core.l2_fund_snapshot as snap_mod

    called = {}

    def _fake_universe(*, symbols=None, now=None):
        called["hit"] = True
        return {"ok": True, "captured": 1234}

    monkeypatch.setattr(snap_mod, "capture_universe", _fake_universe)
    r = lf.backfill_missing(["20261013"], now=_NOW_1013)
    assert called.get("hit") is True
    assert r["ok"] is True and r["captured"] == 1234


def test_backfill_missing_tq_down_is_explicit_failure():
    """TQ 不可用 / 采样零产出 → 显式失败, 不拿『无数据』冒充『补好了』。"""
    r = lf.backfill_missing(
        ["20261013"],
        capture_fn=lambda *, symbols=None, now=None: {
            "ok": False, "captured": 0,
            "reason": "采样未产出任何真实值(非交易时段或数据源不可用)"},
        now=_NOW_1013,
    )
    assert r["ok"] is False and r["captured"] == 0 and "采样未产出" in (r["error"] or "")


def test_backfill_missing_capture_exception_is_explicit():
    def _boom(*, symbols=None, now=None):
        raise RuntimeError("TQ 网关不可达")

    r = lf.backfill_missing(["20261013"], capture_fn=_boom, now=_NOW_1013)
    assert r["ok"] is False and "TQ 网关不可达" in (r["error"] or "")


def test_backfill_missing_no_days_noop():
    r = lf.backfill_missing([])
    assert r["ok"] is True and r["captured"] == 0 and r["days"] == []


# ─────────────── ⑤ 作业入口: 落后 → ok=False + 告警 ───────────────

def test_run_job_stale_alerts(monkeypatch):
    alerted = {}
    monkeypatch.setattr(lf, "check", lambda now=None, latest_fn=lf.latest_snapshot_date:
                        {"ok": False, "latest": None, "expected": "20261013",
                         "missing": ["20261013"], "stale": True, "reason": "空表"})
    monkeypatch.setattr(lf, "_notify_stale", lambda info, now: alerted.update(info))
    monkeypatch.setattr(lf, "backfill_missing",
                        lambda days, **k: {"ok": False, "captured": 0, "error": "TQ 断链"})
    out = lf.run_l2_snapshot_freshness_job(_NOW_1013)
    assert out["ok"] is False
    assert alerted.get("expected") == "20261013"


def test_run_job_skips_when_in_flight(monkeypatch):
    class _J:
        def create(self, kind, label, **k):
            return "job1", False

    monkeypatch.setattr("src.core.jobs.jobs", _J())
    out = lf.run_l2_snapshot_freshness_job(_NOW_1013)
    assert out.get("skipped") == "in_flight"
