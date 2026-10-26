# 页面走查 04：模型预测 / 因子分析 / 多智能体（UI·按钮·可解释性）

**基线**：`v0.48.0 / 5427af0`（工作树 `/home/ubuntu/wt/design-4`，分支 `docs/design-4`）

## 审计路径清单

| 域 | 文件 |
|---|---|
| 预测页 | `frontend/src/pages/Forecast.tsx`（1188 行） |
| 因子页 | `frontend/src/pages/FactorIC.tsx`（261 行） |
| 多智能体 | `frontend/src/pages/Agents.tsx`（1196 行）、`frontend/src/pages/AiCopilot.tsx`（1213 行） |
| 预测引擎 | `forecast_server.py`（1426 行） |
| 模型层/权重/裁判 | `forecast_lib/forecast_models.py`、`forecast_lib/model_weights.py`、`forecast_lib/ai_referee.py` |
| 到期验证 | `src/core/forecast_outcome.py` |
| 因子计算 | `src/core/factor_ic.py`（149 行） |
| 智能体实现 | `src/agents/*.py`、`src/agents/tradingagents/*.py` |

## 覆盖范围

- **已审**：Forecast 页多模型散度图/投票权重/置信度徽标/到期对照列；FactorIC 页整页（表头口径、控件、配色）；Agents 页运行历史与 TA 配置弹窗；AiCopilot 输入区（Prompt Chips、附件、提及）；预测引擎投票与裁判调用链；`forecast_outcome` 三态判定。
- **未审**（本轮不覆盖）：`forecast_lib/forecast_reports.py`（826 行）报告渲染、`forecast_traces.py`（724 行）trace 落库、`ai_referee.py` prompt 全文、`src/web/api/forecast.py` 代理层全量、TradingAgents 内部 debate 循环、设置页 `/forecast/models` 前端消费方。
- **未取到**：Agents 页「多智能体长文本」的渲染组件——本轮 grep 全文件未见任何 agent 输出正文渲染分支（仅 L1171 的 `<summary>` 高级 JSON 折叠），故下文按「当前不存在该展示」定性，不虚构组件名。

---

## 0. 先说事实：4 个模型的真实启用状态

任务书假设「Kronos / Chronos-Bolt / XGBoost / AI裁判」四模型。按代码核实，**实际投票模型是 5 个，AI裁判不在投票里**：

| 名称 | 真实位置 | 生产启用 | 依据 |
|---|---|---|---|
| Kronos | `forecast_lib/forecast_models.py:81 kronos_predict` | 启用，权重最高 | `model_weights.py:49` default 0.25；`forecast_server.py:279` |
| Chronos-Bolt | `forecast_models.py:255`，`amazon/chronos-bolt-small`（`:179`） | 启用，但**首次要从 HF 下载**，失败即静默跳过 | `forecast_server.py:294-297`（"不可用(跳过)"） |
| XGBoost | `forecast_models.py:115`，`XGBRegressor(n_estimators=100, depth=3)` | 启用 | `forecast_server.py:284` |
| 线性回归 | `forecast_models.py` `linreg_predict` | 启用 | `forecast_server.py:289` |
| TimesFM | `timesfm_predict` | **条件启用**：try/except，异常或未装则为 `None` 且不进 votes | `forecast_server.py:302-311`、`:387-392` |
| AI裁判 | `forecast_lib/ai_referee.py` | 启用，**非投票层**：在加权后覆盖 `direction`，异常一律 `abstain` | `forecast_server.py:445-466` |

**关键问题（P0）**：`frontend/src/pages/Forecast.tsx` 的散度图只画三支——`L117 kronos`、`L126 chronos`、`L130 xgboost`、`L131 linreg`，**没有 timesfm 分支**。后端 `:521-526` 的 `models` 里确实带 `timesfm`，前端不画也不标注，等于「投了票但不给出证据」。同时 TimesFM 缺位时前端无任何提示，用户看到的仍是四行，误以为四模型齐全。

- **改法**：`ModelDivergenceChart` 增加 `if (models.timesfm) rows.push({ label: 'TimesFM', start: ..., end: ... })`；缺位模型统一渲染为一行灰态占位：`className="text-[11px] text-muted-foreground/60 line-through"` + 文案 `Chronos-Bolt 未加载`／`TimesFM 未安装`，**不用** `opacity-40`（会和禁用态混淆）。
- **验收线**：`/predict` 返回不含 `timesfm` 时，图上出现 1 行删除线灰字「TimesFM 未装/推理失败」，且行数与 `result.models` 非空键数严格相等。

---

## 1. Forecast.tsx：多模型投票卡片

### 1.1 方向措辞违规（最高优先级）

**现状**：`Forecast.tsx:837`
```tsx
模型加权方向 {result.direction === 'up' ? '↑ 看多' : result.direction === 'down' ? '↓ 看空' : '→ 横盘'}
```
**问题**：这是产品铁律明令禁止的「把模型预测包装成方向结论」。上下文还叠加了 `L789 模型预期 +x.xx%` 与 `L781 置信度高` 徽标，三个信号合起来读，用户拿到的是「看多 + 高置信」的结论，而系统实际只做过一次加权外推、且没有任何样本量支撑。同页 `L1073-1074` 的历史列又用 `✓/✗` 认证，进一步强化结论感。

**改法**（文案级，不改计算）：
- `L837` 整句替换为：`加权走势中枢 {fmt(expected_pct)}%（{days} 日，非方向判断）`，删除 `看多/看空/横盘` 全部出现。
- `direction` 仅作内部色相依据：保留 `text-stock-up` / `text-stock-down`（买红卖绿），但**不加箭头字符 ↑↓**，避免箭头被读成结论。
- `L781` 置信度徽标改文案：`置信度` → `模型一致性`，取值 `高/中/低` 保留，title 补 `∈ 基于 {n} 支可用模型的中位数偏离度，非胜率`。

**验收线**：Forecast.tsx 全文 grep `看多|看空|横盘|买入|卖出` 结果为 0（历史表 `direction` 中文映射若存在，须改为「预期上行/预期下行/预期持平」）；截图审查时顶部区块不出现任何祈使句。

### 1.2 权重展示写死兜底值

**现状**：`Forecast.tsx:847-853`
```tsx
Kronos {Math.round(((modelWeights?.kronos ?? 0.4414)) * 100)}%
Chronos {Math.round(((modelWeights?.chronos ?? 0.3379)) * 100)}%
XGB {Math.round(((modelWeights?.xgboost ?? 0.1103)) * 100)}%
线性回归 {Math.round(((modelWeights?.linreg ?? 0.1103)) * 100)}%
```
**问题**：`??` 兜底值是某次回测的样本值，被硬编码进 UI。`/forecast/weights`（`forecast_server.py:847-871`）请求失败时，页面会显示一组**看起来精确到小数点后四位、实则与当前模型无关**的权重，且四支合计 100.00%，视觉上完全自洽——这是最难被发现的一类假数据。另外 timesfm 有权重（`model_weights.py:49` 0.15）但此块不展示，四支之和与后端真实权重和不一致。

**改法**：`modelWeights === null` 时整块渲染为 `权重未取到`（`text-[11px] text-muted-foreground`），禁止任何 `??` 数字兜底；同时补 TimesFM 行。数值统一 `font-num tabular-nums`，百分比保留 1 位小数（`toFixed(1)`），不用 `Math.round`（0.1103→11% 的双次舍入会丢精度且四行不再合计 100）。
**验收线**：断网/接口 500 时该区块只出现「权重未取到」；正常态五行百分比之和 = 100.0 (±0.1)。

### 1.3 没有「共识度」这个东西

**现状**：`Forecast.tsx` 全文 grep `共识|consensus|std|spread|dispersion` = **0 命中**；后端 `forecast_server.py:400-409` 只算加权均值，也没有输出模型间离散度。
**问题**：任务书要求的「共识度进度条」当前**不存在**。不能用前端自算 `models.*.median` 的近似值充数——散度图（`L113`）只取了首尾点，前端没有完整序列参与方差计算。
**改法**（需后端配合，本文件仅定 UI 契约）：
- 后端在 `/predict` 响应 `models` 同级新增 `consensus: { n_models: int, spread_pct: float, method: "median_abs_dev" }`。
- 前端进度条**禁用红绿**，用中性双色：轨道 `bg-muted`，填充按分档 `bg-sky-500/70`（≥0.7）/ `bg-amber-500/70`（0.4–0.7）/ `bg-slate-400/70`（<0.4）。理由：红绿在本产品已被「涨跌」占用，共识度是质量指标，绝不能读成方向。
- 条旁必须并列 `{n_models} 支模型` 与 `{spread_pct}% 离散`，缺 `n_models` 就不许显示百分比（样本数铁律）。
**验收线**：共识度条出现在权重块上方，宽度变化时颜色只在中性三色间切换，全程不出现 `stock-up/stock-down` 类。

### 1.4 情绪修正系数是死字段

**现状**：`Forecast.tsx:873` 展示 `情绪修正系数：{adjustment_pct}%`；但 `forecast_server.py:313-320` 已硬写 `adjustment_pct: 0.0`（LLM 情绪打分 2026-08-13 停用，由裁判接管），`:419-421` 注释明确 `final` 不再乘该系数。
**问题**：UI 每期都显示 `+0.0%`，用户会以为「情绪面判断为中性」——实际是「该模块不存在」。这是「把缺失包装成零」的典型违规。
**改法**：删除该行；如保留可解释性入口，改为只读标注 `情绪面：已并入 AI 裁判（不再单独修正）`，`text-[11px] text-muted-foreground`，无数字。
**验收线**：页面不出现 `情绪修正系数` 字样与任何 `0.0%` 修正常数。

### 1.5 区间带只有 Kronos 一支

**现状**：`Forecast.tsx:802-803` 把 `result.models.kronos.p5/p95` 传给区间组件；`:166` 画 `r.band`；`:111` 注释「Kronos 额外画 P5-P95 全区间阴影带」。Chronos 虽返回 `p10/p90`（`forecast_models.py:226` 注明对齐口径），前端未使用。
**问题**：置信区间是「不预测方向」这条铁律的唯一硬支点，却只由单一模型提供，且图例未写清是 Kronos 单支的分位，用户会把阴影带当成**全体模型**的不确定性。
**改法**：图例文案改为 `Kronos P5–P95（单模型分位，非全体区间）`；Chronos 的 `p10/p90` 用**虚线描边不带填充**叠加（`stroke-dasharray: 3 3`，`stroke: #7c8ba1`），避免两条填充带叠色。
**验收线**：图上同时可见 Kronos 实心带 + Chronos 虚线界；图例含「单模型」限定词。

### 1.6 到期验证闭环：只有一列，没有闭环

**现状**：
- 后端已实现三态+第四态：`src/core/forecast_outcome.py:18` 定义 `hit|miss|pending|no_data`，`:161 evaluate_history_item(...)` 逐条判定，`:149 _judge_status` 用 `direction` 对实际涨跌幅判命中，`:132 _target_day_is_due` 处理「目标日=今天且未过 15:00 记 pending」。
- 前端 `Forecast.tsx:563-583` 仅渲染 `未到期` / `✓`+涨跌幅 / `✗`+涨跌幅，颜色 `text-emerald-500` / `text-rose-500`（`:579`）；
- 该列受 `L565 historyHasOutcome` 门控，`:1073-1074` 动态增列。

**问题（三条都命中铁律）**：
1. **配色撞车**：`emerald/rose` 在本仓已是「涨绿跌红」的英文语义，而本产品是**买红卖绿**（`text-stock-up/down`）。同一张表里 `✓ 绿` 却可能是「上涨命中」的红场景，直接冲突。
2. **`no_data` 未区分**：`_judge_status` 返回 `no_data` 时前端落到 `!hit` 分支显示 `✗`——**取不到行情被画成「没命中」**，这是把缺失数据当结论，明文违规。
3. **无筛选、无手动核验**：任务书要求的「按模型/按周期/仅看失误」筛选与「手动触发核验」按钮均不存在；且 `/forecast/history`（`forecast_server.py:841-845`）无 `verify` 触发端点，`Forecast.tsx:1049 loadHistory` 只做重拉。

**改法**：
- **三态印章**（替换 `✓/✗` 字符）：
  - 待验证 `pending` → `<span className="rounded border border-border/60 px-1.5 py-0.5 text-[10px] text-muted-foreground">待验证</span>`
  - 已命中 `hit` → `border-rose-500/50 text-rose-500`（红=涨，买红卖绿口径内）
  - 未命中 `miss` → `border-emerald-500/50 text-emerald-500`
  - 无数据 `no_data` → `border-dashed border-border/60 text-muted-foreground` + 文案 `无数据`（**不带任何颜色语义**）
  印章统一 `text-[10px] font-medium`，不要圆角胶囊（去卡片化，方块边更克制）。
- **顶部筛选**：在 `L1045` 工具条左侧插入三个原生 `<select>`（不许用 Dialog）：`模型`（全体/kronos/chronos/xgboost/linreg/timesfm）、`周期`（T+1/T+3/T+5）、`结果`（全部/仅看失误），样式 `h-8 rounded border border-border/60 bg-background px-2 text-[11px] font-mono`。筛选为纯前端过滤已拉取的 `history`，不新增请求。
- **手动核验按钮**：文案 `核验到期待验证`，`variant="outline" size="sm" className="h-8"`；加载态 `disabled` + `<Loader2 className="h-3 w-3 animate-spin" />` + 文案变 `核验中…`（复用 `FactorIC.tsx:152` 既有写法，保持一致）；完成后 toast `已核验 N 条，命中 M 条，无数据 K 条`——**必须报三个数**，只报命中率等于隐去分母。
**验收线**：构造一条 `outcome_status='no_data'` 的历史，页面显示灰虚线「无数据」而非 `✗`；筛选「仅看失误」后列表仅剩 `miss`；点击核验按钮期间按钮不可重复点击且 spinner 可见。

---

## 2. FactorIC.tsx：因子有效性

### 2.1 页面实际形态与任务书假设的差距

**现状**：整页 = 一个 26 列宽表（`L186` `min-w-[860px]`）+ 每行一个 sparkline。控件只有三个：`回看天数`（`L136`）、`持有期`（`L142`）、`刷新`（`L150`）。
**问题**：任务书要求的 **IC 累计曲线 / 分层回测 / 正交化 / 去极值 / 同步权重到预测引擎** —— 五项全部不存在，且后端也不具备：
- `src/core/factor_ic.py` 仅 `:61 compute(db, horizon, max_dates)` 与 `:135 render(m)`，全文无 `quantile|orthogon|winsor|layer` 命中；`_ranks`/`_spearman` 只做单期截面相关。
- 因此「分层回测」与「正交化」不是 UI 缺口，是**能力缺口**，文档不得画假控件。

**结论**：以下 2.2–2.4 按「有数据可画 / 需先补后端 / 明确不做」三档分别给建议，禁止前端先摆空壳按钮。

### 2.2 已有的可辩识性缺陷（可立即改，无需后端）

**现状**：`FactorIC.tsx:48`
```tsx
className={last >= 0 ? 'text-red-400' : 'text-emerald-400'}
```
sparkline 用红涨绿跌；`L62 icCell` 的 IC 正负分档、`L212 ho.cls` 样本外 IC 同样沿用红绿。
**问题**：IC 的正负是**因子与未来收益的相关方向**，不是价格涨跌。用 `text-stock-up/down` 同色系会让用户把「IC 为正」读成「这只票要涨」。IC 曲线属统计量，必须另起一套色相。
**改法**：
- IC 系列改**蓝紫冷色相**：正 IC `#4f7cff`（`text-[#4f7cff]`），负 IC `#a855f7`（紫），零附近 `text-muted-foreground`。sparkline `stroke` 随末值切换同两色。
- 表头 `L190 IC` 加 `title`：`横截面 IC 均值（因子值 vs 未来收益的秩相关），正负≠涨跌`（现有 title 只有「主口径」，未防误读）。
- `L194 参考值`（pooled Spearman）已有正确的降级说明（「只作对照, 不作决策口径」），保留；但 `text-muted-foreground/70` 对比度过低，改 `text-muted-foreground`。
**验收线**：全页 grep `stock-up|stock-down|red-|emerald-|rose-` 在 IC 数值/saprkline 处为 0；截图色彩直方图中 IC 区段无红绿。

### 2.3 IR / t 值缺样本门槛（统计胜率铁律）

**现状**：`L208` t 值 `title="|t| ≥ 2 才谈得上显著"`；`L211` IR 直接 `num(r.ir, 2)`；`L214` 样本量单列但无联动。
**问题**：t/IR 在 `期数` 很小时是纯噪声（`L215` 的期数可能是 2–3）。UI 不给门槛就展示两位小数，等于把小样本包装成能力。
**改法**：当期数 < 12 时，t 与 IR 单元格整体降级 `text-muted-foreground/50` 并在数值后缀追加 `*`，表脚 `L237` 注释补一行 `* 期数 < 12，统计量不可解读`。IR 保留 2 位但改用 `tabular-nums`（当前 `font-mono` 已有，需确认 `font-num` 一致）。
**验收线**：期数=5 的行，t/IR 均带 `*` 且为 50% 透明度；期数=30 的行无标记。

### 2.4 分层回测与正交化：按「明确不做」处理

**现状**：无实现（见 2.1）。
**问题**：任务书要求给「正交化/去极值控件」与「同步权重到预测引擎」按钮的交互。若后端无对应能力，加按钮就是假交互——用户点了没有任何数值变化，比没有按钮更有害。
**改法**：
1. **暂不上控件**。在页脚 `L237` 之后加一行能力声明：`分层回测 / 因子正交化 / 去极值：引擎未实现（factor_ic.compute 仅单期截面 IC）`，`text-[11px] text-muted-foreground`。这是「缺失显式标注」的正解。
2. **权重同步按钮**留给下一版，但先定契约（避免后续实现时口径漂移）：文案 `将高 IC 因子权重同步到预测引擎`，`variant="outline" size="sm"`；点击后必须先弹**二次确认**列出将被改动的因子与权重（`xgboost` 特征权重），不可一键直写；成功后 toast 必须带 `影响 N 个因子，M 个模型`。理由：该按钮直接改变 `/predict` 的输入分布，是本仓库最危险的一个写操作，与 Frozen Ring 的 `number_audit` 口径相邻。
3. **分层回测**若要做，配色沿用 2.2 的蓝紫族并扩展为 5 档明度阶梯：`#4f7cff` / `#6b8cff` / `#8aa1ff` / `#a855f7` / `#c084fc`，**第 1 分位与第 5 分位禁止用红/绿**（否则就是「多空」暗示）。
**验收线**：页脚出现能力声明原文；页面上不存在任何无后端的可点击控件（点击类元素数量 = 实际有 handler 的数量）。

---

## 3. Agents.tsx + AiCopilot.tsx：多智能体与 Copilot

### 3.1 多智能体长文本没有结构化时间轴（能力缺口）

**现状**：
- `Agents.tsx:419-433 toggleRuns` 拉 `/agents/{name}/history?limit=5`，`L562` 逐 agent 卡片，运行记录以折叠面板呈现。
- 全文无角色色边框、无观点 Tag、无 CoT 展开按钮；`L1171` 的 `<details>` 只用于 TA 配置的原始 JSON。
- 进度侧有结构化基础：`src/agents/tradingagents/progress.py:187` 示例 `{"name": "market_analyst", "status": "done", "duration_sec": 12.3, "cost_usd": 0.004}`，`:159` 有 name 归一化。
**问题**：多智能体的价值在「谁说了什么、按什么顺序、分歧在哪」。当前若正文以长文本吐出，用户读不到角色边界；而 progress 模块已有 `name/status/duration/cost` 四要素，**前端没消费**，等于把可解释性数据采了不用。

**改法**（先做能做的，不虚构正文渲染）：
- 时间轴容器：`<ol className="relative ml-3 border-l border-border/60 pl-4">`，每节点 `<li className="relative pb-4">`，角色色边框只加在节点首行左侧 2px：`border-l-2 pl-2`。
- 角色专属色（**避开红绿**）：market_analyst `#4f7cff`、news/sentiment `#a855f7`、fundamentals `#0891b2`、bull `#f59e0b`、bear `#64748b`、risk `#dc2626`（仅风险角色可用红，且语义是「风险」不是「跌」）。避免与买红卖绿混淆——风险红用在**图标与边框**，不用于数字。
- 核心观点 Tag：`rounded bg-muted px-1.5 py-0.5 text-[10px] font-medium text-foreground/80`，一个节点最多 3 个，超出显示 `+N`。
- 展开/折叠 CoT：按钮文案 `展开推理` / `收起推理`，`variant="ghost" size="sm" className="h-7 text-[11px]"`，内联在节点头部右侧，不用图标按钮（图标按钮在 10px 字号下辨识度不足）。
- 节点右侧固定 `duration_sec` 与 `cost_usd`：`font-num tabular-nums text-[11px] text-muted-foreground`——成本是这类 Agent 的真实约束，必须可见。
**验收线**：任一 agent 运行展开后，出现带角色名与色条的 ≥2 个时间轴节点，节点含 duration；CoT 按钮切换前后高度变化且按钮文案同步；无 `<pre>` 承载正文。

### 3.2 Agents 触发按钮缺加载反馈一致性

**现状**：`Agents.tsx:410` `POST /agents/{name}/trigger` 后仅 toast；`L520` 健康检查有 `disabled={healthLoading}` 但无 spinner。
**问题**：深度分析是分钟级任务（`L1093 timeout_minutes` 默认 15），点击后只有一次 toast，用户无法判断是否已入队。
**改法**：trigger 按钮点击后 3 秒内改为 `已入队` + `disabled`（`variant="outline"`），并用 `aria-live="polite"` 播报；健康检查按钮补 `<Loader2 className="h-3 w-3 animate-spin" />`（照抄 `FactorIC.tsx:152`）。
**验收线**：点击触发后按钮文案在 200ms 内变为「已入队」且不可重复点击。

### 3.3 AiCopilot 输入区：现状较好，两处需收口

**现状（已具备，记录以免误改）**：
- Prompt Chips：`AiCopilot.tsx:105-139` 定义 4 组预设（含持仓撤单率、日K量价阶段等），`L580`/`L611`/`L982` 三处 chip 按钮。
- 多模态上传：`L293-314 handleFile` 复用 `/api/chat/upload`，图片走 `image_data` 直传、PDF/Excel/txt 走服务端解析；`L1029-1046` 回形针按钮 + `uploading` 时 `<Loader2 animate-spin>`；`L1048-1053` `accept="image/*,.pdf,.xlsx,.xls,.csv,.txt,.md,.docx"`；`L1127-1132` 支持拖拽；`L67` 上限 20MB 与服务端一致。
- Mention chips：`L1077` `data-testid="copilot-mention-chips"`。
**问题**：
1. **无「交割单截图 / 财报」语义入口**：accept 支持图片与 pdf，但 UI 上只有一个无标签回形针，用户不知道能传交割单。任务书要求的这两类上传**能力已有、提示缺失**。
2. `L1164 placeholder` 为长句，在窄屏会截断，且未提附件能力。
**改法**：
- 在回形针左侧加两枚轻量 chip（非按钮，点击即触发 file input 并设 `accept` 过滤）：`交割单截图`（`accept="image/*"`）、`财报 PDF`（`accept=".pdf"`），样式 `rounded-full border border-border/60 px-2 py-0.5 text-[10px] text-muted-foreground hover:text-foreground`。理由：这是把已有能力暴露出来，不是新增通道。
- placeholder 收敛为：`问点什么，或拖入交割单/财报`（`text-[12px]`），完整用例留给 chips。
- 上传失败沿用 `L314` 的 `ATTACH_FAILED` 错误槽，但错误文案须含文件名与上限：`{name} 超过 20MB`。
**验收线**：输入框上方可见两枚语义 chip；点「财报 PDF」后文件选择器仅可选 pdf；传 >20MB 文件报错文案含文件名。

---

## 4. 汇总：按严重度

| # | 位置 | 问题 | 级别 |
|---|---|---|---|
| 1 | `Forecast.tsx:837` | `↑ 看多 / ↓ 看空` 把预测包装成方向结论，叠加"置信度高"徽标 | **P0 违规** |
| 2 | `Forecast.tsx:563-583` | `no_data` 落入 `✗` 分支，缺失数据被判定为"未命中" | **P0 违规** |
| 3 | `Forecast.tsx:847-853` | 权重硬编码兜底（0.4414 等），接口失败时展示假精确值 | **P0 违规** |
| 4 | `Forecast.tsx:873` | 情绪修正恒为 0 仍展示，缺失被包装成"中性" | P1 |
| 5 | `Forecast.tsx:113-131` | 散度图无 timesfm 分支，缺位模型无灰态占位 | P1 |
| 6 | `Forecast.tsx:565-583` | 到期验证仅一列，无三态印章/筛选/手动核验 | P1 |
| 7 | `FactorIC.tsx:48,62` | IC 用红涨绿跌色，统计量与价格方向混淆 | P1 |
| 8 | `FactorIC.tsx:208-211` | t/IR 小样本无降级标记 | P1 |
| 9 | `Agents.tsx`（全页） | 多智能体无时间轴/角色色/CoT 折叠；progress 的 duration/cost 未被消费 | P2 |
| 10 | `AiCopilot.tsx:1029` | 交割单/财报上传能力已具备但无语义入口 | P2 |
| 11 | `FactorIC.tsx` | 分层回测/正交化/权重同步三项后端未实现，前端不得摆空壳 | 待排期 |
| 12 | `Forecast.tsx`（全页） | 共识度进度条无任何实现，需后端先出 `consensus` 字段 | 待排期 |

**口径备注**：本页所述"红/绿"一律指 `text-stock-up` / `text-stock-down`（买红卖绿）；因子 IC、共识度、模型一致性等统计量禁用该色系。主力意图 tick / 资金流 eastmoney4 / L2 TQ 三套口径在本域内不互相换算，预测页与因子页均未做换算——符合约束，无整改项。Frozen Ring（`symbol_guard` / `number_audit` / `caliber.py` / 交易403 / TQ 优先链 / `SYSTEM_PROMPT`）本轮零改动。
