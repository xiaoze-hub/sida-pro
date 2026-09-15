# Skill Gateway Phase 0 摸底 · 2026-09-15

## 1. 可开放 Skill 清单（chat registry 30 个）

| 工具名 | 用途 | 输入 | 输出 | 依赖 | 外放风险 |
|---|---|---|---|---|---|
| get_stock_quote | 实时行情 | symbol, market | 价/涨跌/量 | 腾讯/TQ | 低 |
| get_technical_analysis | 技术面 | symbol, market | 均线/指标 | klines | 低 |
| get_main_intent | 主力意图 | symbol | 方向/净额 | tencent/thsdk | 低 |
| get_decision_pioneer | 数智决策 | symbol, market | 三指标/共振 | TQ/GS | 低 |
| get_rally_analysis | 拉升段 | symbol | 趋势段 | 逐笔 | 低 |
| get_capital_flow | 资金流向 | symbol | 净流入 | 东财 | 低 |
| get_market_news | 市场新闻 | symbols | 列表 | 多源 | 中(内容) |
| get_kline_patterns | K线形态 | symbol | 形态标签 | klines | 低 |
| get_auction_data | 竞价 | symbol | 竞价量价 | TQ | 低 |
| get_sentiment_cycle | 情绪周期 | - | 阶段 | phase | 低 |
| get_market_anomalies | 异动 | symbols | 异动列表 | 源 | 低 |
| get_northbound | 北向 | - | 净流入 | 源 | 低 |
| get_hot_stocks | 热门 | - | 榜单 | 源 | 低 |
| get_main_flow_compare | 双源比对 | symbol | 一致性 | 两路 | 低 |
| get_delta_series | 秒级Delta | symbol | 序列 | thsdk L2 | 低(慢) |
| get_orderbook | 盘口演变 | symbol | 托压单 | thsdk 20档 | 低(慢) |
| get_event_catalyst | 事件催化 | symbol | 预期差 | 公告+LLM | 中(AI) |
| get_intent_explain | 主力解释 | symbol | 解释 | LLM | 中(AI) |
| get_factor_ic_report | 因子IC | market | 报告 | 因子 | 低 |
| get_opportunities | 机会 | - | 榜单 | 源 | 低 |
| get_strategy_signals | 策略信号 | symbol | 信号 | 策略 | 低 |
| get_fundamentals_detail | 基本面 | symbol | 财务 | 源 | 低 |
| get_forecast | 预测 | symbol | 中位数 | forecast | 中(需引擎) |
| **get_portfolio** | 持仓 | - | **本人持仓** | DB | **禁外放** |
| **get_watchlist** | 自选 | - | **本人自选** | DB | **禁外放** |
| **get_notifications** | 通知 | - | **本人通知** | DB | **禁外放** |
| **get_stock_suggestions** | 建议 | - | **本人建议** | DB | **禁外放** |
| **get_web_content** | 抓网页 | url | 正文 | HTTP | **禁外放(SSRF)** |
| **tdx_wenda** | 问财 | question | 答案 | thsdk | **禁外放(账号)** |
| **get_irm_qa** | IR问答 | question | 答案 | 外部 | **禁外放(账号)** |
| get_stock_suggestions | (见上) | | | | |

**开放集（Phase 1）**: 行情/技术/资金/新闻/形态/竞价/情绪/异动/北向/热门/双源/Delta/盘口/催化/解释/IC/机会/策略/基本面/决策/拉升 = **22 个**

**敏感 prompt**: `get_event_catalyst` / `get_intent_explain` 内部用 LLM prompt，**只返回结构化结果，不回传 prompt 原文**。

## 2. 鉴权现状

- **JWT**: `src/web/api/auth.py::get_current_user` — `Authorization: Bearer`，路由器 `dependencies=protected`
- **Owner**: `require_owner` 管理接口
- **服务 token**: `data_read` 依赖（行情只读可放宽）
- **Gateway 新增**: `X-API-Key` 独立鉴权链，不复用 JWT（外部开发者无系统账号）

## 3. 实现接入点

- 新文件 `src/web/api/skills_gateway.py`
- 新模型 `src/db/models.py` + migration：`SkillApiKey` / `SkillUsage`
- Redis 限流：复用 `src/db/redis_client.py`
- 工具执行：复用 `CHAT_TOOL_REGISTRY` handler（空 `user` 或系统账号）
