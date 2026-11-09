# 数智参谋 · Harness 配方验收契约

规格事实源。对应实现：`src/harness/recipes/*.yaml`（13 份）、`src/harness/recipes.py`（加载与路由）、
`src/harness/pipeline.py`（确定性管线）。

**基准日**：2026-10-22（commit `4faae6c` 起）
**覆盖范围**：12 份 recipe 的结构性契约（工具存在性 / 预算 / 段落 / 关键词唯一 / 验收声明）。
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
| 空头类结论最容易被模型"脑补"证据（编一个上方套牢峰、把四档资金方向当主力意图） | DEVIL-003/004/005 |
| 预算写 9 却挂 12 个工具，后 3 个永远轮不到 | RECIPE-003 |
| 「复盘 + 龙虎榜」被 `postmarket_reviewer` 吞掉：pipeline 配方按 `{}` 调工具，必填 `domain` 的域工具被判 unsatisfiable 静默跳过，结论照写、龙虎榜永远取不到 | LHB-001 |
| 同名同额席位被当成重复合并：两家机构都叫「机构专用」、金额恰好相同时，席位家数与买卖力量直接失真 | LHB-002 |
| 单域 1200 字硬切：`+1.23亿` 被切成 `+1.2`，末行「席位口径」整段消失 | LHB-003 |
| 榜单「按个股排名」与「同股多原因只算一只」没写明，读者把前 5 当全量、把合并当丢数据 | LHB-004 |

## 验收标准

盘后复盘可调用 `get_market_scan_ranks` 与 `get_dark_fund_top`；它们读取已保存快照并执行页面同口径权限检查。快照写入日期不能证明行情数据日，未核实时必须标明，不得称当日实时。

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
- **DEVIL-001** `devil_advocate`（空头司令）必须 `pipeline: true` + `scope: symbol` —— 压力测试是证据链对抗，模型自由选工具就会挑软柿子捏；取数必须由确定性管线按固定顺序完成，模型只写结论（2026-10-24 创新 6，纯 prompt 编排：不加工具、不加表、不碰 chat.py）
- **DEVIL-002** `devil_advocate` 必须同时挂载三口径工具 `get_main_intent`（逐笔主力意图）、`get_capital_flow`（eastmoney4 四档资金）、`get_decision_pioneer`（TQ L2）与筹码证据工具 `get_chip_accumulation` —— 「大单流出」与「上方套牢峰」两类死因证据就靠这四个，缺了哪个模型就只能写空话
- **DEVIL-003** `devil_advocate` 的 `prompt_fragment` 必须包含「亏 15%」情景假设、「死因一/死因二/死因三」三段死因结构，且要求**每条死因都给可证伪的反驳条件**（若…则该死因不成立）—— 不可证伪的看空只是情绪，不是压力测试
- **DEVIL-004** `devil_advocate` 的 `prompt_fragment` 必须要求证据缺失显式写「无数据」、无现成工具的维度（商誉/解禁等）写「无数据源」，并声明禁止编造数字、禁止用 0 冒充缺失 —— 空头证据恰恰最容易被模型"脑补"，这是压力测试的生命线
- **DEVIL-005** `devil_advocate` 的 `prompt_fragment` 必须原文声明三口径红线：主力意图只认逐笔 `get_main_intent`、`get_capital_flow` 禁做主力意图方向判定、L2 主力净流入标 TQ（`get_decision_pioneer`）—— 看空结论引用资金方向若用错口径，等于造谣
- **DEVIL-006** `devil_advocate` 的 `output_sections` 首段必须是「一句话结论」，且必须包含「口径与缺口」收尾段 —— 与全站参谋输出契约一致（结论前置 + 缺口显式），无数据源的维度要有固定落点而不是散落在死因里
- **LHB-001** 「复盘 + 龙虎榜」类问题必须落到 `lhb_reviewer`（`pipeline: false`、工具白名单含 `query_data_domain`）—— 塞进 pipeline 配方等于把必填 `domain` 的域工具判成 unsatisfiable 静默跳过：管线照跑、结论照写，龙虎榜永远取不到，模型只能凭印象编席位。纯「复盘」仍走 `postmarket_reviewer`；**混合问句**（如「收盘复盘，龙虎榜和涨停梯队」）关键词得分会被 pipeline 配方压过，必须靠 `covers_domains` 声明改走本配方，且提示词要求补查 `limit.pool`
- **LHB-002** 席位行只能按**源记录身份**（`row_uid`）判重：同名同额（「机构专用」+ 相同代码 / 相同金额）不得推定重复，必须保留源行逐条列出，并给出上榜原因与「可能不是同一家」的身份提示 —— 按「名称+代码+金额」判重会把两家金额恰好相同的机构吞成一条，席位家数与买卖力量失真
- **LHB-003** 单域渲染（`render_domain_sections`，≤1200 字）必须**按整行 / 条目边界截断**：不得按字符硬切（会把 `+1.23亿` 切成 `+1.2`），「口径」行与「未取到」行必须活过截断，且截断处标注省略行数
- **LHB-004** 全市场榜单必须写明**排名按个股**（同股多个上榜原因合并成 1 只、取净额绝对值最大的那条作代表）、前 5 与库内总条数；单标的路径必须逐条列出该股当日全部上榜原因

## 维护方式

改了任何 recipe 的 `tools` / `output_sections` / `when` / `budget`，先改这里的标准，再改 YAML，
最后跑 `python3 scripts/trace_specs.py`。**不要为了过门禁放宽标准。**
