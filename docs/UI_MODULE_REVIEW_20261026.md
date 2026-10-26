# SIDA 模块布局与交互复查 · 2026-10-26

> 基线：`0b9f83354790a02e13b2b73c767d065b82eed291`，VERSION `v0.80.0`；本文随第二轮模块整改提交生效。
> 对象完整路径：`/Users/winter/Documents/zicalWork/sida-pro/frontend/src/App.tsx`、`/Users/winter/Documents/zicalWork/sida-pro/frontend/src/pages/`、`/Users/winter/Documents/zicalWork/sida-pro/frontend/src/components/`、`/Users/winter/Documents/zicalWork/sida-pro/frontend/packages/base-ui/`。
> 覆盖：App 路由与菜单、枢纽子页签、设置及账户分类、共享浮层/开关；对下表的页面容器、主次操作和详情入口做结构性复查。浏览器覆盖及限制单独列在验收段。
> 未覆盖：真实生产账户、全部历史分析记录/运行详情、所有角色与多用户写操作、上游行情准确性、支付/密钥生成/破坏性操作的端到端业务验收；旧路由仅复查重定向代码。未创建虚假行情或运行记录作为验收数据。

## 整改结果

原先设置、个人中心及管理/文档页的左侧菜单实际只是长页锚点，正文仍同时展示不相关的所有内容。本轮改为共享分类工作区，菜单与正文一一对应，并通过 URL 保存选择。桌面是导航与内容，手机是分类选择器与内容；配置表单与长详情分别进入弹窗、侧边面板。

普通模块使用不透明且无描边的内容卡片。依据用户最终裁决，所有共享变体与手写内容容器统一 `border: none`，移除装饰 ring、悬停描边和内侧高光，并由 R22 门禁防止恢复。属性行用分隔与留白，不把每个字段再包一层完整卡片。保留已经合理的表格、图表、Tabs 与主从工作区，不为统一外观机械替换业务交互。

## 模块与菜单选择

| 入口 / 组件 | 主次层级与交互选择 | 本轮处理 |
| --- | --- | --- |
| `/` Dashboard | 概览/指数卡片、全景下三张独立卡片、低频指标折叠、看板定制弹窗 | 复核上一轮分组、收起卸载及空位重排 |
| `/stocks/:symbol` StockWorkbench | 主图与上下文辅助栏；研究、新闻、基本面等同级 Tabs；AI 内联工具入口 | 保留工作台主从布局与响应式堆叠 |
| 指数/板块工作台、旧 quote/index/boards 路由 | 同一工作台按类型展示，旧入口重定向 | 复查路由与容器复用 |
| `/heatmap` Heatmap | 页面标题/刷新工具栏、编码说明、图表卡片 | 统一 Header 与卡片，说明保持可见 |
| `/theme-mood` ThemeMood | 筛选区、题材列表与选中题材详情 | 统一页面宽度、Header/工具栏、周期/梯队/题材列表/矩阵卡片；保留原位主从分析 |
| `/opportunities` Opportunities | 摘要、候选结果；筛选/工具 Tabs；行详情展开、策略详情入口、低频统计折叠 | 复查现有分层及表格容器 |
| `/auction` Auction | 阶段状态 Chip、数据主区、证据说明 | 统一中性技术分析标题与状态组件，主区卡片 |
| `/dark-fund-top` DarkFundTop | 时点/刷新、排名表、展开详情 | 保留数据表与资金口径说明 |
| `/war-report` WarReport | 结论摘要三卡片、走势/证据分区 | 摘要改为有底色的独立卡片 |
| `/replay-tree` ReplayTree | 阶段树、总览、趋势与口径缺口 | 图表/数据区卡片化，避免直接铺在页面底 |
| `/caliber-compare` CaliberCompare | 查询工具栏、三个来源独立卡片、差异与裁决说明 | 统一页面宽度与 Header；查询移入工具栏、词典独立卡片；来源小屏单列；口径/基准日不折叠隐藏 |
| `/decision-ledger` DecisionLedger | 结论、日志；低频质量/回测/对账说明折叠 | 独立模块卡片，四组低频数据折叠 |
| `/factor-ic` FactorIC | 参数、统计与明细表 | 补齐主要内容卡片边界 |
| `/reports?tab=history` History | 分析概览/列表；选中分析的长正文 | 详情改右侧面板，不在列表中撑开长文 |
| `/reports?tab=reports` Reports | 底稿筛选/归档列表 | 底稿正文右侧面板，保留报告来源 |
| `/portfolio` Stocks | 自选/持仓同级视图；新增标的、持仓修改等短表单弹窗 | 复查既有 Header、Tabs、列表与编辑对话框 |
| `/shadow?tab=shadow` ShadowAccount | 账户概况、资料输入与画像结果 | 统一页面宽度与 PageHeader；输入/结果分区使用卡片 |
| `/shadow?tab=paper` PaperTrading | 曲线、策略、持仓分区；交易历史 | 独立卡片；长历史右侧面板 |
| `/notifications?tab=notifications` Notifications | 通知筛选与列表、详情/低频调试信息 | 保留列表及折叠的次要信息 |
| `/notifications?tab=alerts` PriceAlerts | 规则列表、规则编辑、命中历史 | 历史改右侧面板；短编辑仍弹窗 |
| `/system?tab=agents` Agents | 调度健康卡片、每任务卡片、主操作/运行状态 | 模型与通知、最近运行、绑定列表、深度配置右侧面板；短调度编辑仍弹窗 |
| `/system?tab=datasources` DataSources | 数据质量摘要、分类卡片、连接配置 | 默认只展开首分类；分类有展开语义；诊断结果右侧面板 |
| `/system?tab=jobs` JobPanel | 活跃任务与历史任务；进度、取消操作 | 保留任务列表，不把运行进度藏进弹窗 |
| `/system?tab=errors` ErrorLog | 错误筛选及记录列表 | 保留日志工具布局 |
| `/settings?tab=settings` Settings | 分类工作区；一次一个配置领域 | 新增 13 个权限过滤分类，系统无数据时给重载入口 |
| 设置：AI/我的服务 | 服务商与模型为主；场景分配次要 | 主卡片、服务/模型编辑弹窗、场景模型分配折叠 |
| 设置：通知/订阅/外观 | 渠道列表、四种报告订阅、外观偏好独立分类 | 修复订阅手写 Switch 圆点越界，统一共享组件与可访问名称 |
| 设置：凭证/同花顺/系统/用量/试用/配置包/用户 | 按领域单独呈现；原权限约束保留 | 不再与个人偏好混排；导入导出保持明确操作 |
| `/settings?tab=audit` Audit | 审计筛选与日志表 | 独立枢纽子页签，保留表格 |
| `/settings?tab=help` Help | 帮助主题卡片、使用规则 | 保留说明卡片，不与设置表单混排 |
| `/profile` Profile | 账户摘要；资料安全/分析数据/通知/接口套餐/隐私分类 | 账户资料和密码移入短表单弹窗；隐私单独分类；手机摘要分行 |
| `/api-keys` ApiKeys | Key、用量、安装说明三个分类 | 创建先开表单、提交才生成；一次性结果独立弹窗；去掉无对应正文的创建菜单 |
| `/developers` Developers | 快速开始/鉴权/限流/目录/调试/API/来源/错误八分类 | 从锚点长文改当前章节；统一代码块和主题，不隐藏复制入口 |
| `/admin` Admin | 用户/邀请码/Key/用量/Pro 五分类 | 同一分类工作区；邀请码不再重复挂在用户列表前 |
| `/ai-copilot` AiCopilot | 会话列表、主对话、输入工具栏；手机展开会话 | 保留上一轮布局；侧栏入口改显式“AI 分析助手 / 点击开始技术分析”按钮 |
| `/copilot-governance` CopilotGovernance | 轨迹、成本、配额、记忆、定时五同级视图 | 标准 Header 与有键盘语义的共享 Tabs，去手写警示色页签 |
| `/copilot/runs` RunsPage | 运行筛选列表，独立证据页 | 保留表格与分页，不内嵌长报告 |
| `/copilot/runs/:runId` RunDetailPage | 独立完整证据工作区 | 静态复查容器；无真实记录，未做完整数据浏览验收 |
| `/copilot/experiments` Experiments | 实验创建、运行结果 | 补齐主体卡片边界 |
| `/copilot/agents` AgentCatalog | 目录列表与单项详情展开 | 列表支持换行与展开状态，避免小屏工具数/版本挤压 |
| `/copilot/tools` ToolCatalog | 搜索、分类与工具表格 | 保留检索/表格分层 |
| `/copilot/proposals` ProposalsPage | 提案审核列表 | 保留独立审核区，采纳不等于立即生效的说明保持可见 |
| `/copilot/parallel` ParallelDiagnosis | 输入任务、运行状态、对比结果 | 保留表单/状态/结果卡片分区 |
| `/analysis/:symbol/:date` AnalysisDetail | 完整分析详情页 | 静态复查；未构造假历史记录 |
| `/login`、Landing、`/terms`、`/tiers` | 登录表单、介绍、条款、套餐信息 | 保留公开页面结构与共享主题；本轮不更改鉴权/套餐业务 |

## 验收与限制

前端构建、生产/测试类型检查、ESLint、8 项 UI 规则测试与 1854 项前端测试通过。浏览器扫描覆盖 41 个入口的深色桌面/390px 浅色组合共 82 组，无脚本异常或文档横向溢出；部分市场页面仍在真实接口加载/空态，不能称为数据业务验收。浅色桌面另查设置等关键页面；实际点击 13 个设置分类和 5 个个人分类均有对应正文，两个资料/密码弹窗 Escape 后恢复焦点，四个订阅圆点均在轨道内，卡片计算样式为 `border-style: none` 且无 inset 高光。

截图属于忽略的 `output/playwright/v2/`，不将真实账户资料或凭证提交到仓库。分类 walkthrough 切换到后台时一次导航未完成，后续后台/开发者/API 写入流程不计入本次交互通过数；它们已作结构复查与路由首屏检查。路由截图不等同于逐项写入操作或真实行情验收。

上游数据网关部分请求返回 502/503，预测引擎未启动；缺失保持明确说明。最初独立浏览器上下文的部分文档导航超时，后续在同一上下文进行客户端路由扫描，并单独检验实际分类点击、弹窗与面板交互。扫描使用真实本地接口，没有替换为模拟数据。

## 后续新增页面规则

依照 [UI_GUIDELINES.md](UI_GUIDELINES.md) 的容器选择表。引入新的分类/侧栏模式前先复用 `SectionWorkspace`、`SidePanel` 与基础组件；Switch 不得手工重画轨道/圆点。任何新页面必须写出主要任务、父子模块、次要配置入口、空态与手机布局，避免以“全部放在同一页”代替设计。
