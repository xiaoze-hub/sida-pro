# SIDA 项目大纲与知识库

> 基线标识：源码初始基线 `d2255f0fea9a2c1857f7b4535eb9b66a170d454e`；首次建库修复后基线 `d774ae01`；`VERSION=v0.80.0`。盘点日期：2026-10-26。
> 盘点对象：`/Users/winter/Documents/zicalWork/sida-pro`。
> 覆盖范围：全仓跟踪文件清单；后端与 marketdata 的模块级静态索引；启动、数据库、缓存、Agent、chat/harness、前端路由和 Docker 配置的重点阅读；本地三服务启动验证。
> 未覆盖：全部算法和历史研究逐篇审计、完整后端/前端测试执行、全部页面交互验收、真实交易信号准确度、生产环境与远端部署、外部预测引擎和 L2 专有依赖验收。未收录密钥、账户资料或数据库业务内容。

SIDA（数智分析）是自托管的股票研究与盯盘平台。主服务将多源行情、技术分析、逐笔主力意图、AI 对话、定时报告、模拟交易和权限管理整合在一起；预测引擎是独立的可选服务。SIDA 是源码、镜像与历史部署中仍在使用的名称。

## 阅读入口

| 文档 | 用途 |
| --- | --- |
| [架构与代码入口](architecture.md) | 找到后端启动链、数据链、Agent 和前端页面的实现位置 |
| [业务契约与开发规则](contracts.md) | 数据口径、单位、多用户隔离、数据库、缓存与改动门禁 |
| [本地 Docker 运行手册](local-docker.md) | 启停、登录、日志、源码挂载、数据卷与当前能力边界 |
| [完整代码索引](code-index.md) | 搜索 610 个 Python 文件的职责与公开入口、9 个注册 Agent 和前端路由声明 |

此知识库是仓库文档，不是投资结论库。后续修改以源代码、`AGENTS.md` 和具备相同基线的契约文档为准。

## 项目大纲

1. **产品界面**：市场总览、持仓/自选、个股工作台、机会发现、竞价、暗盘资金、题材情绪、AI 参谋、报告、决策账本、模拟盘和管理设置。
2. **服务入口**：`server.py` → `src/bootstrap/` → `src/web/app.py` → `src/web/api/`。
3. **数据采集**：`packages/marketdata/` 的供应商抽象 + `src/collectors/` 的业务采集、回填和归档。
4. **业务计算**：`src/core/`，包括行情聚合、市场状态、资金/筹码/技术分析、因子、风控、调度、AI 和通知。
5. **Agent 与对话**：`src/agents/` 的注册 Agent；`src/agents/chat/` 的工具注册；`src/harness/` 的配方、白名单、预算与轨迹。
6. **持久化**：PostgreSQL/TimescaleDB 存放生产数据；Redis 提供共享缓存、限流和调度选主；SQLite 限开发和测试。
7. **预测服务**：`forecast_server.py` + `forecast_lib/`，经 HTTP 与主服务通信，模型与运行环境独立。
8. **工程治理**：`tests/`、`frontend/tests/`、`scripts/`、`specs/`、`.github/workflows/`、版本与变更日志。

## 扫描规模

以下计数来自 `d774ae01` 的 Git 树。只统计跟踪文件，不把 node_modules、运行数据和本地旧知识库计入源码规模。

| 区域 | 范围 |
| --- | --- |
| 后端主体 | `src/`：523 个 Python 文件 |
| 独立行情包 | `packages/marketdata/`：71 个 Python 文件 |
| 预测计算库 | `forecast_lib/`：14 个 Python 文件 |
| Python 模块索引 | 上述三项 + 两个服务入口，共 610 个文件，均完成 AST 解析 |
| 前端产品源码 | `frontend/src/`：197 个 TSX、83 个 TS、1 个 CSS 文件 |
| 后端测试目录 | `tests/`：551 个 Python 文件，包含 conftest、工厂及辅助文件，不等于用例数 |
| 注册 Agent | 9 个 `@register_agent` 类；展示、调度与可见性另由 catalog 配置决定 |

历史 `docs/PROJECT_KB.md` 是 2026-09-24、v0.13.9 的本地忽略文件，保留作历史资料；其中旧规模、版本和部署描述不能直接当成当前状态。README 顶部版本与架构概览也不能替代实际入口核对：例如 `server.py` 已是兼容薄壳，注册应使用装饰器。

## 按问题找代码

| 要处理的问题 | 优先入口 | 相关验证 |
| --- | --- | --- |
| 服务起不来 | `src/bootstrap/cli.py`、`startup.py`、`src/core/startup_check.py` | `tests/test_w32_bootstrap.py`、`test_pg_default.py` |
| 新 PG 数据库初始化失败 | `src/db/models.py`、`src/web/migrations.py` | `tests/test_schema_boolean_defaults.py`、容器日志 |
| 数据源和行情切换 | `src/core/marketdata_client.py`、`packages/marketdata/src/marketdata/` | `tests/test_marketdata_client.py`、`test_quote_routing.py` |
| K 线缺失、补数或单位 | `src/collectors/kline_collector.py`、`klines_ingestor.py`、`src/db/klines_repo.py` | `tests/test_klines_ingest_validation.py`、`test_klines_amount_and_halts.py` |
| 主力意图/资金方向 | `src/core/caliber.py`、`src/agents/chat/registry.py`、`tools_market.py` | `tests/test_capital_flow_routing.py`、`test_caliber_contract_tags.py` |
| AI 工具和权限 | `src/web/api/chat.py`、`src/agents/chat/registry.py` | `tests/test_w33_chat_registry.py`、`test_chat_tools_copilot.py` |
| Agent 扩展 | `src/bootstrap/agents.py`、`src/core/agent_catalog.py`、`src/agents/` | `tests/test_w32_bootstrap.py`、`test_catalog_tools.py` |
| 配方、预算、证据轨迹 | `src/harness/`、`src/core/copilot_runtime.py` | `tests/test_agent_kernel.py`、`test_copilot_runtime.py` |
| 用户、权限与个人数据 | `src/web/api/auth.py`、`_scope.py`、`src/core/permissions.py` | `tests/test_multi_user_auth.py`、`test_multitenant_isolation.py` |
| 页面与兼容链接 | `frontend/src/App.tsx`、`frontend/src/pages/` | `frontend/tests/`、`pnpm build` |

这些测试名是检索入口，不表示本次全部执行。模块和公开符号可在完整代码索引中继续查找。

## 更新知识库

结构化索引只读取已提交的 Git 基线，不 import 模块，不连接业务数据库：

```bash
python3 scripts/build_project_knowledge.py --baseline HEAD
python3 scripts/build_project_knowledge.py --baseline HEAD --check
```

当前快照的复核命令使用 `--baseline d774ae01`。后续更新源码后先选定新的 commit，再生成索引并同步本页、架构、契约和运行手册的基线与覆盖范围。索引列出的模块说明来自源码 docstring，历史措辞仍需人工核对。

规划、研究或审计文档必须保留基线、完整对象路径和覆盖/未覆盖范围三个要素。知识库不得收录 `.env` 值、JWT、API Key、数据库导出或真实账户资料。

## UI 设计入口

当前界面与交互规则见 [UI_GUIDELINES.md](../UI_GUIDELINES.md)，覆盖主题、Header/Toolbar、Chip、数据网格、浮层、移动端与 CI 门禁。历史视觉研究不替代当前规范。

模块与菜单的布局、分类、折叠和浮层选择见 [UI 模块复查记录](../UI_MODULE_REVIEW_20261026.md)。

AI 助手由 `frontend/src/components/AssistantWorkspace.tsx` 持有单一会话实例，完整页与迷你窗复用 `frontend/src/pages/AiCopilot.tsx`；形象和本机偏好入口为 `AssistantAvatar.tsx`、`AssistantAvatarPicker.tsx` 与 `lib/assistant-avatar.ts`。动画与显示规范见 [UI 准则第 8 节](../UI_GUIDELINES.md#8-ai-助手形象与会话)。

系统状态：`frontend/src/components/system/` 共用状态图标、摘要小面板、系统状态页签和 `/system/status` 完整页面；自检入口直接进入完整页。`src/web/api/system_status.py` 提供脱敏快照与 SSE 增量更新，`useSystemStatus.ts` 按账号缓存并显示真实进度；主动探测复用 `src/core/selfcheck.py`，管理员权限与通知账号隔离由后端执行。

行情入口：`frontend/src/pages/QuoteEntry.tsx` 与 `frontend/src/lib/quote-universe.ts` 决定当前账号默认标的；`QuoteSymbolPicker.tsx` 提供持仓/关注搜索切换。`StockWorkbench.tsx` 保留市场，日 K/分时显式区分 stock/index，相关缓存身份见 `src/web/api/klines.py`、`src/web/api/quotes.py`。
