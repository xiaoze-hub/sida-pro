"""AgentScheduler 防双跑参数回归(2026-09-08 审计)。

- add_job 必须带 max_instances=1 / coalesce=True / misfire_grace_time
  (LLM Agent 单次数分钟, 无防护时 interval 任务并发双跑: 重复通知/token 翻倍/记录竞态)
"""
from __future__ import annotations

from src.core.scheduler import AgentScheduler


def test_register_job_has_overlap_guard():
    """注册的 Agent job 必须带防双跑参数(max_instances=1/coalesce/misfire_grace)。"""
    sched = AgentScheduler(timezone="Asia/Shanghai")

    captured: dict = {}

    def _fake_add_job(func, **kwargs):
        captured.update(kwargs)

    sched.scheduler.add_job = _fake_add_job  # type: ignore[method-assign]

    class _Agent:
        name = "fake_agent"
        display_name = "假Agent"

    sched.register(_Agent(), "interval:1m")  # type: ignore[arg-type]

    assert captured["id"] == "fake_agent"
    assert captured["max_instances"] == 1
    assert captured["coalesce"] is True
    assert captured["misfire_grace_time"] == 300
