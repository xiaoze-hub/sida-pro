"""成本/配额的归因链路（2026-10-21 线上事故回归）。

事故：`llm_usage.user_id/run_id` 一直是空 —— 成本看板与配额全 0/无限。
根因：`_log_usage` 用 `loop.run_in_executor` 落库，而 **executor 线程不继承 contextvars**，
在线程里读 `llm_context.current()` 恒为 None。
修法：在调用线程先取值，作为参数传给 `_log_usage_sync`。
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from src.core import llm_context


def test_context_does_not_cross_executor_thread():
    """先钉住"为什么必须传参"：线程里读 contextvar 拿不到值。"""
    import concurrent.futures

    async def main():
        llm_context.bind(user_id="u-thread", run_id="r-thread")
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: llm_context.current())

    got = asyncio.run(main())
    assert got["user_id"] is None, "如果这里不是 None，说明运行环境变了，本修复的前提要重新确认"


def test_log_usage_passes_ctx_across_threads(monkeypatch):
    """修复点：_log_usage 必须把 contextvar 的值当参数传进线程。"""
    from src.core.ai_client import AIClient

    captured: dict = {}

    async def main():
        client = AIClient.__new__(AIClient)
        client.scene = "chat"
        client.model = "m"

        def fake_sync(scene, model_name, usage, latency_ms, ctx=None):
            captured["ctx"] = ctx

        client._log_usage_sync = fake_sync  # type: ignore[assignment]
        llm_context.bind(user_id="u1", run_id="r1", scene="chat")
        client._log_usage("chat", "m", SimpleNamespace(prompt_tokens=10, completion_tokens=5), 12)
        await asyncio.sleep(0.05)

    asyncio.run(main())
    assert captured["ctx"] == {"user_id": "u1", "run_id": "r1", "scene": "chat"}


def test_log_usage_sync_falls_back_to_ctx_argument():
    """sync 路径也要认传进来的 ctx（不再回读 contextvar）。"""
    from src.core.ai_client import AIClient

    client = AIClient.__new__(AIClient)
    client.scene = "chat"
    client.model = "m"
    seen = {}

    class _DB:
        def add(self, row):
            seen["user_id"] = getattr(row, "user_id", None)
            seen["run_id"] = getattr(row, "run_id", None)

        def commit(self):
            pass

        def close(self):
            pass

    import src.db.session as dbs

    orig = dbs.SessionLocal
    dbs.SessionLocal = lambda: _DB()  # type: ignore[assignment]
    try:
        client._log_usage_sync("chat", "m", SimpleNamespace(prompt_tokens=1, completion_tokens=1), 1,
                               {"user_id": "u2", "run_id": "r2", "scene": "chat"})
    finally:
        dbs.SessionLocal = orig
    assert seen == {"user_id": "u2", "run_id": "r2"}
