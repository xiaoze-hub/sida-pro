"""后台任务完成通知必须带触发者归属(2026-10-30 OCR warning 回归)。

缺陷: `notify_task_done` 改成"无归属只落 NULL 共享桶、不外发"之后,
`premarket._trigger_run` / `history` 重试这两条调用没传 `user_id` —— 成员触发的
任务完成/失败通知既看不到也收不到, 是静默功能回退。
"""

from __future__ import annotations

import sys
import types

import src.web.api.agents as agents_api


def _fake_server(monkeypatch):
    fake = types.ModuleType("server")
    fake.trigger_agent = lambda *a, **k: None  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "server", fake)
    return fake


def test_premarket_trigger_passes_user_attribution(monkeypatch):
    _fake_server(monkeypatch)
    seen: dict = {}
    monkeypatch.setattr(
        agents_api,
        "_spawn_async_run",
        lambda fn, *a, **k: seen.update(args=a, kwargs=k),
    )

    from src.web.api.premarket import _trigger_run

    _trigger_run("premarket_outlook", "user-42")

    assert seen["kwargs"].get("notify_user_id") == "user-42", (
        "盘前任务的完成通知必须回到触发者, 不能落 NULL 共享桶"
    )
    assert seen["kwargs"].get("notify_label") == "盘前分析"
