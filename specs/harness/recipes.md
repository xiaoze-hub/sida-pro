# 数智参谋 · Harness 配方验收契约

规格事实源。对应实现：`src/harness/recipes/*.yaml`（11 份）、`src/harness/recipes.py`（加载与路由）、
`src/harness/pipeline.py`（确定性管线）。

**基准日**：2026-10-22（commit `4faae6c` 起）
**覆盖范围**：11 份 recipe 的结构性契约（工具存在性 / 预算 / 段落 / 关键词唯一 / 验收声明）。
**不覆盖**：提示词措辞优劣、模型输出质量 —— 那些交给 trace 与人工复核。

> 判据：每条标准都要能回答「删了这个测试，会漏掉哪个 bug？」答不上来的不放进来。

## 为什么是这几条

11 份配方不是「配置」，是**行为契约**：配方决定模型能看到哪些工具、必须写哪几段、最多调几次。
历史上真实发生过的事故，正好对应下面几条：

| 事故 | 对应标准 |
|---|---|
| `left_diagnosis` 只做了第三层滤网，前两层工具没挂，模型只能写「本轮未授权」 | RECIPELD-001 |
| 配方里写了不存在的工具名，模型静默少调一个维度 | RECIPE-004 |
| 两套配方抢同一个关键词，路由结果取决于字典序 | RECIPE-005 |
| 预算写 9 却挂 12 个工具，后 3 个永远轮不到 | RECIPE-003 |

## 验收标准

- **RECIPE-001** 每份 recipe 必须声明 `acceptance`（≥1 条）—— 没有验收标准的配方等于没有承诺，改坏了没人能发现
- **RECIPE-002** `prompt_fragment` 必须原文出现 `output_sections` 的每一个段落名 —— 段落名是输出契约，提示词里缺一段，模型就不写那一段
- **RECIPE-003** `budget.max_tool_calls` 必须 ≥ 该配方挂载的工具数 —— 否则排在后面的工具永远轮不到，等于白挂
- **RECIPE-004** 配方声明的每个工具名必须存在于 `CHAT_TOOL_REGISTRY` —— 拼错一个名字只会静默少一个维度，不报错
- **RECIPE-005** 任意两份配方的 `when` 关键词不得重复 —— 同一个词被两个配方认领，路由结果就取决于列表顺序（不确定）
- **RECIPELD-001** `left_diagnosis` 必须同时挂载 `get_sentiment_cycle` 与 `get_event_catalyst` —— 三层滤网的前两层就靠这两个工具，缺了就只能写「未授权」
- **RECIPEDD-001** `deep_diagnosis` 必须覆盖六层时间轴所需工具：`get_trend_profile`（前期走势）、`get_stock_dossier`（消息面）、`get_scenario_matrix`（情景研判）
- **RECIPEDM-001** `demon_diagnosis` 必须挂载 `get_demon_factor`（妖股因子）—— 用趋势票的尺子量妖股，输出必然是废话
- **RECIPE-006** 采集类工具（`fallback=False`，如 `note_new_dimension`）**不得进「无配方命中 → 全量工具」兜底路径**，且必须挂在一份分析配方里 —— 兜底路径同时服务闲聊/问价，采集工具混进去等于让模型在闲聊里自由写库；而完全不挂载则采集链路永远没有入口，提案表恒空。两头都是失效，一个是脏数据、一个是死管道

## 维护方式

改了任何 recipe 的 `tools` / `output_sections` / `when` / `budget`，先改这里的标准，再改 YAML，
最后跑 `python3 scripts/trace_specs.py`。**不要为了过门禁放宽标准。**
