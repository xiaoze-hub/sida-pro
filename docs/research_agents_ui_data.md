# LLM交易智能体 / 另类数据 / 终端UI 精品调研（补齐版）

> 日期: 2026-08-25 | 人工补齐（原3路子智能体504超时）

## 1. LLM交易智能体

| 项目 | 仓库/论文 | Stars | 亮点 | SIDA可抄 |
|------|-----------|-------|------|----------|
| **TradingAgents (TauricResearch)** | TauricResearch/TradingAgents | 15k+ | 多智能体论坛：基本面/技术/情绪/风控 四角色辩论→共识 | SIDA已缝合，抄其 `presets/` 角色辩论prompt与匿名投票机制 |
| **FinMem** | YuweiZhang/FinMem | 1k+ | 记忆分层：工作记忆+长期记忆+反思，模拟人类交易员记忆衰减 | 抄记忆分层给 SIDA 的 shadow_profile 升级，区分短期/长期持仓记忆 |
| **FinGPT** | AI4Finance-Foundation/FinGPT | 8k+ | 金融LLM微调+情感分析+研报解析 | 抄研报PDF解析管线，接 SIDA 的 events_collector |
| **FinRobot** | AI4Finance-Foundation/FinRobot | 2k+ | Agent组合：数据+策略+执行 三层 | 抄执行层与SIDA paper_trading_bridge 对接 |

**结论**: SIDA的 TradingAgents多智能体已领先，FinMem的记忆机制是唯一增量亮点。

## 2. 另类数据源

| 数据源 | 接入方式 | 价值 | SIDA现状 | 建议 |
|--------|----------|------|----------|------|
| **研报PDF解析** | FinGPT/ pdfplumber + LLM | 挖掘新催化 | events_collector仅公告 | P1 加研报PDF解析Agent |
| **情绪舆情** | 微博/雪球/股吧 + LLM情感 | 领先指标 | news_collector已有 | 优化情感打分接入 sentiment_cycle |
| **资金流L2逐笔** | thsdk L2 / 东财push2delay | 真实主力 | 已有dark_flow | P0 已修口诀，下一步L2替代腾讯逐笔 |
| **大宗商品轮动** | commodity_rotation.py已有 | 宏观前瞻 | 已有模块未接入策略 | P1 接入题材轮动策略 |

## 3. 交易终端UI/可视化

| 精品 | 亮点 | SIDA可抄 |
|------|------|----------|
| **TradingView Lightweight Charts** | 已用，性能极佳 | 已缝合，抄其 pane联动与十字线 |
| **vnpy VeighNa Station** | 专业交易终端布局：持仓/委托/成交 三栏 | 抄布局给 PaperTrading页 |
| **Qlib可视化** | 因子IC热力图、净值归因图 | 抄因子IC面板给因子评估页 |
| **KLineChart** | K线+成交量+指标 三图联动 | 抄联动交互给 AnalysisDetail |

**Top3 缝合优先级**
1. FinMem记忆分层 → SIDA shadow_profile
2. 研报PDF解析 → events_collector
3. Qlib因子IC可视化 → 因子评估面板
