# 数据域目录覆盖率报告

> 本文件由 `scripts/gen_data_catalog.py` 静态扫描生成，**请勿手改**。复现：`python scripts/gen_data_catalog.py`；门禁：`python scripts/gen_data_catalog.py --check`。

## 总览

| 指标 | 数值 |
| --- | --- |
| API 路由（`src/web/api/*.py` 装饰器） | **491** |
| 平台表（models ∪ migrations） | **132** |
| 　其中 `models.py` `__tablename__` | 89 |
| 　仅 `migrations.py` 声明（含 hypertable） | 43 |
| 目录域总数（`src/harness/data_catalog.yaml`） | **26** |
| **有实现的域** | **8 / 26 = 30.8%** |
| source 非空但零命中表的域 | 0 |
| 域已触达的表 | 16 / 132 = 12.1% |
| 未被任何域覆盖的表 | 116 |
| 域引用了不存在的表（目录写错） | 0 个域 / 0 处 |

## ① 域覆盖率：8 / 26（30.8%）

按 level 拆分：

| level | 已实现 / 总数 | 覆盖率 |
| --- | --- | --- |
| L0 | 8 / 23 | 34.8% |
| L1 | 0 / 2 | 0.0% |
| L2 | 0 / 1 | 0.0% |

| # | 域 id | 名称 | level | scope | 状态 | 引用表 | 未匹配 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | `market.sentiment` | 市场情绪周期 | L0 | market | ✅ 已实现 | `market_sentiment_daily` | — |
| 2 | `limit.pool` | 涨停池与连板梯队 | L0 | market | ✅ 已实现 | `limit_up_events`, `ladder_daily_snapshot`, `limit_strength_shadow` | — |
| 3 | `lhb.daily` | 龙虎榜与席位 | L0 | symbol_or_market | ✅ 已实现 | `dragon_tiger_events`, `lhb_seat_details` | — |
| 4 | `theme.mood` | 题材情绪与生命周期 | L0 | market | ✅ 已实现 | `theme_mood_daily`, `board_daily` | — |
| 5 | `stock.chip` | 筹码结构与集中度 | L0 | symbol | ✅ 已实现 | `chip_daily`, `stock_factor_daily` | — |
| 6 | `stock.flow` | 资金与主力意图 | L0 | symbol | ✅ 已实现 | `dde_minute_flow`, `cancel_rate_snapshots`, `l2_fund_snapshots` | — |
| 7 | `stock.intraday` | 分时结构与盘口 | L0 | symbol | ✅ 已实现 | `quote_snapshots`, `orderbook_snapshots` | — |
| 8 | `stock.factor` | 因子分位与有效性 | L0 | symbol | ✅ 已实现 | `stock_factor_daily`, `factor_ic_snapshots` | — |
| 9 | `market.index` | 大盘指数与量能 | L0 | market | ⬜ 待接（source 为空） | — | — |
| 10 | `market.breadth` | 市场广度 | L0 | market | ⬜ 待接（source 为空） | — | — |
| 11 | `market.mainline` | 主线榜 | L0 | market | ⬜ 待接（source 为空） | — | — |
| 12 | `board.daily` | 板块行情与资金 | L0 | market | ⬜ 待接（source 为空） | — | — |
| 13 | `stock.quote` | 实时报价 | L0 | symbol | ⬜ 待接（source 为空） | — | — |
| 14 | `stock.kline` | K 线与周期 | L0 | symbol | ⬜ 待接（source 为空） | — | — |
| 15 | `stock.tech` | 技术面与形态 | L0 | symbol | ⬜ 待接（source 为空） | — | — |
| 16 | `stock.dossier` | 研报公告新闻 | L0 | symbol | ⬜ 待接（source 为空） | — | — |
| 17 | `stock.fundamental` | 财务股东两融分红 | L0 | symbol | ⬜ 待接（source 为空） | — | — |
| 18 | `stock.scenario` | 情景与关键位 | L0 | symbol | ⬜ 待接（source 为空） | — | — |
| 19 | `auction.flow` | 集合竞价 | L0 | symbol_or_market | ⬜ 待接（source 为空） | — | — |
| 20 | `portfolio.risk` | 持仓与风控 | L0 | user | ⬜ 待接（source 为空） | — | — |
| 21 | `watchlist` | 自选股 | L0 | user | ⬜ 待接（source 为空） | — | — |
| 22 | `paper.trading` | 模拟盘 | L0 | user | ⬜ 待接（source 为空） | — | — |
| 23 | `backtest` | 回测 | L1 | user | ⬜ 待接（source 为空） | — | — |
| 24 | `strategy.signal` | 策略信号 | L0 | market | ⬜ 待接（source 为空） | — | — |
| 25 | `notify.task` | 定时任务与通知 | L1 | user | ⬜ 待接（source 为空） | — | — |
| 26 | `ops.config` | 平台设置与数据源 | L2 | admin | ⬜ 待接（source 为空） | — | — |

口径：「已实现」= source 里至少解析出一张**真实存在**的表；source 为空或只写了模块名/路径都算待接。

## ② 未被任何域覆盖的表：116 张

候选域为按表名前缀/关键词的启发式建议，仅作归类参考。

| # | 表名 | 定义来源 | 候选域 | 备注 |
| --- | --- | --- | --- | --- |
| 1 | `auction_anomaly_records` | models/migrations | `auction.flow` | — |
| 2 | `auction_snapshots` | migrations | `auction.flow` | 仅 migrations 声明（ORM 未建模） |
| 3 | `auction_tick_snapshots` | models | `auction.flow` | — |
| 4 | `auction_wudao_daily` | models | `auction.flow` | — |
| 5 | `dragon_tiger_daily` | migrations | `lhb.daily` | 仅 migrations 声明（ORM 未建模） |
| 6 | `lhb_institution_daily` | migrations | `lhb.daily` | 仅 migrations 声明（ORM 未建模） |
| 7 | `seal_quality_samples` | migrations | `limit.pool` | 仅 migrations 声明（ORM 未建模） |
| 8 | `breadth_distribution_snapshot` | models | `market.breadth` | — |
| 9 | `market_flow_snapshots` | migrations | `market.breadth`, `stock.flow` | 仅 migrations 声明（ORM 未建模） |
| 10 | `index_daily` | models | `market.index` | — |
| 11 | `sentiment_selfcalc_daily` | migrations | `market.sentiment` | 仅 migrations 声明（ORM 未建模） |
| 12 | `notify_channels` | models | `notify.task` | — |
| 13 | `notify_throttle` | models | `notify.task` | — |
| 14 | `app_jobs` | migrations | `ops.config` | 仅 migrations 声明（ORM 未建模） |
| 15 | `app_settings` | models | `ops.config` | — |
| 16 | `audit_logs` | models | `ops.config` | — |
| 17 | `datasource_failures` | models | `ops.config` | — |
| 18 | `invite_code_uses` | migrations | `ops.config` | 仅 migrations 声明（ORM 未建模） |
| 19 | `invite_codes` | migrations | `ops.config` | 仅 migrations 声明（ORM 未建模） |
| 20 | `llm_usage` | models | `ops.config` | — |
| 21 | `log_entries` | models | `ops.config` | — |
| 22 | `pro_applications` | models/migrations | `ops.config` | — |
| 23 | `schema_migrations` | migrations | `ops.config` | 仅 migrations 声明（ORM 未建模） |
| 24 | `tier_configs` | models/migrations | `ops.config` | — |
| 25 | `tier_downgrade_logs` | models/migrations | `ops.config` | — |
| 26 | `user_ai_services` | models/migrations | `ops.config` | — |
| 27 | `user_sessions` | models/migrations | `ops.config` | — |
| 28 | `users` | models | `ops.config` | — |
| 29 | `paper_trading_account` | models/migrations | `portfolio.risk` | — |
| 30 | `paper_trading_positions` | models/migrations | `portfolio.risk` | — |
| 31 | `paper_trading_trades` | models/migrations | `portfolio.risk` | — |
| 32 | `portfolio_risk_snapshots` | models/migrations | `portfolio.risk` | — |
| 33 | `positions` | models | `portfolio.risk` | — |
| 34 | `price_distribution_daily` | models | `stock.chip` | — |
| 35 | `price_distribution_snapshots` | models | `stock.chip` | — |
| 36 | `more_info_daily` | models | `stock.dossier` | — |
| 37 | `news_cache` | models | `stock.dossier` | — |
| 38 | `news_topic_snapshots` | models | `stock.dossier` | — |
| 39 | `report_subscriptions` | models | `stock.dossier` | — |
| 40 | `demon_factors` | migrations | `stock.factor`, `strategy.signal` | 仅 migrations 声明（ORM 未建模） |
| 41 | `factor_weight_history` | models | `stock.factor` | — |
| 42 | `factor_weights` | models | `stock.factor` | — |
| 43 | `strategy_factor_snapshots` | models/migrations | `stock.factor`, `strategy.signal` | — |
| 44 | `dark_fund_top_snapshots` | models | `stock.flow` | — |
| 45 | `img_orderbook_frames` | migrations | `stock.intraday` | 仅 migrations 声明（ORM 未建模） |
| 46 | `l2_ticks` | migrations | `stock.intraday` | 仅 migrations 声明（ORM 未建模） |
| 47 | `tick_archive` | migrations | `stock.intraday` | 仅 migrations 声明（ORM 未建模） |
| 48 | `adj_factors` | models | `stock.kline`, `stock.factor` | — |
| 49 | `kline_annotations` | models | `stock.kline` | — |
| 50 | `klines` | migrations | `stock.kline` | 仅 migrations 声明（ORM 未建模） |
| 51 | `signal_snapshots` | migrations | `strategy.signal` | 仅 migrations 声明（ORM 未建模） |
| 52 | `signal_summary_daily` | models | `strategy.signal` | — |
| 53 | `strategy_catalog` | models/migrations | `strategy.signal` | — |
| 54 | `strategy_outcomes` | models/migrations | `strategy.signal` | — |
| 55 | `strategy_signal_runs` | models/migrations | `strategy.signal` | — |
| 56 | `strategy_weight_history` | models/migrations | `strategy.signal` | — |
| 57 | `strategy_weights` | models/migrations | `strategy.signal` | — |
| 58 | `board_flow_snapshots` | migrations | `theme.mood`, `stock.flow` | 仅 migrations 声明（ORM 未建模） |
| 59 | `boards` | models | `theme.mood` | — |
| 60 | `mainline_daily_snapshot` | migrations | `theme.mood` | 仅 migrations 声明（ORM 未建模） |
| 61 | `mainline_rank_daily` | migrations | `theme.mood` | 仅 migrations 声明（ORM 未建模） |
| 62 | `_m121_stock_dup` | migrations | （待归类） | 仅 migrations 声明（ORM 未建模） |
| 63 | `_m121_stock_keep` | migrations | （待归类） | 仅 migrations 声明（ORM 未建模） |
| 64 | `accounts` | models | （待归类） | — |
| 65 | `agent_configs` | models | （待归类） | — |
| 66 | `agent_context_runs` | models | （待归类） | — |
| 67 | `agent_prediction_outcomes` | models | （待归类） | — |
| 68 | `agent_runs` | models | （待归类） | — |
| 69 | `ai_models` | models | （待归类） | — |
| 70 | `ai_pattern_shadow` | migrations | （待归类） | 仅 migrations 声明（ORM 未建模） |
| 71 | `ai_scene_bindings` | models/migrations | （待归类） | — |
| 72 | `ai_services` | models | （待归类） | — |
| 73 | `analysis_history` | models/migrations | （待归类） | — |
| 74 | `caliber_snapshots` | migrations | （待归类） | 仅 migrations 声明（ORM 未建模） |
| 75 | `cb_daily_snapshot` | models | （待归类） | — |
| 76 | `cb_terms` | models | （待归类） | — |
| 77 | `chat_conversations` | models/migrations | （待归类） | — |
| 78 | `chat_messages` | models/migrations | （待归类） | — |
| 79 | `copilot_actions` | models | （待归类） | — |
| 80 | `copilot_feedback` | models | （待归类） | — |
| 81 | `copilot_memories` | models | （待归类） | — |
| 82 | `copilot_quotas` | models | （待归类） | — |
| 83 | `copilot_runs` | models | （待归类） | — |
| 84 | `copilot_schedules` | models | （待归类） | — |
| 85 | `copilot_steps` | models | （待归类） | — |
| 86 | `data_sources` | models | （待归类） | — |
| 87 | `decision_cache` | migrations | （待归类） | 仅 migrations 声明（ORM 未建模） |
| 88 | `decision_log` | migrations | （待归类） | 仅 migrations 声明（ORM 未建模） |
| 89 | `decision_pioneer_history` | migrations | （待归类） | 仅 migrations 声明（ORM 未建模） |
| 90 | `entry_candidate_feedback` | models/migrations | （待归类） | — |
| 91 | `entry_candidate_outcomes` | models/migrations | （待归类） | — |
| 92 | `entry_candidates` | models/migrations | （待归类） | — |
| 93 | `high_value_api_logs` | models/migrations | （待归类） | — |
| 94 | `manual_query_candidates` | models | （待归类） | — |
| 95 | `market_phase_daily` | models | （待归类） | — |
| 96 | `market_regime_snapshots` | models/migrations | （待归类） | — |
| 97 | `market_scan_ranks` | models | （待归类） | — |
| 98 | `market_scan_snapshots` | models/migrations | （待归类） | — |
| 99 | `notifications` | models/migrations | （待归类） | — |
| 100 | `price_alert_hits` | models | （待归类） | — |
| 101 | `price_alert_rules` | models | （待归类） | — |
| 102 | `resonance_ai_verdicts` | migrations | （待归类） | 仅 migrations 声明（ORM 未建模） |
| 103 | `resonance_scan` | migrations | （待归类） | 仅 migrations 声明（ORM 未建模） |
| 104 | `skill_api_keys` | models/migrations | （待归类） | — |
| 105 | `skill_usage` | models/migrations | （待归类） | — |
| 106 | `stock_agents` | models | （待归类） | — |
| 107 | `stock_context_snapshots` | models | （待归类） | — |
| 108 | `stock_suggestions` | models | （待归类） | — |
| 109 | `stock_universe_snapshots` | models | （待归类） | — |
| 110 | `stocks` | models | （待归类） | — |
| 111 | `suggestion_feedback` | models/migrations | （待归类） | — |
| 112 | `summary_cache` | migrations | （待归类） | 仅 migrations 声明（ORM 未建模） |
| 113 | `tq_formula_signal_daily` | migrations | （待归类） | 仅 migrations 声明（ORM 未建模） |
| 114 | `trading_halts` | models | （待归类） | — |
| 115 | `war_report_daily` | models | （待归类） | — |
| 116 | `workbench_layer_daily` | migrations | （待归类） | 仅 migrations 声明（ORM 未建模） |

## ③ 域引用了不存在的表：0 个域

（无，`--check` 通过）

## ④ 附：路由 → 域 启发式映射（导航用，不计入门禁）

按路由路径关键词粗匹配，用于回答「某个域背后大概有哪些 API」；不是精确归属。

| 域 id | 匹配路由数 | 示例路径 |
| --- | --- | --- |
| `ops.config` | 30 | GET /secrets/status, GET /health, POST /login |
| `portfolio.risk` | 13 | GET /positions, POST /positions, PUT /positions/{position_id} |
| `stock.flow` | 12 | GET /{symbol}/pool-flow-series, GET /capital-flow/{symbol}, GET /market-capital-flow |
| `strategy.signal` | 11 | GET /backtest, GET /strategy-catalog, GET /strategy-signals |
| `notify.task` | 8 | GET /schedule/preview, GET /schedules, POST /schedules |
| `stock.intraday` | 5 | POST /intraday/scan, GET /snapshot, GET /snapshots/{symbol} |
| `theme.mood` | 5 | GET /boards, GET /boards/{board_code}/stocks, GET /board-capital-flow |
| `market.sentiment` | 5 | GET /phase, POST /phase/sync, POST /phase/backfill |
| `stock.factor` | 4 | POST /weights/{factor_code}/{market}, GET /strategy-factors/{signal_run_id}, GET /strategy-factor-i… |
| `stock.dossier` | 4 | POST /announcement-eval, GET /report/{shadow_id}, GET /report/{shadow_id}/pdf |
| `market.breadth` | 3 | GET /breadth-distribution, GET /breadth-distribution/status, GET /breadth-history |
| `lhb.daily` | 2 | GET /lhb-institution, GET /lhb-seats |
| `watchlist` | 1 | GET /watchlist-signal-quality |
| `auction.flow` | 1 | GET /auction-snapshots |
| `stock.chip` | 1 | GET /chip-daily |
| `limit.pool` | 1 | GET /ladder |

可粗映射到域的路由：106 / 491 = 21.6%；其余属认证/运维/未归类。

