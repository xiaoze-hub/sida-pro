"""AI 链路 P1(2026-10-10): 内置报告生成成功后推送通知中心。

此前 `report_scheduler` 8:30/15:30 生成报告后只落盘、无人知晓。本轮补"生成即通知":
- 成功 → 调 `notify_center.push_notification`(站内 + 外发, category=report);
- 失败(站内落库返回 None / 调用异常)显式 False, 绝不假装推送成功;
- 生成本身失败 → 不推送。

禁真网络: monkeypatch push_notification 与报告生成函数。
"""
from __future__ import annotations

import asyncio

from src.core import report_scheduler as rs


def _fake_scheduler():
    """绕过 AsyncIOScheduler 构造(无需事件循环), 只取 _generate_job 用到的 _running。"""
    s = object.__new__(rs.ReportScheduler)
    s._running = set()
    return s


# ── _push_report_notification ─────────────────────────────────────────

def test_push_calls_notify_center_with_report_args(monkeypatch):
    calls: dict = {}

    def fake_push(title, body="", **kw):
        calls["title"] = title
        calls["body"] = body
        calls["kw"] = kw
        return 123

    monkeypatch.setattr("src.core.notify_center.push_notification", fake_push)
    ok = rs._push_report_notification(
        "premarket",
        {"path": "/data/reports/premarket-daily/x.md", "title": "# A股盘前报告 2026-10-10", "size": 42},
    )
    assert ok is True
    assert calls["title"] == "A股盘前报告 2026-10-10"  # 去掉 markdown '#' 前缀
    assert "/data/reports/premarket-daily/x.md" in calls["body"]
    assert calls["kw"]["category"] == "report"
    assert calls["kw"]["level"] == "info"
    assert calls["kw"]["source"] == "report_scheduler"


def test_push_none_result_is_not_success(monkeypatch):
    """站内落库失败(None) → False, 不假装成功。"""
    monkeypatch.setattr("src.core.notify_center.push_notification", lambda *a, **k: None)
    ok = rs._push_report_notification(
        "postmarket",
        {"path": "/data/reports/postmarket-review/y.md", "title": "# A股盘后复盘 2026-10-10", "size": 7},
    )
    assert ok is False


def test_push_exception_is_not_success(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("notify down")

    monkeypatch.setattr("src.core.notify_center.push_notification", boom)
    ok = rs._push_report_notification(
        "premarket", {"path": "/z/c.md", "title": "t", "size": 1}
    )
    assert ok is False


def test_push_missing_title_falls_back_not_crash(monkeypatch):
    monkeypatch.setattr("src.core.notify_center.push_notification", lambda *a, **k: 9)
    ok = rs._push_report_notification("premarket", {"path": "/p/x.md", "size": 3})
    assert ok is True


# ── _generate_job 集成(生成→推送) ────────────────────────────────────

def test_generate_job_pushes_after_success(monkeypatch):
    pushed: list = []
    monkeypatch.setattr(
        rs,
        "_generate_once_in_worker",
        lambda rt: {"path": "/p/premarket.md", "title": "# A股盘前报告 2026-10-10", "size": 10},
    )

    def spy(rt, res):
        pushed.append((rt, res))
        return True

    monkeypatch.setattr(rs, "_push_report_notification", spy)
    asyncio.run(rs.ReportScheduler._generate_job(_fake_scheduler(), "premarket"))
    assert pushed == [
        ("premarket", {"path": "/p/premarket.md", "title": "# A股盘前报告 2026-10-10", "size": 10})
    ]


def test_generate_job_no_push_when_generation_fails(monkeypatch):
    pushed: list = []

    def boom(rt):
        raise RuntimeError("collect failed")

    monkeypatch.setattr(rs, "_generate_once_in_worker", boom)
    monkeypatch.setattr(rs, "_push_report_notification", lambda rt, res: pushed.append((rt, res)))
    # 生成异常被 _generate_job 吞掉(只记日志), 不得推送
    asyncio.run(rs.ReportScheduler._generate_job(_fake_scheduler(), "premarket"))
    assert pushed == []
