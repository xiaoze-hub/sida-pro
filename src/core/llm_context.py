"""LLM 调用的归因上下文（2026-10-20, P3 成本看板/多用户配额）。

问题：`ai_client._log_usage` 只记 scene/model，不记"谁"和"哪一轮"，
于是成本看板回答不了"谁在烧 token"，配额也没法在网关层硬拒。

做法：请求进入时把 user_id / run_id 塞进 contextvar，`_log_usage_sync` 落库时读出来。
contextvar 在 asyncio 任务内自动继承，不会串到别的请求（不要用全局变量）。

用法：
    token = bind(user_id=..., run_id=...)
    try: ...
    finally: unbind(token)
"""
from __future__ import annotations

import contextvars

_user_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("llm_user_id", default=None)
_run_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("llm_run_id", default=None)
_scene: contextvars.ContextVar[str | None] = contextvars.ContextVar("llm_scene", default=None)


def bind(*, user_id=None, run_id=None, scene=None):
    """返回 token 列表；调用方在 finally 里传回 unbind()。"""
    tokens = []
    if user_id is not None:
        tokens.append((_user_id, _user_id.set(str(user_id))))
    if run_id is not None:
        tokens.append((_run_id, _run_id.set(str(run_id))))
    if scene is not None:
        tokens.append((_scene, _scene.set(str(scene))))
    return tokens


def bind_chat(*, user_id=None, run_id=None):
    """整轮替换归因；None 明确清除旧值，禁止沿用上一轮身份或轨迹。"""
    return [
        (_user_id, _user_id.set(str(user_id) if user_id is not None else None)),
        (_run_id, _run_id.set(str(run_id) if run_id is not None else None)),
        (_scene, _scene.set("chat")),
    ]


def unbind(tokens) -> None:
    for var, tok in reversed(tokens or []):
        try:
            var.reset(tok)
        except Exception:  # noqa: BLE001 - 跨任务 reset 会报错, 忽略即可
            pass


def current() -> dict:
    return {"user_id": _user_id.get(), "run_id": _run_id.get(), "scene": _scene.get()}
