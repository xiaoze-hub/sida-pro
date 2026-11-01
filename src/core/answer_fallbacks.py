"""回答质量兜底文案（2026-11-01）。

为什么要独立成模块：`src/web/api/chat.py` 有 2310 行硬护栏（`tests/test_w33_chat_registry.py`
与 `tests/test_capability_notes.py` 双重钉住），兜底文案放进去会直接顶破门禁。

线上实例（2026-11-01 01:49）：模型返回空内容，5 个工具全部成功、轨迹 status=ok，
`chat_messages.content` 落库 0 字 —— 用户拿到一个空气泡，看板还算一次成功。
"""

from __future__ import annotations


def empty_final_notice(tool_count: int = 0) -> str:
    """模型本轮返回空内容时的兜底文案。

    原则：说清"不是你的问题、下一步做什么"，不假装这是分析结论，也不把空内容当成品。
    """
    got = f"已取到 {tool_count} 项数据，但" if tool_count > 0 else ""
    return (
        f"**模型本轮没有返回内容**（{got}没有生成结论）。"
        "这通常是模型服务端的空响应，不是你问错了 —— 直接重发一次，或在右上角换一个模型再问；"
        "已取到的数据不会重复消耗。"
    )
