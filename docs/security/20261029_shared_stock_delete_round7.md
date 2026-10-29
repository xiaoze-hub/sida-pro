# 七审：作用域门禁被放宽 + 共享自选删除的并发级联（2026-10-29）

- **基线**: `344b9335`（六审 HEAD，分支 `fix/portfolio-security-round3-20261029`）
- **触发**: 独立实审两个阻断 —— B1 静态门禁被"名义过滤"绕过；B2 删除自选的
  预检/删除之间可被并发插入他人的规则/命中/持仓，随后靠 FK 级联静默删掉。
- **未改**: `AGENTS.md` / `CHANGELOG.md` / `VERSION` / `docs/_frozen` / `SYSTEM_PROMPT` / `.github`；
  未跑全量 `pytest tests/`；未 push / merge；未跨 worktree；未写生产库。

## 1. B1 静态门禁：任意 `*scope_clause` 都能当证据

六审为了让收口后的助手名（如 `analysis_history_scope_clause`）不被误判，在
`scripts/check_scoped_queries.py` 里加了 `name.endswith("scope_clause")`，并且
证据只按"函数体内出现过"判定。于是下列写法都能让**裸全表查询**通过：

| 反例 | 绕过方式 |
| --- | --- |
| 无关调用 | `analysis_history_scope_clause(uid)` 调完就丢 |
| 死分支 | 过滤写在 `if False:` / `False and (...)` 里 |
| 参数错误 | `analysis_history_scope_clause()` 缺参，运行时会 TypeError |
| 另一个 query | 只给同函数的**另一条**查询加了过滤 |
| 自造名字 | `evil_scope_clause(uid)` 这种全新名字也照样认 |

### 修复

- 名称恢复原守卫：`SCOPE_HELPERS{scoped,owned_or_404,writable}` + `user_scope` 子串；
  新增助手只进**精确白名单** `SCOPE_CLAUSE_HELPERS`（当前的唯一成员是
  `analysis_history_scope_clause`），不做前后缀泛匹配。
- 证据必须落在**同一条查询链**上：该链的 `filter/where/filter_by` 实参，或整条
  查询/取回行被交给归属助手，或取回对象的 `obj.user_id` 校验。
- 支持 `q = db.query(X)` 之后 `q = q.filter(...)` 的同变量追加过滤（真实代码里的
  `if user is not None:` 模式），但变量若被重绑到另一条查询则不算。
- 死代码不算证据：`if False:`/`while False:` body、`A if False else B` 的 A、
  `False and <expr>`、`True or <expr>`；零实参的助手调用不算证据。
- 单行豁免 `# scoped-check: allow — <具体理由>` 保留（可写在查询首行或紧邻上一行），
  仍要求写明可核对的事实；本次新增的豁免全部指名"id 集来自哪一条已按归属过滤的查询"
  或"该端点仅 owner/admin（require_owner(_admin)）"。
- 反例钉进 `tests/test_scoped_query_gate_round7.py`（14 例，含 `src/web/api` 全树为绿）。

## 2. B2 删除自选：预检通过后并发插入他人数据仍 200 删除

`DELETE /api/stocks/{id}` 的"预检"和"DELETE"是两条语句。窗口内另一个事务插入
`positions` / `price_alert_rules` / `price_alert_hits` / `stock_agents`（都带
`stocks.id` 外键）后，旧实现照样 200 成功，并靠 `ON DELETE CASCADE` 把这些行删掉。

### 修复

- 处理器第一步就对 stock 行 `SELECT ... FOR UPDATE`（PG；SQLite 方言忽略该子句，
  而 SQLite 是库级写锁，没有该窗口）。PG 里任何引用被删行的 INSERT/UPDATE 都必须
  先取该行的 `FOR KEY SHARE` 锁，与 `FOR UPDATE` 冲突 → 并发写入被挡到本事务结束；
  之后它拿到 FK 冲突（23503），**不会**"写成功再被静默级联删"。
- 删除语句自身再带一次归属条件（本人私有 / owner 删共享 NULL），挡"预检后行归属被改"。
- `uid` 缺失直接 403（fail-closed）：`user_id == None` 在 SQL 里是 `IS NULL`，
  会把 NULL 归属的历史规则当"自己的"，任何这类推断都会变成越权。
- 命中判定收紧：`hit.stock_id` 指向本行、但规则缺失 / 非本人 / NULL / **已迁到别的
  自选**（`rule.stock_id != stock_id`）→ 409，命中历史先于自选存活。
- 锁等待超时（55P03/40P01）映射 409（`src/web/api/_db_errors.py`），不冒 500；
  `create_position` 的外键冲突（23503）同样从 500 改为 409 通用文案。
- 并发 `adopt`：同一共享行取同样的 `FOR UPDATE`（与删除互斥，也把"同一共享行的
  并发迁移"串行化）；目标私有自选的 INSERT 走 SAVEPOINT，撞唯一约束时回滚该保存点
  并**重查**对手已建好的行（幂等复用，`created_stock=false`），不留半截事务、不 500。
- `adopt` 的 `uid` 缺失也 403，NULL 历史规则谁也不能认领（本人 404，owner 也 404）。

## 3. 验证

| 环境 | 内容 | 结果 |
| --- | --- | --- |
| SQLite（隔离库） | `test_scoped_query_gate_round7`（14 例，含全树为绿） | 14 passed |
| SQLite | `test_round7_delete_adopt_concurrency` 非竞态 5 例 | 5 passed / 4 skipped |
| SQLite | `test_round6_*` + `test_multitenant_isolation` + `test_stock_scope_cross_tenant` | 44 passed / 5 skipped |
| SQLite | 警报/通知/安全 6 文件 + 组合 4 文件 + 审计/通知 5 文件 | 72 + 50 + 48 passed |
| **真实 PG16**（临时库） | `test_round7_delete_adopt_concurrency` 9 例 | 9 passed |
| **真实 PG16** | `test_round6_shared_stock_delete_scope` 15 例 | 15 passed |
| 门禁 | `check_scoped_queries` / `check_is_pg_scope` / `check_migrations` / ruff | 全绿 |

PG 竞态用例的具体做法（`tests/test_round7_delete_adopt_concurrency.py`）：

1. 在删自选的工作线程里挂 `after_cursor_execute` 监听，**在拿到 stock 行锁之后**暂停；
   主线程用 `SET LOCAL lock_timeout='700ms'` 分别尝试插入他人规则、他人持仓、命中、
   Agent 绑定 —— 四条都必须是 `55P03`，证明 FK 插入路径与 `FOR UPDATE` 真的冲突；
   放行后删除 200，再插入只能拿到 `23503`（行已不存在），且库里没有任何残留行。
2. 删除与"他人建规则"并发压 8 轮：**任何交错都不许出现"删除 200 且他人规则消失"**，
   也不许 500；建成功则自选必须还在、规则一条不少。
3. 并发 adopt 同一共享行：两条都 200、只建一条私有自选、两条规则与两条命中都指向它。
4. 唯一索引下的冲突恢复：`_my_stock` 首次查不到（模拟并发提交）→ INSERT 撞索引 →
   SAVEPOINT 回滚 + 重查复用（`created_stock=false`），不 500、不重复建行。
5. NULL 历史规则：member/owner 都不能 adopt（404），规则仍为 NULL、命中未动。

## 4. 已知边界

- 行锁只保护"通过 FK 引用 stocks.id 的写入"；绕过外键的批量数据导入（如
  `session.execute(text(...))` 直接写、或 `SET CONSTRAINTS ALL DEFERRED`）不在防护范围。
- `stocks` 上仍只有 `(symbol, market) WHERE user_id IS NULL` 的唯一索引；私有自选
  的 `(user_id, symbol, market)` 唯一性目前靠 adopt 的行锁 + 冲突重查保证，未新增迁移
  （避免对存量重复行做破坏性约束）。
