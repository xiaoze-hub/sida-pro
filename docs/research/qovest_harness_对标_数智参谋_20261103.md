# qovest（阿里云 AI 投研工作台）harness 对标：SIDA-Pro 数智参谋该借什么、不该借什么

- **基线**：`/home/ubuntu/sida-pro`，HEAD `ef01decf`（`release: v0.96.0`），`VERSION` = v0.96.0，`git status` 干净。本报告**未修改任何代码**。
- **对标对象**：
  - 外部：qovest（万睿研见）— 附件《qovest harness 机制研究报告》（2026-11-03，登录态页面 + 前端 bundle `maas-fin-web 0.1.10` + 站内 API 交叉分析，标注为 AI 生成）
  - 内部：数智参谋链路 —— `src/harness/{pipeline,pipeline_market,recipes,routing,skills,actions,trace}.py`、`src/agents/chat/{registry,tools_catalog}.py`、`src/web/api/{copilot,copilot_jobs,copilot_workbench,chat}.py`、`src/core/{copilot_brief,delegate,copilot_schedule,number_audit}.py`、`frontend/src/pages/AiCopilot.tsx`、`frontend/src/components/copilot/*`、`frontend/src/lib/copilot/*`
- **覆盖范围**：上列文件的静态阅读 + 定点 grep/计数验证（62 工具 / 12 配方 / 10 管线配方 / 6 技能手册 / brief 781 行）。
- **未覆盖**：未启服务、未连生产库与真 LLM、未跑全量测试、未实测 qovest（仅凭附件二手事实）、未核实 qovest 附件中"40 位专家"与 `/agents` 返回 36 个的口径差异（文档自相矛盾，故本文不把任何专家数量当技术基线）。

---

## 0. 结论先行

一句话：**qovest 值得借的不是"40 个专家"，而是它对"同一轮研究不得有多种口径"和"审核只有阻断权、没有修改权"这两条纪律；SIDA-Pro 已经有实现底座，缺的是接线和门禁，不是新框架。**

| 判定 | 项 | 一句话理由 |
|---|---|---|
| **借（5 项）** | 共享事实包接线 | SIDA 已有 `build_copilot_brief`（781 行，含 as_of/stale/missing/caliber_notes），但**只服务看板，不服务对话和会诊** |
| | 结构化证据对象 | 证据仍是 `string[]`，无法回答"这个数字出自哪个工具、哪个时点" |
| | 审核阻断权 | `number_audit` 只追加 ⚠️ 附言，不阻断；应阻断**会误导决策的那张图/那个百分比**，不是整篇回答 |
| | 波次记账 | 委派已有 `parent_run_id`，缺 `dispatch_reason` / `artifact_ref` / `fact_pack_id` 三列 |
| | 调度四档语义 | 现只有 2 类任务 + `HH:MM`，无盘中间隔、无运行记录 |
| **不借（4 项）** | 36/40 专家 + 14 专家团规模 | 违反产品定位铁律：SIDA-Pro 是**行情终端**（看图决策），不是研究报告工厂 |
| | workspace 四层隔离 + 存储配额 | 4 账号用 `user_id` 已够，多加一层是概念债 |
| | 内部工具改 MCP | SIDA 62 个工具在进程内，有口径校验与预算记账；套 MCP 是纯序列化开销 |
| | 第二个 LLM 审核员 | 成本翻倍、延迟翻倍，且"LLM 审 LLM"可靠性未验证 → 改确定性审核 |
| **已领先（4 项）** | 口径硬约束 / 确定性路由 / 管线化取数 / 数据源故障转移 | 见 §5，别在改造中把这些能力弄丢 |

---

## 1. 先对齐定位：qovest 和 SIDA-Pro 是同一哲学、不同形态

外部事实（2026-11-02 阿里云发布，公开报道）：

- 定位：面向专业投资者的 AI 投研工作台，slogan **"你负责判断，研究交给 Qovest"**。
- 形态：一句话研究需求 → 查数、读财报公告、深度分析 → **生成 HTML 报告** + 沉淀到"投研空间" + 定时跟踪。
- 商业化：邀测期送积分，后续 **99 元/199 元每月**订阅。

对照 SIDA-Pro（`AGENTS.md` + 产品定位铁律）：

- 定位：Web 大屏行情终端，**"看图决策，不做数据堆砌"**，K 线是绝对主角（≥80% 屏宽）。
- 形态：K 线 + 标注 + 一屏判定卡 + 对话式参谋；结论必须能回到图上的价位。
- 数据：TQ 直连（172.27.16.1:17709）+ 腾讯/东财/新浪，暗盘 v6 腾讯口径已定局。

**结论：哲学一致（人判断、AI 备料），交付物相反（报告 vs 图）。** 所以 qovest 的"编排纪律"可以借，"研究工厂"的形态不能借 —— 照搬 40 专家 + HTML 研究页会把数智参谋变成"每天读 8 篇长文的终端"，直接违反定位铁律。

---

## 2. 逐项对标（每一项都落到 SIDA 的代码位置）

| # | qovest 机制 | SIDA 现状（文件:行/事实） | 判定 |
|---|---|---|---|
| 1 | **共享事实包**：并行分析使用同一事实包，成员不得各自采用不同截止时间或口径；含价格市值股本、预测财务、估值历史、可比公司、宏观假设 | `src/core/copilot_brief.py` 781 行已产出 `{as_of, session.stale, session.age_s, evidence_missing, verdict.caliber_notes, quote.source}`；但唯一调用方是 `src/web/api/copilot.py:74` 的 REST 路由 | **缺口=F1：只给看板用** |
| 2 | **波次记账**：每波次必须存 输入/责任专家/状态/产物ID/失败原因；条件派发必须记 `dispatch_reason` | `CopilotRun` 有 `parent_run_id`、`recipe`、`status`、`error`、`tool_calls`；`delegate.aggregate()` 只汇总「事实与缺口」 | **缺口=F2：无派发理由、无产物引用** |
| 3 | **专家指令 = 岗位说明书**：驱动逻辑 + `skill_ids` + 产物协议 + 完成标准 + 工作记忆 | 12 个 YAML 配方已有 `when/tools/skills/output_sections/max_tool_calls/prompt_fragment/priority/pipeline/scope`，`recipe_fingerprint` 做内容版本；`devil_advocate.yaml` 已含「可证伪条件」段落 | **基本对齐，缺 §2.3 的产物协议与完成标准强校验** |
| 4 | **E35 只读审核 + 返修/阻断权**；E36 出稿 | `src/core/number_audit.py:201 audit()` + `:325 annotate()` = **只追加附言，不改正文、不阻断**；`chat_messages.evidence_audit`（m235）只存对账结果 | **缺口=F3：有审核，无阻断权** |
| 5 | **skills.lock.yaml** 锁定技能版本，审核须核对；技能有 versions 数组与 published 阶段 | `src/harness/skills.py`：按文件名读 md，`lru_cache`，`MAX_CHARS=1200`，**无版本、无 lock、无存在性校验**；`recipes.py` 的 `skills` 字段解析后从不校验 | **缺口=F4：写错技能名 = 静默不注入** |
| 6 | **连接器统一 MCP** + `credentialFields` + `domainKeys` + `supportsAgentRuntime` + `toolPreferences` | 62 工具进程内注册（`registry.py`），`ChatTool.caliber` 带口径标签；`harness/data_catalog.yaml` 26 域 ≈ domainKeys；`data_sources` 管优先级/启停/故障转移；`skills_gateway.py` 已对外暴露 HTTP API | **等价物已存在，且工程成熟度更高** |
| 7 | **WatchTask 调度**：premarket/intraday/postmarket/custom + 交易所时区 | `src/core/copilot_schedule.py`：只有 `auction_digest` / `daily_brief` 两类，`at_hhmm` + `weekdays`；`_build_text()` 已调 `is_trading_day`（非交易日不出内容）；无盘中间隔、无 run 记录、无停止 | **缺口=F5：只有"一次性到点"，没有"时段循环"** |
| 8 | **消息协议判别联合**：question/todo/subagent/research-progress/inventory/file，未知 kind 显式处理 | SSE 4 事件 `stage/delta/error/done`；`AiCopilot.tsx:1246` 已消费 `onDone`（替换流式草稿，保留对账附言），`:1262` 已有 `onMalformed` 上报 | **已修，无需借** |
| 9 | **结构化产物契约**（CashFlowEligibility / ValuationModelResult，schema 在服务端） | 证据仍是 `answer-blocks.ts:21 evidence: string[]`（从 markdown `【依据】` 切段）；`EvidenceBundle` 只在 `frontend/src/lib/copilot/types.ts:130` 有 TS 类型 | **缺口=F6：契约有、对象没有** |
| 10 | **会话/空间/run/subagent 四层隔离**，`subagent.reference = {conversationId, runId, agentName, status}` | `CopilotRun.parent_run_id` 已有真实父子关系；但 UI 的"多维依据透视"用的是 `AnswerCard.tsx:128 extractRealPerspectives()` —— **按关键词把单模型文本切成 agent 名** | **缺口=F7：有真子 run，UI 却在演假智能体** |

---

## 3. 建议采纳的 5 项（按 价值/成本 排序）

### F1 事实包接线：`build_copilot_brief` 接进对话与会诊链路

**为什么最重要**：qovest 写死的那条契约——「并行分析使用同一共享事实包，成员不得各自采用不同截止时间或口径」——正是 SIDA 用户最敏感的幻觉类型（"用昨日数据推断今日"）。现在 `delegate_many()`（`src/core/delegate.py:191`）是**串行**执行、每个子 run 自己重新取数：两个子结论可能落在不同分钟、甚至不同口径。父 run 汇总时没有任何机制能发现这件事。

**怎么落（不新造名词，直接复用）**：
1. `build_copilot_brief()` 增加可选参数 `now`/`symbols` 复用（签名已支持 `now`，见 `:692`），产出可为多标的的 `brief` 集合。
2. `delegate.delegate_many()` 增加 `fact_pack` 入参：父 run 取一次 → 子 run 通过 `compose_prompt()` 顶部注入，并在提示词里硬写「本轮事实包冻结于 {as_of}，来源 {source}，任何结论不得引用晚于该时点的数据，缺项按 evidence_missing 写「暂无」」。
3. 子 run 若再请求同一标的行情，命中 pack 直接返回（计数断言：上游调用次数不增加）。

**验收线**：
- 同一父 run 下 N 个子 run 的 `quote.as_of` 完全一致（哈希断言）；
- 子 run 重复取同标的时上游计数为 0；
- 注入文本里 `as_of` 与实际返回的 `as_of` 同源（不是各写一份）。

**成本**：中（1 个 dataclass 边界 + 提示词注入 + 2 个断言）。**前置依赖**：无。

---

### F2 结构化证据对象：让"这条数出自哪"可回答

**为什么**：qovest 专家的完成标准是「关键数字可复算、主张有来源」；SIDA 现在是 `evidence: string[]`，只能验证"文本里有这个数"，不能验证"这个数属于哪个工具、哪个时点、什么口径"。

**怎么落**：不改渲染，先补**出站载荷**。`done` 事件里除 `evidence_audit` 外增加 `evidence[]`：
```
{evidence_id, tool, source, as_of, caliber, status: ok|missing|degraded, digest, run_id}
```
底座都是现成的：`tools_catalog.py` 已按「域 / 时点 / 来源表」拼文本（`:144-165`），`registry.ChatTool.caliber` 已有口径，`CopilotStep` 已有 `seq/tool/ok/elapsed_ms`。**只做"文本 → 对象"的搬运，不新增取数逻辑。**

**验收线**：任取一条回答，其正文中每个硬数字都能在 `evidence[]` 里找到唯一 `evidence_id`（抽样 20 条，命中率 100%；找不到的必须落在 `missing`）。

**成本**：中。**依赖**：无（可独立上）。

---

### F3 审核阻断权分级：阻断"会误导决策的产物"，不是整篇回答

**为什么**：这是 qovest 最该学的一条纪律 —— E35 **只读复核，只报问题和阻断，不修改模型**。SIDA 的 `number_audit` 现在等价于"事后附言"，而真正危险的不是数字错，是**一张把模型编的价位画到真实 K 线上**的图。

**怎么落（三级，前两级零 LLM）**：
- **L1 确定性阻断（服务端，本期做）**：对账未通过的数字在卡片上标灰 + 不可复制；置信度自评**不得渲染为统计百分比**（该问题已于 10-28 前后修掉，`AnswerCard.tsx:310` 注释明确"不换算百分制"，保持）；`evidence_missing` 非空的维度不得出现结论句。
- **L2 产物阻断（服务端，正解已在仓库）**：`harness/actions.py:296 propose_scenario_annotations` 是"来源可信才画"的唯一正路（`source: scenario_matrix`，缺/非法不画）；**保持正则提价画线路径为已删除状态**，任何新前端需求都必须走 pending action → 用户确认 → `/copilot/annotations`。
- **L3 第二 LLM 审核员（明确不做，见 §4）**。

**验收线**：构造 3 个反例（编造价位、口径冲突、缺数据给结论），断言：① 图上不出线；② 卡片上该维度无结论句；③ 审核记录里写明触发的是哪条规则。**禁止用"审核通过率"当指标**，只统计"反例是否被拦住"。

**成本**：低-中（复用既有函数，主要是加拦截点与回归）。**依赖**：F2（需要 evidence 对象才知道哪条数该标灰）。

---

### F4 波次记账三列 + skills.lock

**为什么**：qovest 要求「每波次必须存 输入、责任专家、状态、产物ID、失败原因」和「派发必须记录 dispatch_reason」。SIDA 的委派已经能回答"谁跑了、成没成"，但回答不了**"为什么派它"**和**"它产出了什么"**。用户问"这条结论谁说的"时，现在只能靠 `recipe` 猜。

**怎么落**：
1. 迁移加 3 列到 `copilot_runs`：`dispatch_reason`（`persona:xxx` / `recipe:xxx` / `mention` / `parallel:user_split`，直接取 `routing.route_for_request` 返回的 `reason`）、`artifact_ref`（产物 id，先存 `run_id` 或 annotation id）、`fact_pack_id`（对齐 F1）。
2. `skills.lock`：仿 `recipe_fingerprint` 生成 `{name: {sha1, chars}}`，配方加载时校验 `recipe.skills ⊂ lock`，CI 加一条门禁：改 `src/harness/skills/*.md` 未同步 lock → 红。

**验收线**：`/copilot/parallel` 提交后，治理页能看到每个子 run 的派发理由；故意写一个不存在的 skill 名 → 加载即报错（而不是静默不注入）。

**成本**：低（1 迁移 + 2 处校验 + 1 条 CI 门禁）。**依赖**：F1（`fact_pack_id` 列先留空也行，但一起做省一次迁移）。

---

### F5 调度四档语义（premarket / intraday / postmarket / custom）

**为什么**：qovest 把"盘前一次 / 盘中循环 / 盘后一次 / 自定义"做成四档，并强制按交易所时区与交易时段执行。SIDA 的 `copilot_schedule` 只有"到 `HH:MM` 跑一次"，用户想要"盘中每 20 分钟盯一次"只能建 12 条任务。

**怎么落**：`kind` 从 2 类扩到「任务内容 × 时段」两个正交维度：
- 时段：沿用现有 `is_trading_day`（已有）+ 新增 `session_window` 判定（可直接复用 `src/core/trade_phase.phase_meta`，`copilot_brief.py` 已在用）；
- 循环：`interval_min` 字段，仅在盘中窗口内生效，跳到下个窗口不补跑（避免收盘后一次性刷 9 条）；
- 记录：`copilot_schedule_runs`（run_id / 状态 / 失败原因），对标 qovest 的 `/api/watch-tasks/runs`。

**明确不做**：多市场时区表（SIDA 只做 A 股，引入 Asia/Hong_Kong 表是空转）。

**验收线**：国庆/周末不跑（已有能力不能退化）；盘中任务在 11:30–13:00 不触发；单条失败不影响其他任务且可在记录页看到。

**成本**：中。**依赖**：无。

---

## 4. 明确不采纳（并说明理由，避免下一轮又有人提）

| 项 | 为什么不 |
|---|---|
| **36/40 个专家 + 14 个专家团** | qovest 的专家是"报告作者"（现金流建模、估值模型、HTML 研究页），SIDA 的配方是"看盘角度"（全景诊断 14 工具、暗盘、竞价、风险官）。**缺的不是更多专家，是同一问题口径一致 + 结论可回溯**。再加 20 个配方只会让路由更难、上下文更挤（现有 12 配方 62 工具已到提示词预算边界）。 |
| **workspace 四层隔离 + 存储配额** | qovest 服务多租户机构研究，空间承载"研究可接着做"。SIDA 是 4 个账号（admin/黄磊/娟姐/demo），`user_id` + conversation 已足够；再加一圈边界会让 `check_scoped_queries.py` 的既有门禁复杂化而无收益。 |
| **内部 62 工具改 MCP** | 进程内调用才有 `caliber` 校验、`charge_tool_budget` 预算记账、`Trace` 逐工具记账。套一层 MCP = 序列化开销 + 丢失这三样。qovest 用 MCP 是因为它要接**厂商**数据源（iFinD/Wind/Tushare），SIDA 的工具是**自建算法**，性质不同。 |
| **第二个 LLM 审核员（E35 的 LLM 版）** | 成本翻倍、首字延迟翻倍，且"LLM 审 LLM"的漏检率没有任何实测支撑 —— 与用户"先验证再投、证伪即停"的做法冲突。**改为确定性审核**（数对账 + 口径 + 时点 + 缺口披露），拦不住的部分宁可在 UI 显式标 `unknown`。 |
| **HTML 研究页模板** | 直接违反"看图决策、不做数据堆砌"。数智参谋的产物是**卡片 + 图上的线**，不是长文档。 |

### 一个反向机会（非本期工程项）

qovest 把"连接器"当产品面卖（iFinD/Wind/Tushare 各自配 Token 即接入）。SIDA 的 `skills_gateway.py` 已经在对外暴露 HTTP 能力（含配额、限流、游客 key）。**方向是反的**：不是 SIDA 去接 MCP，而是把 SIDA 的暗盘主力意图 / 决策先锋做成一个 MCP server，让 qovest 这类平台来调 —— 这是 SIDA 独有数据（腾讯逐笔 v6 口径）的对外变现路径。qovest 订阅价 99/199 元/月可作为对外的定价锚。**本期只做记录，不动手。**

---

## 5. SIDA 已领先的 4 项（改造时不许弄丢）

1. **口径硬约束**：`src/core/caliber.py` + `require_directional()`，非 tick 口径做方向判定直接抛 `CaliberViolationError`。qovest 的附件通篇只有"不得采用不同口径"的**软约束（写在指令里）**，没有任何代码级裁决。这是 SIDA 最硬的一条护城河。
2. **确定性路由**：`harness/routing.py` 优先级 `@提及 > 标的画像 > 关键词 > 兜底`，并把数据类问题温度压到 0.2。qovest 的"条件派发"由 LLM 指令决定激活（`dispatch_reason` 靠模型自觉写），SIDA 是代码判定、可复现。
3. **管线化取数**：12 配方里 10 个 `pipeline: true`，模型不选工具、只写结论，工具集合 Jaccard = 1.0。qovest 仍是"模板 + 模型 + 服务端契约"混合编排。
4. **数据源故障转移**：`data_sources` 优先级/启停/死源治理（同花顺实时 0/13,300、智兔分红 4/10,597 已默认关闭）。qovest 的 10 个连接器状态是"**0 已接入**"—— 数据管道默认未启用。

> 换句话说：qovest 的**框架骨架**更完整，SIDA 的**数据可信度与口径纪律**更成熟。要学的是前者里"编排记账"的部分，不是它的报告形态。

---

## 6. 分三期落地 + 依赖

```
F2 结构化证据 ──┐
                ├──> F3 审核阻断（需要 evidence 定位到具体数字）
F1 事实包接线 ──┴──> F4 波次记账（fact_pack_id 与 F1 同一次迁移）
F5 调度四档（独立，可并行）
```

| 期 | 内容 | 责任方 | 验收线（硬） |
|---|---|---|---|
| P1 | F1 事实包接线 + F2 结构化证据 | Hermes | 子 run `quote.as_of` 全一致；抽样 20 条回答硬数字 100% 可回溯 |
| P2 | F3 审核阻断 L1/L2 + F4 记账三列 + skills.lock | Hermes | 3 个反例全被拦；改了 skill md 未同步 lock → CI 红 |
| P3 | F5 调度四档 + 运行记录 | Hermes | 节假日不跑、午休不跑、失败可查 |

**每期自带门禁**（沿用仓库既有约定）：`ruff` + 迁移连续性 + `check_scoped_queries.py` + 前端 `tsc/ESLint/ui:check`；P2 额外跑 `tests/test_copilot_*` 定向批次。**性能未实测不得写"达标"。**

**不做项**（写死，防止范围蔓延）：第二 LLM 审核员、workspace 配额层、内部工具 MCP 化、HTML 研究报告页、专家数量扩张。

---

## 7. 证据边界

- 附件是**单一来源、AI 生成**的二手报告，且自相矛盾（`/agents` 返回 36 个 vs 宣传 40 位）。本文引用的是它的**机制契约**（波次记账字段、dispatch_reason、共享事实包内容清单、E35 只读语义），这些在附件里有 API 字段级引用支撑；**未引用的**是它的专家数量、技能数量与成熟度判断。
- 本文所有"SIDA 现状"结论均来自当前 `ef01decf` 的静态代码与 grep 计数，**非推测**。
- 10-28 那份《数智参谋_源码核查》里的 P0 有 4 项在 v0.89–v0.96 之间已修（`onDone` 已消费、坏帧 `onMalformed` 已上报、齐套度 pending 不再洗成 ok、正则提价画线已删、`recipe` 已透传到执行链）。**本报告不复述那些已关闭项**，避免基于旧结论重复施工。
