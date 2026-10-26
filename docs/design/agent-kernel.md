# 数智参谋 Agent 内核设计（可执行版）

> **基线**：`v0.20.6` @ `88c4016`（2026-10-20）
> **对象**：`src/web/api/chat.py`(2161 行)、`src/harness/{context,recipes}.py`、`src/agents/chat/registry.py`(55 工具)、`frontend/src/pages/AiCopilot.tsx`、`src/db/models.py`
> **覆盖范围**：后端对话链路 + 运行时 + 轨迹 + 迁移；前端只定协议不实现（P1 才动）。**未覆盖**：`src/agents/*` 后台定时 Agent（那是 `agent_runs` 的领地）、交易执行链路（红线，永不开）

---

## 0. 一句话

把 AI 从「页面右下角的对话框」变成**终端的一层运行时**：它和 K 线共用同一份数据口径、和页面共用同一份上下文、和用户共用同一套权限与审计——而不是在旁边多一个聊天窗口。

---

## 1. 目标与非目标

### 目标（按优先级）

| # | 目标 | 验收方式 |
|---|---|---|
| G1 | 回答**可回放**：每次问答能查到调了哪些工具、参数、结果摘要、耗时 | `copilot_runs` + `copilot_steps` 有数据，任选一条可还原链路 |
| G2 | 运行**可治理**：工具白名单、调用预算在运行时强制执行，不靠提示词自觉 | 单测：白名单外被拒、超预算被拒 |
| G3 | 上下文**原生**：页面状态（标的/周期/圈选区）自动进 context，用户不用重复声明 | P1 验收：从 K 线页面提问，回答不出现"你指哪只股票" |
| G4 | 结论**落在图上**：AI 产出可回写为标注/自选/盯盘任务 | P1 验收：点击"标注到图"能在 K 线看到 |
| G5 | 好坏**可度量**：30 条历史事故成为回归集，配方改动前后跑分对比 | P2 验收：回归集通过率不掉 |

### 非目标（明确不做，写进代码注释）

1. **不做通用助手**：不问天气/写代码/做百科。
2. **不做自动交易**：不生成买卖指令，工具层硬 403，只给客观证据。
3. **不让模型算数字**：所有数值来自结构化工具，模型只做解读（幻觉红线）。
4. **不做数据堆砌**：不堆指标卡片，按"决策需要"分层。
5. **不复制 Hermes 的多平台能力**：宿主是行情终端，不是 IM。

---

## 2. 架构：三层

```
┌──────────────────────────────────────────────────────────┐
│ 融合层（P1）                                              │
│  页面锚点 · 动作回写 · 口径标签 · 标注图层                 │
├──────────────────────────────────────────────────────────┤
│ 运行时层（P0 本次交付）                                     │
│  AgentRuntime: 白名单 / 预算 / 计时                        │
│  CopilotTrace: copilot_runs · copilot_steps                │
│  既有: context.py(配方/提及) · recipes/*.yaml(6 配方)        │
├──────────────────────────────────────────────────────────┤
│ 工具层（已有，55 个）                                       │
│  CHAT_TOOL_REGISTRY → _execute_tool → 采集器/业务 API       │
│  口径统一走 src/core/caliber.py（禁 LLM 自行判定方向）        │
└──────────────────────────────────────────────────────────┘
```

**分层铁律**：融合层不直接调采集器；运行时层不写业务逻辑；工具层不做展示。

---

## 3. 模块契约（P0 已实现）

### 3.1 `src/harness/trace.py`

```python
CopilotTrace(conversation_id, user_id, recipe, question)
  .open()                                   # 落 running 行
  .step(tool, args, ok, error, result, elapsed_ms)
  .finish(status="ok"|"error", error)       # 收尾
start_copilot_run(...) -> CopilotTrace | None   # 任何异常返回 None
finish_copilot_run(trace, status, error)        # 幂等收尾
```

克制点（写死在实现里）：
- `result_digest` 只存前 **500 字**——工具返回含用户持仓/自选，全量落库是二次留存；
- `args` 截断 **2KB**；
- **所有写入吞异常**：轨迹是旁路，绝不能因为它把用户的问题搞挂。

### 3.2 `src/harness/runtime.py`

```python
AgentRuntime(trace=None, max_tool_calls=40, allowed_tools=None)
  .allow(tool) -> (bool, reason)
  await .call(tool, args, executor, **kwargs) -> str
```

- 默认 `allowed_tools=None`（不限制）、预算 40 次（现网实测 2~6 次，留 6 倍冗余）；
- 超预算/越权时**返回人话文案**给模型，让它基于已有数据作答而不是死循环；
- 工具异常原样抛出（保持 `_execute_tool` 既有契约），但轨迹里记 `ok=False`。

### 3.3 `chat.py` 的接入方式（strangler，不重写）

```
_run_tool_loop(..., conversation_id, recipe, question)   ← 新外壳：开轨迹/收尾
        └── _run_tool_loop_inner(..., runtime)           ← 原函数体，只换工具执行那行
                └── runtime.call(name, args, _execute_tool, db=db, user=user)
```

原 2161 行函数体**一行未改**，只在工具执行处换成 `runtime.call`。这样回滚成本 = 删掉外壳。

---

## 4. 数据表（迁移 `_m208`）

```sql
copilot_runs(
  id, run_id UNIQUE, conversation_id, user_id, recipe, question,
  status(running|ok|error), error, tool_calls, elapsed_ms,
  created_at, finished_at
)
-- 索引: (conversation_id, created_at), (user_id, created_at)

copilot_steps(
  id, run_id, seq, tool, args(≤2KB JSON), ok, error,
  result_digest(≤500字), elapsed_ms, created_at
)
-- 索引: (run_id, seq)

copilot_feedback(
  id, run_id, user_id, useful, reason, created_at
)
```

**与 `agent_runs` 的区别**：`agent_runs` 记后台定时 Agent（agent_name / trigger_source / notify_*），本设计记**用户对话**的运行时轨迹。两张表语义不同，不合并。

---

## 5. 融合面协议（P1 接口草案，本次不实现）

前端把"我现在在看什么"推给后端，而不是靠 URL：

```http
POST /api/copilot/context
{
  "page": "kline",
  "symbol": "600111", "market": "SH",
  "period": "1d", "range": {"from": "2026-09-01", "to": "2026-10-20"},
  "selection": null,                    // 圈选的 K 线区间，P1 用
  "indicators": ["MA5", "MA20", "VOL"],
  "anchor_line": 35.20                  // 当前光标所在价位
}
```

规则：
1. **优先级**：用户本条文字里的代码 > `@提及` > 会话绑定 > 页面锚点（已实现，`_bind_conv_symbol` + 工具层三态守卫）。
2. 页面锚点**只补空**，绝不覆盖用户明说的标的。
3. 无标的且三者皆空 → 必须先问"哪只股票"，禁止用持仓/自选顶替（v0.19.10 教训）。

---

## 6. 分期与验收

### P0（本次，1 天）—— 内核 + 轨迹

| 项目 | 内容 | 责任方 |
|---|---|---|
| 表 + 迁移 | `copilot_runs/steps/feedback`，`_m208` | Hermes |
| 运行时 | 白名单 / 预算 / 计时 | Hermes |
| 接入 | `_run_tool_loop` 外壳，工具执行走 runtime | Hermes |
| 测试 | 7 例（轨迹/错误态/摘要截断/预算/白名单/kwargs/迁移） | Hermes |

**验收线**：真机问一次"600111 现在怎么样"，`copilot_runs` 出现 1 行 `status=ok`，`copilot_steps` 出现该轮全部工具调用且 seq 连续。

### P1（1–2 周）—— 页面即上下文 + 动作回写

- `/api/copilot/context` 落地；前端在 K 线页挂载时上报视图状态；
- AI 动作白名单：`add_watchlist` / `create_annotation` / `create_alert`（全部需二次确认）；
- **验收线**：在 K 线页面圈一段区间直接问"这段为什么跌"，回答不需要用户补充标的；点"标注到图"能在图上看到，且标注带口径标签。

### P2（2 周）—— 技能手册 + 评测集

- `src/harness/skills/*.md`：盘前读法、竞价读法、筹码读法，按配方注入；
- 30 条历史事故转回归用例（竞价别名、标的串台、`9:25` YAML 整数、跌停 `--`、成交额 `--` 等）；
- `copilot_feedback` 接前端"有用/没用"按钮；
- **验收线**：改动前后回归集通过率可对比，掉分阻断合并。

### P3（可选）—— 治理看板

- 轨迹回放 UI（时间轴 + 每步摘要）；
- 成本看板：按用户/按配方统计 token、工具次数、P95 延迟；
- 多用户配额（`user_id` 维度限流）。

---

## 7. 风险与对策

| 风险 | 表现 | 对策 |
|---|---|---|
| **成本失控** | 页面上下文 + 55 工具 + 长历史，单轮 3–4 万 token | P2 起按配方裁剪上下文，预算守卫前置而非事后丢弃 |
| **延迟** | 看 K 线时等 30 秒不可接受 | P1 拆分"即时标注（无模型）"与"深度研判（有模型）" |
| **口径分叉** | AI 说"主力净流入"、前端说"主力意图"，两个数当场失信 | 强制 `caliber.py` 标签，工具返回必带 `caliber` + `direction_semantics` |
| **假融合** | AI 只能读、不能改页面 | P1 验收只看"动作回写"这一个硬指标 |
| **轨迹污染** | 落库失败拖垮问答 | 全部写入 try/except，失败静默 |

---

## 8. 回滚

- 代码：删 `_run_tool_loop` 外壳即可回到 v0.20.6 行为（内核函数体未改）；
- 数据：三张新表，无外键依赖，`DROP TABLE` 即可，不影响既有表；
- 迁移：`_m208` 只做 `create(checkfirst=True)`，重复执行安全。

---

## 9. 与「金蝶灵基」的对应关系（定位澄清）

| 灵基 | 本设计 | 说明 |
|---|---|---|
| 企业 AI 操作系统 | **行情终端的 AI 运行时** | 都强调"AI 是底座不是插件" |
| 组织/岗位/角色权限 | `user_id` 隔离 + BYOK + 工具白名单 | 灵基更完整，SIDA 先做"够用" |
| 业务对象本体 | **看盘对象**：标的/周期/口径/标注 | SIDA 的本体是"图"，不是单据 |
| Work/Manage/Build 三模式 | 研判 / 治理 / 配方编辑 | P1–P3 逐步对齐 |
| 智能体市场 | 配方（6 个 YAML）→ 技能手册 | 小规模自用，不做交易市场 |
| 灵基点计费 | token + 工具次数审计 | P3 看板 |

**关键差异**：灵基交付"流程跑完"，SIDA 交付"这条线为什么在这"——结论必须落到图上。
