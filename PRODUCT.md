# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

- **主要用户：作者本人与熟手交易者**（专家密度优先，2026-10-24 用户裁定）。盘中盯盘、盘后复盘、选股决策是高频场景，信息密度与扫描速度优先。
- **新手散户：可用但非一等受众**（「专家为主，小白可用」）。要求：入口必须有可点击暗示、术语有解释通道（tooltip/帮助）、关键数字可追溯口径；但**不**为小白系统性降低术语密度或砍掉专业面板。

## Product Purpose

个人股票交易分析终端（SIDA Pro / 数智分析 Pro）：读市场（通达信/同花顺双源）→ AI 参谋多智能体研判 → Kronos/Chronos/XGBoost 集成预测 → 到期自动验证 → 微信双向对话的完整闭环。成功标准：用户**更快做出有依据的看图决策**；AI 只做情报备料，方向判断交给人。

## Positioning

「精心工程的缝合怪」——把 TradingAgents、Kronos、Chronos-Bolt、XGBoost、TA-Lib、TimescaleDB 缝成一条可审计的闭环流水线，**整合本身即产品**。别人抄不走的机制是**口径纪律**：五类资金口径（tick 逐笔 / eastmoney4 / ths DDE / TQ L2 / tdx_exday4）不可换算，同屏必须各带标签，方向判定强制逐笔。

## Operating Context

- A 股交易时段驱动：盘前 08:30 报告 / 盘中监控与竞价 / 15:45–15:50 盘后落库（四档资金结构、涨停三池、因子胜率）。
- 自托管 Docker 部署；微信 iLink 双向推送；4 账号并存、user_id 全链路隔离。
- 一切 UI 评估的前提：**买红卖绿**（A 股语义）、交易日历、T+1 规则。
- 开发节奏：多智能体/多作者并行（git worktree 纪律），CHANGELOG 原子提交。

## Capabilities and Constraints

- **产品铁律（仍然有效）**：不做大盘涨跌方向预测；指标缺失必须显式标「无数据」，禁止编造；提统计胜率必须带三件套（样本数 + 同池无条件基准 + 是否剔除 T+1 一字涨停）；数字 tabular-nums；金额/量自动「亿/万」；买红卖绿。
- **2026-10-24 用户裁定：去卡片化不再是铁律**。hairline 分隔、卡片禁用、shadow-float 单例等旧约束降级为**默认偏好**——只要对 UI 优化有好处，可以用卡片/阴影/分组容器；K 线主体不被遮挡仍是布局常识。`scripts/check_ui_rules.mjs` 的 R1 等基线需随后续改动由仓库所有者重定基线。
- **Frozen Ring（禁自动改动）**：symbol_guard、number_audit、src/core/caliber.py、交易 403 守卫、TQ 优先链、SYSTEM_PROMPT 约束段。
- **工程门禁**：chat.py ≤2310 行；chat 工具数 71（改 5 处）；迁移版本 223；trace_specs 100%；禁全量 pytest。
- **术语双名不可混用**：「数智参谋」= AI 对话工作台（Copilot）；「数智决策」= 三指标共振选股（GS趋势 × AI机构活跃度 × L2主力净流入）。界面上提到任一名称必须指向正确功能。

## Brand Commitments

- 名称：数智分析 Pro（SIDA Pro）；UI 文案中文为主，i18n 双语（zh-CN / en-US）。
- 语义色板：stock-up 红 / stock-down 绿 / 阶段用中性色；warning=amber、danger=destructive，不与涨跌色混用。
- 设计资产：docs/design/20261023_0*.md 全站走查 8 份（2538 行，带 文件:行）是视觉现状权威；`docs/_frozen/caliber_matrix.md` 是口径标签权威。

## Evidence on Hand

- 用户实测截图（v0.50.1 首页、数智参谋页 v0.23.3 旧版）与真实反馈：「数智决策的入口很多人会以为是标签或标题」。
- 生产实测数据与口径案例（v0.48 release notes：东财 82 vs TQ 86 差北交所 4 家）。
- 交接备注 docs/design/20261024_数智参谋优化交接备注.md（冲突清单/门禁/优先级）。

## Product Principles

1. **结论先行**：每屏先给结论再给证据；数字堆可折叠，结论常显。
2. **口径诚实**：宁可标「无数据 / 口径缺口」，绝不编数字、绝不跨源混算。
3. **专家密度、新手可入门**：术语保留但可解释；每个可交互元素必须有可点击暗示（箭头/底色/形态）。
4. **AI 备料，人决策**：输出情报与证据链，不给买卖指令；可证伪优先于断言。
5. **UI 收益优先（2026-10-24 起）**：卡片化/阴影/分组等手段按收益取舍，不再被旧约束一刀切禁止。

## Accessibility & Inclusion

- 本 pass 设定的底线（仓库此前无正式标准）：图标按钮带 aria-label；色彩不单通道承载语义（红绿同时配文字或位置区分，兼容色盲）；键盘可达核心路径；字号遵守现有档位制（10/11/12/13/16/20）。
