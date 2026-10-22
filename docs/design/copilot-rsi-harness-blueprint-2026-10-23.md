# 数智参谋深度研究蓝图：RSI 受控自我改进 / Harness 提速提准 / 创新点 / UI-UX

- **基线标识**：`a138a46`（v0.43.4，2026-10-23 04:00 前后 local main；含 P0~P3 四线合并）
- **审计/设计对象**：
  - `src/harness/`（`runtime.py` / `pipeline.py` / `routing.py` / `recipes.py` / `actions.py` / `viewstate.py` / `skills.py` / `trace.py`）
  - `src/agents/chat/registry.py`（66 工具注册表）、`src/core/{gap_collector,scenario_engine,trend_profile,trader_playbook,copilot_quota,copilot_memory}.py`
  - `frontend/src/pages/AiCopilot.tsx`、`frontend/src/components/copilot/*`、`frontend/src/lib/copilot/*`
- **覆盖范围**：
  - **已读并核对**：`src/harness/pipeline.py`(全) / `runtime.py`(全) / `context.py`(全) / `recipes.py`(全) / `actions.py`(全) / `viewstate.py`(全) / `skills.py`(全) / `trace.py`(全) / `pipeline_market` 尚未存在；`src/core/gap_collector.py`(实机行为) / `scenario_engine.py`(全) / `klines_repo.py`(全)；`frontend/src/components/copilot/AnswerCard.tsx`(全) / `pages/AiCopilot.tsx`(关键段) / `lib/copilot/answer-blocks.ts`(前半)
  - **未覆盖**：`src/web/api/chat.py` 2296→2307 行整段（只查了 pipeline 调用点与行数护栏）；11 份配方 YAML 中 6 份非管线配方只抽查 `auction_hunter`/`premarket_strategist`/`postmarket_reviewer`/`risk_judge`；未做生产数据聚合（本文档是设计，不是复盘，复盘口径见 `sida-agent-kernel` §9）

---

## 0. 一句话结论

参谋的**底座已经够了**（66 工具 + 11 配方 + 管线 + 轨迹 + 治理），现在缺的是三样东西：
**① 让「跑过一轮」变成能改进自己的证据（受控 RSI）**、**② 把剩余 6 份配方从「模型自选工具」升级成确定性管线并砍掉重复取数**、
**③ 让结论从「一段文字」变成「图上可点的证据」**。三者都能量化验收，不需要换模型。

---

## 1. RSI（Recursive Self-Improvement）可行性：可行，但只允许「受控双环」

### 1.1 为什么通用 RSI 直接搬会翻车

| 前提 | 通用 RSI（SWE-bench / DSPy） | SIDA-Pro 现实 |
|---|---|---|
| 真值判定 | 编译 + 单测，0/1 | **A 股没有即时真值**：今天涨不代表逻辑对 |
| 样本量 | 上万条 | 4 个账号、日均几十轮，统计意义不足 |
| 负迁移 | 任务独立 | **市场 regime 会切换**：退潮期的「炸板核按钮」经验在主升期会把弱转强全部误杀 |
| 优化目标 | 通过率 | 用户满意 ≠ 更激进；最短路径永远是「硬编一个结论」 |

**结论**：不能让 LLM 自动改提示词/护栏。**护栏必须先冻结，再谈进化。**

### 1.2 双环结构

```
        ┌─────────────── Frozen Ring（只读，禁止任何自动改动）───────────────┐
        │ symbol_guard 零工具拦截/标的核对 · number_audit 数字对账            │
        │ caliber.py 逐笔 vs 东财四档口径红线 · actions 交易 403              │
        │ TQ 优先链 · copilot_quota 配额 · SYSTEM_PROMPT 约束段               │
        └────────────────────────────────────────────────────────────────────┘

        ┌─────────────── Mutable Ring（允许半自动生成补丁）───────────────────┐
        │ ① recipes/*.yaml 的 when 触发词   ② recipes/*.yaml 的 tools 组合     │
        │ ③ harness/skills/*.md 的条件分支（必须绑 regime 标签）               │
        │ ④ tools_catalog 的挂载提示（哪些已注册工具该进哪份配方）              │
        └────────────────────────────────────────────────────────────────────┘
```

### 1.3 三个机器可验证的适应度函数（替代模型自评）

| 信号 | 定义 | 为什么可信 | 落地落点 |
|---|---|---|---|
| **F1 一致性** | 单轮 `number_audit` 漂移数 == 0 且 `status == ok`；工具序列 Jaccard == 1.0（管线） | 纯机械判定 | 已有 `copilot_runs`/`copilot_steps` |
| **F2 负样本** | 用户 5 分钟内点「重新生成」/ 触发 `correction_note` / 零工具拒答 | 用户行为，不由模型自评 | 需新增 `CopilotRun.user_rejected`（迁移） |
| **F3 关键位兑现**（A 股独有真值） | 每轮产出的 `support/resist` 在 T+1/T+3 是否被触及、触及后方向是否与情景一致；关键位误差 bp | **延迟到账的客观真值**，与模型说法无关 | 新增 `core/fitness_keypoints.py` 离线批 |

**F3 是这套 RSI 能不能成立的关键**：它把「说得像不像」换成「关键位准不准」。没有 F3，任何自进化都是自我感觉良好。

### 1.4 上线阶段（每阶段都有退出条件）

| 阶段 | 内容 | 退出条件 |
|---|---|---|
| S0（做） | **离线复盘器**：拿历史 `copilot_runs` 重跑新旧配方，输出「建议修改清单 + 证据」，**不写任何配置** | 产出 ≥20 条带证据的建议，人工审核采纳率 >50% |
| S1（做） | **F3 兑现台账**：每日批算关键位兑现率，进治理页 | 连续 5 个交易日有数，且与人工抽查一致 |
| S2（暂不做） | 提案自动生成 recipe diff（仍人工点合入） | S0/S1 稳定 2 周后再议 |
| S3（明确不做） | 任何对 Frozen Ring 的自动修改、任何参数搜索式调参 | —— |

### 1.5 四条红线（写进代码注释，不是文档口号）

1. 工具不自动加（加工具 = 代码 + 测试 + 规格，归代码不归学习）。
2. 能自动学的只有**路由词与提示词策略**；数字/阈值/方向判断不许学（要回测）。
3. 每次改动必须可回放（`copilot_runs` 能复现当时工具序列）。
4. 经验必须带 **regime 标签**（情绪阶段 + 主线题材），跨 regime 不继承。

---

## 2. Harness 提速提准

### 2.1 已量化的浪费（代码证据）

| 问题 | 证据 | 影响 |
|---|---|---|
| **同轮重复取数** | `deep_diagnosis` 的 4 个工具（`get_trend_profile`/`get_chip_accumulation`/`get_scenario_matrix`/`get_stock_dossier`）各自 `load_qfq_bars(sym, mk, 250)`，且 `compute_chips(bars[-120:])` 各算一遍 | 每轮 4 次同 SQL + 4 次同计算 |
| **上游预拉 + 管线重拉** | `chat.py:_build_ai_messages` 先拉实时/技术面，命中管线再拉一遍 | 首字前白烧 1~3s |
| **6 份配方仍是模型自选** | `pipeline: false`：`auction_hunter`/`premarket_strategist`/`postmarket_reviewer`/`risk_judge`/`theme_leader`/`intraday_watcher` | p50 45~90s，Jaccard 0.57 |
| **证据冲突无人裁决** | 逐笔流入 vs DDE 流出、日线破位 vs 分时大单托底，全靠模型自由发挥 | 结论随机性 |

### 2.2 四项改造（按 ROI 排序）

1. **P5 单轮取数缓存（`src/harness/bar_cache.py`）**：contextvar 作用域 + 单飞 + TTL 30s + 条目上限；未开作用域时**完全穿透**（不影响回填/回测）。预期取数段再降 40~50%。
2. **P6 全市场确定性管线（`src/harness/pipeline_market.py`）**：`Recipe.scope: symbol|market`。无参工具以 `{}` 跑；需 symbol 的工具按自选展开（上限 8 只）；缺 scope 时记 `no_symbol_scope` 让它老实写「未取到」。预期 6 份配方 p50 → 15~25s。
3. **P7 证据冲突交叉核验器**：管线 `compose_prompt` 前，用纯 Python 抽各工具核心读数，冲突时在证据块顶部自动插入 `【多源分歧警示】`，强制模型按口径优先级解释而不是挑一边。
4. **P8 regime 感知**：管线取数时附带当日情绪周期锚点（≤100 字），退潮期收紧追高阈值、冰点期抬高左侧权重。**只影响提示词措辞，不新增数据调用。**

---

## 3. 创新点（都指向「更准的决策」）

### 3.1 历史相似分形对标（给赔率，不给预测）

对当前形态（如「高位缩量首阴 + 筹码峰未散 + 20 日动量 85 分位」）在本股/同板块近 1 年做最近邻检索，输出**客观频次与赔率**：
`同形态 18 次 → 次日反包率 38.9%、5 日内跌破 MA10 61.1%`。
价值：把大模型的文字玄学换成可核对的历史频率。**必须标注样本量 n 与检索窗口，n<10 只报不评。**

### 3.2 持仓成本锚定情景（同一张图，不同人不同位）

`scenario_engine` 现在对所有人算出同一组支撑/阻力。接入 `get_portfolio` + `trader_playbook` 后，把**本人成本线**与历史平均止损位叠进矩阵，输出本人视角的解套位 / 保本位 / 纪律止损位。
价值：参谋第一次真正「知道你在什么位置」，而不是复述通用技术位。

### 3.3 盘前预案 → 盘中触发验真

早盘产出的预案（「站稳 35.20 且量比>1.5 转强」）可一键转成 `PriceAlertRule` 并绑定原 `run_id`；触发时通知卡片直接并列「早盘原话 vs 此刻逐笔主力意图」。
价值：把「说对了没有」变成可回看的对账，同时天然产出 F2/F3 负样本。

---

## 4. UI/UX 升级（守「看图决策，不做数据堆砌」）

| 改造 | 现状 | 目标 | 判据 |
|---|---|---|---|
| **证据齐套度条** | 流式只有一行 `正在取数 3/10：get_xxx`，生成完就消失 | 常驻一行 10px 胶囊：`走势 ✓ · 筹码 ✓ · 逐笔 ✓ · 撤单 ⏳ · 消息面 ○`，失败标 `未取到`（`role-risk`） | 不虚构分母；未知工具显示原名 |
| **价格锚定** | 回答里的 `支撑 32.80` 是死文本，用户要自己回图上找 | 价格变可点 chip，回调 `onAnchoredPrice`（供 K 线画水平尺/加标注） | 百分比/代码/日期不得误判为价位 |
| **框选即问** | 已能接收 `selection`，但没有入口 | K 线框选后浮 `.shadow-float` 极简条：`问参谋/算区间筹码/标为洗盘区间` | 浮层唯一允许的 shadow |

**明确不做**：不做卡片化重排、不做仪表盘 tile、不给每个数字配图标动画（与产品定位冲突）。

---

## 5. 派单与批次（文件不相交）

| 批 | 任务 | Worktree / 分支 | 负责文件 | 验收线 |
|---|---|---|---|---|
| W1 | P5 取数缓存 | `wt-bar-cache` / `feat/harness-bar-cache-1023` | `src/harness/bar_cache.py`(新) `src/db/klines_repo.py` `src/harness/pipeline.py` `tests/test_bar_cache.py` | 同 key 一轮只 1 次 loader；无作用域完全穿透 |
| W1 | P6 全市场管线 | `wt-market-pipeline` / `feat/harness-market-pipeline-1023` | `src/harness/pipeline_market.py`(新) `src/harness/recipes.py` 6×YAML `tests/test_market_pipeline.py` | 配方 CI 全绿；接线留给集成方 |
| W1 | P7 UI 双改 | `wt-copilot-ux` / `feat/copilot-ux-anchor-1023` | `frontend/src/lib/copilot/{evidence-readiness,price-anchors}.ts`(新) `components/copilot/{EvidenceReadinessStrip.tsx,AnswerCard.tsx}` `pages/AiCopilot.tsx` | tsc 0 错 / vitest 全绿 / UI-RULES OK / build 通过 |
| W2 | P8 分形对标 + 成本锚定 | 待派 | `src/core/{analog_finder,scenario_cost_anchor}.py`(新) | n<10 只报不评；成本线缺失写「未取到」 |
| W2 | P9 RSI S0+S1 | 待派 | `src/core/{harness_replay,fitness_keypoints}.py`(新) `scripts/rsi_offline_review.py` | 只产出建议清单，绝不写配置 |
| 集成 | 接线 | 主模型 | `src/web/api/chat.py`（≤2310 行护栏） | 跨支冒烟 + 全量门禁 |

**不做清单**（本轮明确排除）：换模型、上向量记忆/RAG、任何自动改 Frozen Ring、K 线渲染层改造、卡片化重排。

---

## 6. 验收口径（不认自述，只认跑出来的数）

1. 每支：`git log origin/main..HEAD` 只含本任务 commit；禁改文件一行未动。
2. 后端：`env -u PYTHONPATH .venv/bin/pytest <本支相关> -q` 全绿 + `scripts/trace_specs.py` 100%。
3. 前端：`npx tsc --noEmit` 0 错 + 定向 `vitest` 全绿 + `node scripts/check_ui_rules.mjs` = UI-RULES OK + `pnpm build` 成功。
4. 合并后：跨支组合冒烟（各支新测试文件放一起跑），再跑一次 chat.py 行数护栏。
5. 效果类指标（P5/P6）必须在合并后用真实一轮 trace 对比 `Σ elapsed_ms`，不接受「理论上更快」。
