# 数智参谋 · Chat 工具注册表契约

规格事实源。对应实现: `src/agents/chat/registry.py`（注册表本体）
与 `src/web/api/chat.py`（`CHAT_TOOLS = chat_tool_schemas()`，导给模型的那份）。

## 术语

| 名字 | 含义 | 数量 |
|---|---|---|
| **注册表** | `CHAT_TOOL_REGISTRY`，name → `ChatTool` | **78** |
| **对模型可见** | `chat_tool_schemas()` 导出，有 schema 的那些 | **77** |
| **handler-only** | 有 handler 但 `schema is None`，不暴露给模型 | **1** |
| **不进兜底** | `fallback=False`，只在挂了它的配方里可见（见 RECIPE-006） | **2** |

handler-only 的一个：`get_opportunities`。

`note_new_dimension` 自 2026-10-22 起**不再 handler-only**：schema 就位（否则采集链路没有入口、
提案表恒空），但 `fallback=False` 且只挂 `deep_diagnosis` —— 闲聊/问价的服务兜底路径里没有它。

## 验收标准

- **TOOL-001** 每个 chat 工具必须注册 `caliber`（口径标签）且非空 —— 没有口径的结论会被误读成事实
- **TOOL-002** `get_capital_flow` 的 caliber 必须包含「禁止据此判定主力意图」红线 —— 该工具是东财四档口径，方向位与逐笔相反，不能当主力意图用
- **TOOL-003** 注册表工具总数必须与本规格声明的一致 —— 新增工具漏改规格会在此失败
- **TOOL-004** 对模型可见的工具数 = 注册表数 − handler-only 数 —— 两者对不上说明 schema 漏配或多配

`fallback=False` 的两个：`note_new_dimension`（采集，只挂 `deep_diagnosis`）与
`get_premarket_brief`（2026-10-23，盘前底稿，只挂 `premarket_strategist`）—— 两者都不许进
「无配方兜底」，否则闲聊里模型会去写库 / 去抓当日盘前分析。

2026-10-23 新增两个取数工具（客户端 `get_exday_data` 口径 `tdx_exday4`）：
`get_fund_structure`（四档资金结构 + 散户化率/筹码迁移）与 `get_order_granularity`
（委托粒度 / 拆单隐匿度 / 撤单不对称）。两者都 `fallback=True`（属通用取数，闲聊问到也
应当能取），但只在挂了它们的配方里优先使用。**与东财四档、腾讯逐笔不可换算**，
同屏必须各带 caliber 标签。

2026-10-25 新增 `get_market_ranking`（市场级排行，追加在注册表**末尾**）：
个股 / 行业板块 / 概念板块 / 地区板块 × 涨跌幅 / 成交额 / 涨速 / 主力净流入。
数据源是腾讯排行（`proxy.finance.qq.com/cgi/cgi-bin/rank/...`），**免配额零 key**；
之所以不用通达信问小达，是因为它的配额已经用尽（`MCP error -32603`）。
口径：`腾讯排行 · 实时快照 · 非复权`；**板块的涨/跌家数不是涨停家数**，两者不可混用。
