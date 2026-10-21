"""轨迹的「配方回写」与「失败分类」（2026-10-22）。

两个线上可见的缺陷，各自一个断言口径：
1. `copilot_runs.recipe` 永远 NULL —— 轨迹在路由前创建，命中哪套配方没人回写，
   于是「配方改好改坏」没有回放依据。→ `set_recipe()` 必须内存 + 落库双写。
2. `status` 只有 ok —— 工具全挂和全成都记 ok，治理页的失败率永远是 0。
   → 全败 error / 部分失败 degraded / 全成 ok，且调用方显式状态优先。

写库失败是旁路（trace 所有写入都吞异常），所以这里一律查库读回，不信任返回值。
"""
from __future__ import annotations

from src.db.models import CopilotRun, CopilotStep
from src.db.session import SessionLocal
from src.harness.trace import CopilotTrace, finish_copilot_run, set_recipe


def _cleanup(run_id: str) -> None:
    db = SessionLocal()
    try:
        db.query(CopilotStep).filter(CopilotStep.run_id == run_id).delete()
        db.query(CopilotRun).filter(CopilotRun.run_id == run_id).delete()
        db.commit()
    finally:
        db.close()


def _row(trace: CopilotTrace) -> CopilotRun:
    db = SessionLocal()
    try:
        return db.query(CopilotRun).filter(CopilotRun.run_id == trace.run_id).one()
    finally:
        db.close()


def _new_trace(**kw) -> CopilotTrace:
    trace = CopilotTrace(conversation_id=kw.pop("conversation_id", 7), user_id=None, **kw)
    trace.open()
    return trace


# -- ① 配方回写 ---------------------------------------------------------------

def test_set_recipe_writes_memory_and_db():
    """路由之后回写：内存立即生效，已落库的 running 行也要改掉（不是等 finish）。"""
    trace = _new_trace(question="我的票竞价怎么样")
    try:
        assert trace.recipe is None
        assert _row(trace).recipe is None   # 复现缺陷：路由前建的行走 NULL

        set_recipe(trace, "auction_hunter")

        assert trace.recipe == "auction_hunter"
        assert _row(trace).recipe == "auction_hunter"
        # running 行还在跑，回写不能顺手把状态改掉
        assert _row(trace).status == "running"
    finally:
        _cleanup(trace.run_id)


def test_set_recipe_none_clears_both_sides():
    """改成 None 表示本轮没命中配方，库里不能留着上一轮的 id。"""
    trace = _new_trace(recipe="limit_up_ladder")
    try:
        assert _row(trace).recipe == "limit_up_ladder"
        set_recipe(trace, None)
        assert trace.recipe is None
        assert _row(trace).recipe is None
    finally:
        _cleanup(trace.run_id)


def test_set_recipe_with_none_trace_is_noop():
    """调用点手里是 trace | None，None 必须安全返回。"""
    assert set_recipe(None, "auction_hunter") is None
    assert set_recipe(None, None) is None


# -- ② 失败分类 ---------------------------------------------------------------

def test_partial_failure_marks_degraded():
    """2 败 1 成：本轮仍有答案，但结论是残缺的 —— degraded，不是 ok。"""
    trace = _new_trace(recipe="auction_hunter")
    try:
        trace.step("get_auction_data", {}, ok=False, error="upstream 502")
        trace.step("get_watchlist", {})
        trace.step("get_capital_flow", {}, ok=False, error="empty result")

        finish_copilot_run(trace)

        run = _row(trace)
        assert run.status == "degraded"
        assert run.tool_calls == 3
        assert run.finished_at is not None
        assert "failed_tools=2/3" in (run.error or "")
        assert "get_auction_data: upstream 502" in (run.error or "")
    finally:
        _cleanup(trace.run_id)


def test_all_failed_marks_error():
    """工具全挂：这轮基本没有事实依据，必须 error。"""
    trace = _new_trace()
    try:
        trace.step("get_auction_data", {}, ok=False, error="timeout 8s")
        trace.step("get_watchlist", {}, ok=False, error="no session")

        finish_copilot_run(trace)

        run = _row(trace)
        assert run.status == "error"
        assert "failed_tools=2/2" in (run.error or "")
        assert "timeout 8s" in (run.error or "")
    finally:
        _cleanup(trace.run_id)


def test_all_ok_marks_ok_and_keeps_error_null():
    trace = _new_trace()
    try:
        trace.step("get_auction_data", {})
        trace.step("get_watchlist", {})

        finish_copilot_run(trace)

        run = _row(trace)
        assert run.status == "ok"
        assert run.error is None
    finally:
        _cleanup(trace.run_id)


def test_no_tools_no_error_is_ok():
    """纯聊天（没调工具）不是失败。"""
    trace = _new_trace()
    try:
        finish_copilot_run(trace)
        run = _row(trace)
        assert run.status == "ok"
        assert run.tool_calls == 0
        assert run.error is None
    finally:
        _cleanup(trace.run_id)


def test_explicit_status_is_not_overridden():
    """调用方显式说 timeout，就别被「工具部分成功」降级成 degraded。"""
    trace = _new_trace()
    try:
        trace.step("get_auction_data", {})
        trace.step("get_watchlist", {}, ok=False, error="boom")

        finish_copilot_run(trace, status="timeout", error="LLM 响应超时")

        run = _row(trace)
        assert run.status == "timeout"
        # 显式错误保留在前，工具失败明细附后（不丢治理信息）
        assert (run.error or "").startswith("LLM 响应超时")
        assert "failed_tools=1/2" in (run.error or "")
    finally:
        _cleanup(trace.run_id)


def test_finish_none_trace_and_method_path_agree():
    """模块入口与实例方法两条路的状态口径必须一致。"""
    assert finish_copilot_run(None) is None

    trace = _new_trace()
    try:
        trace.step("a", {}, ok=False, error="x")
        trace.step("b", {}, ok=False, error="y")
        trace.finish()          # 直接调方法（老调用点）
        assert _row(trace).status == "error"
    finally:
        _cleanup(trace.run_id)


def test_recipe_survives_finish_after_set():
    """回写过的配方不能在收尾时被覆盖回 NULL。"""
    trace = _new_trace()
    try:
        set_recipe(trace, "earnings_digest")
        trace.step("get_financials", {})
        finish_copilot_run(trace)
        assert _row(trace).recipe == "earnings_digest"
        assert _row(trace).status == "ok"
    finally:
        _cleanup(trace.run_id)
