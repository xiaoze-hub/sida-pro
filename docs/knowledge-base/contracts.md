# SIDA 业务契约与开发规则

> 基线标识：`d774ae01`，`VERSION=v0.80.0`；盘点日期：2026-10-26。
> 盘点对象：`/Users/winter/Documents/zicalWork/sida-pro`。
> 覆盖范围：AGENTS.md、caliber.py、数据库与缓存入口、Agent/catalog、测试与 CI 配置中的开发约束。
> 未覆盖：完整因子/技术指标算法审计、所有接口隔离实测、供应商字段逐项核验、生产数据和金融结论。

## 数据不能脱离口径

金额使用元，成交量使用股；`vol × price == amt` 必须精确匹配。数据缺失写“无数据”，日期滞后在 UI 明示。逐笔方向位、竞价 M 标记等字段按专门规范解释，不能按直觉猜测。

| 用户要了解什么 | 正确能力 | 标签与限制 |
| --- | --- | --- |
| 主力意图、吸筹或派发 | `get_main_intent` | 腾讯逐笔 `tick`，可用于方向判断 |
| 资金流向、主力净流入、超大单/大单 | `get_capital_flow` | `eastmoney4`，按单金额四档归类，不能当逐笔方向 |
| TQ 口径 L2 主力净流入 | `get_decision_pioneer` | 遵守该接口口径，不能替代 tick 意图判断 |
| 同花顺 DDE | 相应 thsdk 能力 | `ths`，与逐笔和东财四档不同 |
| 没带来源标签的数据 | 视为 `unknown` | 不得用于方向性判定 |

契约在 [src/core/caliber.py](../../src/core/caliber.py)。资金数据向下游携带 `caliber` 与 `direction_semantics`，UI 显示口径标签。方向性出口调用 `require_directional()`，非 tick 抛 `CaliberViolationError`。两种方向冲突时说明“主动买卖”与“按单金额归类”的差异，优先采用逐笔。

口径矩阵位于 [docs/_frozen/caliber_matrix.md](../_frozen/caliber_matrix.md)。本知识库只做规则索引；不复述未经本次逐字段校准的供应商编码。

## 数据库、缓存与用户隔离

- 生产只使用 PostgreSQL；SQLite 仅本地开发或单测。容器缺少显式数据库配置应启动失败，禁止静默回退 SQLite。
- 方言判断与引擎构造只经 `src/db/dialect.py`；业务代码使用 `is_postgres()`、`declared_backend()`、`upsert_sql()`、`insert_ignore_sql()`，不得引入 `IS_PG`。
- schema 变更通过 `src/web/migrations.py` 的版本化迁移。新增模型必须同时核对新库创建和旧库升级，避免只在已有数据库正常。
- 本次修复的 `users.is_deleted`、`chat_conversations.pinned` 改为 SQLAlchemy `false()`，PG 编译为 `false`、SQLite 编译为 `0`；既有迁移已经用 `DEFAULT FALSE`。
- K 线读取使用 PG 优先的 `get_klines()`，补数使用 klines_ingestor；明确复权、日期和标的市场维度。
- 业务缓存使用 `src/web/cache/biz_cache.py`。用户数据的 cache key 也要带用户维度，可检索 `user_scoped_key()`。
- 所有个人接口、会话、Agent 配置和结果须核对 `user_id` 隔离；不能只验证 owner 账号。

## 新增功能的入口

| 改动 | 做法 |
| --- | --- |
| 新 Agent | `src/agents/*.py` 实现与 `@register_agent`；`AGENT_SEED_SPECS` 播种；对应 prompt 放 `prompts/`；不改 server.py 人工注册 |
| 新采集器 | `src/collectors/`；无状态、类型明确；复用来源适配与单位规则 |
| 新 API | 检查 `src/web/app.py` 实际 router prefix、认证依赖、权限与用户范围；不要只看装饰器局部路径 |
| 新 chat 工具 | 复核 registry/schema、handler、权限、预算、口径和 UI 文本；按相关规格补守卫测试 |
| 新前端界面 | `frontend/src/`；优先复用 workspace 的 api/base-ui/biz-ui；确认主路由和权限 |
| 新配方 | `src/harness/recipes/` 与配方加载/白名单；确认调用预算、轨迹和无数据行为 |

Python 新代码带类型标注，4 空格缩进；TypeScript 组件采用 PascalCase；密钥只进本地 `.env`、环境变量或设置界面。

## 检查与提交

常见检查入口如下，选择与改动相称的范围；本知识库不将这些命令全部标为已通过。

```bash
# 后端（已安装项目依赖的环境）
python -m pytest tests/test_schema_boolean_defaults.py tests/test_admin_user_console.py -q
python scripts/check_is_pg_scope.py
python scripts/trace_specs.py

# 前端
cd frontend
pnpm install --frozen-lockfile
pnpm build
pnpm test
```

后端联网测试使用 `network` marker，与普通单测分开；默认不发送通知。测试数据必须隔离，不能让测试迁移落入真实 DATA_DIR。`tests/conftest.py` 已提供隔离机制。

新工作从 main 开分支。提交使用 `<type>: <中文主题>`，每个提交必须同时带 `CHANGELOG.md`，标题为 `### <type>-<中文标题>`。任务完成后记录本地提交；push、合并和发版另按用户指令执行。

`VERSION` 是版本真源。发版变更必须同步 README.md 和 README.zh-CN.md 徽章；本次知识库和本地启动不提升产品版本。

## 历史资料的使用

[KNOWN_ISSUES.md](../KNOWN_ISSUES.md)、[roadmap.md](../roadmap.md)、设计目录与研究目录可作为查找线索。引用结论前核对其基线、对象路径和覆盖范围；旧计划不等于当前已落地状态。源码 docstring 也可能保留旧架构术语，需结合调用链确认。

仓库要求优先使用 `tdai_search` / `tdai_remember` 访问动态经验。本次工具环境未提供这两个工具，因此知识内容保存在本目录，未声称写入远程记忆服务，也未修改 Codex 全局记忆。
