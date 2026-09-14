"""回归: 调度器的 context_builder 契约 —— uid 必须进 `user_id`, **绝不能**进 `stock_agent_id`。

生产事故(2026-09-10 v0.5.37 M7 多用户隔离上线 ~ 2026-09-14): 接线处直接把 `build_context`
交给调度器, 而 `AgentScheduler._build_contexts` 是**位置传参** `builder(agent_name, uid)`,
`build_context` 的第 2 形参却是 `stock_agent_id` ⇒ UUID 落入 `stock_agent_id`,
`resolve_ai_model`/`resolve_notify_channels` 拿它去 `WHERE stock_agents.id = '<uuid>'`:

    psycopg2.errors.InvalidTextRepresentation: invalid input syntax for type integer: "3a5a..."

后果是**每轮调度都失败**(盘中监测每 3 分钟一次, agent_runs 里 464 条 failed), 且 `user_id`
停在 `_UNSET` ⇒ M7 的多用户隔离形同虚设。`_build_contexts` 里的 `except TypeError` 兜不住
psycopg2 错误, 所以这个错能潜伏数日只在 agent_runs 留痕。

本文件守三件事:
  ① 适配层把第 2 个位置参数送进 `user_id`, 且 `stock_agent_id` 保持 None;
  ② 接线处用的确实是适配层(不是裸 `build_context`) —— 只测适配层会发现不了"忘了接线";
  ③ `build_context` 的签名顺序没变(第 2 形参仍是 stock_agent_id) —— 一旦有人调换签名,
     本文件的 ①② 前提失效, 这条会红, 逼人重审适配层是否还需要。
"""
from __future__ import annotations

import inspect
from unittest import mock

import pytest

from src.bootstrap import runtime as rt


def test_adapter_sends_uid_to_user_id_not_stock_agent_id() -> None:
    """① 第 2 个位置参数(uid) 必须进 `user_id`, `stock_agent_id` 恒为 None。"""
    uid = "3a5a119b-125c-4946-9cd1-099c63a6367d"
    with mock.patch.object(rt, "build_context", return_value="CTX") as m:
        out = rt._scheduled_context_builder("intraday_monitor", uid)
    assert out == "CTX"
    assert m.call_count == 1
    args, kwargs = m.call_args
    assert args == ("intraday_monitor",), f"不得再位置传 2 个参: {args}"
    assert kwargs.get("user_id") == uid, "uid 必须进 user_id"
    assert kwargs.get("stock_agent_id") is None, (
        "stock_agent_id 必须保持 None —— 定时运行不属于某个具体绑定(传参上去就是本轮事故)"
    )


def test_adapter_accepts_missing_uid() -> None:
    """调度器在无用户桶时按 1 个参数调用, 适配层要能吃下。"""
    with mock.patch.object(rt, "build_context", return_value="CTX") as m:
        assert rt._scheduled_context_builder("intraday_monitor") == "CTX"
    assert m.call_args.kwargs.get("user_id") is None


def test_build_context_second_positional_is_stock_agent_id() -> None:
    """③ 前提守卫: `build_context` 第 2 形参仍是 `stock_agent_id`。

    这条不是在测业务, 而是**钉住适配层存在的理由**: 若将来有人把 build_context 的签名改成
    `user_id` 在第 2 位, 那么裸接线也不再串位, 适配层就该被重新评估 —— 本用例会红, 提醒重审。
    """
    params = list(inspect.signature(rt.build_context).parameters)
    assert params[:3] == ["agent_name", "stock_agent_id", "user_id"], (
        f"build_context 形参顺序变了: {params[:3]} —— 请重审 _scheduled_context_builder 是否仍需适配"
    )


def test_build_scheduler_wires_the_adapter_not_bare_build_context() -> None:
    """② 接线处必须用适配层。只测适配层无法发现"接线时又换回裸函数"这种回归。"""
    captured: dict = {}

    class _FakeSched:
        def __init__(self, timezone=None):
            pass

        def set_context_builder(self, b):
            captured["builder"] = b

        def set_user_bucket_resolver(self, r):
            captured["resolver"] = r

        def register(self, *a, **k):
            pass

        def start(self):
            pass

    with mock.patch.object(rt, "AgentScheduler", _FakeSched), \
         mock.patch.object(rt, "SessionLocal") as sl:
        sl.return_value.query.side_effect = Exception("stop-here: 只为捕获接线")
        with pytest.raises(Exception):
            rt.build_scheduler()

    b = captured.get("builder")
    assert b is not None, "build_scheduler 应设置 context_builder"
    assert b is rt._scheduled_context_builder, (
        "必须接 _scheduled_context_builder; 接裸 build_context 会让 uid 串位到 stock_agent_id(本轮生产事故)"
    )
