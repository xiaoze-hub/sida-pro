"""EVENT_JOB_MISSED 不是异常（2026-10-22 线上噪声事故回归）。

实测 24 小时内 725 条 `scheduler job missed` 灌满错误表，把真故障淹了 ——
淹掉监控比没有监控更危险。missed 说明"采样跟不上/事件循环被堵"，是**指标**不是报错。
"""

from __future__ import annotations

from src.core import error_tracker as et


class _FakeScheduler:
    def add_listener(self, fn, mask):
        self.fn = fn

    def _emit_missed(self, job_id):
        class _Ev:
            pass

        ev = _Ev()
        ev.job_id = job_id
        ev.exception = None
        self.fn(ev)

    def _emit_error(self, job_id, exc):
        class _Ev:
            pass

        ev = _Ev()
        ev.job_id = job_id
        ev.exception = exc
        self.fn(ev)


def test_missed_is_counted_not_captured(monkeypatch):
    captured = []
    monkeypatch.setattr(et, "capture_exception", lambda exc, ctx=None: captured.append(exc) or True)
    et._clear_state()

    sched = _FakeScheduler()
    assert et.install_scheduler_error_tracking(sched) is True

    sched._emit_missed("klines-minute-1min")
    sched._emit_missed("klines-minute-1min")
    assert captured == [], "EVENT_JOB_MISSED 不该进错误表"
    assert et.missed_counts()["klines-minute-1min"] == 2

    sched._emit_error("backfill", RuntimeError("boom"))
    assert len(captured) == 1, "真正的异常必须照样捕获"


def test_missed_counts_exposed_and_reset():
    et._clear_state()
    et._MISSED_COUNTS["x"] = 3
    assert et.missed_counts() == {"x": 3}
    et._clear_state()
    assert et.missed_counts() == {}
