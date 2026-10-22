# 规格追溯表 (自动生成, 请勿手工修改)

- 验收标准: **19** 条
- 已覆盖: **19** 条 (100.0%)
- 测试用例: **28** 个

| 规格 ID | 摘要 | 规格位置 | 守卫测试 |
|---|---|---|---|
| `DATA-001` | `get_capital_flow` 的 `caliber` 必须显式包含「禁止据此判定主力意图」类红线 —— 它是按单金额四档归类，不是逐笔方向 | `specs/caliber/data_caliber.md:15` | `tests/test_spec_data_caliber.py:26` `test_capital_flow_caliber_has_redline` |
| `DATA-002` | 方向性判定只能走逐笔口径：`require_directional()` 对四档口径必须抛 `CaliberViolationError`，而 `get_main_intent` 的 caliber 必须是逐笔 | `specs/caliber/data_caliber.md:16` | `tests/test_spec_data_caliber.py:38` `test_directional_requires_tick_caliber` |
| `DATA-003` | K 线读数必须带时点标注 `as_of`（盘中合成的今日 bar 另标 `partial`）—— 缺了它，昨日数据会被当成今日行情 | `specs/caliber/data_caliber.md:17` | `tests/test_spec_data_caliber.py:56` `test_kline_reads_carry_as_of` |
| `RECIPE-001` | 每份 recipe 必须声明 `acceptance`（≥1 条）—— 没有验收标准的配方等于没有承诺，改坏了没人能发现 | `specs/harness/recipes.md:26` | `tests/test_spec_harness_recipes.py:26` `test_every_recipe_declares_acceptance` |
| `RECIPE-002` | `prompt_fragment` 必须原文出现 `output_sections` 的每一个段落名 —— 段落名是输出契约，提示词里缺一段，模型就不写那一段 | `specs/harness/recipes.md:27` | `tests/test_spec_harness_recipes.py:32` `test_prompt_fragment_covers_output_sections` |
| `RECIPE-003` | `budget.max_tool_calls` 必须 ≥ 该配方挂载的工具数 —— 否则排在后面的工具永远轮不到，等于白挂 | `specs/harness/recipes.md:28` | `tests/test_proposal_collection_gating.py:76` `test_deep_diagnosis_budget_covers_mounted_tools`<br>`tests/test_spec_harness_recipes.py:43` `test_budget_covers_tool_count` |
| `RECIPE-004` | 配方声明的每个工具名必须存在于 `CHAT_TOOL_REGISTRY` —— 拼错一个名字只会静默少一个维度，不报错 | `specs/harness/recipes.md:29` | `tests/test_spec_harness_recipes.py:54` `test_recipe_tools_exist_in_registry` |
| `RECIPE-005` | 任意两份配方的 `when` 关键词不得重复 —— 同一个词被两个配方认领，路由结果就取决于列表顺序（不确定） | `specs/harness/recipes.md:30` | `tests/test_spec_harness_recipes.py:67` `test_when_keywords_are_unique_across_recipes` |
| `RECIPE-006` | 采集类工具（`fallback=False`，如 `note_new_dimension`）**不得进「无配方命中 → 全量工具」兜底路径**，且必须挂在一份分析配方里 —— 兜底路径同时服务闲聊/问价，采集工具混进去等于让模型在闲聊里自由写库；而完全不挂载则采集链路永远没有入口，提案表恒空。两头都是失效，一个是脏数据、一个是死管道 | `specs/harness/recipes.md:34` | `tests/test_proposal_collection_gating.py:29` `test_collection_tool_is_visible_to_model`<br>`tests/test_proposal_collection_gating.py:44` `test_collection_tool_schema_is_well_formed`<br>`tests/test_proposal_collection_gating.py:70` `test_collection_tool_is_mounted_in_deep_diagnosis`<br>`tests/test_proposal_collection_gating.py:84` `test_collection_tool_declares_no_fallback`<br>`tests/test_proposal_collection_gating.py:92` `test_collection_tool_does_not_charge_tool_budget`<br>`tests/test_proposal_collection_gating.py:108` `test_casual_chat_fallback_excludes_collection_tool`<br>`tests/test_proposal_collection_gating.py:118` `test_stock_question_fallback_excludes_collection_tool`<br>`tests/test_proposal_collection_gating.py:131` `test_fallback_is_not_emptied_by_the_filter` |
| `RECIPE-010` | 白名单一个工具都没命中时必须**收紧到 0 工具**(fail-closed), | `specs/harness/recipe_whitelist.md:13` | `tests/test_harness_routing.py:58` `test_filter_schemas_fails_closed_on_whitelist_miss`<br>`tests/test_spec_recipe_whitelist.py:18` `test_whitelist_miss_fails_closed` |
| `RECIPE-011` | 白名单为 None / 空集时**不过滤**(返回全量) —— 这是显式的「不用配方」路径, | `specs/harness/recipe_whitelist.md:15` | `tests/test_spec_recipe_whitelist.py:26` `test_empty_whitelist_means_no_filter` |
| `RECIPE-012` | 白名单一个都没命中时必须记 warning 并列出没对上的工具名 —— 排障时要能看出是哪份配方坏了 | `specs/harness/recipe_whitelist.md:17` | `tests/test_spec_recipe_whitelist.py:37` `test_whitelist_miss_logs_unknown_names` |
| `RECIPEDD-001` | `deep_diagnosis` 必须覆盖六层时间轴所需工具：`get_trend_profile`（前期走势）、`get_stock_dossier`（消息面）、`get_scenario_matrix`（情景研判） | `specs/harness/recipes.md:32` | `tests/test_spec_harness_recipes.py:88` `test_deep_diagnosis_covers_six_layers` |
| `RECIPEDM-001` | `demon_diagnosis` 必须挂载 `get_demon_factor`（妖股因子）—— 用趋势票的尺子量妖股，输出必然是废话 | `specs/harness/recipes.md:33` | `tests/test_spec_harness_recipes.py:100` `test_demon_diagnosis_uses_demon_factor` |
| `RECIPELD-001` | `left_diagnosis` 必须同时挂载 `get_sentiment_cycle` 与 `get_event_catalyst` —— 三层滤网的前两层就靠这两个工具，缺了就只能写「未授权」 | `specs/harness/recipes.md:31` | `tests/test_spec_harness_recipes.py:80` `test_left_diagnosis_has_three_tier_tools` |
| `TOOL-001` | 每个 chat 工具必须注册 `caliber`（口径标签）且非空 —— 没有口径的结论会被误读成事实 | `specs/tools/chat_registry.md:22` | `tests/test_spec_chat_registry.py:40` `test_every_tool_has_caliber` |
| `TOOL-002` | `get_capital_flow` 的 caliber 必须包含「禁止据此判定主力意图」红线 —— 该工具是东财四档口径，方向位与逐笔相反，不能当主力意图用 | `specs/tools/chat_registry.md:23` | `tests/test_spec_chat_registry.py:48` `test_capital_flow_caliber_is_redline` |
| `TOOL-003` | 注册表工具总数必须与本规格声明的一致 —— 新增工具漏改规格会在此失败 | `specs/tools/chat_registry.md:24` | `tests/test_spec_chat_registry.py:62` `test_registry_count_matches_specs` |
| `TOOL-004` | 对模型可见的工具数 = 注册表数 − handler-only 数 —— 两者对不上说明 schema 漏配或多配 | `specs/tools/chat_registry.md:25` | `tests/test_spec_chat_registry.py:75` `test_visible_tools_count_matches_specs` |
