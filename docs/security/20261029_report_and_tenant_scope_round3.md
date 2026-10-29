# 报告中心 / 持仓 / 通知 —— 二轮安全残留收口（三审）

- **基线标识**: `0216521b`（`feat/copilot-p2-integration-20261029` HEAD；本分支 `fix/portfolio-security-round3-20261029` 即基于它）
- **审计/修改对象**: `src/core/report_scope.py`(新)、`src/core/report_generator.py`、`src/core/report_scheduler.py`、`src/core/premarket_evidence.py`、`src/core/portfolio_scope.py`、`src/core/numutil.py`、`src/core/decision.py`、`src/core/scenario_compose.py`、`src/core/notify_center.py`、`src/web/api/reports.py`、`src/web/api/dashboard.py`、`src/web/api/decision.py`、`src/web/api/accounts.py`、`src/web/api/stocks.py`、`src/bootstrap/runtime.py`、`src/agents/intraday_monitor.py`
- **覆盖范围**: 审阅并修改了上述文件的报告生成/读取、持仓与自选作用域、通知渠道解析、看板与决策的数值序列化、盘中监控的归属链路。**未覆盖**：Hermes cron 外部任务自身写报告的逻辑（本仓只读它的产物）、前端展示层、`docs/_frozen` 与 `AGENTS.md`/`CHANGELOG.md`/`VERSION`（本次按任务要求禁改）、以及历史报告文件的清理（属运维动作，见文末）。

## 1. 修的是什么（按发现顺序，每条都有实测复现）

### R1 报告中心跨租户泄漏（最严重，已在生产存在）

- `report_generator._collect_positions()` 只 `filter(Position.quantity > 0)`，**没有任何用户条件**，盘前/盘后报告把全库所有人的成本价、持仓量写进同一份 `.md`；
- `premarket_evidence.collect(user_id=None)` 的三处 `if user_id:` 守卫在 `None` 时**不加任何过滤**，证据块同样读全库；
- `/api/reports/list`、`/api/reports/content` 只要登录即可读（路由级只有 `protected`），且 `job_id` 未做路径分量校验（`job_id=../..` 可逃出报告根目录）。

### R2 看板/决策的脏成本与账户作用域

- 看板持仓统计只按 `Position.user_id` 过滤（漏父账户），可用资金 `SUM(accounts)` **全表**求和；
- 历史脏成本 `NaN/Inf` 能通过 `float(x or 0)`，最终被 Python `json` 写成 `NaN`/`Infinity` **字面量**（非法 JSON，前端 `JSON.parse` 直接抛，看板白屏）；决策响应把 `position.cost_price` 原样带出。

### R3 跨租户建仓与删除自选

- `POST /api/positions` 按主键取 `Stock`，A 传 B 的私有自选 `stock_id` 可建仓（持仓挂到他人 Stock 行，对方删自选会级联删掉 A 的持仓）；
- `DELETE /api/stocks/{id}` 的 `has_position` 查**全库**持仓，他人引用会让我永远删不掉，且报错等于泄露"有人在持有这只票"。

### R4 手动触发通知归属

- 异步线程的 `_notify` 不传 `user_id` → `notify_task_done` 兜底推 owner；线程里还直接用了请求级 ORM `user.id`。

### R5 渠道解析无视配置选择集合

- `resolve_notify_channels(user_id=…)` 返回该用户**全部启用渠道**，完全忽略 `stock_agent.notify_channel_ids` / `agent.notify_channel_ids`（静默超发）；交集为空与"本来没渠道"无法区分（静默跳过）；共享桶缺渠道同样无声。

### R6 情景成本锚定

- `owner_cost` 用 `db.query(...).first()` 在多账户时**随便挑一行**当成本；`if cost:` 放过 `NaN`。

### R7 盘中监控缺归属链路（先证明后修）

- `StockData` 无 id/user 字段，`collect()` 返回 dict 无 `user_id` → `_uid` 恒 `None` → 用户自设防守线**永不触发**，风险告警无归属。修法是**显式下传** `context.user.id`（标量），不按 symbol 反查归属。

## 2. 修法与口径

| 项 | 修法 |
|---|---|
| R1 生成 | `_collect_positions(user_id=…)` → 账户可见 AND 持仓可见；`None` = 历史共享桶（两侧 `IS NULL`）；`premarket_evidence` 三处改用 `portfolio_scope` 条件 |
| R1 读 | 新增 `src/core/report_scope.py`：每份报告旁 `<name>.md.meta.json` 记 `scope`/`owner_user_id`/`contains_private_holdings`；无 meta = 来源不明 = **仅 owner**；member 不可读统一 404，`/list` 不出现；缓存键含查看者；`job_id`/`file` 走白名单字符校验 |
| R1 定时任务 | `report_scheduler` 显式解析 owner 并作为归属（系统报告 = owner 的报告）；解析失败 → 共享桶（只含历史共享项），绝不回退全库 |
| R2 | 看板持仓/自选/资金全部走 `portfolio_scope`；新增 `finite_float()`，所有出参数值有限化；脏成本计 `unpriced_positions`（不计 0，不冒充）；决策上下文 `cost_price` 有限化并带 `cost_unknown` |
| R3 | 建仓前校验 `stock_user_scope_clause(user.id)`（他人私有 → "股票不存在"）；删除自选分两档：自己可见持仓 → 400，仅他人引用 → 409 通用文案并拒绝删除 |
| R4 | 起线程前取标量 `_uid`，`_notify` 与 `trigger_agent_for_stock` 都用它；`notify_task_done` 兜底 owner 时打 warning 留痕 |
| R5 | 新增 `resolve_notify_channels_ex()` 返回 `ChannelResolution`：配置集合 ∩ 归属可见集合（只缩不扩）；配置与归属**完全不相交**（选的是他人/已禁用渠道）→ 该配置对本次运行不适用，改用**该归属自己的**渠道并 warning（`config-not-applicable`）；归属无渠道 → 不发 + warning（`shared-bucket-empty`/`no-channels`/`config-not-permitted`）；任何情况不回退 owner |
| R6 | `owner_cost_detail()` 返回 `CostAnchor`：只采纳有限正成本；单账户 → `single`；多账户成本不同 → `weighted`（按持仓量）+ 逐账户明细 + 正文口径说明；全脏 → `unknown` 且不锚定 |
| R7 | `collect()` 写 `data["user_id"]`；告警段只认它，不读 `position` 块、不按 symbol 反查 |

## 3. 对 owner / member 的可访问性影响（明确写清）

- **member（普通登录用户）**
  - 报告中心：只能看到 `scope=shared` 且不含私有持仓的报告，以及 `scope=private` 且 `owner_user_id` 是自己的报告；
  - **历史存量报告一律看不到**（无 meta 或 `contains_private_holdings=true`）。这是有意的 fail-closed：那些文件里确实写着别人（也可能包括自己）的成本，无法逐条判定归属；
  - 持仓/自选/看板/决策/盘中告警：本来就该只看自己的，行为不变或更严（他人私有标的不能再被引用）。
  - 通知：agent 上勾的渠道若是 owner 私有的，这条配置对 member 不适用 → 用 member 自己的渠道（并 warning）；member 没有任何渠道 → 不发（不再借 owner 的）。
- **owner（数据责任人）**
  - 报告中心**可见全部**（含历史存量与 member 的私有报告）。这是明确的产品裁决：owner 是这台机器的运维与数据责任人，必须能自查"到底泄了什么"；且该路径不读 meta，保住了 `/list` "只读命中 limit 项标题" 的性能护栏（旧问题曾导致 30–45s 渲染）；
  - 代价要写明：owner 仍能看到残留的他人数据，代码层不再加遮挡。
  - 定时报告现在是 **owner 的报告**（内容 = owner 可见的持仓 + 历史共享项），member 不再收到含他人成本的那份。
- **无会话 / 后台任务**：一律收敛到共享桶（`user_id IS NULL`），**不**等于"全库"，也**不**改发 owner 渠道。

## 4. 生产现存风险与补救建议（需主代理决策，本次未执行任何清理）

1. **已落盘的历史报告仍是明文泄漏**：`CRON_OUTPUT_DIR/<job_id>/*.md` 里 2026-10-29 之前的盘前/盘后报告含全库持仓成本。本次只做了**读侧拦截**（member 已读不到），文件本身还在盘上，且 owner/有文件系统权限的人仍可读。
   - 建议（需授权）：由 owner 确认保留期后，用 `scripts/cleanup_cron_outputs.py` 或一次性脚本**删除或迁移**受影响 job 的历史文件；迁移前先备份到仅 owner 可读的目录。
   - 也可选择"重新生成覆盖"：新报告已按 owner 归属重写，历史文件不会再被更新，所以重生成不解决存量文件。
2. **报告正文里的 `**归属:**` 元信息行**只说明口径，不构成访问控制；访问控制只在 API 层。
3. **历史跨租户错位数据**（某人持仓挂在他人 Stock 行上）不会被本次修复自动清理：现在只是"删不掉 + 不可见"。若要根治，需要一次性数据修复（按 `symbol` 为持仓所属用户重建 Stock 行），**属生产写操作，未做**。
4. **通道解析的既有语义**：`user_id` 完全不传（`_UNSET`）的旧路径仍走 agent/default 全局模板；当前仓内调用点都已显式传参，但未来新增调用方必须传，否则回到旧语义（已在 docstring 写明）。

## 5. 验证

- 新增/扩展测试：`tests/test_reports_scope.py`、`tests/test_stock_scope_cross_tenant.py`、`tests/test_dashboard_strict_json.py`、`tests/test_premarket_evidence_scope.py`、`tests/test_notify_channel_scope.py`、`tests/test_intraday_monitor_user_chain.py`、`tests/test_scenario_compose.py`(新增 5 条)、`tests/test_reports_list.py`(适配 viewer 入参)。
- 定向组合：SQLite 与**独立临时 PostgreSQL 16**（`sec3_verify_pg`，验证后已 DROP）各跑一遍；PG 上额外覆盖 `cost_price = 'NaN'::float8` 的真实脏数据路径。
- 门禁：`scripts/check_scoped_queries.py`、`scripts/check_is_pg_scope.py`、`tests/test_w41_core_web_dependency.py`、`chat.py <= 2310` 行护栏、`ruff check`（E9/F821/F601/F811）。
- 未跑：全量 `pytest tests/`（按任务要求禁止）。
