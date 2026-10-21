# 数智参谋工具归属表（62 个 chat 工具 × 维度 × 画像 × 权限）

基线：`v0.37.5` @ `main`。本表由注册表 + `src/harness/recipes/*.yaml` **实测生成**，不是手工罗列。

> 用途：画像判定器（`src/core/stock_persona.py`）决定「用哪把尺子」，本表决定「每把尺子该拿哪些工具」。
> 规则：★ = 该维度的主证据；进配方 = 该工具已被某个配方白名单引用（空 = **孤儿工具**，当前无人调用）。

## 总览

| 指标 | 数值 |
|---|---|
| 工具总数 | **62** |
| 被配方引用 | **27** |
| 孤儿工具（从未进任何配方） | **35** |

## D0 画像/身位（5 个）

| 工具 | 定位 | 适用画像 | 权限 | 进配方 |
|---|---|---|---|---|
| `get_demon_factor` | 妖股六维因子分+分级+身位（画像判定与妖股结论的唯一依据） | 妖★ 龙 | L0 | **孤儿** |
| `get_early_signals` | 客户端条件选股全市场扫描：放量上攻/突破盘整等启动早期信号家数 | 左 常 | L0 | **孤儿** |
| `get_hot_stocks` | 同花顺热榜（小时/日），热度与概念标签 | 妖 龙 | L0 | premarket_strategist、postmarket_reviewer、auction_hunter |
| `get_rally_analysis` | 盘中拉升段逐单拆解：放量上涨(真拉升) vs 拉高出货(假拉升) | 妖 龙 | L0 | **孤儿** |
| `get_sentiment_cycle` | 市场情绪周期（退潮/冰点/发酵/高潮）——三层滤网第一层 | 全 | L0 | premarket_strategist、postmarket_reviewer |

## D1 前期走势（5 个）

| 工具 | 定位 | 适用画像 | 权限 | 进配方 |
|---|---|---|---|---|
| `get_trend_profile` | 5/20/60/120 日涨跌、阶段高低、均线与斜率、量能节奏、RS vs HS300 | 全★ | L0 | deep_diagnosis |
| `get_wyckoff_structure` | 威科夫量价结构（放量滞涨/弹簧试探/缩量沉淀/放量突破）——三层滤网第三层 | 左★ | L0 | **孤儿** |
| `get_delta_series` | 涨跌分布的时间序列 | 全 | L0 | **孤儿** |
| `get_kline_patterns` | K 线形态严格规则识别（金针探底/红三兵等） | 左 趋 | L0 | **孤儿** |
| `get_technical_analysis` | 均线/MACD/RSI 等技术面汇总 | 全 | L0 | deep_diagnosis、risk_judge |

## D2 筹码与资金（9 个）

| 工具 | 定位 | 适用画像 | 权限 | 进配方 |
|---|---|---|---|---|
| `get_cancel_order_intent` | 买/卖撤单率与偏向（托单诱多/压盘吸筹） | 全★ | L0 | deep_diagnosis、intraday_analyst、auction_hunter |
| `get_chip_accumulation` | 筹码累积/集中度变化，CYQ 筹码峰 | 全★ | L0 | deep_diagnosis、chip_analyst |
| `get_main_intent` | 主力意图（逐笔大单口径，对齐同花顺） | 全★ | L0 | deep_diagnosis、intraday_watcher、chip_analyst、intraday_analyst、auction_hunter |
| `get_northbound` | 北向持股变化（机构动向） | 趋★ | L0 | **孤儿** |
| `get_capital_flow` | 资金流向（注意：方向位口径不可单独用于判断方向） | 全 | L0 | risk_judge |
| `get_intent_explain` | 主力意图解释（为什么这么判） | 全 | L0 | **孤儿** |
| `get_main_flow_compare` | 多口径资金对比（东财/腾讯/TQ） | 全 | L0 | **孤儿** |
| `get_stock_chips` | 筹码分布（成本分布、获利盘） | 全 | L0 | risk_judge、chip_analyst |
| `get_thsdk_dde` | 同花顺 DDE 大单动向（DDX/DDY/DDZ） | 趋 | L0 | **孤儿** |

## D3 当日盘面（6 个）

| 工具 | 定位 | 适用画像 | 权限 | 进配方 |
|---|---|---|---|---|
| `get_intraday_profile` | 分时画像：日内位置/VWAP/分时段量能/内外盘/五档/量比 | 全★ | L0 | deep_diagnosis、intraday_analyst |
| `get_stock_quote` | 实时报价 | 全★ | L0 | risk_judge、intraday_watcher、auction_hunter |
| `get_auction_data` | 竞价全景/最强/主线/自选 | 妖 龙 | L0 | auction_hunter |
| `get_market_anomalies` | 全市场异动 | 全 | L0 | intraday_watcher |
| `get_orderbook` | 盘口挂单结构（实测均值 19s，必须限时/缓存） | 妖 龙 | L0 | **孤儿** |
| `scan_auction_anomaly` | 集合竞价异动扫描 | 妖 龙 | L0 | auction_hunter |

## D4 位置与因子（3 个）

| 工具 | 定位 | 适用画像 | 权限 | 进配方 |
|---|---|---|---|---|
| `get_factor_ic_report` | 因子 IC/IR 有效性（本地算，回答“这个因子现在管不管用”） | 全★ | L0 | **孤儿** |
| `get_fundamentals_detail` | 龙虎榜/两融/股东户数/分红/事件日历——左侧筹码层证据 | 左★ 趋 | L0 | **孤儿** |
| `get_stock_factors` | 六因子分位（动量/波动/换手/位置/资金/筹码集中度） | 全★ | L0 | deep_diagnosis |

## D5 消息面（8 个）

| 工具 | 定位 | 适用画像 | 权限 | 进配方 |
|---|---|---|---|---|
| `get_event_catalyst` | 事件与催化（涨价/政策/产业事件）——三层滤网第二层 | 全★ | L0 | premarket_strategist |
| `get_stock_dossier` | 十维档案：报价/技术/筹码/资金/研报/公告/利好利空 | 全★ | L0 | premarket_strategist、deep_diagnosis |
| `get_ipo_calendar` | 新股日历 | 常 | L0 | **孤儿** |
| `get_irm_qa` | 互动易问答（蹭概念的原始证据） | 全 | L0 | **孤儿** |
| `get_market_news` | 市场新闻 | 全 | L0 | premarket_strategist、postmarket_reviewer |
| `get_thsdk_corporate_action` | 公司行动（分红/送转/解禁） | 全 | L0 | **孤儿** |
| `get_thsdk_news` | 同花顺个股新闻 | 全 | L0 | **孤儿** |
| `get_web_content` | 网页正文抓取（长文/公告原文） | 全 | L0 | **孤儿** |

## D6 情景研判（8 个）

| 工具 | 定位 | 适用画像 | 权限 | 进配方 |
|---|---|---|---|---|
| `get_scenario_matrix` | 三档情景+关键位（不预测方向，只给条件-反应） | 全★ | L0 | deep_diagnosis |
| `get_decision_pioneer` | 数智决策三指标共振 | 全 | L0 | deep_diagnosis、risk_judge、intraday_watcher |
| `get_forecast` | 预测引擎（当前返回“未找到预测引擎数据库”，慎用） | 全 | L0 | **孤儿** |
| `get_opportunities` | 机会榜 | 全 | L0 | premarket_strategist、postmarket_reviewer |
| `get_stock_suggestions` | 个股操作建议（平台规则引擎） | 全 | L0 | **孤儿** |
| `get_strategy_signals` | 策略信号 | 全 | L0 | postmarket_reviewer |
| `get_wencai_enhanced` | 问小达增强（自然语言选股） | 全 | L0 | **孤儿** |
| `tdx_wenda` | 通达信问答 | 全 | L0 | **孤儿** |

## D7 板块与大盘（4 个）

| 工具 | 定位 | 适用画像 | 权限 | 进配方 |
|---|---|---|---|---|
| `get_stock_sectors` | 所属行业/概念（只给“属于哪些板块”，缺板块内排名） | 全★ | L0 | postmarket_reviewer |
| `get_index_etfs` | 指数 ETF | 常 | L0 | **孤儿** |
| `get_thsdk_hs300_constituents` | 沪深300 成分 | 常 | L0 | **孤儿** |
| `get_thsdk_market_data_index` | 指数行情（明盘） | 常 | L0 | **孤儿** |

## X 账户/平台（14 个）

| 工具 | 定位 | 适用画像 | 权限 | 进配方 |
|---|---|---|---|---|
| `create_daily_task` | 建每日定时任务（需用户确认后才落库） | 全 | L1 提案 | **孤儿** |
| `download_client_data` | 让通达信客户端下载数据包（写操作，非取数） | — | L2 写 | **孤儿** |
| `get_kzz_terms` | 可转债条款 | — | L0 | **孤儿** |
| `get_notifications` | 通知 | 全 | L0 | intraday_watcher |
| `get_portfolio` | 持仓 | 全 | L0 | risk_judge、intraday_watcher |
| `get_thsdk_market_data_bond` | 债券行情 | — | L0 | **孤儿** |
| `get_thsdk_market_data_cn_extended` | 同花顺 A 股扩展行情 | — | L0 | **孤儿** |
| `get_thsdk_market_data_fund` | 基金行情 | — | L0 | **孤儿** |
| `get_thsdk_market_data_hk` | 港股行情 | — | L0 | **孤儿** |
| `get_thsdk_market_data_us` | 美股行情 | — | L0 | **孤儿** |
| `get_watchlist` | 自选 | 全 | L0 | premarket_strategist |
| `propose_action` | 提案式动作：只能提议，用户确认才执行 | 全 | L1 提案 | **孤儿** |
| `remember_preference` | 写入长期记忆（用户明确说“记住”才写） | 全 | L1 提案 | **孤儿** |
| `search_symbols` | 代码/名称检索 | 全 | L0 | premarket_strategist |

## 按画像的必答清单（改 `deep_diagnosis` 分叉用）

| 画像 | 必答工具（按顺序取数） | 禁止话术 |
|---|---|---|
| 情绪妖股 | `get_demon_factor` → `get_main_intent` → `get_cancel_order_intent` → `get_intraday_profile` → `get_stock_sectors` → `get_sentiment_cycle` → `get_event_catalyst` | 「估值偏高」「跌破支撑位」「建议观望」 |
| 左侧超跌 | `get_wyckoff_structure`（三层滤网·量价）→ `get_sentiment_cycle`（情绪）→ `get_event_catalyst`（催化）→ `get_fundamentals_detail`（股东户数）→ `get_chip_accumulation` → `get_stock_factors` + `get_factor_ic_report` → `get_northbound` | 「无主力」「资金流出所以不行」 |
| 题材龙头 | `get_stock_sectors` → `get_hot_stocks` → `get_sentiment_cycle` → `get_main_intent` → `get_decision_pioneer` | 只报“属于某板块”而不给板块内地位 |
| 趋势白马 | `get_trend_profile` → `get_northbound` → `get_main_flow_compare` → `get_fundamentals_detail` → `get_scenario_matrix` | 拿“题材热度”当主逻辑 |
| 常规个股 | `deep_diagnosis` 现有 10 个工具 | — |

## 孤儿工具（35 个，按处置建议分组）

**A. 必须接线（画像/维度主证据，当前没人调用）**

- `get_demon_factor` — 妖股六维因子分+分级+身位（画像判定与妖股结论的唯一依据）（D0 画像/身位，妖★ 龙）
- `get_wyckoff_structure` — 威科夫量价结构（放量滞涨/弹簧试探/缩量沉淀/放量突破）——三层滤网第三层（D1 前期走势，左★）
- `get_factor_ic_report` — 因子 IC/IR 有效性（本地算，回答“这个因子现在管不管用”）（D4 位置与因子，全★）
- `get_fundamentals_detail` — 龙虎榜/两融/股东户数/分红/事件日历——左侧筹码层证据（D4 位置与因子，左★ 趋）
- `get_northbound` — 北向持股变化（机构动向）（D2 筹码与资金，趋★）
- `get_kline_patterns` — K 线形态严格规则识别（金针探底/红三兵等）（D1 前期走势，左 趋）
- `get_early_signals` — 客户端条件选股全市场扫描：放量上攻/突破盘整等启动早期信号家数（D0 画像/身位，左 常）
- `get_rally_analysis` — 盘中拉升段逐单拆解：放量上涨(真拉升) vs 拉高出货(假拉升)（D0 画像/身位，妖 龙）
- `get_main_flow_compare` — 多口径资金对比（东财/腾讯/TQ）（D2 筹码与资金，全）
- `get_intent_explain` — 主力意图解释（为什么这么判）（D2 筹码与资金，全）

**B. 平台操作（L1 提案层，需确认后执行）**

- `create_daily_task` — 建每日定时任务（需用户确认后才落库）（L1 提案）
- `remember_preference` — 写入长期记忆（用户明确说“记住”才写）（L1 提案）
- `propose_action` — 提案式动作：只能提议，用户确认才执行（L1 提案）
- `download_client_data` — 让通达信客户端下载数据包（写操作，非取数）（L2 写）

**C. 长尾/多市场（可保留为兜底，不必进配方）**

`get_delta_series`、`get_forecast`、`get_index_etfs`、`get_ipo_calendar`、`get_irm_qa`、`get_kzz_terms`、`get_orderbook`、`get_stock_suggestions`、`get_thsdk_corporate_action`、`get_thsdk_dde`、`get_thsdk_hs300_constituents`、`get_thsdk_market_data_bond`、`get_thsdk_market_data_cn_extended`、`get_thsdk_market_data_fund`、`get_thsdk_market_data_hk`、`get_thsdk_market_data_index`、`get_thsdk_market_data_us`、`get_thsdk_news`、`get_web_content`、`get_wencai_enhanced`、`tdx_wenda`

## 维护约定

- 新增工具必须在本表登记「维度 / 适用画像 / 权限层」，否则不算上线。
- 新增配方必须说明它覆盖哪些维度；孤儿工具数（62 − 被引用）是每月要看的健康指标，**上升即编排退化**。
- 工具名写错不会报错，只会静默 0 命中：登记前用 `grep -rn '"name": "<tool>"' src/agents/chat/` 核实。
