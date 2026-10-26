"""参谋能力清单（CAPABILITY_NOTES）——新工具接进注册表 ≠ 模型知道。

背景（2026-10-25 用户截图）：v0.59.0 已上线两融工具，参谋仍回答
「我本地没有 7 月以来的新闻库记录，无法逐周回溯内部资金面/两融数据」。
**工具在，但模型凭旧印象作答** —— 等于没接。

所以把「有什么 / 没有什么」写成事实清单拼在 SYSTEM_PROMPT 后面，
并在这里钉住关键条目，防止以后新增能力忘记更新清单。
"""

from __future__ import annotations

import re

from src.agents.chat.capability_notes import CAPABILITY_NOTES


def test_清单里必须写明已具备的关键能力():
    for tool in ("get_range_summary", "get_margin_trading", "get_macro_series",
                 "search_news", "web_search_fallback"):
        assert tool in CAPABILITY_NOTES, f"能力清单漏了 {tool}"


def test_必须写明指数历史已支持_避免模型继续声称没有():
    assert "index_daily" in CAPABILITY_NOTES
    assert "不要再声称" in CAPABILITY_NOTES
    # 主要指数代码要列出来，否则模型不知道 000001 可以传
    for code in ("000001", "399001", "399006", "000300"):
        assert code in CAPABILITY_NOTES


def test_必须写明缺什么_防止拿别的数据冒充():
    for gap in ("险资", "汇金", "ETF 份额", "板块涨幅榜"):
        assert gap in CAPABILITY_NOTES, f"缺口清单漏了 {gap}"
    assert "不要用两融、资金流或涨停池去冒充" in CAPABILITY_NOTES


def test_两融缺所日口径要写进去():
    assert "缺所日" in CAPABILITY_NOTES or "不可比" in CAPABILITY_NOTES


def test_联网兜底必须带来源标注要求():
    assert "非系统数据源" in CAPABILITY_NOTES
    assert "仅当系统数据源" in CAPABILITY_NOTES


def test_清单已拼进实际使用的_system_content(monkeypatch):
    """不是只写在模块里 —— 要真的进 system_content。"""
    import src.web.api.chat as chat_api

    src = open(chat_api.__file__, encoding="utf-8").read()
    assert re.search(r"system_content\s*=\s*SYSTEM_PROMPT\s*\+\s*CAPABILITY_NOTES", src), \
        "chat.py 里没有把 CAPABILITY_NOTES 拼进 system_content"


def test_chat_py_仍在行数护栏内():
    """chat.py 有 2310 行硬护栏；清单走独立模块就是为了不撑破它。"""
    import src.web.api.chat as chat_api

    n = len(open(chat_api.__file__, encoding="utf-8").read().split("\n"))
    assert n <= 2310, f"chat.py 超护栏: {n} 行"
