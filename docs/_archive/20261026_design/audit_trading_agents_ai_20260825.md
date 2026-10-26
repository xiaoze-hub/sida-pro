# SIDA 交易智能体 + AI 层审计报告

日期: 2026-08-25
对象: src/agents/, src/web/api/chat.py, prompts/, src/core/ai_client.py + 反证层 + 预测相关
定位: AI = 情报备料 + 供给逻辑, 不做方向预测

---

## 0. 审计范围与关键文件

| 模块 | 文件 | 行 |
|---|---|---|
| AI 客户端 | `src/core/ai_client.py` | 11-259 (AIClient), 262-510 (场景配置中心) |
| Agent 基类 | `src/agents/base.py` | 272-469 (BaseAgent.run), 53-118 (场景绑定) |
| 盘中监控/反证层 | `src/agents/intraday_monitor.py` | 288-577 (AI 反证层 + 限速/冷却/缓存) |
| 对话助手 | `src/web/api/chat.py` | 42-58 (SYSTEM_PROMPT), 1935-2058 (tool 循环), 2700-2800 (对话端点) |
| 深度分析 | `src/agents/tradingagents/agent.py` | 197-748, `llm_adapter.py` 23-113 |
| 预测代理 | `src/web/api/forecast.py` + `chat.get_forecast` | 63-97, chat.py 319-330 / 1275-1278 |
| 预测后验 | `src/core/prediction_outcome.py` + `context_store.py` | 75-163 |
| 调用统计 | `src/web/api/llm_usage.py` | 32-97 |
| 登录/演示限流 | `login_ratelimit.py`, `demo_limit.py` | 全文件 |
| Prompt | `prompts/*.txt` | 全文件 |

---

## 1. 智能体能力边界 — 总体健康 ✅

**边界定位正确处:**
- `prompts/premarket_outlook.txt:1` 明确「任务是情报 + 供给逻辑, 不是预测明天涨跌（AI 方向预测已被证明不可靠）, 方向判断由交易者自己完成」→ 与「情报备料不预测方向」定位一致。
- 反证层 `intraday_monitor.py:288-291` 定位正确: LLM 只做「规则结论的解释/评级/置信度」, 不改变算法结论, 失败静默降级不影响主链路。
- 事件催化 `get_intent_explain` (chat.py:1602-1626): 「规则给结论, AI 只做解释不改变结论」— 边界清晰。

**能力边界风险:**
- **TradingAgentsAgent 是最强的"方向预测/交易决策"面** (`agent.py:197-233`): 多智能体委员会产出 BUY/SELL + 目标价, 还 `emit_paper_trading_signal` 驱动模拟盘。虽然质量高于单次 LLM, 但这与「不做方向预测」的定位存在张力, 且单次 3-5 分钟 / 30 分钟硬超时 (`agent.py:212`) / $10/月预算 (`agent.py:206`) 是可观成本面。
  - 建议: 保留但强化「委员会结论必须附证伪条件/置信度区间」, 并审计其目标价是否被模拟盘直接采用。
- **几个 agent 的 analyze 直接 `context.ai_client.chat()` 无任何重试/降级包装上游** (`base.py:299`, `daily_report.py:697`, `news_digest.py:479`, `premarket_outlook.py:986`, `intraday_monitor.py:1826`)。单一 LLM 故障会整链抛异常 (base.py:467-469 re-raise)。这里有降级空间。

---

## 2. Prompt 质量 — 整体高, 见表

| Prompt | 质量评估 | 主要问题 |
|---|---|---|
| `premarket_outlook.txt` | ✅ 最佳 | 情报定位+证伪条件+环境分+今日不碰清单, 结构最严谨 |
| `intraday_monitor.txt` | ✅ 好 | 反幻觉铁律强, 见下方 ⚠️ 输出规范自相矛盾 |
| `chat.py SYSTEM_PROMPT` | ✅ 好 | 规则都有日期可溯源, 但已累积 16 条长达 6 段 (42-58), 有碎片化风险 |
| `daily_report.txt` | ✅ 好 | `<--PANWATCH_JSON-->` 结构化提取, 但见定位张力 |
| `news_digest.txt` | ✅ 好 | 精简, 按股票合并要点 |
| `chart_analyst.txt` | ⚠️ 偏薄 | 无证伪逻辑, 纯 K 线形态, 易输出方向性结论 |

**⚠️ 关键问题 1 — `intraday_monitor.txt` 输出规范自相矛盾:**
- 行 98: 「你必须只输出一个 JSON 对象（不要任何额外文字/解释/Markdown）」。
- 行 119-138: 却给了纯文本示例「【示例】无需提醒时:[无需提醒]」「「信号」放量突破压力位…」。
- 两种规范并存在同一 prompt, LLM 可能随机选择 → 下游 JSON 解析 (`signals/structured_output.py try_parse_action_json`) 失败率上升。
- 修复: 删掉 119-138 的纯文本示例, 只保留 JSON schema 示例; 或在 collect/build_prompt 侧对两种输出都做容错归一。

---

## 3. 限流 / 429 治理 — 反证层最优, 全局缺失 ❌⚠️

**已成型的局部限制 (质量高):**
- `intraday_monitor.py:293-327` 反证层三重治理:
  - 8s LLM 超时 (`_AI_LLM_TIMEOUT`) @293
  - 全局令牌桶 10/min (`_AI_RATE_MAX_PER_MIN`) @296, `_ai_rate_allow()` @304-318
  - 429 全局冷却 600s (`_AI_429_COOLDOWN_S`) @297, `_ai_rate_mark_429()` @321
  - 当日缓存 skips re-call (`biz_cache`) @479-489, 成功写缓存 6h @558
  - 进程级后台事件循环复用修 `Event loop is closed` @344-372
- `ai_client.py:22` `max_retries=0` 关 SDK 自动重试 — 正确, 防 retry 放大风暴。
- `login_ratelimit.py` 5 次失败锁 10 分钟; `demo_limit.py` demo 账号 10 次对话/天 @16 + 20 次 GET/小时 @51。

**❌ 全局缺口 — 缺统一 LLM 限流/熔断器 (最严重发现):**
- `AIClient.chat` (`ai_client.py:56-111`)、`chat_multi` (113-140)、`chat_with_tools` (142-165): **无客户端重试、无指数退避、无 429 检测、无熔断**。任何异常只 `raise` (行 110-111)。
- 唯一做 429 治理的是反证层 (`_ai_counter_check` @466-577)。其余所有调用点 (`base.py:299`, `chat.py:_run_tool_loop@1935-1990`) 裸调 client, 429 直接 yield/raise "AI 服务暂时不可用" — **无 backoff 重试**。
- **多 agent 并发无全局协调**: 盘前(9AM)、盘中每 30 分钟 ×N 只、日报、新闻 digest + 用户并发会话可同时打爆一个 provider 的 rpm → 429 风暴。当前仅反证层被限流, 主 LLM 通道裸奔。
- 建议: 在 `ai_client.py` AIClient 内统一加**进程级半开熔断器 + 令牌桶 + 指数退避** (可借鉴反证层现成实现 @304-327 上提为共享 RateLimiter), 所有 `.chat*` 调用自动经过。非 demo 账号的对话 LLM 调用也应有 per-user/cost 配额 (当前仅 demo 有)。

---

## 4. 预测准确率与 AI 定位 — 方向预测面与定位冲突 ⚠️

**已建立的后验体系 (好):**
- Agent 建议按 horizon 1/5 日记录 `save_agent_prediction_outcome` (premarket_outlook.py:1097-1115, daily_report.py:790, intraday_monitor.py:1893), 由 `prediction_outcome.py:75-163` 评估, 交易日历已对齐节假日 (`trading_calendar.py`, pred fix S6)。
- 预测引擎(外部 :8010, Kronos+XGBoost+回归)回测方向命中率 `forecast.py:166-194`。已知 LLM/引擎方向准确率 31.7% < 抛硬币。

**❌ 与「不预测方向」定位冲突的点 (应整改):**
1. **对话助手暴露不可靠方向预测**: `chat.py` 的 `get_forecast` 工具定义 @319-330 + 执行 @1275-1278, 会把 31.7% 准确率的「方向/目标价」直接给用户; 且工具描述 (320) 让 LLM 主动"解读预测结果"。
2. **`suggested_questions` 主动引导**: `chat.py:2497-2500` 生成「系统预测 X 多少？和我的判断比呢?」— 把不可靠预测抬成决策锚点。
3. **`daily_report.txt:61-63`「明日关注」** 要求 LLM 给方向性前瞻, 与盘前 prompt 的"不做方向预测"口径不一致。

建议:
- `get_forecast` 从 CHAT_TOOLS 默认集摘除或加"低准确率仅供参考"强制标注; `suggested_questions` 删除 @2498-2500 的预测引流分支。
- `daily_report` 明日关注改为「明日需验证的证伪信号」而非"看涨/看跌前瞻", 与盘前定位对齐。
- 在预测页/设置页把 31.7% 命中率显性展示, 并加"命中率低于随机, 不建议据此下单"提示。

---

## 5. Token 节奏 (agnes / deepseek) — 架构合理, 可选优化

- **分工清晰**: 主对话/报告走 deepseek (`_get_ai_client` chat.py:2128-2193); 视觉看图走 **agnes-2.5-flash** (`_describe_image` chat.py:1776-1837, 兜底 @1801-1805)。agnes 仅处理图像描述(text→主模型), 不参与主推理 — token 占比小。
- **用量记账完整**: `ai_client.py:_log_usage` @33-54 + 流式 `chat_with_tools_stream` 也记 usage @222-225; `llm_usage.py` 费用估算 (flash 0.004 元/千tok @18, 其他 0.02 @19)。
- **画像注入节流**: profile 截断 300 字 + 前 3 条规则 (`ai_client.py:268-269`), 控 token 成本 — 好。
- 可优化: 反证层 10/min 令牌桶虽只作用于反证, 但若复用为全局限流, 需按 scene/service 分桶 (deepseek vs agnes 各自配额), 避免 429 冷却误伤另一通道。

---

## 6. 优化建议汇总 (按优先级)

1. **[P0] 缺全局 LLM 限流/熔断**: 在 `ai_client.py` AIClient 内建共享 token bucket + 半开熔断器 + 指数退避 (复用 `intraday_monitor.py:304-327` 实现), 所有 `.chat*` / agent 调用自动经过。解决多 agent 并发 429 风暴。
2. **[P1] `intraday_monitor.txt` 输出规范矛盾**: 删 119-138 纯文本示例, 只留 JSON schema。
3. **[P1] 方向预测面收缩**: 摘除/标注 `get_forecast` (chat.py:319-330,1275-1278), 删 `suggested_questions` 预测引流 (chat.py:2497-2500), `daily_report.txt:61-63` 改证伪信号口。
4. **[P1] TradingAgents 决策面加约束**: `agent.py:197-233` 的 BUY/SELL + 目标价须强制附证伪条件, 审计 `emit_paper_trading_signal` 是否让低置信方向直达模拟盘。
5. **[P2] agent 单点 LLM 调用降级**: `base.py:299` 及各 agent analyze 增加"多 provider 切换/降级提示"包装, 而非裸 raise (目前 base.py:467-469 整链 re-raise)。
6. **[P2] 非 demo 用户对话配额**: 现仅 demo 有限流 (`demo_limit.py`), 建议加 per-user 每日 LLM 调用/费用配额。

---

## 7. 结论

- **最强面**: 反证层治理 (限速/冷却/缓存/loop复用/ws豁免) 已到位, 是 5 连修后的稳健实现; 盘前 prompt 是「情报备料不预测方向」定位的最佳示范。
- **主要风险**: 全局 LLM 通道无统一限流/熔断 (multi-agent + 并发会话易爆 429), 以及 `get_forecast`/`suggested_questions`/TradingAgents 三处仍向用户输出不可靠方向预测, 与既定定位冲突。
- **未改动任何源文件** (仅新增本审计文档; `src/agents/__init__.py` 在 HEAD 本就为空, 无变更)。