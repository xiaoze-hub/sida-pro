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
- **REPLAY-001** `postmarket_reviewer` 必须挂载 `get_market_replay` —— 涨停/炸板/跌停/晋级率只能从三池同源取，从策略信号表里找涨停数据会得到「0家涨停」（2026-10-22/23 真实是 44/86 家）
- **REPLAY-002** 盘后复盘的涨停家数、炸板率、跌停家数、晋级率四项**不得用 0 冒充缺失** —— 取数失败必须显式写「未取到/暂无」，因为 0 是一个有效观测值，会被模型当成事实写进结论
- **REPLAY-003** `postmarket_reviewer` 的 `prompt_fragment` 必须包含「结论前置 + 核心数字表格 + 每条要点独立成行 ≤50 字 + 禁止连续 3 行以上段落」四条排版约束，且 `output_sections` 首段必须是「一句话结论」
- **RECIPE-006** 新分析维度的采集**必须走确定性后置解析**（`src/core/gap_collector.py`：深度诊断 + 回答里有「未取到」类标记 → 一次**不带工具**的独立判定 → 严格校验后入 `harness_proposals`），**不得依赖模型调用采集工具** —— 2026-10-22 三次真机验收实测：`agnes-3.0-flash` 在连续 10 个只吃 `symbol` 的工具之后，对第 11 个工具会无视 schema 直接照抄 `{"symbol":…}`（9 次实验 6 次，删掉 `symbol` 字段也照吐），工具被调用但提案表恒空；`note_new_dimension` 因此**不挂任何配方、不进兜底路径**，只留 handler 供人工/API 使用

## 维护方式

改了任何 recipe 的 `tools` / `output_sections` / `when` / `budget`，先改这里的标准，再改 YAML，
最后跑 `python3 scripts/trace_specs.py`。**不要为了过门禁放宽标准。**
