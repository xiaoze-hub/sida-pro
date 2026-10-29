# 报告 / 推送 / 删除 / 情景 —— 四审收口（跨租户泄漏与误删误发回归）

- **基线**: `8f1b5e13`（三审 HEAD，本分支 `fix/portfolio-security-round3-20261029`）
- **触发**: 独立复审对三审修复做的真实红测/探针（`pfsec_r3_probe.py` / `pfsec_sub_probe.py` /
  `pfsec_del_probe.py` / `pfsec_http_probe.py` / `pfsec_pg_nan_probe.py`）发现 7 类残留，
  其中 2 类是**三审修复自身引入或未收口**的（共享报告"假共享"、订阅推送仍拿最后一份正文）。
- **改动文件**: `src/core/report_generator.py`、`premarket_evidence.py`、`report_scheduler.py`、
  `report_scope.py`、`scheduler.py`、`notify_center.py`、`agent_runs.py`、`scenario_compose.py`、
  `decision.py`、`decision_cache.py`、`numutil.py`、`src/bootstrap/runtime.py`、
  `src/web/api/{reports,agents,stocks,decision,accounts,notifications}.py`、
  `src/web/api/{recommendations,skills_gateway}.py`（通知归属）、前端 2 处类型声明。
- **未改**: `AGENTS.md`/`CHANGELOG.md`/`VERSION`/`docs/_frozen`/`SYSTEM_PROMPT`；未清理历史文件。

## 1. 每条残留、修法与实测

### R1 共享报告"假共享"（修前可被 member 读到别人的成本）

三审把 `user_id=None` 收敛成"共享桶"，但共享桶（`positions.user_id IS NULL`）里仍是
**某个人的历史成本**；而 meta 用 `bool(user_id)` 写 `contains_private_holdings=false`，
于是这份文件对全部 member 可读（探针 A：`A_shared_bucket_rows` 有 LEGACY1 成本，`A_member_reads_this_file=true`）。

修法（三层）:

1. **生成侧**: 共享口径 `_collect_positions(user_id=None) → []`；`premarket_evidence` 的
   `portfolio` / `fund_structure` 块在共享口径直接返回 `missing`（不查库）。共享报告=纯市场
   （指数/资金流/涨停/快讯/全市场候选/策略信号）。
2. **标记**: `contains_private_holdings` 改由 `_detect_private_holdings(data)` 按**实际采集内容**
   判定（有 positions/portfolio/fund_structure 项或 `user_id` 都算私有）；判定异常 → True。
   共享口径却采到私有内容时打 error，并据此写 meta（下游 member 读不到）。
3. **读侧**: meta 判定通过后，member 再走 `report_scope.content_looks_private_holdings()`
   内容绊线（扫头 64KB，只认列表行上的 `N股 成本 X` / `成本: X`）。meta 自称公共但正文有
   持仓 → 一律 404（不假推断；读不了文件也按不可见）。

产品口径同时落地：定时报告改为**共享市场报告**（成员报告中心不会永久为空）；私有持仓只按
用户生成（`generate_market_report(report_type, user_id=uid)` → `scope=private`）。

### R2 订阅推送跨租户（真实推送）

`scheduler._run_agent` 把 `results[-1]`（最后一个用户桶的正文）传给 `_push_to_subscribers`，
后者推给所有未投递订阅者 → u2 收到 u1 的私有正文（探针 B：`PUSHED` 含 u1 内容）。

修法: `_push_to_subscribers(agent_name, results, ...)` 建立 `user_id → result` 映射，**按接收用户
匹配本人桶**；无匹配 → 不发送 + 逐条 warning + 汇总日志（已发 N/无匹配 M/订阅 K）。
渠道集合同样按接收用户解析（`resolve_notify_channels_ex`）并传给通知中心做最终交叉校验。

### R3 通知归属缺失

- `src/web/api/agents.py::_spawn_async_run` 起线程后 `_safe_notify` 不带 user_id → 兜底推 owner。
  现在起线程前取标量 `user.id`，经 `notify_user_id` → `notify_task_done(user_id=...)`；
  同时解析该 agent 的渠道集合并带下去。
- `report_scheduler._push_report_notification` 不传 user_id → 通知落 NULL 归属、外发打全局渠道。
  现在显式传可信归属：共享报告通知归 owner（数据责任人），解析不到则只走全局渠道并 warning。
- `notify_task_done` 的 owner 兜底改为**显式 opt-in**（`allow_owner_fallback=True`），仅系统运维
  告警（skill key 冻结、全市场策略刷新）使用；普通任务漏传 user_id 时按共享桶处理并 warning，
  **不再推给 owner/任何人**。顺手修了 `skills_gateway` 调用缺 `ok` 关键字（此前静默 TypeError）。

### R4 最终外发绕过渠道选择集

`resolve_notify_channels_ex` 返回的是"配置 ∩ 归属可见"，但 `notify_center._build_notifier`
只看 user_id → 全渠道（探针 D：`resolved=[1]`，`push_layer=[1,2]`）。

修法: `push_notification(..., channel_ids=...)` + `_build_notifier_checked(user_id, channel_ids)`:
- `channel_ids=None` → 保持旧语义（该归属全部启用渠道）；
- 显式集合 → **只发集合内 ∩ 归属可见**的渠道；他人的/已禁用的 id 进 `denied` 并 warning；
- 空集合 / 全部被剔除 → **一条都不发**，`push_status=skipped` + 明确原因（不退化回全渠道）。
调用点（agent 触发完成通知、stock agent 完成通知、订阅推送）都带上了选择集。

### R5 删自选误删他人提醒

`DELETE /api/stocks/{id}` 按 `stock_id` 无条件删 `PriceAlertRule`/`PriceAlertHit`/`StockAgent`，
owner 删共享自选会连带删掉 member 的提醒规则（探针：`member rule still exists: False`）。

修法: 先判"是否存在**他人的**规则"（`user_id != 我` 且非 NULL）→ 有则 409 通用文案、整单拒绝；
只删本人的规则/命中（共享 NULL 规则仅 owner 可处置）。`StockAgent` 无 user_id 列，写入路径已按
股票归属校验（私有=本人/共享=owner），故随股票删除不构成跨租户删除。

### R6 情景成本与决策数值

- `owner_cost_detail` 只采纳**有效正持仓**（quantity 有限且 > 0）+ **有限正成本**，并按
  `Stock.market == market` 过滤（同 symbol 不同市场/币种不得加权）；缺数量不再当权重 1；
  已清仓(qty=0)不再产生锚点（method=none）。模拟盘同样按 market + qty>0。
- `web/api/decision.py` 的数量用 `finite_float` 变成 `100.0`，而 `decision._personalize` 只认
  `int` → 响应里 quantity 恒为 None。现在整值还原为 int，`_personalize` 也接受整值 float；
  NaN/Inf 成本/收盘一律 None。
- `decision_cache` 落库用 `json_safe + allow_nan=False`（NaN 不写库），读回也净化历史行；
  决策响应出口再 `json_safe`。`/api/positions` 的 `cost_price` 有限化并声明 `float | None`
  （真实 PG16 + `cost_price='NaN'::float8` 实测：修前 500，修后 200 且 `cost_price: null`）。

### R7 agent_runs NULL 归属 + 可猜 trace_id

`record_agent_run` 从不写 `user_id`（全部 NULL），而 progress 端点把 NULL 当"系统公共任务"
放行给所有登录用户；调度器的 trace_id 是 `sch-<agent>-<毫秒>`，`result` 恰是**最后一个用户桶**
的正文 → 任何 member 可枚举读别人的报告。

修法:
- `record_agent_run(..., user_id=...)` 写入归属；调度器只有"本轮唯一用户"才落正文，多用户桶
  只落 `buckets=N` 计数（不落任何人的正文）；`runtime` 的手动触发/单票触发都带上触发用户。
- `/api/agents/runs/{trace}/progress`: 有归属只认本人；NULL 归属只有 owner 可读，其他角色
  按 `not_found`。公共任务的其他客户端不受影响（`/tradingagents/running`、`/ta_status` 只读
  状态/进度，不返回 result）。
- 通知列表/详情按 trace 关联 `AgentRun` 时同样按归属过滤（NULL 仅 owner），避免"通知的主人
  读到别人的运行正文"。

## 2. 对权限/功能的影响（明确）

- **member**
  - 报告中心：可见 `scope=shared` 且内容不含持仓的市场报告 + 自己 `scope=private` 的报告；
    历史无 meta / 正文有持仓的共享报告仍不可见（fail-closed）。
  - 定时报告：现在**能看到**共享市场报告（此前只有 owner 的私有报告 → 永久空白）；但报告里
    没有个人持仓/成本（要个人数据走按用户生成）。
  - 通知：任务完成通知归触发者本人；渠道按 agent/stock_agent 选择集收窄；无选择集/无渠道时
    只落站内（不在别人渠道里出现）。
- **owner**
  - 报告中心仍全量可见（数据责任人，不读 meta）；可复核被内容绊线拦下的共享报告。
  - 系统运维告警（skill key 冻结、策略刷新）仍会兜底到 owner（显式 opt-in）。
  - 删共享自选时若仍有他人规则/持仓引用 → 409 拒绝（不再级联删别人数据）。
- **无会话/后台任务**：一律收敛到共享桶/共享市场口径，绝不扩大收件范围，也绝不回退 owner 渠道。

## 3. 残余风险（本次未处理）

1. **历史报告文件**（2026-10-29 之前）仍在磁盘上，内容含全库成本。读侧已拦截（无 meta → 仅
   owner；共享自称但内容有持仓 → member 也拦），但文件本身未删/未迁移，需 owner 授权后清理。
2. **历史 `agent_runs` 行**全部 `user_id IS NULL` 且 result 可能是他人正文：现在只对 owner 可见，
   未回填归属（无法可靠推断触发人）。需要时按 trace/时间人工清理。
3. **共享桶持仓（`positions.user_id IS NULL`）** 仍会在**按用户生成的私有报告**里出现
   （`portfolio_scope` 的既有"共享可见"语义），只是不会再进共享报告、也有 `scope=private` 标签。
4. **内容绊线是启发式**（只认列表行 `N股 成本 X`/`成本: X`）；改格式的历史文件可能漏判，仍靠
   "无 meta=仅 owner" 兜底。绊线只影响 member 可见性，不影响生成。
5. **`notify_task_done` 的 user_id 依赖调用方显式传**；未来新增调用点若漏传，会退化为"只落
   站内共享桶"（fail-closed，不发他人），但用户收不到通知 —— 已在 docstring 写明。
6. **订阅推送无匹配内容时不发送**：用户若没有执行桶（无自选/绑定），本轮不会收到推送；这是
   "宁可不发也不跨租户"的取舍，日志有 `无匹配内容跳过 N` 可观测。

## 4. 验证（原始输出摘要）

- 定向 pytest（SQLite）：`tests/test_round4_report_and_notify_scope.py`（20）+ 既有
  `test_reports_scope/test_reports_list/test_report_push/test_premarket_evidence*/test_scenario*/`
  `test_notify_channel_scope/test_notification_links/test_stock_scope_cross_tenant/`
  `test_dashboard_strict_json/test_multiuser_agent_scope/test_decision_cache` → **138 passed, 1 skipped**。
- 独立临时 **PostgreSQL 16**（新库 `pfsec_r4` + 独立角色，验证后 DROP）：同一组核心用例
  **46 passed**；NaN 脏成本实测 `/api/positions 200 (cost_price=null)`、`/api/dashboard/overview 200`。
- 探针复跑：共享桶采集 `[]`；订阅探针 `PUSHED: []`；删自选探针 `409 + member rule still exists: True`；
  伪造 shared 报告 HTTP 读 → `member /list: []`、`/content 404`；情景锚定 `closed→none`、
  `mixed→20.0 single`（已清仓 10.0 不再混入）。
- 门禁：`check_scoped_queries` OK、`check_is_pg_scope` OK、core→web 棘轮 1 passed、`ruff check src` 全绿。
- **未跑全量 `pytest tests/`**（按任务要求）；未执行任何生产库/生产文件写操作。
