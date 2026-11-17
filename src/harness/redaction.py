# -*- coding: utf-8 -*-
"""轨迹/日志里的图表摘要脱敏(2026-11-17 二轮审计缺口 2)。

问题: 缠论快照的 ``digest`` 按契约「只进本轮请求上下文, 不入库、不跨轮复用」,
但 ``copilot_steps`` 会把每次工具调用的 args/result 前若干字落库 —— 工具参数里有模型
回传的完整摘要, 工具结果第 3 行是「摘要 <64hex>」。两条路径都让一份"本轮专用"的
摘要变成了**库里可跨轮读取的持久值**, 也给了下一轮复用旧摘要的机会。

做法: 只对**声明过摘要语义的工具**做收缩(默认 ``get_chan_structure``), 把 64 位小写
十六进制摘要替换成「前 8 位 + …」。为什么不全局替换所有哈希: 其它工具的 result 里可能
有正常的签名字符串/校验和, 全局正则会把它们一起打断。为什么保留前 8 位: 治理页/回放 UI
要能肉眼对上"这是不是同一份", 8 位足够区分, 且不足以重放(完整摘要才是端点凭据)。

边界(诚实): 用户自己在消息正文里贴出的完整摘要属于**用户内容**, 系统无法也不该改写;
这里只保证「本功能自己产生的摘要」不会被自己持久化。
"""
from __future__ import annotations

import logging
import re
from typing import Any

#: 结果/参数里出现 64 位小写十六进制的地方一律视为「图表快照摘要」。
_DIGEST64_RE = re.compile(r"(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])")

#: 保留的可见前缀长度(与 UI 展示口径一致)。
SHORT_DIGEST_CHARS = 8

#: 需要脱敏的工具名(精确匹配; 只有这些工具的摘要属于"本轮专用凭据")。
DIGEST_TOOLS: frozenset[str] = frozenset({"get_chan_structure"})


def is_digest_tool(tool: str | None) -> bool:
    """该工具的 args/result 是否含图表快照摘要。"""
    return str(tool or "") in DIGEST_TOOLS


def short_digest(value: Any, keep: int = SHORT_DIGEST_CHARS) -> str:
    """64 位摘要 → 前 ``keep`` 位 + '…'; 其它值原样转字符串。"""
    text = "" if value is None else str(value)
    if len(text) == 64 and _DIGEST64_RE.fullmatch(text):
        return text[:keep] + "…"
    return text


def redact_digests(value: Any, keep: int = SHORT_DIGEST_CHARS) -> Any:
    """递归收缩字符串/字典/列表里的完整摘要(保持结构, 不改键名)。"""
    if isinstance(value, str):
        return _DIGEST64_RE.sub(lambda m: m.group(0)[:keep] + "…", value)
    if isinstance(value, dict):
        return {k: redact_digests(v, keep=keep) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_digests(v, keep=keep) for v in value]
    return value


def redact_step_args(tool: str | None, args: Any) -> Any:
    """轨迹/日志用: 只有摘要类工具的参数才脱敏。"""
    return redact_digests(args) if is_digest_tool(tool) else args


def redact_step_result(tool: str | None, result: Any) -> Any:
    """轨迹/日志用: 只有摘要类工具的结果才脱敏。"""
    return redact_digests(result) if is_digest_tool(tool) else result


def log_tool_call(tool: str, args: Any) -> None:
    """工具调用日志的唯一出口（日志也是落盘路径, 与轨迹同口径收缩）。"""
    logging.getLogger("src.web.api.chat").info(
        "Tool call: %s(%s)", tool, redact_step_args(tool, args),
    )


__all__ = [
    "DIGEST_TOOLS",
    "SHORT_DIGEST_CHARS",
    "is_digest_tool",
    "log_tool_call",
    "redact_digests",
    "redact_step_args",
    "redact_step_result",
    "short_digest",
]
