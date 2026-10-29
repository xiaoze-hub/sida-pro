# 六审：共享自选删除的跨账号副作用与历史泄露收口（2026-10-29）

- **基线**: `d19879e1`（五审 HEAD，分支 `fix/portfolio-security-round3-20261029`）
- **触发**: 独立 PG 实测发现五审的"安全拆离"引入了三个跨账号副作用，且 `GET /api/agents/health`
  仍把全库最近一次 `AgentRun` 的错误正文展示给 member。
- **未改**: `AGENTS.md` / `CHANGELOG.md` / `VERSION` / `docs/_frozen` / `SYSTEM_PROMPT`；
  未跑全量 `pytest tests/`；未 push / merge；未跨 worktree。

## 1. 三条阻断（真实链路可复现）

### S1 删共享自选会"代别人订阅"并触发其 LLM/外发

五审在 `DELETE /api/stocks/{id}` 里为每个在共享票上建过提醒的用户：

1. 自动创建一条同 symbol/market 的**私有 Stock**；
2. 把原票的 `StockAgent` 绑定**复制**到该私有 Stock；
3. 把其规则 `rule.stock_id` 改到克隆行。

`StockAgent` 带 `schedule` / `ai_model_id` / `notify_channel_ids`，写入后调度器会按计划真的执行
该用户的 Agent —— LLM 调用与按渠道外发都发生在**用户没有同意**的情况下。这是"为实现便利
代别人订阅"。

### S2 `PriceAlertHit.stock_id ON DELETE CASCADE` 删掉他人命中历史

拆离只迁了 `PriceAlertRule.stock_id`，**没有迁 `PriceAlertHit.stock_id`**。删原共享行时，
数据库按 `price_alert_hits.stock_id` 级联把 member 的历史命中一并删掉（SQLite 默认未开 FK 时
则留下指向已删行的孤儿，同样不可用）。

### S3 NULL 归属规则被三值逻辑滤掉后级联删除

`~own_rule_filter` 在 SQL 里对 `user_id IS NULL` 求值为 NULL（非真），于是旧 NULL 规则
既不算"他人的"（不触发 409），也不在"本人的"删除集合里；最后随 `stocks` 行被 FK 级联删除，
命中历史一起丢。自己私有 stock 上的 NULL 历史规则同理。

### S4 health 泄露他人 AgentRun.error

`/api/agents/health` 用 `agent_name` 取**全库**最近一次 `AgentRun`，把 `error` 原样返回给任何
登录用户；`recent_failed_count` 也按全库统计。member 因此能读到别人的报错正文（其中可能含
标的/持仓/账号信息）。

## 2. 修复口径（保守优先）

### 删除自选：不猜、不代劳、不丢历史

- **本人规则**（`PriceAlertRule.user_id == 我`）：允许随自选一起删（用户自己的显式操作），
  删除前先删其命中（`rule_id in (...)`），不依赖数据库级联。
- **任何非本人规则**（含 `user_id IS NULL`）：整单 **409 通用文案**，不做半截删除；
  共享票、规则、`StockAgent` 绑定、全部命中历史原样保留。
- **命中兜底**：`stock_id` 指向本行但 `rule_id` 缺失或属于他人的命中，同样 409 ——
  没有可证明"命中随规则属于本人"的证据就不删。
- 文案不报归属人、不报数量、不含 `detached_users`；只说明"仍被其它数据引用"。
- 并发双删：用 `DELETE ... rowcount` 判定是否真的删到，0 行 → 幂等 404，不冒 500。
- 任何异常 → `rollback()` + 409，绝不留半截删除。

### 用户可操作的安全路径（显式、带同意）

新增 `POST /api/price-alerts/{rule_id}/adopt`：

- 仅规则**本人**可调用（他人规则 404，不探测）；
- 目标 = 本人同 symbol/market 的私有自选（没有才创建，受自选配额/演示账号上限约束）；
- 只迁该规则及其命中 `stock_id`，**不复制 StockAgent 绑定** → 不订阅、不触发 LLM/通知；
- 迁空后共享行可由 owner 正常删除，member 数据完整保留。
- `/api/price-alerts` 响应新增 `stock_shared`，前端「价格提醒」页对共享票规则显示
  「迁到我的自选」按钮（桌面 + 移动），确认框写明"不复制 Agent 绑定、不触发分析/通知"。

### health 报错归属隔离

- `owner`：保持全局运维视角（可看全局最近一次运行）。
- `member`：`last_run` 只查 `AgentRun.user_id == 本人`；NULL 归属历史运行不猜、不可见。
- `summary.recent_failed_count` 同口径，不再把别人的失败数带给 member。

### 私有报告"漏仓"显式提示 + 共享报告文案

- 严格双层归属（`Account.user_id == uid AND Position.user_id == uid`）会漏掉历史"一侧 NULL"的行。
  新增 `portfolio_scope.count_unattributed_positions()`：**只统计** `(持仓是本人, 账户 NULL)` 或
  `(账户是本人, 持仓 NULL)` 的行；两侧都 NULL 的行不计数、不猜给任何人。
- 私有报告数据带 `unattributed_positions`，模板正文与 LLM 输入**都**显式提示
  "另有 N 条历史归属不完整的持仓未计入（无法确认属于你）"，且只出现在本人报告里。
- 共享市场报告文案不再承诺"按用户生成的报告"（该入口当前不存在），改为
  "个人持仓请在「持仓」页查看"；`premarket_evidence` 的 missing reason 同步。

## 3. 影响面

- **member**：删不掉仍被他人/历史数据引用的共享票（通用 409），但可用 adopt 自主迁出提醒；
  health 只看到自己的运行状态与错误。
- **owner**：删共享票不再自动替他人建自选/订阅；运维 health 视角不变。
- **通知/配额**：删除拒绝路径不写通知、不外发；adopt 建自选走与 `POST /api/stocks` 相同的
  `check_watchlist_quota` 与 demo/guest 上限，不构成配额绕过。
- **静默迁移**：删除路径已无任何"复制/迁移/新建"写入；迁移只发生在用户本人显式调用 adopt。

## 4. 验证（原始输出）

- SQLite 定向：`tests/test_round6_shared_stock_delete_scope.py` → **14 passed, 1 skipped**
  （并发用例按设计在非 PG 跳过）。
- 独立 PostgreSQL 16（临时库 `r6_verify_pg` + 独立角色，验证后不触碰其它库）：
  同一文件 → **15 passed**（含并发双删、故障回滚与迁移配额）。
- 回归：`test_round4_report_and_notify_scope` + `test_stock_scope_cross_tenant` +
  `test_multitenant_isolation` → **46 passed**；警报/通知/报告/证据/组合共 16 个文件 →
  **197 passed, 1 skipped**。
- 门禁：`check_scoped_queries` OK、`check_is_pg_scope` OK、`ruff check src tests/...` 全绿。

### 门禁修正说明

`check_scoped_queries` 原先只识别名字含 `user_scope` 的条件助手，HEAD 五审把历史读路径收口为
`analysis_history_scope_clause(...)` 后出现 **10 处误报**（查询实际已带归属过滤）。
现按 `*scope_clause` 识别（与 `scoped()` 同义：返回带归属过滤的 SQL 条件），并对
`retry_history` 的"按主键取后立刻对象级 `analysis_history_visible()` 判定"加
`# scoped-check: allow` 理由注释。红样例（裸 `db.query(Stock).all()`）仍被判红。

## 5. 残余风险（本次未处理）

1. 直接对数据库执行 `DELETE FROM stocks` 仍会按 FK 级联删 `price_alert_hits`；应用层已不再
   提供该路径，DBA 手工操作需自行备份。
2. 历史 NULL 规则/命中没有归属，无法迁给任何人，也不能安全删除 → 只能保留并提示联系管理员；
   真正清理需 owner 授权后按数据责任人流程处理。
3. 两侧都 NULL 的持仓无法归属，报告不计数；若用户确认属于自己，需要管理员校准
   `accounts.user_id` / `positions.user_id` 后才会进入统计。
4. adopt 在并发下可能为同一用户创建两条同标的私有自选（`stocks` 无 `(user_id,symbol,market)`
   唯一约束）；不影响数据归属，后续可加唯一索引治理。
