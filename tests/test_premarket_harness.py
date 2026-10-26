"""盘前 agent 的 Harness 优化验收（2026-10-20）。

背景（真实事故 2026-10-20 08:52）：prompt 一次 39,323 字符、同 trace 三次重打、
板块轮动/push2 断连、LLM 429 → 整份报告 degraded 且**一条都没推**。

这里锁三件事：
  1. 上下文超预算时按优先级整段丢弃（不截半句）；
  2. LLM 不可用时要能渲染"事实快报"（数据照发，AI 段落标注不可用）；
  3. 基类允许"降级但带 fallback_brief"的推送，其余降级仍然拦。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.agents.base import BaseAgent
from src.agents.premarket_outlook import PremarketOutlookAgent


def _agent() -> PremarketOutlookAgent:
    a = PremarketOutlookAgent()
    a.name = "premarket_outlook"
    a.display_name = "盘前分析"
    return a


def _ctx(watchlist=None, model_label=""):
    return SimpleNamespace(watchlist=watchlist or [], model_label=model_label)


def test_budget_drops_low_priority_sections_first():
    a = _agent()
    big = "\n".join([f"- 行{i}" for i in range(2000)])
    text = (
        "## 主要指数\n- 上证 3000\n"
        f"## 近48小时事件流\n{big}\n"
        f"## 相关新闻资讯\n{big}\n"
        f"## 突发重磅科技/大宗事件全市场受益龙头映射\n{big}\n"
        "## 自选股技术状态\n- 600111 35.23\n"
    )
    out, dropped = a._apply_context_budget(text, limit=8000)
    assert len(out) <= 8000 + 400            # 附带一行省略说明
    assert any("突发重磅科技" in d for d in dropped)   # 优先级最高，先丢
    assert "## 主要指数" in out               # 关键事实保留
    assert "## 自选股技术状态" in out


def test_budget_noop_under_limit():
    a = _agent()
    text = "## A\n- 1\n## B\n- 2\n"
    out, dropped = a._apply_context_budget(text, limit=24000)
    assert out == text and dropped == []


def test_fact_brief_renders_real_numbers_and_marks_ai_down():
    a = _agent()
    data = {
        "timestamp": "2026-10-20T08:52:00",
        "us_indices": [{"name": "道指", "current": 43000, "change_pct": 0.42}],
        "mood": {"stage": "分歧", "temperature": 45, "limit_up": 32, "limit_down": 5},
        "event_stream": [SimpleNamespace(title="吴清12条定调资本市场")],
        "theme_candidates": [{"theme": "人形机器人", "leaders": [{"name": "三花智控"}]}],
        "signal_packs": {"600111": SimpleNamespace(quote=SimpleNamespace(current_price=35.23, change_pct=-0.25))},
        "quality_overview": {"failed": ["板块轮动-industries"]},
    }
    ctx = _ctx([SimpleNamespace(symbol="600111", name="北方稀土")])
    brief = a._build_fact_brief(data, ctx, "429 限流")

    assert "AI 解读本轮不可用" in brief and "429" in brief
    assert "道指" in brief and "+0.42%" in brief
    assert "北方稀土(600111)" in brief and "35.23" in brief
    assert "吴清12条" in brief and "人形机器人" in brief
    assert "板块轮动-industries" in brief      # 缺失的数据源要写出来
    # 不许出现编造的结论性话术
    assert "建议买入" not in brief and "目标价" not in brief


def test_base_gate_allows_degraded_with_fallback_brief():
    class _A(BaseAgent):
        name = "x"
        display_name = "X"
        allow_degraded_notify = True

        async def collect(self, context):  # pragma: no cover - 仅为可实例化
            return {}

        def build_prompt(self, data, context):  # pragma: no cover
            return "", ""

    from src.agents.base import AnalysisResult

    r = AnalysisResult(agent_name="x", title="t", content="c",
                       raw_data={"fallback_brief": True}, status="degraded", error="429")
    import asyncio
    assert asyncio.run(_A().should_notify(r)) is True


def test_base_gate_still_blocks_plain_degraded():
    class _B(BaseAgent):
        name = "y"
        display_name = "Y"

        async def collect(self, context):  # pragma: no cover
            return {}

        def build_prompt(self, data, context):  # pragma: no cover
            return "", ""

    from src.agents.base import AnalysisResult

    r = AnalysisResult(agent_name="y", title="t", content="c", raw_data={}, status="degraded", error="429")
    import asyncio
    assert asyncio.run(_B().should_notify(r)) is False


def test_premarket_agent_opts_into_degraded_notify():
    assert PremarketOutlookAgent.allow_degraded_notify is True
