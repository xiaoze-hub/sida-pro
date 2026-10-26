# -*- coding: utf-8 -*-
"""chat 工具注册表规格守卫。

每条测试绑定一条 specs 里的验收标准(门禁 scripts/trace_specs.py 双向校验)。
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agents.chat.registry import CHAT_TOOL_REGISTRY, chat_tool_schemas

# 规格要守的是**注册表本体**(CHAT_TOOL_REGISTRY: name -> ChatTool),
# 不是导给前端的 schema 列表 —— schema 里不携带 caliber/requires。
CHAT_TOOLS = list(CHAT_TOOL_REGISTRY.values())
SCHEMAS = chat_tool_schemas()

# handler-only 工具: 有 handler 但 schema is None, 不暴露给模型。
HANDLER_ONLY = {"get_opportunities"}


def _spec_field(label: str) -> int:
    """从 specs/tools/chat_registry.md 的表格里解析某个数字。

    表格形如: | **注册表** | ... | **66** |
    """
    spec = (ROOT / "specs" / "tools" / "chat_registry.md").read_text(encoding="utf-8")
    for line in spec.splitlines():
        if label in line:
            m = re.search(r"\*\*(\d+)\*\*", line)
            if m:
                return int(m.group(1))
    raise AssertionError(f"specs/tools/chat_registry.md 里找不到 {label} 的数字")


def test_every_tool_has_caliber():
    """@spec TOOL-001
    每个 chat 工具必须注册 caliber(口径标签)且非空。
    """
    missing = [t.name for t in CHAT_TOOLS if not (t.caliber or "").strip()]
    assert not missing, f"这些工具缺 caliber(口径标签): {missing}"


def test_capital_flow_caliber_is_redline():
    """@spec TOOL-002
    get_capital_flow 的 caliber 必须是固定红线文案。
    """
    t = CHAT_TOOL_REGISTRY.get("get_capital_flow")
    assert t is not None, "get_capital_flow 未注册"
    caliber = t.caliber or ""
    # 红线的实际文案: 明确禁止用这个工具的数值判定主力意图
    # (东财四档方向位与逐笔相反 —— 平台「主力净流入」是第三方估算口径)。
    assert "禁止据此判定主力意图" in caliber, (
        f"get_capital_flow 的 caliber 必须含「禁止据此判定主力意图」红线, 实际: {caliber!r}"
    )


def test_registry_count_matches_specs():
    """@spec TOOL-003
    注册表工具总数必须与 specs 声明的一致。
    新增工具漏改 specs → 此测试失败。
    """
    declared = _spec_field("注册表")
    actual = len(CHAT_TOOLS)
    assert actual == declared, (
        f"注册表工具总数与 specs 不一致: 代码 {actual} 个, specs 声明 {declared} 个。"
        f"新增/删除工具后必须同步更新 specs/tools/chat_registry.md"
    )


def test_visible_tools_count_matches_specs():
    """@spec TOOL-004
    对模型可见的工具数 = 注册表数 − handler-only 数。
    两者对不上说明某个工具的 schema 漏配或多配。
    """
    declared_visible = _spec_field("对模型可见")
    actual_visible = len(SCHEMAS)
    expected = len(CHAT_TOOLS) - len(HANDLER_ONLY)
    assert actual_visible == expected, (
        f"对模型可见的工具数不对: 实际 {actual_visible}, "
        f"注册表 {len(CHAT_TOOLS)} − handler-only {len(HANDLER_ONLY)} = {expected}。"
        f"可能是某个工具 schema 漏配或多配"
    )
    assert actual_visible == declared_visible, (
        f"对模型可见的工具数与 specs 不一致: 代码 {actual_visible}, "
        f"specs 声明 {declared_visible}"
    )
