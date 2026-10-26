# 数据域目录覆盖率报告

> 本文件由 `scripts/gen_data_catalog.py` 静态扫描生成，**请勿手改**。复现：`python scripts/gen_data_catalog.py`；门禁：`python scripts/gen_data_catalog.py --check`。

## 总览

| 指标 | 数值 |
| --- | --- |
| API 路由（`src/web/api/*.py` 装饰器） | **541** |
| 平台表（models ∪ migrations） | **141** |
| 　其中 `models.py` `__tablename__` | 97 |
| 　仅 `migrations.py` 声明（含 hypertable） | 44 |
| 目录域总数（`src/harness/data_catalog.yaml`） | **26** |
| **有实现的域** | **26 / 26 = 100.0%** |
| source 非空但零命中表的域 | 0 |
| 域已触达的表 | 44 / 141 = 31.2% |
| 未被任何域覆盖的表 | 97 |
| 域引用了不存在的表（目录写错） | 0 个域 / 0 处 |

## ① 域覆盖率：26 / 26（100.0%）

按 level 拆分：

| level | 已实现 / 总数 | 覆盖率 |
| --- | --- | --- |
| L0 | 23 / 23 | 100.0% |
| L1 | 2 / 2 | 100.0% |
| L2 | 1 / 1 | 100.0% |

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
| 9 | `market.index` | 大盘指数与量能 | L0 | market | ✅ 已实现 | `quote_snapshots`, `index_daily` | — |
| 10 | `market.breadth` | 市场广度 | L0 | market | ✅ 已实现 | `breadth_distribution_snapshot`, `market_regime_snapshots` | — |
| 11 | `market.mainline` | 主线榜 | L0 | market | ✅ 已实现 | `mainline_rank_daily`, `mainline_daily_snapshot` | — |
| 12 | `board.daily` | 板块行情与资金 | L0 | market | ✅ 已实现 | `board_daily`, `boards`, `board_flow_snapshots` | — |
| 13 | `stock.quote` | 实时报价 | L0 | symbol | ✅ 已实现 | `quote_snapshots` | — |
| 14 | `stock.kline` | K 线与周期 | L0 | symbol | ✅ 已实现 | `klines` | — |
| 15 | `stock.tech` | 技术面与形态 | L0 | symbol | ✅ 已实现 | `stock_factor_daily`, `klines` | — |
| 16 | `stock.dossier` | 研报公告新闻 | L0 | symbol | ✅ 已实现 | `more_info_daily`, `news_cache` | — |
| 17 | `stock.fundamental` | 财务股东两融分红 | L0 | symbol | ✅ 已实现 | `dragon_tiger_events` | — |
| 18 | `stock.scenario` | 情景与关键位 | L0 | symbol | ✅ 已实现 | `klines`, `stock_factor_daily` | — |
| 19 | `auction.flow` | 集合竞价 | L0 | symbol_or_market | ✅ 已实现 | `auction_snapshots`, `auction_tick_snapshots`, `auction_anomaly_records` | — |
| 20 | `portfolio.risk` | 持仓与风控 | L0 | user | ✅ 已实现 | `positions`, `portfolio_risk_snapshots` | — |
| 21 | `watchlist` | 自选股 | L0 | user | ✅ 已实现 | `stocks` | — |
| 22 | `paper.trading` | 模拟盘 | L0 | user | ✅ 已实现 | `paper_trading_account`, `paper_trading_positions`, `paper_trading_trades` | — |
| 23 | `backtest` | 回测 | L1 | user | ✅ 已实现 | `strategy_outcomes`, `strategy_factor_snapshots` | — |
| 24 | `strategy.signal` | 策略信号 | L0 | market | ✅ 已实现 | `strategy_factor_snapshots`, `signal_snapshots` | — |
| 25 | `notify.task` | 定时任务与通知 | L1 | user | ✅ 已实现 | `copilot_schedules`, `notify_channels`, `notifications` | — |
| 26 | `ops.config` | 平台设置与数据源 | L2 | admin | ✅ 已实现 | `data_sources`, `app_settings`, `datasource_failures` | — |

口径：「已实现」= source 里至少解析出一张**真实存在**的表；source 为空或只写了模块名/路径都算待接。

## ② 未被任何域覆盖的表：97 张

候选域为按表名前缀/关键词的启发式建议，仅作归类参考。

| # | 表名 | 定义来源 | 候选域 | 备注 |
| --- | --- | --- | --- | --- |
| 1 | `auction_wudao_daily` | models | `auction.flow` | — |
| 2 | `dragon_tiger_daily` | migrations | `lhb.daily` | 仅 migrations 声明（ORM 未建模） |
| 3 | `lhb_institution_daily` | migrations | `lhb.daily` | 仅 migrations 声明（ORM 未建模） |
| 4 | `seal_quality_samples` | migrations | `limit.pool` | 仅 migrations 声明（ORM 未建模） |
| 5 | `market_flow_snapshots` | migrations | `market.breadth`, `stock.flow` | 仅 migrations 声明（ORM 未建模） |
| 6 | `sentiment_selfcalc_daily` | migrations | `market.sentiment` | 仅 migrations 声明（ORM 未建模） |
| 7 | `notify_throttle` | models | `notify.task` | — |
| 8 | `app_jobs` | migrations | `ops.config` | 仅 migrations 声明（ORM 未建模） |
| 9 | `audit_logs` | models | `ops.config` | — |
| 10 | `auth_session_revocations` | migrations | `ops.config` | 仅 migrations 声明（ORM 未建模） |
| 11 | `invite_code_uses` | migrations | `ops.config` | 仅 migrations 声明（ORM 未建模） |
| 12 | `invite_codes` | migrations | `ops.config` | 仅 migrations 声明（ORM 未建模） |
| 13 | `llm_usage` | models | `ops.config` | — |
| 14 | `log_entries` | models | `ops.config` | — |
| 15 | `pro_applications` | models/migrations | `ops.config` | — |
| 16 | `schema_migrations` | migrations | `ops.config` | 仅 migrations 声明（ORM 未建模） |
| 17 | `tier_configs` | models/migrations | `ops.config` | — |
| 18 | `tier_downgrade_logs` | models/migrations | `ops.config` | — |
| 19 | `user_ai_prefs` | models/migrations | `ops.config` | — |
| 20 | `user_ai_services` | models/migrations | `ops.config` | — |
| 21 | `user_sessions` | models/migrations | `ops.config` | — |
| 22 | `users` | models | `ops.config` | — |
| 23 | `price_distribution_daily` | models | `stock.chip` | — |
| 24 | `price_distribution_snapshots` | models | `stock.chip` | — |
| 25 | `news_topic_snapshots` | models | `stock.dossier` | — |
| 26 | `report_subscriptions` | models | `stock.dossier` | — |
| 27 | `demon_factors` | migrations | `stock.factor`, `strategy.signal` | 仅 migrations 声明（ORM 未建模） |
| 28 | `factor_weight_history` | models | `stock.factor` | — |
| 29 | `factor_weights` | models | `stock.factor` | — |
| 30 | `factor_winrate_daily` | models/migrations | `stock.factor` | — |
| 31 | `dark_fund_top_snapshots` | models | `stock.flow` | — |
| 32 | `img_orderbook_frames` | migrations | `stock.intraday` | 仅 migrations 声明（ORM 未建模） |
| 33 | `l2_ticks` | migrations | `stock.intraday` | 仅 migrations 声明（ORM 未建模） |
| 34 | `tick_archive` | migrations | `stock.intraday` | 仅 migrations 声明（ORM 未建模） |
| 35 | `adj_factors` | models | `stock.kline`, `stock.factor` | — |
| 36 | `kline_annotations` | models | `stock.kline` | — |
| 37 | `recipe_versions` | models/migrations | `strategy.signal` | — |
| 38 | `signal_summary_daily` | models | `strategy.signal` | — |
| 39 | `strategy_catalog` | models/migrations | `strategy.signal` | — |
| 40 | `strategy_signal_runs` | models/migrations | `strategy.signal` | — |
| 41 | `strategy_weight_history` | models/migrations | `strategy.signal` | — |
| 42 | `strategy_weights` | models/migrations | `strategy.signal` | — |
| 43 | `_m121_stock_dup` | migrations | （待归类） | 仅 migrations 声明（ORM 未建模） |
| 44 | `_m121_stock_keep` | migrations | （待归类） | 仅 migrations 声明（ORM 未建模） |
| 45 | `accounts` | models | （待归类） | — |
| 46 | `agent_configs` | models | （待归类） | — |
| 47 | `agent_context_runs` | models | （待归类） | — |
| 48 | `agent_prediction_outcomes` | models | （待归类） | — |
| 49 | `agent_runs` | models | （待归类） | — |
| 50 | `ai_models` | models | （待归类） | — |
| 51 | `ai_pattern_shadow` | migrations | （待归类） | 仅 migrations 声明（ORM 未建模） |
| 52 | `ai_scene_bindings` | models/migrations | （待归类） | — |
| 53 | `ai_services` | models | （待归类） | — |
| 54 | `analysis_history` | models/migrations | （待归类） | — |
| 55 | `caliber_snapshots` | migrations | （待归类） | 仅 migrations 声明（ORM 未建模） |
| 56 | `cb_daily_snapshot` | models | （待归类） | — |
| 57 | `cb_terms` | models | （待归类） | — |
| 58 | `chat_conversations` | models/migrations | （待归类） | — |
| 59 | `chat_messages` | models/migrations | （待归类） | — |
| 60 | `copilot_actions` | models | （待归类） | — |
| 61 | `copilot_feedback` | models | （待归类） | — |
| 62 | `copilot_memories` | models | （待归类） | — |
| 63 | `copilot_quotas` | models | （待归类） | — |
| 64 | `copilot_runs` | models | （待归类） | — |
| 65 | `copilot_steps` | models | （待归类） | — |
| 66 | `decision_cache` | migrations | （待归类） | 仅 migrations 声明（ORM 未建模） |
| 67 | `decision_log` | migrations | （待归类） | 仅 migrations 声明（ORM 未建模） |
| 68 | `decision_pioneer_history` | migrations | （待归类） | 仅 migrations 声明（ORM 未建模） |
| 69 | `entry_candidate_feedback` | models/migrations | （待归类） | — |
| 70 | `entry_candidate_outcomes` | models/migrations | （待归类） | — |
| 71 | `entry_candidates` | models/migrations | （待归类） | — |
| 72 | `exday_daily` | models/migrations | （待归类） | — |
| 73 | `harness_experiments` | models/migrations | （待归类） | — |
| 74 | `harness_proposals` | models | （待归类） | — |
| 75 | `high_value_api_logs` | models/migrations | （待归类） | — |
| 76 | `manual_query_candidates` | models | （待归类） | — |
| 77 | `market_phase_daily` | models | （待归类） | — |
| 78 | `market_replay_daily` | models/migrations | （待归类） | — |
| 79 | `market_scan_ranks` | models | （待归类） | — |
| 80 | `market_scan_snapshots` | models/migrations | （待归类） | — |
| 81 | `price_alert_hits` | models | （待归类） | — |
| 82 | `price_alert_rules` | models | （待归类） | — |
| 83 | `resonance_ai_verdicts` | migrations | （待归类） | 仅 migrations 声明（ORM 未建模） |
| 84 | `resonance_scan` | migrations | （待归类） | 仅 migrations 声明（ORM 未建模） |
| 85 | `skill_api_keys` | models/migrations | （待归类） | — |
| 86 | `skill_usage` | models/migrations | （待归类） | — |
| 87 | `stock_agents` | models | （待归类） | — |
| 88 | `stock_context_snapshots` | models | （待归类） | — |
| 89 | `stock_suggestions` | models | （待归类） | — |
| 90 | `stock_universe_snapshots` | models | （待归类） | — |
| 91 | `suggestion_feedback` | models/migrations | （待归类） | — |
| 92 | `summary_cache` | migrations | （待归类） | 仅 migrations 声明（ORM 未建模） |
| 93 | `tq_formula_signal_daily` | migrations | （待归类） | 仅 migrations 声明（ORM 未建模） |
| 94 | `trading_halts` | models | （待归类） | — |
| 95 | `war_report_daily` | models | （待归类） | — |
| 96 | `webhook_subscriptions` | models/migrations | （待归类） | — |
| 97 | `workbench_layer_daily` | migrations | （待归类） | 仅 migrations 声明（ORM 未建模） |

## ③ 域引用了不存在的表：0 个域

（无，`--check` 通过）

## ④ 附：路由 → 域 启发式映射（导航用，不计入门禁）

按路由路径关键词粗匹配，用于回答「某个域背后大概有哪些 API」；不是精确归属。

| 域 id | 匹配路由数 | 示例路径 |
| --- | --- | --- |
| `ops.config` | 34 | GET /secrets/status, GET /health, POST /login |
| `portfolio.risk` | 14 | GET /positions, POST /positions, PUT /positions/{position_id} |
| `stock.flow` | 14 | GET /fund-structure/{symbol}, GET /{symbol}/pool-flow-series, GET /capital-flow/{symbol} |
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

可粗映射到域的路由：113 / 541 = 20.9%；其余属认证/运维/未归类。

