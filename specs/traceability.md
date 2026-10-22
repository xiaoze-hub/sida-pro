# 规格追溯表 (自动生成, 请勿手工修改)

- 验收标准: **4** 条
- 已覆盖: **4** 条 (100.0%)
- 测试用例: **4** 个

| 规格 ID | 摘要 | 规格位置 | 守卫测试 |
|---|---|---|---|
| `TOOL-001` | 每个 chat 工具必须注册 `caliber`（口径标签）且非空 —— 没有口径的结论会被误读成事实 | `specs/tools/chat_registry.md:18` | `tests/test_spec_chat_registry.py:40` `test_every_tool_has_caliber` |
| `TOOL-002` | `get_capital_flow` 的 caliber 必须包含「禁止据此判定主力意图」红线 —— 该工具是东财四档口径，方向位与逐笔相反，不能当主力意图用 | `specs/tools/chat_registry.md:19` | `tests/test_spec_chat_registry.py:48` `test_capital_flow_caliber_is_redline` |
| `TOOL-003` | 注册表工具总数必须与本规格声明的一致 —— 新增工具漏改规格会在此失败 | `specs/tools/chat_registry.md:20` | `tests/test_spec_chat_registry.py:62` `test_registry_count_matches_specs` |
| `TOOL-004` | 对模型可见的工具数 = 注册表数 − handler-only 数 —— 两者对不上说明 schema 漏配或多配 | `specs/tools/chat_registry.md:21` | `tests/test_spec_chat_registry.py:75` `test_visible_tools_count_matches_specs` |
