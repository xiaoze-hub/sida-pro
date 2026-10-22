# 数智参谋 · Chat 工具注册表契约

规格事实源。对应实现: `src/agents/chat/registry.py`（注册表本体）
与 `src/web/api/chat.py`（`CHAT_TOOLS = chat_tool_schemas()`，导给模型的那份）。

## 术语

| 名字 | 含义 | 数量 |
|---|---|---|
| **注册表** | `CHAT_TOOL_REGISTRY`，name → `ChatTool` | **66** |
| **对模型可见** | `chat_tool_schemas()` 导出，有 schema 的那些 | **64** |
| **handler-only** | 有 handler 但 `schema is None`，不暴露给模型 | **2** |

handler-only 的两个：`get_opportunities`、`note_new_dimension`。

## 验收标准

- **TOOL-001** 每个 chat 工具必须注册 `caliber`（口径标签）且非空 —— 没有口径的结论会被误读成事实
- **TOOL-002** `get_capital_flow` 的 caliber 必须包含「禁止据此判定主力意图」红线 —— 该工具是东财四档口径，方向位与逐笔相反，不能当主力意图用
- **TOOL-003** 注册表工具总数必须与本规格声明的一致 —— 新增工具漏改规格会在此失败
- **TOOL-004** 对模型可见的工具数 = 注册表数 − handler-only 数 —— 两者对不上说明 schema 漏配或多配
