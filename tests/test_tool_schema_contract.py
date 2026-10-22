"""工具 schema 契约：所有 description 必须是字符串（2026-10-22 线上事故回归）。

事故：`get_stock_factors` 的 description 因为多写了一个逗号变成 tuple，JSON 序列化成数组，
agnes 网关直接 500（`json: cannot unmarshal array into ... description of type string`）。
它只在「全量工具集」的兜底路径发作 —— 配方路径的工具子集恰好不含它，所以是
「只有问价这类最普通的问题才报 AI 服务不可用」，排查方向极易跑偏。
"""

from __future__ import annotations

import json

from src.agents.chat.registry import chat_tool_schemas


def _walk_bad(node, path, out):
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "description" and not isinstance(v, str):
                out.append((path + "." + k, type(v).__name__))
            else:
                _walk_bad(v, path + "." + k, out)
    elif isinstance(node, (list, tuple)):
        for i, v in enumerate(node):
            _walk_bad(v, f"{path}[{i}]", out)


def test_every_description_is_string():
    bad = []
    for sc in chat_tool_schemas():
        name = (sc.get("function") or {}).get("name") or "?"
        _walk_bad(sc, name, bad)
    assert not bad, f"description 不是字符串: {bad[:5]}"


def test_schemas_serialize_to_json_without_arrays_in_description():
    payload = json.dumps(chat_tool_schemas(), ensure_ascii=False)
    assert '"description": [' not in payload, "description 被序列化成了数组，网关会 500"


def test_sanitizer_coerces_tuple_description():
    from src.core.ai_client import _sanitize_tools

    tools = [{"type": "function", "function": {"name": "x", "description": ("a", "b"),
                                               "parameters": {"type": "object", "properties": {}}}}]
    out = _sanitize_tools(tools)
    assert out[0]["function"]["description"] == "ab"
