"""问价快答（2026-10-22）：纯读数问题不走模型。

起因：问「600111 现价多少」走完整工具循环要 70 秒，工具只占 2~5 秒，
其余全是模型生成（一轮 2 次调用）。而这类问题工具已经给了确定数字。
风险是**误伤分析类问题** —— 所以「多少钱 → 快答」「能不能拿 → 必须走诊断」是硬边界。
"""

from __future__ import annotations

import pytest

from src.harness import quick_answer as qa


@pytest.mark.parametrize(
    "text",
    ["600111 现价多少", "北方稀土多少钱", "600111 价格", "神剑股份 现在什么价", "002361 涨跌幅"],
)
def test_pure_quote_questions(text):
    assert qa.is_pure_quote(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "600111 还能持有吗",          # 判断类
        "北方稀土今日冲高回落，还能拿吗，是洗盘还是出货",
        "600111 走势怎么样",
        "600111 现价多少，还能买吗",   # 混合意图一律不算纯读数
        "",                           # 空
        "今天大盘怎么样",
    ],
)
def test_analysis_questions_are_not_quick(text):
    assert qa.is_pure_quote(text) is False


def test_long_sentence_is_not_quick():
    """长句基本都带判断，不许猜。"""
    assert qa.is_pure_quote("600111 现价多少" + "，另外我想知道这个位置能不能加仓" * 2) is False


def test_resolve_symbol_prefers_code_then_bound_then_name(monkeypatch):
    assert qa.resolve_symbol("600111 现价多少") == "600111"
    assert qa.resolve_symbol("多少钱", "002361.SZ") == "002361"
    monkeypatch.setattr("src.core.symbol_guard.first_code_from_names", lambda t: "600111")
    assert qa.resolve_symbol("北方稀土多少钱") == "600111"


@pytest.mark.asyncio
async def test_answer_formats_tool_output(monkeypatch):
    """快答必须原样带工具口径，并标注"未经模型改写"。"""
    from src.agents.chat.registry import CHAT_TOOL_REGISTRY

    class _Fake:
        async def handler(self, db, args, user=None):
            assert args["symbol"] == "600111"
            return "行情（实时）：北方稀土（CN:600111）价格 35.77，涨跌幅 -0.31%\n口径：实时 RPC 15:47"

    monkeypatch.setitem(CHAT_TOOL_REGISTRY, "get_stock_quote", _Fake())
    out = await qa.answer(None, "600111 现价多少", None, None)
    assert out is not None
    assert "35.77" in out
    assert "未经模型改写" in out
    assert "不构成投资建议" in out


@pytest.mark.asyncio
async def test_answer_returns_none_when_no_data(monkeypatch):
    """取不到数就别抢答 —— 让主流程去走降级/说明，而不是回一句空壳。"""
    from src.agents.chat.registry import CHAT_TOOL_REGISTRY

    class _Fake:
        async def handler(self, db, args, user=None):
            return "未能获取 CN:600111 的行情数据。\n口径：库无新鲜快照"

    monkeypatch.setitem(CHAT_TOOL_REGISTRY, "get_stock_quote", _Fake())
    assert await qa.answer(None, "600111 现价多少", None, None) is None


@pytest.mark.asyncio
async def test_answer_returns_none_for_analysis_question():
    assert await qa.answer(None, "600111 还能持有吗", "600111", None) is None


@pytest.mark.asyncio
async def test_answer_swallows_handler_exception(monkeypatch):
    """工具炸了也必须回落到主流程，不能把对话打断。"""
    from src.agents.chat.registry import CHAT_TOOL_REGISTRY

    class _Boom:
        async def handler(self, db, args, user=None):
            raise RuntimeError("boom")

    monkeypatch.setitem(CHAT_TOOL_REGISTRY, "get_stock_quote", _Boom())
    assert await qa.answer(None, "600111 现价多少", None, None) is None


@pytest.mark.asyncio
async def test_answer_records_trace_step(monkeypatch):
    """快答也要留痕：否则治理页看到 tool_calls=0，会被误读成「零工具作答」。"""
    from src.agents.chat.registry import CHAT_TOOL_REGISTRY

    steps: list[tuple] = []

    class _Trace:
        def step(self, tool, args=None, **kw):
            steps.append((tool, args))

    class _Runtime:
        trace = _Trace()

    class _Fake:
        async def handler(self, db, args, user=None):
            return "行情：600111 价格 35.77，涨跌幅 -0.31%"

    monkeypatch.setitem(CHAT_TOOL_REGISTRY, "get_stock_quote", _Fake())
    out = await qa.answer(None, "600111 现价多少", None, None, _Runtime())
    assert out and "35.77" in out
    assert steps and steps[0][0] == "get_stock_quote"


def test_polish_makes_raw_numbers_readable():
    """快答是原文给用户看的，成交量/涨跌幅的毛刺必须自己收拾。"""
    out = qa.polish("实时行情：（CN:600111）价格 35.77，涨跌幅 -0.3066%，成交量 421023.0", None)
    assert "42.10 万股" in out
    assert "涨跌幅 -0.31%" in out
    assert "421023.0" not in out


def test_polish_large_volume_uses_yi():
    assert "1.23 亿股" in qa.polish("成交量 123456789.0", None)


def test_polish_never_crashes_without_name(monkeypatch):
    monkeypatch.setattr(qa, "_name_of", lambda s: None)
    assert "35.77" in qa.polish("价格 35.77", "600111")
