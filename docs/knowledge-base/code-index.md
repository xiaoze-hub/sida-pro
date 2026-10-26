# SIDA 项目代码索引

> 基线标识：`d774ae017189d90b79a89fa1c327a9a66858a6cc`，`VERSION=v0.80.0`。
> 盘点对象：`/Users/winter/Documents/zicalWork/sida-pro`。
> 覆盖范围：基线中 src、packages/marketdata、forecast_lib 与两个服务入口的全部 Python AST；前端 App.tsx 路由声明；全仓 Git 跟踪文件的分类计数。
> 未覆盖：测试用例执行、Python 函数内部算法正确性、前端组件内部行为、外部接口可用性、运行数据和密钥。模块说明取自源码首行文档字符串，可能带历史描述，不代表功能验收。
> 生成器：`scripts/build_project_knowledge.py`；所有内容读取自上述 commit，不读取 .env。

## 文件规模

基线共 2447 个 Git 跟踪文件；本索引解析 610 个 Python 文件。

| 路径 | 跟踪文件数 | 主要扩展名 |
| --- | ---: | --- |
| `src/` | 551 | .py: 523, .yaml: 21, .md: 6, .json: 1 |
| `frontend/src/` | 281 | .tsx: 197, .ts: 83, .css: 1 |
| `frontend/tests/` | 222 | .tsx: 122, .ts: 99, .json: 1 |
| `packages/marketdata/` | 73 | .py: 71, .md: 1, .toml: 1 |
| `forecast_lib/` | 14 | .py: 14 |
| `tests/` | 560 | .py: 551, .txt: 4, .img: 2, .json: 2, .sys: 1 |
| `scripts/` | 168 | .sh: 76, .py: 74, .json: 7, .cjs: 7, .mjs: 3 |
| `docs/` | 287 | .png: 121, .md: 110, .txt: 44, .json: 6, .svg: 2 |
| `prompts/` | 9 | .txt: 8, .md: 1 |
| `strategies/` | 1 | .yaml: 1 |
| `.github/workflows/` | 7 | .yml: 7 |

## Agent 注册声明

| 名称 | 实现 |
| --- | --- |
| `auction_review` | [src/agents/auction_review.py:22](../../src/agents/auction_review.py#L22) |
| `chart_analyst` | [src/agents/chart_analyst.py:19](../../src/agents/chart_analyst.py#L19) |
| `daily_report` | [src/agents/daily_report.py:64](../../src/agents/daily_report.py#L64) |
| `intraday_monitor` | [src/agents/intraday_monitor.py:929](../../src/agents/intraday_monitor.py#L929) |
| `news_digest` | [src/agents/news_digest.py:46](../../src/agents/news_digest.py#L46) |
| `premarket_outlook` | [src/agents/premarket_outlook.py:54](../../src/agents/premarket_outlook.py#L54) |
| `stock_attribution` | [src/agents/stock_attribution.py:31](../../src/agents/stock_attribution.py#L31) |
| `theme_launch_detector` | [src/agents/theme_launch_detector.py:19](../../src/agents/theme_launch_detector.py#L19) |
| `tradingagents` | [src/agents/tradingagents/agent.py:201](../../src/agents/tradingagents/agent.py#L201) |

## 前端路由声明

声明来自 frontend/src/App.tsx；含兼容重定向与重复声明，是否可见仍由登录态、角色和权限决定。

| 路由 | 元素入口 | 源码行 |
| --- | --- | ---: |
| `/login` | LoginPage | 473 |
| `/developers` | DevelopersPage | 486 |
| `/terms` | TermsPage | 499 |
| `/tiers` | TiersPage | 513 |
| `/` | DashboardPage | 750 |
| `/opportunities` | PermGuard → OpportunitiesPage | 751 |
| `/auction` | PermGuard → AuctionPage | 752 |
| `/ai-copilot` | PermGuard → AiCopilotPage | 754 |
| `/copilot-governance` | PermGuard → CopilotGovernancePage | 755 |
| `/copilot/runs` | PermGuard → CopilotRunsPage | 757 |
| `/copilot/experiments` | PermGuard → ExperimentsPage | 758 |
| `/copilot/agents` | PermGuard → AgentCatalogPage | 759 |
| `/copilot/tools` | PermGuard → ToolCatalogPage | 760 |
| `/copilot/proposals` | PermGuard → ProposalsPage | 761 |
| `/copilot/parallel` | PermGuard → ParallelDiagnosisPage | 762 |
| `/copilot/runs/:runId` | PermGuard → CopilotRunDetailPage | 764 |
| `/dark-fund-top` | PermGuard → DarkFundTopPage | 766 |
| `/war-report` | PermGuard → WarReportPage | 767 |
| `/replay-tree` | PermGuard → ReplayTreePage | 769 |
| `/forecast` | LegacyForecastRedirect | 772 |
| `/quote` | LegacyForecastRedirect | 773 |
| `/quote/:symbol` | LegacyQuoteSymbolRedirect | 774 |
| `/l2` | LegacyL2Redirect | 775 |
| `/heatmap` | PermGuard → HeatmapPage | 777 |
| `/caliber-compare` | PermGuard → CaliberComparePage | 779 |
| `/decision-ledger` | DecisionLedgerPage | 781 |
| `/theme-mood` | PermGuard → ThemeMoodPage | 782 |
| `/index/:symbol` | LegacyIndexRedirect | 785 |
| `/boards/:blockCode` | LegacyIndexRedirect | 786 |
| `/portfolio` | PermGuard → StocksPage | 787 |
| `/stocks/:symbol` | PermGuard → StockWorkbenchPage | 788 |
| `/stocks` | LegacyStocksRedirect | 789 |
| `/system` | SystemPage | 791 |
| `/agents` | LegacyTabRedirect | 792 |
| `/reports` | ReportsHubPage | 796 |
| `/history` | LegacyTabRedirect | 797 |
| `/shadow` | ShadowHubPage | 801 |
| `/paper-trading` | LegacyTabRedirect | 802 |
| `/notifications` | NotificationsHubPage | 804 |
| `/alerts` | LegacyTabRedirect | 805 |
| `/profile` | ProfilePage | 806 |
| `/api-keys` | ApiKeysPage | 808 |
| `/settings` | SettingsHubPage | 810 |
| `/audit` | LegacyTabRedirect | 811 |
| `/help` | LegacyTabRedirect | 812 |
| `/developers` | DevelopersPage | 814 |
| `/terms` | TermsPage | 816 |
| `/tiers` | TiersPage | 817 |
| `/factor-ic` | FactorICPage | 818 |
| `/admin` | PermGuard → AdminPage | 820 |
| `/datasources` | LegacyTabRedirect | 821 |
| `/analysis/:symbol/:date` | PermGuard → AnalysisDetailPage | 823 |
| `*` | Navigate | 824 |

## Python 模块检索

公开入口只列模块顶层类和非下划线函数；路由数仅计装饰器声明，不等于最终 API URL。完整 API 前缀与鉴权依赖须核对 src/web/app.py 的 include_router。

| 模块 | 源码说明 | 公开入口 | 路由声明数 |
| --- | --- | --- | ---: |
| [forecast_lib/ai_referee.py](../../forecast_lib/ai_referee.py) | AI 裁判层: 模型预测交给 PanWatch 对话助手(8000)评估, 可改最终方向(B方案)。 | RefereeConfigUnavailable, resolve_referee_model_cfg, evaluate_prediction, referee_impact_stats | 0 |
| [forecast_lib/forecast_history.py](../../forecast_lib/forecast_history.py) | 无模块说明 | patch_baostock_timeout, get_stock_name, save_forecast, list_forecasts | 0 |
| [forecast_lib/forecast_models.py](../../forecast_lib/forecast_models.py) | 无模块说明 | get_predictor, load_kline, kronos_predict, xgboost_predict, linreg_predict, get_chronos_predictor, get_timesfm_predictor, timesfm_predict, chronos_predict | 0 |
| [forecast_lib/forecast_paths.py](../../forecast_lib/forecast_paths.py) | Filesystem paths used by the forecast engine. | resolve_forecast_db_path | 0 |
| [forecast_lib/forecast_reports.py](../../forecast_lib/forecast_reports.py) | A股预测报告生成器 — DSA 双格式 | generate_report, generate_backtest_report, generate_wecom_report | 0 |
| [forecast_lib/forecast_sentiment.py](../../forecast_lib/forecast_sentiment.py) | 无模块说明 | set_runtime_llm_config, llm_sentiment_score, fetch_sentiment | 0 |
| [forecast_lib/forecast_traces.py](../../forecast_lib/forecast_traces.py) | 无模块说明 | start_run, record_model_output, record_referee_eval, record_sentiment_eval, finalize_run, get_run, list_model_outputs, list_referee_evals, list_sentiment_evals, list_runs_for_symbol …（共 21 个） | 0 |
| [forecast_lib/forecast_utils.py](../../forecast_lib/forecast_utils.py) | 无模块说明 | new_task, direction_label, calc_capital_score, build_recommendation | 0 |
| [forecast_lib/marketdata_bridge.py](../../forecast_lib/marketdata_bridge.py) | 8010 接入 PanWatch 市场数据源(龙虎榜) — 经 8000 HTTP 代理端点。 | get_dragon_tiger | 0 |
| [forecast_lib/model_weights.py](../../forecast_lib/model_weights.py) | 模型权重动态调整 — 预测质量闭环(后端) | last_weights_source, load_weights, update_weights_after_backtest | 0 |
| [forecast_lib/panwatch_bridge.py](../../forecast_lib/panwatch_bridge.py) | 从 PanWatch 主后端(8000, 容器内 marketdata/tdx 数据源)获取主力资金数据。 | fetch_capital_flow | 0 |
| [forecast_lib/panwatch_client.py](../../forecast_lib/panwatch_client.py) | PanWatch HTTP client shared by forecast bridge modules. | get_panwatch_url, get_service_token, invalidate_token, get_token, auth_headers, has_credentials, request_json | 0 |
| [forecast_lib/trading_days.py](../../forecast_lib/trading_days.py) | forecast 独立进程的交易日工具(KI-007, 2026-09-18; 2026-09-18 二次修 D5)。 | TradingCalendarError, is_trading_day, is_trading_day_or_weekday, next_trading_day, next_trading_days, add_trading_days | 0 |
| [forecast_lib/zhitu_bridge.py](../../forecast_lib/zhitu_bridge.py) | 无模块说明 | fetch_capital_flow | 0 |
| [forecast_server.py](../../forecast_server.py) | A股预测引擎服务 (:8010) — 主入口 | health, predict, predict_post, predict_status, backtest, history, forecast_weights, referee_stats, forecast_models, stocks_search …（共 16 个） | 16 |
| [packages/marketdata/src/marketdata/__init__.py](../../packages/marketdata/src/marketdata/__init__.py) | marketdata —— 多市场行情数据抓取层(可插拔数据源)。 |  | 0 |
| [packages/marketdata/src/marketdata/cache.py](../../packages/marketdata/src/marketdata/cache.py) | 带 TTL 的轻量内存缓存,线程安全,过期 key 在下次 get 时被动剔除。 | CacheBackend, TTLCache | 0 |
| [packages/marketdata/src/marketdata/client.py](../../packages/marketdata/src/marketdata/client.py) | 对象式入口:注入 ConfigProvider(+可选 MetricsSink),对外提供 quotes()/health()。 | MarketData | 0 |
| [packages/marketdata/src/marketdata/defaults.py](../../packages/marketdata/src/marketdata/defaults.py) | 端口的内置默认实现:静态配置 + 内存指标(供独立使用与测试)。 | StaticConfigProvider, InMemoryMetricsSink | 0 |
| [packages/marketdata/src/marketdata/engine.py](../../packages/marketdata/src/marketdata/engine.py) | 数据源主备调度器:按 ConfigProvider 的优先级链取数,首个非空即返回。 | Engine | 0 |
| [packages/marketdata/src/marketdata/errors.py](../../packages/marketdata/src/marketdata/errors.py) | marketdata 异常类型。 | MarketDataError, VendorError | 0 |
| [packages/marketdata/src/marketdata/http.py](../../packages/marketdata/src/marketdata/http.py) | 统一 HTTP 工具:走系统代理(trust_env=True)+ 按 host 节流 + 退避重试 + 来源标记。 | fetch_source, source_suffix, capture_errors, record_error, throttle, market_get | 0 |
| [packages/marketdata/src/marketdata/keypool.py](../../packages/marketdata/src/marketdata/keypool.py) | 多 key 池: 同源多个凭证的轮换 + 限流冷却调度。 | KeyPool | 0 |
| [packages/marketdata/src/marketdata/log_throttle.py](../../packages/marketdata/src/marketdata/log_throttle.py) | 失败日志限流(2026-10-08 可观测性审计 P2)。 | should_log, log_failure, reset | 0 |
| [packages/marketdata/src/marketdata/ports.py](../../packages/marketdata/src/marketdata/ports.py) | 解耦端口:宿主实现这两个 Protocol 即可接入,包本身不依赖 web/DB。 | SourceConfig, ConfigProvider, MetricsSink | 0 |
| [packages/marketdata/src/marketdata/registry.py](../../packages/marketdata/src/marketdata/registry.py) | 包内各数据类型的合法 vendor 权威(唯一真相源)。 | build_vendors | 0 |
| [packages/marketdata/src/marketdata/symbol.py](../../packages/marketdata/src/marketdata/symbol.py) | 跨市场股票代码值对象:一处归一化,替代散落各处的 _to_market/前缀逻辑。 | Market, Symbol | 0 |
| [packages/marketdata/src/marketdata/types.py](../../packages/marketdata/src/marketdata/types.py) | 请求 / 响应 / 行情数据类型。 | Request, Quote, Bar, CapitalFlow, BoardCapitalFlow, MarketCapitalFlow, HotStock, HotBoard, EventItem, Fundamentals …（共 20 个） | 0 |
| [packages/marketdata/src/marketdata/vendors/__init__.py](../../packages/marketdata/src/marketdata/vendors/__init__.py) | 无模块说明 |  | 0 |
| [packages/marketdata/src/marketdata/vendors/alphavantage.py](../../packages/marketdata/src/marketdata/vendors/alphavantage.py) | Alpha Vantage 行情 vendor(美股/全球, 免费 25 req/day, 多 key 池可放大额度)。 | AlphaVantageQuoteVendor | 0 |
| [packages/marketdata/src/marketdata/vendors/base.py](../../packages/marketdata/src/marketdata/vendors/base.py) | Vendor 抽象:每个 vendor 只负责"一家源怎么抓 + 解析成标准类型",内部无 fallback。 | on_vendor_failure, emit_vendor_failure, Vendor, QuoteVendor, KlineVendor, CapitalFlowVendor, EventsVendor, FlashNewsVendor, NewsVendor, FundamentalsVendor …（共 16 个） | 0 |
| [packages/marketdata/src/marketdata/vendors/board_fund_flow.py](../../packages/marketdata/src/marketdata/vendors/board_fund_flow.py) | 板块资金流向 vendor:东财 push2 clist 接口（仅国内可用，海外 502）。 | BoardFundFlowVendor | 0 |
| [packages/marketdata/src/marketdata/vendors/capital_flow.py](../../packages/marketdata/src/marketdata/vendors/capital_flow.py) | 资金流向 vendor:东财 + 新浪(CN)。移植自 PanWatch capital_flow_collector 抓取核。 | EastmoneyCapitalFlowVendor, SinaCapitalFlowVendor | 0 |
| [packages/marketdata/src/marketdata/vendors/cninfo_irm.py](../../packages/marketdata/src/marketdata/vendors/cninfo_irm.py) | 互动易问答 vendor:巨潮 cninfo(海外可达,公司对传闻/利好的官方回应信源)。 | CninfoIrmVendor | 0 |
| [packages/marketdata/src/marketdata/vendors/discovery.py](../../packages/marketdata/src/marketdata/vendors/discovery.py) | 发现(东财热门榜)vendor:单源、市场级、非 symbol 模型。 | DiscoveryVendor | 0 |
| [packages/marketdata/src/marketdata/vendors/eastmoney.py](../../packages/marketdata/src/marketdata/vendors/eastmoney.py) | 东财 CN 报价 vendor(quote 第二源)。push2 stock/get,单只查询,逐只循环取批量。 | EastmoneyQuoteVendor | 0 |
| [packages/marketdata/src/marketdata/vendors/em_anomaly.py](../../packages/marketdata/src/marketdata/vendors/em_anomaly.py) | 日内异动池 vendor:东财 dycalchis.eastmoney.com(海外可达)。 | AnomalyItem, EmAnomalyVendor, EmAnomalyCountVendor | 0 |
| [packages/marketdata/src/marketdata/vendors/events.py](../../packages/marketdata/src/marketdata/vendors/events.py) | 事件 vendor:东财公告(单源)。移植自 PanWatch EastMoneyEventsCollector.fetch_events 抓取核 | EventsVendor | 0 |
| [packages/marketdata/src/marketdata/vendors/flash_news.py](../../packages/marketdata/src/marketdata/vendors/flash_news.py) | 快讯(7×24)vendor:cls(财联社)/ sina(新浪直播)/ eastmoney(东财快讯),均市场级、单源。 | ClsFlashNewsVendor, SinaFlashNewsVendor, EastmoneyFlashNewsVendor | 0 |
| [packages/marketdata/src/marketdata/vendors/ftshare.py](../../packages/marketdata/src/marketdata/vendors/ftshare.py) | FTShare vendor:龙虎榜 / 融资融券(东财 datacenter 的免费备源)。 | FtshareMCPClient, FtshareDragonTigerVendor, FtshareMarginVendor, fetch_financial_calendar | 0 |
| [packages/marketdata/src/marketdata/vendors/fundamentals.py](../../packages/marketdata/src/marketdata/vendors/fundamentals.py) | 基本面/财务 vendor:腾讯(CN,~数组)+ 东财(CN push2 / US·HK datacenter GMAININDICATOR)。 | TencentFundamentalsVendor, EastmoneyFundamentalsVendor | 0 |
| [packages/marketdata/src/marketdata/vendors/kline.py](../../packages/marketdata/src/marketdata/vendors/kline.py) | K 线 vendors:腾讯(全市场)/ Stooq(US)/ 东财(CN/HK)/ Yahoo(US/HK)。移植自 PanWatch kline_collector 抓取核。 | fetch_sina_index_kline, fetch_tencent_kline_raw, fetch_tencent_minute_kline, TencentKlineVendor, StooqKlineVendor, fetch_eastmoney_kline, EastmoneyKlineVendor, YahooKlineVendor | 0 |
| [packages/marketdata/src/marketdata/vendors/market_flow.py](../../packages/marketdata/src/marketdata/vendors/market_flow.py) | 市场/资金面 vendor:龙虎榜 / 融资融券 / 股东户数 / 分红,均走东财 datacenter 同构接口 | EastmoneyDragonTigerVendor, fetch_lhb_institutions, fetch_lhb_seat_details, EastmoneyMarginVendor, EastmoneyShareholdersVendor, EastmoneyDividendVendor | 0 |
| [packages/marketdata/src/marketdata/vendors/news.py](../../packages/marketdata/src/marketdata/vendors/news.py) | 新闻资讯 vendor:xueqiu(雪球个股新闻)/ eastmoney_news(东财个股新闻搜索)/ | XueqiuNewsVendor, EastmoneyStockNewsVendor, EastmoneyAnnNewsVendor | 0 |
| [packages/marketdata/src/marketdata/vendors/northbound.py](../../packages/marketdata/src/marketdata/vendors/northbound.py) | 北向资金 vendor:同花顺(ths/hexin)当日分钟累计净买入,市场级(symbols 恒空)。 | HexinNorthboundVendor | 0 |
| [packages/marketdata/src/marketdata/vendors/sina.py](../../packages/marketdata/src/marketdata/vendors/sina.py) | 新浪(Sina)US/HK 行情 vendor(HTTP,GBK)。免 key 免代理,作腾讯之后的 US/HK 备源。 | index_output_code, fetch_index_quotes, SinaQuoteVendor | 0 |
| [packages/marketdata/src/marketdata/vendors/tdx.py](../../packages/marketdata/src/marketdata/vendors/tdx.py) | 通达信问小达 MCP 客户端(HTTP 直连,免 Hermes)。 | TdxMCPClient, ask_wenda | 0 |
| [packages/marketdata/src/marketdata/vendors/tencent.py](../../packages/marketdata/src/marketdata/vendors/tencent.py) | 腾讯行情 vendor(HTTP,GBK)。移植自 akshare_collector._parse_tencent_line/_fetch_tencent_quotes。 | fetch_raw, TencentQuoteVendor | 0 |
| [packages/marketdata/src/marketdata/vendors/tencent_fundflow.py](../../packages/marketdata/src/marketdata/vendors/tencent_fundflow.py) | 腾讯证券个股资金流 vendor(2026-08-11 接入)。 | TencentFundflowVendor | 0 |
| [packages/marketdata/src/marketdata/vendors/tencent_info.py](../../packages/marketdata/src/marketdata/vendors/tencent_info.py) | 腾讯证券资讯/股东/基本面 vendor(2026-08-11 深挖接入)。 | fetch_notice_list, fetch_news_list, fetch_top_holders, fetch_org_rating, fetch_industry_rank, fetch_stock_brief, fetch_plate_list, fetch_investment, fetch_hot_rank | 0 |
| [packages/marketdata/src/marketdata/vendors/tencent_panel.py](../../packages/marketdata/src/marketdata/vendors/tencent_panel.py) | 腾讯证券盘口面板 vendor(2026-08-11 接入): 盘口大单占比 + 大单分档统计。 | fetch_pan_analysis, fetch_big_order_stats, fetch_price_bins, fetch_quote_brief, fetch_price_distribution | 0 |
| [packages/marketdata/src/marketdata/vendors/tencent_rank.py](../../packages/marketdata/src/marketdata/vendors/tencent_rank.py) | 腾讯排行（个股榜 / 板块榜）—— 免配额、零 key（2026-10-25 实测）。 | fetch_board_rank | 0 |
| [packages/marketdata/src/marketdata/vendors/ths_flow.py](../../packages/marketdata/src/marketdata/vendors/ths_flow.py) | 同花顺板块/大盘资金 vendor(2026-08-09 香港节点实测可用)。 | ThsBoardFlowVendor, ThsMarketFlowVendor | 0 |
| [packages/marketdata/src/marketdata/vendors/ths_hot.py](../../packages/marketdata/src/marketdata/vendors/ths_hot.py) | 同花顺热点 vendor:zx.10jqka.com.cn(海外可达,板块归因 reason + 热榜 AI 分析)。 | ThsHotReasonVendor, ThsHotListVendor | 0 |
| [packages/marketdata/src/marketdata/vendors/ths_web.py](../../packages/marketdata/src/marketdata/vendors/ths_web.py) | 同花顺 Web 数据源 vendor(2026-08-09 香港节点实测可用,免登录)。 | ThsQuoteVendor, ThsKlineVendor, ThsFlashNewsVendor, ThsFundamentalsVendor | 0 |
| [packages/marketdata/src/marketdata/vendors/tq.py](../../packages/marketdata/src/marketdata/vendors/tq.py) | 通达信TQ行情 vendor(本机网关 http://127.0.0.1:5100, JSON-RPC)。 | tq_bars_fresh, TqUnavailable, tq_rpc, to_tq_code, TqQuoteVendor, TqMoreInfoVendor, TqKlineVendor, TqShareholdersVendor, TqDividendVendor, TqCapitalFlowVendor …（共 55 个） | 0 |
| [packages/marketdata/src/marketdata/vendors/twelvedata.py](../../packages/marketdata/src/marketdata/vendors/twelvedata.py) | Twelve Data 行情 vendor(美股为主, 免费 800 req/day, 多 key 池可放大额度)。 | TwelveDataQuoteVendor | 0 |
| [packages/marketdata/src/marketdata/vendors/yfinance.py](../../packages/marketdata/src/marketdata/vendors/yfinance.py) | YFinance 行情 vendor(可选,HK/US)。无状态 + 惰性 import;缺库抛 VendorError。 | YFinanceQuoteVendor | 0 |
| [packages/marketdata/src/marketdata/vendors/zhitu.py](../../packages/marketdata/src/marketdata/vendors/zhitu.py) | 智兔数服 vendor:分红送配(东财分红的免费备源)。 | pick_zhitu_token, ZhituDividendVendor | 0 |
| [packages/marketdata/src/marketdata/vendors/zhitu_api.py](../../packages/marketdata/src/marketdata/vendors/zhitu_api.py) | 智兔数服 API 统一客户端(全量能力封装)。 | kline, capital_flow, finance_main, quarterly_income, quarterly_cashflow, business_scope, performance_forecast, top10_holders, top_holder_changes, fund_holdings …（共 16 个） | 0 |
| [packages/marketdata/src/marketdata/vendors/zhitu_full.py](../../packages/marketdata/src/marketdata/vendors/zhitu_full.py) | 智兔数服 Engine vendors(K线/资金流/股东/财务), 走 ZhituApiClient, token 多 key 池化。 | ZhituKlineVendor, ZhituCapitalFlowVendor, ZhituShareholdersVendor, ZhituFundamentalsVendor | 0 |
| [packages/marketdata/tests/test_cache.py](../../packages/marketdata/tests/test_cache.py) | 无模块说明 | test_set_get_roundtrip, test_expired_key_returns_none, test_zero_ttl_not_cached, test_max_size_evicts | 0 |
| [packages/marketdata/tests/test_capital_flow_vendor.py](../../packages/marketdata/tests/test_capital_flow_vendor.py) | 无模块说明 | test_capital_flow_parses, test_capital_flow_empty, test_sina_capital_flow_parses, test_sina_capital_flow_empty, test_sina_capital_flow_non_cn_market_returns_empty | 0 |
| [packages/marketdata/tests/test_client.py](../../packages/marketdata/tests/test_client.py) | 无模块说明 | test_quotes_returns_typed, test_quotes_groups_mixed_markets, test_health_after_fetch, test_quotes_stamps_winning_vendor | 0 |
| [packages/marketdata/tests/test_discovery_vendor.py](../../packages/marketdata/tests/test_discovery_vendor.py) | 无模块说明 | test_hot_stocks_maps_fcodes, test_hot_stocks_normalizes_dict_diff, test_hot_boards_maps_fcodes, test_board_stocks_maps_fcodes, test_board_stocks_empty_code_returns_empty | 0 |
| [packages/marketdata/tests/test_eastmoney_quote.py](../../packages/marketdata/tests/test_eastmoney_quote.py) | 无模块说明 | test_eastmoney_parses_quote_with_decimal_restore, test_eastmoney_batch_multiple_symbols_loops_calls, test_eastmoney_empty_response_returns_empty, test_eastmoney_no_symbols_returns_empty, test_eastmoney_unsupported_market_skipped, test_eastmoney_missing_data_key_returns_empty | 0 |
| [packages/marketdata/tests/test_engine.py](../../packages/marketdata/tests/test_engine.py) | 无模块说明 | FakeVendor, FakeConfig, test_first_success_wins, test_failover_empty_then_raise_then_ok, test_all_fail_returns_not_ok, test_market_filter_skips_unsupported, test_cache_hit_skips_second_call, test_metrics_recorded, test_priority_resort_when_config_unsorted, test_min_count_prefers_first_sufficient …（共 14 个） | 0 |
| [packages/marketdata/tests/test_events_vendor.py](../../packages/marketdata/tests/test_events_vendor.py) | 无模块说明 | test_events_parses_and_filters, test_events_empty | 0 |
| [packages/marketdata/tests/test_flash_news.py](../../packages/marketdata/tests/test_flash_news.py) | 快讯(7×24)vendor 测试:cls/sina/eastmoney 均离线构造样例,monkeypatch 各自模块内 market_get。 | test_cls_sign_is_deterministic, test_cls_parses_and_maps_importance_and_symbols, test_cls_empty_response, test_cls_tolerates_broken_item, test_sina_parses_ext_stocks_and_time, test_sina_empty_response, test_sina_tolerates_broken_ext_json, test_eastmoney_parses_title_summary_time, test_eastmoney_empty_response, test_marketdata_flash_news_single_source | 0 |
| [packages/marketdata/tests/test_fundamentals.py](../../packages/marketdata/tests/test_fundamentals.py) | fundamentals(基本面/财务)vendor + client 方法测试。离线 monkeypatch market_get,不实抓。 | TestTencentFundamentals, TestEastmoneyFundamentalsCN, TestEastmoneyFundamentalsUS, TestEastmoneyFundamentalsHK, TestEastmoneyFundamentalsMisc, test_client_fundamentals_via_single_source_engine, test_client_fundamentals_no_sources_returns_empty | 0 |
| [packages/marketdata/tests/test_http.py](../../packages/marketdata/tests/test_http.py) | 无模块说明 | test_market_get_returns_text, test_market_get_returns_none_on_failure, test_capture_errors_collects_failure_reason, test_capture_errors_empty_on_success, test_record_error_no_sink_is_noop, test_market_get_passes_proxy_when_set, test_market_get_omits_proxy_when_not_set, test_throttle_sleeps_on_second_call | 0 |
| [packages/marketdata/tests/test_index_methods.py](../../packages/marketdata/tests/test_index_methods.py) | 指数 quote/kline:显式符号/secid 专用路径(不经 Symbol.parse,避免 000001 股/指歧义)。 | test_index_quotes, test_index_quotes_falls_back_to_sina_on_tencent_empty, test_index_quotes_tencent_ok_skips_sina, test_index_quotes_partial_fills_only_missing_from_sina, test_index_klines, test_index_klines_unmapped_returns_empty, test_index_klines_us_via_tencent_fallback, test_index_klines_eastmoney_empty_falls_back_to_tencent, test_index_klines_eastmoney_ok_skips_tencent | 0 |
| [packages/marketdata/tests/test_kline_vendors.py](../../packages/marketdata/tests/test_kline_vendors.py) | 无模块说明 | test_tencent_kline_parses, test_tencent_kline_volume_unit_shares, test_eastmoney_kline_parses, test_eastmoney_kline_cn_volume_unit_shares, test_stooq_kline_parses, test_tencent_us_kline_resolves_exchange_suffix | 0 |
| [packages/marketdata/tests/test_market_flow.py](../../packages/marketdata/tests/test_market_flow.py) | 市场/资金面(龙虎榜/融资融券/股东户数/分红)vendor + client 方法测试。 | TestDatacenterGetHelper, TestDragonTiger, TestMargin, TestShareholders, TestDividend, TestClientMethods | 0 |
| [packages/marketdata/tests/test_news.py](../../packages/marketdata/tests/test_news.py) | 新闻资讯 vendor 测试:xueqiu/eastmoney_news/eastmoney 均离线构造样例, | test_xueqiu_parses_item_and_importance, test_xueqiu_symbol_id_prefix_rules, test_xueqiu_no_a_share_symbols_returns_empty, test_xueqiu_waf_blocked_records_error, test_xueqiu_non_json_non_waf_response_returns_empty, test_xueqiu_market_get_failure_returns_empty, test_eastmoney_news_parses_and_uses_names, test_eastmoney_news_falls_back_to_code_without_names, test_eastmoney_news_dedup_across_symbols, test_eastmoney_news_code_not_zero_returns_empty …（共 26 个） | 0 |
| [packages/marketdata/tests/test_northbound.py](../../packages/marketdata/tests/test_northbound.py) | 北向资金(同花顺 hexin)vendor + client 方法测试。 | TestUnwrapPayload, TestSeriesLast, TestToFloatAndSgtValid, TestHexinNorthboundVendor, TestClientMethod | 0 |
| [packages/marketdata/tests/test_ports_defaults.py](../../packages/marketdata/tests/test_ports_defaults.py) | 无模块说明 | test_static_config_returns_sorted_enabled, test_static_config_unknown_type_empty, test_metrics_snapshot, test_metrics_ewma_latency, test_metrics_ewma_none_before_first_record | 0 |
| [packages/marketdata/tests/test_registry.py](../../packages/marketdata/tests/test_registry.py) | PACKAGE_VENDORS_BY_TYPE:包内各数据类型合法 vendor 权威,防与 Engine 实际注册漂移。 | test_package_vendors_by_type_content, test_package_vendors_by_type_matches_actual_engine_registration, test_build_vendors_instantiates_all_registered_classes, test_build_vendors_unknown_datatype_returns_empty | 0 |
| [packages/marketdata/tests/test_sina_quote_vendor.py](../../packages/marketdata/tests/test_sina_quote_vendor.py) | 无模块说明 | test_sina_us_quote, test_sina_hk_quote, test_sina_cn_unsupported, test_sina_index_quotes_cn_hk_us, test_sina_index_quotes_skips_unknown_and_empty | 0 |
| [packages/marketdata/tests/test_smoke.py](../../packages/marketdata/tests/test_smoke.py) | 无模块说明 | test_import_package, test_public_api_exports | 0 |
| [packages/marketdata/tests/test_symbol.py](../../packages/marketdata/tests/test_symbol.py) | 无模块说明 | test_parse_detects_market, test_parse_respects_explicit_market, test_to_tencent, test_to_yfinance | 0 |
| [packages/marketdata/tests/test_tencent_quote.py](../../packages/marketdata/tests/test_tencent_quote.py) | 无模块说明 | test_tencent_parses_quote, test_tencent_empty_content_returns_empty | 0 |
| [packages/marketdata/tests/test_tq_stale_guard.py](../../packages/marketdata/tests/test_tq_stale_guard.py) | TQ 陈旧快照防护单测 (2026-09-04, 09-03 漏数事故)。 | test_fresh_today, test_fresh_yesterday_boundary, test_stale_beyond_floor, test_empty_is_stale, test_dashed_format_accepted, test_today_floor_sanity | 0 |
| [packages/marketdata/tests/test_types.py](../../packages/marketdata/tests/test_types.py) | 无模块说明 | test_request_cache_key_stable, test_response_is_empty, test_quote_defaults_optional_fields_none | 0 |
| [packages/marketdata/tests/test_yahoo_kline.py](../../packages/marketdata/tests/test_yahoo_kline.py) | 无模块说明 | test_yahoo_kline_parses_us, test_yahoo_kline_parses_hk, test_yahoo_kline_prefers_adjclose, test_yahoo_kline_skips_null_bar, test_yahoo_kline_truncates_to_days, test_yahoo_kline_empty_result_returns_empty, test_yahoo_kline_none_response_returns_empty, test_yahoo_kline_no_symbols_returns_empty, test_yahoo_kline_rejects_cn_market, test_yahoo_kline_passes_proxy | 0 |
| [packages/marketdata/tests/test_yfinance_quote.py](../../packages/marketdata/tests/test_yfinance_quote.py) | 无模块说明 | test_yfinance_parses, test_yfinance_missing_lib_raises | 0 |
| [server.py](../../server.py) | PanWatch 统一服务入口 —— W3.2/D1 起为兼容 shim, 实现在 src/bootstrap/。 |  | 0 |
| [src/__init__.py](../../src/__init__.py) | 无模块说明 |  | 0 |
| [src/agents/__init__.py](../../src/agents/__init__.py) | 无模块说明 |  | 0 |
| [src/agents/auction_review.py](../../src/agents/auction_review.py) | 竞价复盘 Agent(9:30 触发):wudao 竞价数据 + 竞价博弈方法论。 | AuctionReviewAgent, json_dumps | 0 |
| [src/agents/base.py](../../src/agents/base.py) | 无模块说明 | apply_scene_binding, resolve_scene_model, PositionInfo, AccountInfo, PortfolioInfo, AgentContext, AnalysisResult, BaseAgent | 0 |
| [src/agents/chart_analyst.py](../../src/agents/chart_analyst.py) | 技术分析 Agent - 多模态 K 线图分析 | ChartAnalystAgent | 0 |
| [src/agents/chat/__init__.py](../../src/agents/chat/__init__.py) | src.agents.chat — 对话助手 thsdk 工具包。 |  | 0 |
| [src/agents/chat/capability_notes.py](../../src/agents/chat/capability_notes.py) | 参谋「当前能力补充」——拼在 SYSTEM_PROMPT 之后的事实清单（2026-10-25）。 |  | 0 |
| [src/agents/chat/registry.py](../../src/agents/chat/registry.py) | chat 工具注册表(W3.3/D3, 2026-09-09): schema 与 handler 同处注册。 | ChatTool, register_chat_tool, chat_tool_schemas, schema_tool_name, no_fallback_tool_names, charge_tool_budget | 0 |
| [src/agents/chat/stock_dossier_aggregator.py](../../src/agents/chat/stock_dossier_aggregator.py) | 标的全面证据档案聚合器 (Stock Full Dossier Aggregator) | classify_news_sentiment, build_stock_full_dossier, format_stock_dossier_text | 0 |
| [src/agents/chat/tools_catalog.py](../../src/agents/chat/tools_catalog.py) | 数据域目录 + 统一查询入口（2026-10-21，数据域架构 P0）。 | catalog_path, load_domains, required_params, render_domain_catalog, render_domain_sections, render_limit_pool, render_lhb, render_theme_mood, render_market_index, render_market_breadth …（共 22 个） | 0 |
| [src/agents/chat/tools_market.py](../../src/agents/chat/tools_market.py) | 数智参谋（Copilot）专属 chat 工具（2026-10-18）。 | register_market_tools | 0 |
| [src/agents/chat/tools_thsdk.py](../../src/agents/chat/tools_thsdk.py) | thsdk 完整能力注册到对话助手 — 11 个工具。 | get_thsdk_news, get_thsdk_corporate_action, get_thsdk_dde, get_thsdk_hs300_constituents, get_thsdk_market_data_cn_extended, get_thsdk_market_data_index, get_thsdk_market_data_hk, get_thsdk_market_data_us, get_thsdk_market_data_bond, get_thsdk_market_data_fund …（共 13 个） | 0 |
| [src/agents/daily_report.py](../../src/agents/daily_report.py) | 无模块说明 | get_market_data, DailyReportAgent | 0 |
| [src/agents/intraday_monitor.py](../../src/agents/intraday_monitor.py) | 盘中监测 Agent - 实时监控持仓，AI 判断是否需要提醒 | get_realtime_volume_ratio, is_market_trading, market_label, build_ma_critical_warnings, evaluate_defense_breach, load_defense_lines, IntradayMonitorAgent | 0 |
| [src/agents/news_digest.py](../../src/agents/news_digest.py) | 新闻速递 Agent - 自选股相关新闻摘要 | NewsDigestAgent | 0 |
| [src/agents/premarket_outlook.py](../../src/agents/premarket_outlook.py) | 盘前分析 Agent - 开盘前展望今日走势 | PremarketOutlookAgent | 0 |
| [src/agents/stock_attribution.py](../../src/agents/stock_attribution.py) | 短线归因 Agent:判断一只股票短线上涨/涨停/异动的核心原因。 | md_quote_rows_wrapper, StockAttributionAgent | 0 |
| [src/agents/theme_launch_detector.py](../../src/agents/theme_launch_detector.py) | 题材启动识别 Agent(9:45 触发)。 | ThemeLaunchDetectorAgent | 0 |
| [src/agents/tradingagents/__init__.py](../../src/agents/tradingagents/__init__.py) | TradingAgents 集成模块。 |  | 0 |
| [src/agents/tradingagents/agent.py](../../src/agents/tradingagents/agent.py) | TradingAgentsAgent — PanWatch 的 BaseAgent 子类,集成 TauricResearch/TradingAgents。 | get_market_data, TradingAgentsUnavailable, TradingAgentsAgent | 0 |
| [src/agents/tradingagents/auto_trigger.py](../../src/agents/tradingagents/auto_trigger.py) | 盘中急涨/急跌联动:自动触发 TradingAgents 深度分析。 | should_auto_trigger, fire_and_forget_trigger, try_auto_trigger | 0 |
| [src/agents/tradingagents/backfill.py](../../src/agents/tradingagents/backfill.py) | 把 analysis_history 里历史 TradingAgents 记录回填到 stock_suggestions。 | backfill_tradingagents_suggestions | 0 |
| [src/agents/tradingagents/cost_tracker.py](../../src/agents/tradingagents/cost_tracker.py) | TradingAgents 月度成本预算与单次成本估算。 | check_budget, estimate_cost, get_today_cache_key | 0 |
| [src/agents/tradingagents/financial_data.py](../../src/agents/tradingagents/financial_data.py) | A 股财务数据采集 — 用 akshare 拉真实财务报表给 TradingAgents 分析师用。 | fetch_financial_abstract, render_fundamentals_summary, render_income_statement, render_balance_sheet, render_cashflow | 0 |
| [src/agents/tradingagents/history_comparison.py](../../src/agents/tradingagents/history_comparison.py) | TradingAgents 历史决策 vs 实际涨跌对比。 | build_history_comparison | 0 |
| [src/agents/tradingagents/langchain_compat.py](../../src/agents/tradingagents/langchain_compat.py) | LangChain 兼容性补丁:让小模型(Qwen 7B 等)返回的 tool_calls 也能通过校验。 | apply_compat_patches | 0 |
| [src/agents/tradingagents/llm_adapter.py](../../src/agents/tradingagents/llm_adapter.py) | 桥接 PanWatch AIClient 配置 → TradingAgents LLM config。 | build_ta_llm_config, inject_api_key_env | 0 |
| [src/agents/tradingagents/paper_trading_bridge.py](../../src/agents/tradingagents/paper_trading_bridge.py) | TradingAgents 决策 → 模拟盘 StrategySignalRun 信号。 | maybe_emit_paper_trading_signal | 0 |
| [src/agents/tradingagents/portfolio_context.py](../../src/agents/tradingagents/portfolio_context.py) | 把 PanWatch 的 PortfolioInfo 渲染成 TradingAgents past_context 文本。 | build_main_intent_context, build_stock_metadata_context, build_portfolio_context, patch_propagator | 0 |
| [src/agents/tradingagents/presets/__init__.py](../../src/agents/tradingagents/presets/__init__.py) | Swarm Preset 路由层 — 8 个 A 股可用的智能体团队预设。 | AgentSpec, PresetConfig, list_presets, get_preset, build_swarm_context, run_preset | 0 |
| [src/agents/tradingagents/progress.py](../../src/agents/tradingagents/progress.py) | TradingAgents 进度回调。 | PanWatchProgressHandler, aggregate_progress | 0 |
| [src/agents/tradingagents/result_mapper.py](../../src/agents/tradingagents/result_mapper.py) | TradingAgents 输出 → PanWatch AnalysisResult 映射。 | map_state_to_result | 0 |
| [src/agents/tradingagents/toolkit_adapter.py](../../src/agents/tradingagents/toolkit_adapter.py) | 把 PanWatch Provider 体系适配进 TradingAgents 数据流。 | panwatch_data_context, is_a_share, is_hk_share, is_panwatch_routable, hk_symbol_to_yfinance, patch_route_to_vendor | 0 |
| [src/bootstrap/__init__.py](../../src/bootstrap/__init__.py) | src.bootstrap — W3.2/D1 从 server.py 拆出的启动装配层。 |  | 0 |
| [src/bootstrap/agents.py](../../src/bootstrap/agents.py) | Agent 注册表 + 装饰器自动发现(W3.2/D1)。 | register_agent, discover_agents, seed_agents | 0 |
| [src/bootstrap/app.py](../../src/bootstrap/app.py) | FastAPI 应用装配(W3.2/D1): agent 自动发现 → lifespan 挂载 → SPA 兜底。 | create_app | 1 |
| [src/bootstrap/cli.py](../../src/bootstrap/cli.py) | 'python server.py' 的启动逻辑(W3.2 shim 瘦身: 从 server.py 搬出, 2026-09-18)。 | run | 0 |
| [src/bootstrap/datasources.py](../../src/bootstrap/datasources.py) | 数据源/策略/示例股票种子与对账(原 server.py 原样搬移, W3.2/D1)。 | seed_sample_stocks, seed_data_sources, reconcile_data_sources, seed_strategies | 0 |
| [src/bootstrap/env.py](../../src/bootstrap/env.py) | 进程环境装配: 代理桥接 / SSL 证书 / 日志 / Playwright。 | apply_proxy_env, setup_proxy, setup_ssl, setup_logging, setup_playwright | 0 |
| [src/bootstrap/runtime.py](../../src/bootstrap/runtime.py) | 运行时核心: 调度器全局实例 + Agent 触发/上下文构建。 | load_watchlist_for_agent, agent_user_buckets, load_portfolio_for_agent, load_portfolio_for_stock, resolve_ai_model, resolve_notify_channels, build_context, build_scheduler, reload_scheduler, get_agent_execution_mode …（共 13 个） | 0 |
| [src/bootstrap/startup.py](../../src/bootstrap/startup.py) | 应用 lifespan: 启动门禁 → init_db → 自检 → 种子 → 调度器编排(原 server.py 搬移)。 | lifespan | 0 |
| [src/collectors/__init__.py](../../src/collectors/__init__.py) | 无模块说明 |  | 0 |
| [src/collectors/auction_collector.py](../../src/collectors/auction_collector.py) | 集合竞价采集器(2026-08-11): 悟道优先 + 腾讯批量行情降级。 | in_async_loop, to_async, fetch_auction_overview, fetch_auction_strongest, fetch_auction_theme, fetch_auction_weak_to_strong, fetch_auction_raw, fetch_auction_snapshots_thsdk, fetch_auction_snapshots_tq, fetch_auction_risk …（共 18 个） | 0 |
| [src/collectors/cancel_rate_sampler.py](../../src/collectors/cancel_rate_sampler.py) | 撤单率时序采样（2026-10-21）。 | targets, collect_once, cancel_trend, run_scheduled | 0 |
| [src/collectors/capital_flow_collector.py](../../src/collectors/capital_flow_collector.py) | 资金流向采集器 - 经 marketdata 包统一接入 | normalize_net_pct, CapitalFlow, get_market_data, CapitalFlowCollector | 0 |
| [src/collectors/discovery_collector.py](../../src/collectors/discovery_collector.py) | 无模块说明 | HotStock, HotBoard, get_market_data, EastMoneyDiscoveryCollector | 0 |
| [src/collectors/eastmoney_research.py](../../src/collectors/eastmoney_research.py) | 东方财富个股研报数据采集器 (Eastmoney Report API) | fetch_eastmoney_research_reports | 0 |
| [src/collectors/events_collector.py](../../src/collectors/events_collector.py) | 无模块说明 | EventItem, fetch_announcement_fulltext, get_market_data, EastMoneyEventsCollector, EventsCollector | 0 |
| [src/collectors/exday_daily.py](../../src/collectors/exday_daily.py) | TQ 'get_exday_data' 四档资金结构 全市场日采集器（2026-10-23）。 | is_market_day, targets, fetch_one, upsert_rows, write_through, collect_once, run_scheduled | 0 |
| [src/collectors/factor_backfill.py](../../src/collectors/factor_backfill.py) | 因子历史回填（2026-10-21）。 | backfill | 0 |
| [src/collectors/factor_daily.py](../../src/collectors/factor_daily.py) | 个股因子日快照（2026-10-21）。 | compute_factors, collect_once | 0 |
| [src/collectors/index_daily.py](../../src/collectors/index_daily.py) | 指数日K落库(2026-10-18 P0-b, 方案 §7.2⑤ / §8 批次 2)。 | upsert_bars, load_recent_closes, history, count_rows, backfill, run_index_daily_job | 0 |
| [src/collectors/kline_collector.py](../../src/collectors/kline_collector.py) | K线和技术指标采集器 - 基于腾讯 API（更稳定） | get_market_data, clear_kline_cache, get_index_klines, KlineData, TechnicalIndicators, KlineCollector | 0 |
| [src/collectors/klines_ingestor.py](../../src/collectors/klines_ingestor.py) | K线后台入库 worker (2026-08-17, 2026-09-08 风险方案1.2/B1 重写) | validate_bar, ingest_symbol, ingest_batch, get_default_symbols, main_async, main | 0 |
| [src/collectors/lhb_tq_backfill.py](../../src/collectors/lhb_tq_backfill.py) | 龙虎榜金额「次日 TQ 回填」（2026-10-20 用户要求）。 | merge_rows, backfill, run_scheduled | 0 |
| [src/collectors/market_breadth_series.py](../../src/collectors/market_breadth_series.py) | 市场广度日序列 —— 从 PG klines 自算并落库（零网关配额）。 | load_bars_from_pg, sync_breadth_series, latest_with_percentile, render_text | 0 |
| [src/collectors/market_http.py](../../src/collectors/market_http.py) | 行情/数据采集的统一 HTTP 工具。 | fetch_source, source_suffix, throttle, market_get, TTLCache | 0 |
| [src/collectors/market_sentiment_collector.py](../../src/collectors/market_sentiment_collector.py) | 市场情绪采集器:涨停池 + 涨跌家数统计 + 连板梯队。 | MarketSentimentCollector | 0 |
| [src/collectors/news_collector.py](../../src/collectors/news_collector.py) | 新闻数据结构 + 聚合采集器薄 shim。 | NewsItem, NewsCollector | 0 |
| [src/collectors/screenshot_collector.py](../../src/collectors/screenshot_collector.py) | K线图截图采集器 - 基于 Playwright | ChartScreenshot, ScreenshotCollector | 0 |
| [src/collectors/stock_list.py](../../src/collectors/stock_list.py) | 股票列表缓存与模糊搜索 | refresh_stock_list, get_stock_list, search_stocks | 0 |
| [src/collectors/tdx_collector.py](../../src/collectors/tdx_collector.py) | 通达信问小达投研数据采集器(盘前/盘后 Agent 用)。 | collect_wenda, TdxWendaCollector | 0 |
| [src/collectors/tdx_local_parser.py](../../src/collectors/tdx_local_parser.py) | TDX (TongDaXin) local file parser for: | parse_northbound_positions, parse_concept_membership, load_datacfg_mapping | 0 |
| [src/collectors/tq_formula_batch.py](../../src/collectors/tq_formula_batch.py) | TQ 条件选股公式 —— **按需通用批量执行引擎**(2026-10-02)。 | run_formula_batch | 0 |
| [src/collectors/tq_formula_signals.py](../../src/collectors/tq_formula_signals.py) | TQ 条件选股信号日序列采集器(2026-09-25)。 | fetch_formula_signals, sync_formula_signals, latest_with_baseline | 0 |
| [src/collectors/tq_market_flow.py](../../src/collectors/tq_market_flow.py) | 全市场成交额/主力净流入 —— 通达信 TQ 原生公式(AMO/SUPAMO)汇总，落 market_flow_snapshots。 | market_totals, write_snapshot, run_scheduled, collect_once, last_run | 0 |
| [src/collectors/tq_sentiment_series.py](../../src/collectors/tq_sentiment_series.py) | TQ 专业序列 → 市场情绪日序列落库(SCJYVALUE 编号空间)。 | fetch_sentiment_series, sync_sentiment_series, latest_with_baseline | 0 |
| [src/collectors/wencai.py](../../src/collectors/wencai.py) | 问财 NLP 自然语言选股接口。 | convert_symbol, run_wencai | 0 |
| [src/collectors/wudao_mcp_client.py](../../src/collectors/wudao_mcp_client.py) | wudao MCP 竞价数据采集器(HTTP 直连,免 Hermes)。 | WudaoMCPClient | 0 |
| [src/config.py](../../src/config.py) | 无模块说明 | Settings, StockConfig, AppConfig, load_watchlist, load_config | 0 |
| [src/core/__init__.py](../../src/core/__init__.py) | 无模块说明 |  | 0 |
| [src/core/abnormal_moves.py](../../src/core/abnormal_moves.py) | Exchange abnormal-move rule monitor (Task C, 2026-08-24). | board_of, benchmark_for, AbnormalRule, rules_for, cumulative_change, compute_deviation, status_of, proximity_of, worst_window, analyze_abnormal_moves …（共 12 个） | 0 |
| [src/core/adjust.py](../../src/core/adjust.py) | 除权除息因子(B1.4/KI-040)。 | upsert_adj_factor, load_adj_factors, adjust_close, to_qfq_series | 0 |
| [src/core/agent_catalog.py](../../src/core/agent_catalog.py) | Agent catalog and kind helpers. | infer_agent_kind, is_workflow_agent, is_capability_agent, AgentSeedSpec | 0 |
| [src/core/agent_runs.py](../../src/core/agent_runs.py) | Agent 运行记录 - 写入 agent_runs 表（供 UI 查询） | record_agent_run | 0 |
| [src/core/ai_activity.py](../../src/core/ai_activity.py) | AI 机构活跃度(阶段1 五件套 ④)。 | eval_activity, activity_of_value, activity_series | 0 |
| [src/core/ai_client.py](../../src/core/ai_client.py) | 无模块说明 | LLMDegradedError, GlobalLLMCircuitBreaker, AIClient, get_model_for_scene, with_compliance, build_system_prompt, resolve_scene_model_with_source | 0 |
| [src/core/ai_vision_pattern.py](../../src/core/ai_vision_pattern.py) | AI 看图形态 **影子证据层**(2026-10-14 胜率创新 · 主线一并行)。 | render_kline_png, build_vision_prompt, parse_vision_result, compare_with_rules, build_rows, recognize_from_bars, resolve_vision_model, make_vision_provider, shadow_universe, mark_rows …（共 14 个） | 0 |
| [src/core/alerting.py](../../src/core/alerting.py) | 运维告警体系 (2026-09-18 tier1-compliance)。 | AlertLevel, get_data_dir, AlertMessage, AlertManager, get_alert_manager, set_alert_manager, record_http_status, reset_5xx_window, record_llm_429, reset_llm_429_window …（共 18 个） | 0 |
| [src/core/ambush_score.py](../../src/core/ambush_score.py) | 埋伏四维评分合成(批次C C2, 2026-09-06 28号)。 | score_event, score_transmission, demon_boost, score_mood, score_signal, risk_deduction, build_invalidations, enrich_ambush_list | 0 |
| [src/core/analog_finder.py](../../src/core/analog_finder.py) | 历史相似分形对标（2026-10-23）。 | find_analogs, render, build | 0 |
| [src/core/analysis_history.py](../../src/core/analysis_history.py) | 分析历史记录管理 | save_analysis, get_analysis, get_latest_analysis, get_analysis_history, get_latest_ta_verdict_row, get_latest_ta_verdict | 0 |
| [src/core/analysis_link.py](../../src/core/analysis_link.py) | PanWatch 内部页面链接生成：深度分析详情页等。 | get_base_url, analysis_detail_url, analysis_detail_markdown | 0 |
| [src/core/apm.py](../../src/core/apm.py) | APM 轻量追踪 (P1 稳定性, 2026-09-18)。 | apm_slow_threshold_ms, apm_sample_rate, apm_enabled, new_trace_id, get_trace_id, set_trace_id, clear_trace_id, trace_scope, get_apm_stats, record_op …（共 15 个） | 0 |
| [src/core/auction_pool.py](../../src/core/auction_pool.py) | 竞价异动池 v0.3.2 (2026-08-24, 修正 gap_pct/withdraw_rate 计算口径)。 | clear_cache, normalize_auction_volume_ratio, fetch_auction_anomaly, fetch_auction_payload, sync_auction_to_db, get_anomaly_history, register_cron | 0 |
| [src/core/auction_tick_sampler.py](../../src/core/auction_tick_sampler.py) | 竞价过程帧采样器(2026-10-18 P0-a, 方案 §7.2② / §8 批次 1)。 | limit_threshold, classify_verdict, withdraw_rate, frame_from_snapshot, upsert_frames, load_frames, replay, purge_old_frames, fetch_frame_rows, pool_symbols …（共 12 个） | 0 |
| [src/core/auction_wudao_store.py](../../src/core/auction_wudao_store.py) | 悟道竞价多维因子结构化落库(2026-10-18 P0-a, 方案 §7.2③)。 | normalize_row, extract_rows, upsert_daily, load_daily, capture_daily, run_auction_wudao_job | 0 |
| [src/core/auth_tokens.py](../../src/core/auth_tokens.py) | JWT 令牌与密钥(中立层, KI-039 第二阶段, 2026-09-09)。 | invalidate_jwt_secret_cache, get_jwt_secret, get_verification_secrets, TokenResult, create_token, session_id_from_payload, decode_token, principal_from_payload | 0 |
| [src/core/backfill_guard.py](../../src/core/backfill_guard.py) | 回填防污染契约(v0.5.80 B3)。 | check_ident, coverage_gate, count_by_field, purge_rows_outside | 0 |
| [src/core/backtest/__init__.py](../../src/core/backtest/__init__.py) | PanWatch 回测模块(Phase 0 地基)。 |  | 0 |
| [src/core/backtest/cost_model.py](../../src/core/backtest/cost_model.py) | A 股交易成本模型 —— 回测(Phase 0)与模拟盘(Phase 1)共用。 | CostConfig, Fill, CostModel | 0 |
| [src/core/backtest/data_adapter.py](../../src/core/backtest/data_adapter.py) | 回测数据适配:PG klines(优先) → KlineCollector(兜底) → PriceBar。 | PriceBar, from_klines, load_price_history, first_index_after | 0 |
| [src/core/backtest/engine.py](../../src/core/backtest/engine.py) | 轻量事件式回测内核(纯 Python,无第三方依赖)。 | Signal, BTTrade, BacktestResult, simulate_exit, fixed_cash_sizer, Backtester, horizon_return | 0 |
| [src/core/backtest/metrics.py](../../src/core/backtest/metrics.py) | 回测绩效指标 —— 纯函数,仅依赖标准库。 | validate_equity_curve, daily_returns, total_return, annualized_return, max_drawdown, sharpe, sortino, kalmar, turnover, win_rate …（共 13 个） | 0 |
| [src/core/backtest/portfolio.py](../../src/core/backtest/portfolio.py) | 组合级回测撮合(B2.1)。 | PortfolioConfig, PortfolioBacktester | 0 |
| [src/core/backtest/research.py](../../src/core/backtest/research.py) | 参数扫描 + 滚动(walk-forward)验证(B2.2/B2.3)。 | golden_cross_signals, run_once, sweep, walk_forward | 0 |
| [src/core/backtest/service.py](../../src/core/backtest/service.py) | 回测服务(B2.4/KI-039): 把内核接成"给标的+区间就出结果"的可调用入口。 | run_backtest | 0 |
| [src/core/backtest_lab.py](../../src/core/backtest_lab.py) | 量化因子多维胜率回测与持久化实验室（2026-10-23）。 | evaluate_bars_universe, upsert_winrate_rows, load_bars_from_pg, run_and_persist, load_winrates, render, run_scheduled | 0 |
| [src/core/beneficiary_resolver.py](../../src/core/beneficiary_resolver.py) | 受益名 → 交易代码落地(2026-09-05, v0.5.6 CKPT4)。 | resolve_beneficiaries | 0 |
| [src/core/board_flow_sampler.py](../../src/core/board_flow_sampler.py) | 板块资金采样落库(2026-10-14, 资金类落库收口)。 | board_to_item, write_snapshot, try_write_async, read_fresh, collect_once | 0 |
| [src/core/board_snapshot.py](../../src/core/board_snapshot.py) | 腾讯五档盘口快照采样 + 托单/压单识别(算法4, 2026-08-14 新建, 未部署)。 | sample_board, get_board_manipulation | 0 |
| [src/core/caliber.py](../../src/core/caliber.py) | 资金类指标口径契约(风险整改 3.4/B3)。 | CaliberTag, CaliberViolationError, require_directional, reconcile_direction | 0 |
| [src/core/caliber_diff.py](../../src/core/caliber_diff.py) | 口径成对差异解释器(P2-1, 2026-09-18)。 | PairDiff, compare_pair, build_pair_diffs, conclusion_level | 0 |
| [src/core/cancel_order_intent.py](../../src/core/cancel_order_intent.py) | 高频撤单意图分析引擎 (Cancel Order Intent Engine). | baseline_coords, calc_cancel_order_intent | 0 |
| [src/core/cancel_profile.py](../../src/core/cancel_profile.py) | I-3 委托行为画像: 撤单率历史基线 + 动态 Z-Score 坐标系. | norm_date, cancel_rate, normalize_exday_row, describe, percentile_rank, zscore, classify, conclusion_text, build_cancel_profile, fetch_cancel_profile | 0 |
| [src/core/catalyst_calendar.py](../../src/core/catalyst_calendar.py) | 未来催化日历(2026-09-05, v0.5.6 CKPT1)。 | get_calendar | 0 |
| [src/core/catalyst_screener.py](../../src/core/catalyst_screener.py) | 埋伏漏斗(2026-09-05, v0.5.6 CKPT3)。 | build_ambush_list, events_to_calendar | 0 |
| [src/core/cb_radar.py](../../src/core/cb_radar.py) | 可转债 - 正股联动套利与折溢价雷达(I-4, 2026-10-18)。 | clear_terms_cache, limit_up_threshold, compute_metrics, classify_scenarios, build_rows, build_radar, build_one, get_radar, run_cb_daily_job | 0 |
| [src/core/cb_radar_store.py](../../src/core/cb_radar_store.py) | 可转债条款与每日折溢价的持久化 / 读穿透层(2026-10-18 P0-a, 方案 §7.2④)。 | term_columns, upsert_terms, upsert_daily_rows, load_terms, terms_are_fresh, latest_trade_date, load_daily_rows, persist_radar, purge_old_snapshots | 0 |
| [src/core/chart_notes.py](../../src/core/chart_notes.py) | 图表说明(看图决策产品件, 2026-10-14): 逐标记「为什么」——人可读 / AI 可引。 | build_note, build_chart_notes, note_sentence, chart_notes_sentence, pick_text, gs_state_note, gs_state_text | 0 |
| [src/core/chip_distribution.py](../../src/core/chip_distribution.py) | 筹码分布计算器(成本分布 CYQ) — 对齐通达信/同花顺口径。 | fetch_sina_hist_price, compute_chips_sina, fetch_tencent_price_dist, compute_near_term_chips, compute_chips, compute_main_accumulation_chips | 0 |
| [src/core/cn_gateway.py](../../src/core/cn_gateway.py) | 国内数据网关地址的唯一事实源(2026-09-26)。 | gateway_url | 0 |
| [src/core/cn_gateway_health.py](../../src/core/cn_gateway_health.py) | 国内数据网关存活监控(2026-09-27)。 | probe, GatewayWatch | 0 |
| [src/core/cn_symbol.py](../../src/core/cn_symbol.py) | CN symbol helpers. | get_cn_exchange, get_cn_prefix, is_cn_sh | 0 |
| [src/core/commodity_quotes.py](../../src/core/commodity_quotes.py) | 大宗商品期货主力连续行情(批次C C5, 2026-09-06 28号)。 | fetch_snapshot, fetch_daily_close, momentum_score | 0 |
| [src/core/commodity_rotation.py](../../src/core/commodity_rotation.py) | 大宗商品轮动前瞻(2026-08-10 同花顺学习文《大宗商品的轮动顺序》)。 | detect_rotation_stage, format_rotation | 0 |
| [src/core/confidence.py](../../src/core/confidence.py) | 双源/多源置信度徽章(批次A A4, 2026-09-06 28号)。 | level_of, payload | 0 |
| [src/core/context_builder.py](../../src/core/context_builder.py) | 无模块说明 | ContextBuilder | 0 |
| [src/core/context_scheduler.py](../../src/core/context_scheduler.py) | 上下文维护调度器：后验评估 + 过期数据清理 + 机会自动刷新。 | ContextMaintenanceScheduler | 0 |
| [src/core/context_store.py](../../src/core/context_store.py) | 无模块说明 | save_stock_context_snapshot, get_recent_stock_context_snapshots, save_news_topic_snapshot, get_latest_news_topic_snapshot, save_agent_context_run, list_recent_agent_context_runs, save_agent_prediction_outcome, mark_agent_prediction_outcome, list_pending_prediction_outcomes, list_agent_prediction_outcomes …（共 11 个） | 0 |
| [src/core/copilot_brief.py](../../src/core/copilot_brief.py) | src/core/copilot_brief.py | build_copilot_brief | 0 |
| [src/core/copilot_memory.py](../../src/core/copilot_memory.py) | 数智参谋的长期记忆（2026-10-21, P0）。 | extract_candidates, remember, recall, format_block, memory_block, list_all, set_enabled, update, delete | 0 |
| [src/core/copilot_model_prefs.py](../../src/core/copilot_model_prefs.py) | 参谋/对话场景的「用户显式模型选择」存取（2026-10-25）。 | get_pref, set_pref, clear_pref | 0 |
| [src/core/copilot_quota.py](../../src/core/copilot_quota.py) | 多用户 AI 配额与用量统计（2026-10-20, P3 治理）。 | get_quota, set_quota, usage_today, check, error_message, enforce, audit_limit_hit, active_run_count, enforce_run_concurrency, run_tool_calls …（共 13 个） | 0 |
| [src/core/copilot_runtime.py](../../src/core/copilot_runtime.py) | 数智参谋 Durable Run 运行时（P0-0.1，2026-10-25）。 | is_terminal, is_active, can_transition, stale_cutoff, takeover_error, reap_stale_runs, heartbeat | 0 |
| [src/core/copilot_schedule.py](../../src/core/copilot_schedule.py) | 会话里创建的定时任务（2026-10-21, P0）。 | create, list_all, set_enabled, delete, run_due | 0 |
| [src/core/dark_flow.py](../../src/core/dark_flow.py) | 暗盘资金计算器 v5(2026-08-11 优化版, 截图验证对齐同花顺结构)。 | tick_snapshot_info, refresh_ticks, clear_ticks_cache, compute_dark_flow, compute_tck_active_ratio | 0 |
| [src/core/dark_flow_fusion.py](../../src/core/dark_flow_fusion.py) | 暗盘资金「通达信 .tck + 同花顺 thsdk」**融合** —— 决策先锋暗盘的完整解。 | active_net_from_tck, split_clusters_from_orders, compute_dark_fusion | 0 |
| [src/core/dark_flow_l2.py](../../src/core/dark_flow_l2.py) | 暗盘资金(L2 逐笔版)—— 决策先锋"主力暗盘资金"的主线实现。 | compute_dark_flow_l2 | 0 |
| [src/core/dark_flow_sampler.py](../../src/core/dark_flow_sampler.py) | 暗盘逐笔后台增量采集器(2026-10-14, 冷启动移出请求路径)。 | register_code, tracked_codes, clear_tracking, universe_codes, sampler_interval_s, sampler_stats, collect_once | 0 |
| [src/core/dark_fund_scan.py](../../src/core/dark_fund_scan.py) | 全市场暗盘资金 TOP 扫描(设计稿 §6.1「暗盘资金 TOP 榜 .tck/thsdk」, A6, 2026-09-01)。 | scan_dark_fund_top, scan_dde_universe, attach_tck_dark | 0 |
| [src/core/dark_l2.py](../../src/core/dark_l2.py) | L2 逐笔数据源(thsdk 同花顺 + 通达信 .tck, 双实现) | fetch_l2_ticks | 0 |
| [src/core/dark_pool_flow.py](../../src/core/dark_pool_flow.py) | 主力资金聚合: 明盘(权威) + 暗盘(L1 近似) = 主力净额。 | compute_pool_flow, compute_pool_flow_series, fund_flow_cross | 0 |
| [src/core/dark_split.py](../../src/core/dark_split.py) | 暗盘 / 明盘 拆分 + big_order_flow 方向解码(纯函数层, 无 IO, 可单测)。 | FlowSplit, decode_direction, split_ming_dark, active_passive_net, ming_net_from_big_orders, find_tck_file, dark_flow_from_tck | 0 |
| [src/core/darkflow_alerts.py](../../src/core/darkflow_alerts.py) | 主力意图异常告警(#5, 2026-09-04)。 | should_alert, maybe_alert_anomaly | 0 |
| [src/core/data_capabilities.py](../../src/core/data_capabilities.py) | 数据集能力视图(2026-09-12, 借鉴 tick-stock-panel 的能力路由矩阵)。 | verdict_source, classify, build_capabilities | 0 |
| [src/core/data_collector.py](../../src/core/data_collector.py) | 统一数据源管理器 | CollectorResult, CollectorLog, DataCollectorManager, get_collector_manager | 0 |
| [src/core/data_impact.py](../../src/core/data_impact.py) | 数据集「影响面」映射(P2-3, 2026-09-18)。 | impact_of | 0 |
| [src/core/data_quality_sentinel.py](../../src/core/data_quality_sentinel.py) | 数据质量哨兵(2026-08-21)。 | reset_tq_gateway_streak, run_dq_checks, register_hourly_job | 0 |
| [src/core/datasource_failures.py](../../src/core/datasource_failures.py) | 数据源失败明细(B1.6/KI-040)。 | record, recent, reset_dedupe | 0 |
| [src/core/db_backup_auto.py](../../src/core/db_backup_auto.py) | 数据库自动备份/恢复核心 (2026-09-18 tier1-compliance)。 | get_data_dir, get_backup_dir, BackupResult, verify_gzip_file, list_backups, cleanup_old_backups, run_backup, restore_backup, register_daily_backup_job | 0 |
| [src/core/db_safe.py](../../src/core/db_safe.py) | 事务自愈: 吞异常之前先把 PostgreSQL 的 aborted 事务清掉。 | safe_rollback | 0 |
| [src/core/dde_sampler.py](../../src/core/dde_sampler.py) | 逐 N 分钟 DDE 大单净流入采样落库(P3 补差 A, 2026-10-10)。 | sample_cadence_min, sample_dde_once, run_dde_sample_job | 0 |
| [src/core/decision.py](../../src/core/decision.py) | 决策合成(2026-09-08 方向2): 三信号 → 动手/看看/别碰 + 一行理由。 | synthesize, synthesize_two_dimension, personalize, decide | 0 |
| [src/core/decision_backtest.py](../../src/core/decision_backtest.py) | 数智决策「三指标共振」回测 —— 对照官方 2024.10-2025.10 基准。 | backtest_resonance | 0 |
| [src/core/decision_cache.py](../../src/core/decision_cache.py) | 决策合成缓存层(2026-10-10 冗余设计): '/api/decision/{symbol}' 预落库 + 读时个性化。 | now_utc_naive, iso_utc, precompute_ttl_s, compute_base, apply_user_overlay, get_cached_decision, put_cached_decision, clear_decision_cache | 0 |
| [src/core/decision_log.py](../../src/core/decision_log.py) | 决策日志(B6, 2026-09-18) —— 把"信号 → 结果"变成可统计的样本。 | record_signal, record_many, record_many_safe, backfill_outcomes, backfill_spans, stats, query_log, backfill_runner, spawn_backfill, backfill_daily_job …（共 15 个） | 0 |
| [src/core/decision_pioneer.py](../../src/core/decision_pioneer.py) | 数智决策三指标复刻 + 主力意图 L2 增强 (2026-08-30, 周一开盘前上线)。 | compute_institution_activity, compute_gs_signal, compute_gs_windows, fetch_bars, fetch_tq_l2, fetch_decision_pioneer, decision_pioneer_text | 0 |
| [src/core/decision_precompute.py](../../src/core/decision_precompute.py) | 决策合成批算调度(2026-10-10 冗余设计): 自选/持仓标的**盘后批算落库**。 | collect_targets, precompute, DecisionPrecomputeScheduler, run_precompute_job | 0 |
| [src/core/delegate.py](../../src/core/delegate.py) | 委派子 run（P1-1.1, 2026-10-25）。 | create_child_run, delegate, children_of, aggregate, delegate_many | 0 |
| [src/core/delta_engine.py](../../src/core/delta_engine.py) | 秒级 Delta 序列引擎(2026-08-19) | compute_delta_series | 0 |
| [src/core/demo_limit.py](../../src/core/demo_limit.py) | demo 账号限流 — 防止公开演示账号烧共享模型 API key。 | allow, remaining, get_hourly_limit, allow_api_get | 0 |
| [src/core/demon_factors.py](../../src/core/demon_factors.py) | 妖股因子存档与增量管线(批次B 存档化, 2026-09-06 28号, 老板要求)。 | set_lhb_stats_hook, backfill_direct_tq, backfill_incremental, recompute_factors, update_pipeline, load_factor_pool, top_demon_factors | 0 |
| [src/core/demon_score.py](../../src/core/demon_score.py) | 妖股评分/股性雷达(批次B, 2026-09-06 28号)。 | lianban_score, seal_quality_score, recent_activity, demon_score_from_events | 0 |
| [src/core/disk_cache.py](../../src/core/disk_cache.py) | 磁盘持久化缓存层(2026-08-12): 解决"容器重启后内存缓存全丢, 首次请求慢"。 | DiskCache, register, flush_all | 0 |
| [src/core/doctor.py](../../src/core/doctor.py) | 命令行系统自检:'python -m src.core.doctor' 或 'make doctor'。 | main | 0 |
| [src/core/entry_candidates.py](../../src/core/entry_candidates.py) | 无模块说明 | refresh_entry_candidates, list_entry_candidates, record_manual_query_candidates, save_entry_candidate_feedback, list_entry_candidate_feedback, evaluate_entry_candidate_outcomes, count_missing_candidate_outcomes, get_entry_candidate_stats, entry_outcomes_summary | 0 |
| [src/core/error_tracker.py](../../src/core/error_tracker.py) | 轻量错误追踪 (2026-08-21)。 | capture_exception, recent_errors, install_error_tracker, missed_counts, install_scheduler_error_tracking, configure | 0 |
| [src/core/event_catalyst_engine.py](../../src/core/event_catalyst_engine.py) | 事件驱动预期差引擎(2026-08-22)。 | build_catalyst_prompt, parse_catalyst_reply, analyze_event_catalyst | 0 |
| [src/core/evidence_chain.py](../../src/core/evidence_chain.py) | AI 建议证据化(2026-10-10): 结论必带 证据链 + 失效条件 + 置信度账本校准 + 历史相似情形。 | build_evidence_chain, calibrate_confidence, historical_similarity, load_stats, build_suggestion_evidence, build_decision_evidence | 0 |
| [src/core/exday_structure.py](../../src/core/exday_structure.py) | 通达信 TQ 'get_exday_data' 四档资金结构解析与因子（2026-10-23 实测标定）。 | parse_matrix, compute, fetch, to_row, query_series, render | 0 |
| [src/core/experiment_log.py](../../src/core/experiment_log.py) | 实验日志(B6.7): 记录每次回测/研究的参数、数据指纹与指标, 支持事后复现。 | log_experiment, list_experiments, find_experiment, reproduce_command | 0 |
| [src/core/factor_calibration.py](../../src/core/factor_calibration.py) | 因子自校准(M2):把孤立的 IC/IR 接进因子权重的轻量标定闭环。 | compute_target, blend, calibrate_factor_weights, calibrate_all_markets | 0 |
| [src/core/factor_eval.py](../../src/core/factor_eval.py) | 因子有效性评估(Phase 2):横截面 IC / IR。 | pearson, spearman, evaluate_factor_ic | 0 |
| [src/core/factor_ic.py](../../src/core/factor_ic.py) | 因子 IC/IR（2026-10-21，本地计算版）。 | compute, render, winsorize_cross_section, orthogonalize_cross_section, quantile_backtest_core, quantile_backtest, render_quantile_backtest | 0 |
| [src/core/factor_ic_history.py](../../src/core/factor_ic_history.py) | 因子 IC 每日快照(时序落库) —— B9 数据资产 E1 收口(2026-09-19)。 | snapshot_ic, render_latest, history, register_daily_job | 0 |
| [src/core/factor_ic_report.py](../../src/core/factor_ic_report.py) | 因子 IC 归因报告:把 IC/IR 喂给 LLM,输出归因解读 + 调权建议(自然语言)。 | build_ic_report_prompt, parse_ic_report_reply, generate_factor_ic_report | 0 |
| [src/core/factor_lab.py](../../src/core/factor_lab.py) | 因子工厂核心(B6.1): 因子注册表 + 横截面分层回测。 | FactorSpec, register_factor, factor_registry, factor_fn, cross_section_quantiles, factor_summary | 0 |
| [src/core/factor_weights.py](../../src/core/factor_weights.py) | 因子权重存取层(M1)。 | get_factor_weights, get_all_factor_weights, set_factor_weight | 0 |
| [src/core/fitness_keypoints.py](../../src/core/fitness_keypoints.py) | 关键位兑现（F3）—— A 股版的「延迟到账真值」（2026-10-23，受控 RSI 证据层 S1）。 | extract_levels, score_run, aggregate | 0 |
| [src/core/forecast_outcome.py](../../src/core/forecast_outcome.py) | 预测历史「到期对照」应用侧 enrichment(2026-09-29)。 | evaluate_history_item, merge_outcome, enrich_history_outcomes | 0 |
| [src/core/free_tier.py](../../src/core/free_tier.py) | 免费档("免费级别")运行时可调配置 —— 2026-09-18 老板需求。 | invalidate_cache, cached_config, get_config, save_config, trial_features, trial_daily_limit, member_watchlist_max, member_alert_max, skill_tier_min, max_sessions_per_user …（共 12 个） | 0 |
| [src/core/fund_flow_nd.py](../../src/core/fund_flow_nd.py) | 主力资金 1/3/5 日 + 0 轴多空判定(数智决策官方口径)。 | ming_history_tq, daily_dark_series, zero_axis_cross, fund_flow_nd | 0 |
| [src/core/gap_collector.py](../../src/core/gap_collector.py) | 分析维度缺口采集（确定性后置解析，2026-10-22 第三次改道）。 | should_check, build_user_content, parse_gaps, collect, spawn | 0 |
| [src/core/gap_study.py](../../src/core/gap_study.py) | 高开幅度 → 后续收益 的实证分档(v0.5.80 A7, 借鉴 tick-stock-panel 的"把回测结论标在盘前榜单上")。 | bucket_of, is_unfillable, summarize, run_study | 0 |
| [src/core/global_indices.py](../../src/core/global_indices.py) | 全球隔夜市场指数采集(yahoo finance 免费 HTTP, 无 key)。 | fetch_global_indices, clear_cache | 0 |
| [src/core/gs_state.py](../../src/core/gs_state.py) | GS 状态单一事实源(只消费 'gs_strategy' 输出; 'gs_strategy' 本体禁改)。 | resolve_gs_state, trend_label | 0 |
| [src/core/gs_strategy.py](../../src/core/gs_strategy.py) | GS 信号(阶段1 五件套 ①)。 | eval_gs, merge_whipsaw, compute_gs_signals, trend_label | 0 |
| [src/core/halts.py](../../src/core/halts.py) | 停牌区间(B1.5/KI-040)。 | upsert_halt, is_halted, load_halts | 0 |
| [src/core/harness_experiments.py](../../src/core/harness_experiments.py) | 实验记录（P0-0.5）：把「改了什么 → 指标怎么变」落库，供人工 accept/reject。 | record, record_compare, list_recent, verdict_stats | 0 |
| [src/core/harness_proposals.py](../../src/core/harness_proposals.py) | 新分析维度提案（2026-10-22, P0 采集层）：**只记录，不生效**。 | match_candidate_tools, normalize_dimension, propose, list_proposals, decide, stats, summary | 0 |
| [src/core/harness_replay.py](../../src/core/harness_replay.py) | 离线复盘（S0）—— 只提建议，绝不改配置（2026-10-23，受控 RSI 证据层）。 | collect_signals, propose, render | 0 |
| [src/core/history_store.py](../../src/core/history_store.py) | 历史落库: 数智决策快照 + L2 逐笔(回测回查用)。 | record_dp_snapshot, query_dp_history, persist_l2_ticks, query_l2_ticks | 0 |
| [src/core/hv_api_log.py](../../src/core/hv_api_log.py) | 高价值接口调用日志(2026-09-15 C.1)。 | log_high_value_call, check_spike_and_alert | 0 |
| [src/core/img_orderbook_store.py](../../src/core/img_orderbook_store.py) | '.img' 十档盘口帧落库(入库层, 2026-10-02)。 | trade_date_from_path, snapshot_to_row, persist_frames, ingest_img_file, load_frames, latest_frame | 0 |
| [src/core/index_codes.py](../../src/core/index_codes.py) | 指数代码表（单一事实源，2026-10-25）。 | is_index_code, index_name | 0 |
| [src/core/index_daily.py](../../src/core/index_daily.py) | 指数日线读取（index_daily，2026-10-25）。 | load_bars | 0 |
| [src/core/indicator_calibration.py](../../src/core/indicator_calibration.py) | 每日跨源指标标定作业(Volume 单位事故教训, 2026-10-10)。 | Observation, SymbolVerdict, calibrate_observations, collect_observations, run_indicator_calibration, daily_job | 0 |
| [src/core/indicators.py](../../src/core/indicators.py) | 统一技术指标库(B4.3/KI-037)。 | sma, ema_series, true_ranges, atr, atr_wilder, macd, rsi, rsi_wilder, kdj, boll | 0 |
| [src/core/intent_explain.py](../../src/core/intent_explain.py) | 主力意图 AI 解释层(规则预判 + AI 解释双层设计)。 | build_explain_prompt, parse_explain_reply, build_intent_evidence, explain_main_intent | 0 |
| [src/core/intraday_event_gate.py](../../src/core/intraday_event_gate.py) | Event-driven gate for intraday monitor. | adaptive_price_threshold, is_abnormal_move, EventDecision, check_and_update | 0 |
| [src/core/intraday_profile.py](../../src/core/intraday_profile.py) | 分时画像（2026-10-21）：把"分时图怎么读"变成可复算的数字，而不是让模型看图猜。 | parse_qt, compute, render, fetch_qt, fetch_minute, build | 0 |
| [src/core/invite_codes.py](../../src/core/invite_codes.py) | 邀请码注册（2026-09-19 用户拍板：内部使用 + 邀请制）。 | InviteCodeError, mask_code, generate_code, normalize_code, create_invite_code, list_invite_codes, disable_invite_code, redeem, list_uses, get_register_mode …（共 11 个） | 0 |
| [src/core/jbp_pool.py](../../src/core/jbp_pool.py) | 聚宝盆选股(数智决策 规格 §4.2 官方选股流程)。 | evaluate_one, screen | 0 |
| [src/core/jobs.py](../../src/core/jobs.py) | 长任务作业框架(2026-09-12, 借鉴 tick-stock-panel 的 pipeline job 设计)。 | result_failure, JobStore | 0 |
| [src/core/json_safe.py](../../src/core/json_safe.py) | 无模块说明 | to_jsonable | 0 |
| [src/core/json_store.py](../../src/core/json_store.py) | Tiny JSON file store with atomic writes. | read_json, write_json_atomic | 0 |
| [src/core/kline_backfill_scheduler.py](../../src/core/kline_backfill_scheduler.py) | K线每日 backfill 调度器(2026-08-17) | KlineBackfillScheduler | 0 |
| [src/core/kline_context.py](../../src/core/kline_context.py) | 无模块说明 | build_kline_history_context | 0 |
| [src/core/kline_pattern.py](../../src/core/kline_pattern.py) | K线组合形态识别(2026-08-10,基于同花顺教学文《K线经典形态》《K线形态大全》)。 | PatternHit, detect_patterns, format_patterns | 0 |
| [src/core/kline_patterns.py](../../src/core/kline_patterns.py) | K线形态识别(严格规则版) —— 供 **K 线图层标注** 使用。 | Candle, PatternMark, pattern_catalog, detect_pattern_marks, format_marks | 0 |
| [src/core/kline_precache.py](../../src/core/kline_precache.py) | K线盘前预缓存(v0.4.10)。 | register_precache_cron | 0 |
| [src/core/klines_daily_refresh.py](../../src/core/klines_daily_refresh.py) | 全市场日线(qfq)每日增量 job(v0.5.87)。 | daily_job | 0 |
| [src/core/klines_fullmarket.py](../../src/core/klines_fullmarket.py) | 全市场日线(qfq)回填/每日增量(v0.5.87, 老板 2026-09-13 批)。 | a_share_universe, limit_up_symbols, merge_universe, filter_needing, load_state, save_state, run_backfill | 0 |
| [src/core/klines_minute.py](../../src/core/klines_minute.py) | 分钟K线(1m)盘中滚动入库(2026-09-10, 数据落库设计 Matrix #2)。 | collect_minute_klines_once | 0 |
| [src/core/l2_event_stream.py](../../src/core/l2_event_stream.py) | L2 事件流推送(批次F F2, 2026-09-06 28号)。 | install, evaluate_dark_cluster, evaluate_seal_anomaly, push_event, eval_tick | 0 |
| [src/core/l2_fund_snapshot.py](../../src/core/l2_fund_snapshot.py) | L2 成品资金收盘快照 + 非交易时段回退源(2026-10-11 P1 数据断档修复)。 | has_live_values, capture_symbol, capture_universe, run_l2_snapshot_job, latest_snapshot | 0 |
| [src/core/l2_snapshot_freshness.py](../../src/core/l2_snapshot_freshness.py) | L2 成品资金收盘快照(l2_fund_snapshots)新鲜度自检 + 自动补采(2026-10-13 快照源空修复)。 | latest_snapshot_date, check, backfill_missing, guard, run_l2_snapshot_freshness_job | 0 |
| [src/core/l2_ticks_scheduler.py](../../src/core/l2_ticks_scheduler.py) | L2 逐笔定期落库调度器(v0.4.77)。 | L2TicksScheduler | 0 |
| [src/core/l4_events.py](../../src/core/l4_events.py) | L4 事件数据源(2026-09-01, 设计稿 §5): 把四类真实事件算成 K 线可标注的事件列表。 | split_clusters, cancel_anomalies, dragon_tiger_events, announcement_events, chip_levels, unlock_levels_from_chips | 0 |
| [src/core/ladder_freshness.py](../../src/core/ladder_freshness.py) | 连板梯队数据新鲜度自检 + 自动补数(2026-10-11 P1 数据断档修复)。 | expected_latest_trading_date, missing_trading_days, latest_event_date, check, backfill_missing, guard, run_ladder_freshness_job | 0 |
| [src/core/ladder_live_state.py](../../src/core/ladder_live_state.py) | 连板梯队盘中态 Redis 读写(v0.5.87, spec §4)。 | client, load_state, load_meta, save_state, save_day, load_day | 0 |
| [src/core/layer_precompute.py](../../src/core/layer_precompute.py) | 工作台日级图层派生块预落库(2026-10-14 首屏 P0: 26s → 秒开)。 | register_daily_block_builder, get_daily_block_builder, now_cst, is_settled, in_live_window, payload_is_empty, get_layer_daily, usable, put_layer_daily, clear_layer_daily …（共 15 个） | 0 |
| [src/core/lhb_backfill.py](../../src/core/lhb_backfill.py) | 龙虎榜(东财)回填(2026-09-10, 老板要求: 妖股因子 lhb 维接入)。 | set_recompute_hook, backfill_history, daily_recent, daily_job, lhb_stats | 0 |
| [src/core/lhb_detail_backfill.py](../../src/core/lhb_detail_backfill.py) | 龙虎榜机构/营业部明细回填(东财, 全自动) — 2026-09-10 老板"切换到东财全自动方案"。 | sync_dates, backfill_history, daily_job | 0 |
| [src/core/limit_ladder.py](../../src/core/limit_ladder.py) | 连板梯队(v0.5.81, 老板: "连扳梯队也要做")。 | boards_for_date, ladder_window, touched_by_date, finalize_marks, attach_candles | 0 |
| [src/core/limit_ladder_live.py](../../src/core/limit_ladder_live.py) | 连板梯队盘中状态机(v0.5.87 三态 → v0.5.88 细分, 借鉴 quicktiny 连板天梯)。 | classify, merge_state, build_live_day, scan_tick | 0 |
| [src/core/limit_pool_zdt.py](../../src/core/limit_pool_zdt.py) | 涨停池 TQ 涨跌停快照备源(审计 A-6, 2026-09-28)。 | parse_zdt_snapshot, zdt_fallback_pool, fmt_zdt_time | 0 |
| [src/core/limit_rules.py](../../src/core/limit_rules.py) | A 股涨跌停价规则(批次A/批次B共用, 2026-09-06 28号)。 | limit_ratio, limit_up_price, limit_down_price | 0 |
| [src/core/limit_strength_shadow.py](../../src/core/limit_strength_shadow.py) | 首封时间×封成比 强度因子 **影子运行** 落库 + 读取(2026-10-11 胜率创新 P0-1)。 | build_rows, mark_rows, collect_and_mark, run_strength_shadow_job, labels_map, latest_date, labels_for_date | 0 |
| [src/core/limit_up_backfill.py](../../src/core/limit_up_backfill.py) | 涨停事件回填(批次B, 2026-09-06 28号)。 | backfill_symbol, backfill_all, get_events_window | 0 |
| [src/core/limit_up_calc.py](../../src/core/limit_up_calc.py) | 涨停判定与连板数计算(纯函数, 无 I/O —— 便于逐条钉口径)。 | raw_code, limit_up_ratio, consecutive_boards, is_limit_up, build_pool_items | 0 |
| [src/core/limit_up_strength.py](../../src/core/limit_up_strength.py) | 『首封时间 × 封成比』打板强度因子(2026-10-11 胜率创新 P0-1, **影子运行**)。 | parse_clock, first_seal_bucket, compute_seal_ratio, seal_ratio_grade, strength_label | 0 |
| [src/core/live_daily_bar.py](../../src/core/live_daily_bar.py) | 今日形成中日 K 的实时合成(2026-10-12)。 | LiveDailyBar, aggregate_minute_bars, daily_bar_from_minutes, is_stub_bar, is_flat_zero_volume, is_degenerate_placeholder, daily_bar_from_daily_bars, read_minute_bars_pg, synthesize_live_daily_bar | 0 |
| [src/core/llm_context.py](../../src/core/llm_context.py) | LLM 调用的归因上下文（2026-10-20, P3 成本看板/多用户配额）。 | bind, unbind, current | 0 |
| [src/core/log_context.py](../../src/core/log_context.py) | Structured log context helpers (contextvars + LogRecord factory). | get_log_context, log_context, bind_log_context, install_log_record_factory | 0 |
| [src/core/login_ratelimit.py](../../src/core/login_ratelimit.py) | 登录限速 — 防暴力破解(2026-08-15, 公开 demo 后登录接口暴露在公网)。 | check, fail, success, reset | 0 |
| [src/core/loki_logger.py](../../src/core/loki_logger.py) | Loki 日志聚合推送 (P1 稳定性, 2026-09-18)。 | loki_enabled, LokiLogHandler, get_loki_handler, get_loki_stats, ensure_loki_handler, shutdown_loki | 0 |
| [src/core/macro_free_sources.py](../../src/core/macro_free_sources.py) | 宏观指标免费取数（2026-10-25）。 | get_series, summarize, snapshot_all | 0 |
| [src/core/main_flow_compare.py](../../src/core/main_flow_compare.py) | 主力意图双源对比(腾讯逐笔 vs thsdk L2) v0.5.0。 | clear_cache, compare_main_flow | 0 |
| [src/core/mainflow_tri.py](../../src/core/mainflow_tri.py) | 明盘主力净额三源交叉验证(P1, 2026-09-04)。 | judge_agree, triangulate | 0 |
| [src/core/market_archive.py](../../src/core/market_archive.py) | 批次2(2026-09-10): 市场数据落库 —— 竞价快照 + 筹码日频。 | persist_auction_snapshots, read_auction_snapshots, persist_chip_daily, read_chip_daily | 0 |
| [src/core/market_breadth.py](../../src/core/market_breadth.py) | 市场广度 / 情绪周期量化 —— 自研口径。 | BreadthBar, percentile_rank, compute_series, compute_series_with_percentile, sentiment_score, latest_summary | 0 |
| [src/core/market_breadth_scheduler.py](../../src/core/market_breadth_scheduler.py) | 市场广度日序列调度器 —— 每交易日盘后同步一次（含启动回补）。 | MarketBreadthScheduler, start_scheduler | 0 |
| [src/core/market_flow_sampler.py](../../src/core/market_flow_sampler.py) | 大盘资金流快照采样(2026-09-11, 老板报"大盘资金流图没内容")。 | collect_once | 0 |
| [src/core/market_mainline.py](../../src/core/market_mainline.py) | 市场主线识别 (v0.3.0, 2026-08-24)。 | aggregate_mainline | 0 |
| [src/core/market_phase.py](../../src/core/market_phase.py) | A 股市场情绪周期 6 阶段体系 — 冰点 / 启动 / 主升 / 高潮 / 退潮 / 修复 (2026-08-24) | DailyMetrics, compute_daily_metrics, classify_phase_series, classify_phase_series_full, phase_distribution, ordered_distribution, calibrate, consecutive_boards, ladder_completeness, metrics_rows_from_events …（共 15 个） | 0 |
| [src/core/market_regime.py](../../src/core/market_regime.py) | 情绪周期条件化门槛(2026-10-10 决策提胜率 A): 把"当前情绪周期状态"引入决策合成。 | normalize_regime, regime_policy, temperature_policy, condition_verdict, current_regime, clear_regime_memo, regime_key, resolve_regime_for_day, regime_for_day_key | 0 |
| [src/core/market_replay.py](../../src/core/market_replay.py) | 盘后复盘统一证据装配器（2026-10-23）。 | build_replay, to_row, persist, read_latest, render, run_scheduled | 0 |
| [src/core/market_scan.py](../../src/core/market_scan.py) | 全市场三榜扫描(P2.3)。 | resonance_pick, scan | 0 |
| [src/core/market_scan_jobs.py](../../src/core/market_scan_jobs.py) | 盘后扫描 cron 入口(KI-039 第二阶段, 2026-09-09)。 | run_market_scan_job, run_dark_fund_top_job | 0 |
| [src/core/marketdata_client.py](../../src/core/marketdata_client.py) | PanWatch ↔ marketdata 接线:DB 配置端口 + 单例 + flag 门控的报价兼容层。 | DbConfigProvider, get_market_data, reset_market_data, md_quote_rows, md_news, md_news_by_keyword, md_stock_data, md_more_info, md_dark_flow_tq, md_formula_mul …（共 11 个） | 0 |
| [src/core/md_metrics_sink.py](../../src/core/md_metrics_sink.py) | vendor 调用统计落库(MetricsSink 装饰器)。 | DbCountingMetricsSink, get_metrics_sink | 0 |
| [src/core/md_redis_cache.py](../../src/core/md_redis_cache.py) | marketdata 的 Redis 共享缓存后端(批次1-B, 2026-09-10)。 | redis_cache_enabled, RedisTTLCache, redis_cache_factory | 0 |
| [src/core/media_utils.py](../../src/core/media_utils.py) | 媒体内容文本化工具: 图片 OCR + 文件文本提取。 | save_bytes, image_to_text, file_to_text | 0 |
| [src/core/minute_breakthrough.py](../../src/core/minute_breakthrough.py) | 分时突破「突 / 积」信号(决策先锋辅助指标, P3 补差, 2026-10-10)。 | compute_breakthrough, fetch_minute_bars, fetch_dde_series, fetch_dde_series_with_source, fetch_minute_breakthrough | 0 |
| [src/core/mnemonic_overlay.py](../../src/core/mnemonic_overlay.py) | 内外盘七口诀叠加确认(2026-10-10 决策提胜率 B)。 | eval_bdqk_effect, resolve_mnemonic | 0 |
| [src/core/money.py](../../src/core/money.py) | 货币结算的 Decimal 原语(风险整改 3.5/B5)。 | to_dec, q2, q4, q6 | 0 |
| [src/core/mood_cycle.py](../../src/core/mood_cycle.py) | 情绪周期 → 题材操作容许度映射(批次C C1, 2026-09-06 28号)。 | mood_from_phase, current_mood | 0 |
| [src/core/more_info_daily.py](../../src/core/more_info_daily.py) | TQ 'more_info' 104 字段收盘定稿落库(2026-10-18 P0-b, 方案 §7.2①)。 | has_live_values, row_to_columns, upsert_rows, latest, history, capture_universe, run_more_info_daily_job | 0 |
| [src/core/news_ranker.py](../../src/core/news_ranker.py) | 无模块说明 | parse_news_time, dedupe_news_items, rank_news_items, summarize_news_topics | 0 |
| [src/core/news_store.py](../../src/core/news_store.py) | 新闻/政策沉淀与检索（2026-10-24）。 | is_policy, save_flash_news, search_news, coverage | 0 |
| [src/core/niuxiong_line.py](../../src/core/niuxiong_line.py) | 牛熊线(决策先锋辅助指标, P3 补差, 2026-10-10)。 | wma_series, sma_series, ema_series, compute_niuxiong, fetch_niuxiong | 0 |
| [src/core/notifier.py](../../src/core/notifier.py) | 无模块说明 | get_global_proxy, sanitize_for_telegram, build_apprise_url, NotifierManager | 0 |
| [src/core/notify_center.py](../../src/core/notify_center.py) | 通知中心 + 外发推送联动。 | push_notification, push_notification_async, notify_task_done | 0 |
| [src/core/notify_dedupe.py](../../src/core/notify_dedupe.py) | Notification dedupe helpers. | build_notify_dedupe_key, check_and_mark_notify | 0 |
| [src/core/notify_policy.py](../../src/core/notify_policy.py) | 无模块说明 | NotifyPolicy, parse_dedupe_overrides | 0 |
| [src/core/notify_sink.py](../../src/core/notify_sink.py) | 通知实时推送槽(中立层, KI-039 第二阶段, 2026-09-09)。 | register, broadcast_notification, incr_unread | 0 |
| [src/core/number_audit.py](../../src/core/number_audit.py) | 数字对账护栏（2026-10-21）。 | extract_claims, audit, annotate | 0 |
| [src/core/numutil.py](../../src/core/numutil.py) | 统一数值工具(audit P1, 2026-09-15)。 | safe_float, clamp | 0 |
| [src/core/ohlc_dark.py](../../src/core/ohlc_dark.py) | OHLC 分摊暗盘(APZJ 对照项)。 | BarAllocation, allocate_bar, ohlc_dark_net | 0 |
| [src/core/orderbook_engine.py](../../src/core/orderbook_engine.py) | 盘口演变引擎(order_book_engine) — 基于 THS L2 逐笔委托盘口(20档)的三个创新算法。 | fetch_snapshot, img_frame_to_snapshot, load_snapshots_from_img, order_book_queue, to_ths_code, find_img_file, order_book_evolution, order_book_imbalance, ghost_order, run | 0 |
| [src/core/orderbook_snapshot.py](../../src/core/orderbook_snapshot.py) | 十档盘口快照后台采集 + 请求端读快照(2026-10-14, 盘口移出用户请求路径)。 | snapshot_interval_sec, stale_threshold_sec, max_symbols, trim_frame, rebuild_snapshot, append_frame, read_snapshot, fresh_snapshot, active_symbols, collect_symbol …（共 11 个） | 0 |
| [src/core/paper_trading_engine.py](../../src/core/paper_trading_engine.py) | 模拟盘引擎：自动按策略信号建仓/平仓，跟踪虚拟账户收益。 | normalize_allocations, market_allocations_or_default, allocations_from_excluded, compute_market_cash, market_realized_open, market_available_cash, PaperTradingEngine | 0 |
| [src/core/paper_trading_notifier.py](../../src/core/paper_trading_notifier.py) | 模拟盘跟单通知：建仓/平仓实时推送、盘前计划、日终摘要。 | notify_entry, notify_exit, send_premarket_plan, send_daily_summary, send_test_notification | 0 |
| [src/core/paper_trading_scheduler.py](../../src/core/paper_trading_scheduler.py) | 模拟盘调度器：60 秒间隔扫描建仓/平仓。 | PaperTradingScheduler | 0 |
| [src/core/paths.py](../../src/core/paths.py) | 中立路径解析(KI-039 第二阶段, 2026-09-09)。 |  | 0 |
| [src/core/pdf_export.py](../../src/core/pdf_export.py) | 详情报告导出 PDF —— HTML 保真排版。 | assemble_report_markdown, render_analysis_pdf | 0 |
| [src/core/permissions.py](../../src/core/permissions.py) | SIDA 用户权限体系(2026-09-15 四档重构, 合并原 RBAC)。 | effective_trial_features, effective_trial_limit, effective_max_sessions, effective_guest_strategy, normalize_role, get_role_permissions, has_permission, has_perm, enforce_perm, get_trial_remaining …（共 19 个） | 0 |
| [src/core/portfolio_benchmark.py](../../src/core/portfolio_benchmark.py) | 组合 vs 基准对比(M2):超额收益 / 信息比率 / 相对回撤 + 归一化净值曲线。 | benchmark_label, compute_benchmark_metrics, build_portfolio_benchmark, build_attribution | 0 |
| [src/core/portfolio_diagnostics.py](../../src/core/portfolio_diagnostics.py) | 组合诊断(Phase 4):只读分析模拟盘持仓的集中度 / 分布 / 风险。 | herfindahl, diagnose_positions, diagnose_paper_portfolio | 0 |
| [src/core/postmarket_review.py](../../src/core/postmarket_review.py) | 盘后复盘(设计稿 §6.2 / 复刻手册 §5.2 §8) —— .tck 委托号级精确暗盘复盘。 | split_clusters_from_orders, cancel_rate, active_passive_ratio, dark_review_from_tck | 0 |
| [src/core/prediction_outcome.py](../../src/core/prediction_outcome.py) | 无模块说明 | evaluate_pending_prediction_outcomes | 0 |
| [src/core/premarket_evidence.py](../../src/core/premarket_evidence.py) | 盘前结构化证据层 (src/core/premarket_evidence.py)。 | EvidenceBlock, PremarketEvidence, audit_ladder_summary, audit_news_items, enrich_news_clusters, collect | 0 |
| [src/core/price_alert_engine.py](../../src/core/price_alert_engine.py) | 价格提醒引擎：规则评估、命中落库与通知发送。 | RuleEvalResult, PriceAlertEngine | 0 |
| [src/core/price_alert_scheduler.py](../../src/core/price_alert_scheduler.py) | 价格提醒调度器：独立于 Agent 调度。 | PriceAlertScheduler | 0 |
| [src/core/price_distribution_analyzer.py](../../src/core/price_distribution_analyzer.py) | I-8 盘中微观分价主力博弈矩阵与拆单检测(2026-10-18)。 | normalize_bin, detect_support_levels, detect_resistance_levels, detect_iceberg_signals, tag_bins, build_summary, build_bias, build_matrix, fetch_analysis | 0 |
| [src/core/price_distribution_archive.py](../../src/core/price_distribution_archive.py) | I-8 分价矩阵**收盘归档**调度(2026-10-18)。 | archive_symbols, register_archive_cron | 0 |
| [src/core/price_distribution_store.py](../../src/core/price_distribution_store.py) | I-8 分价矩阵持久化与读穿透缓存(2026-10-18)。 | snapshot_to_dict, bucket_of, upsert_snapshot, archive_daily, load_latest_snapshot, load_daily_history, purge_old_snapshots, get_or_refresh | 0 |
| [src/core/quant_adapters.py](../../src/core/quant_adapters.py) | 量化框架适配器接口(Phase 4 预留,轻量)。 | BacktestAdapter, available_backends | 0 |
| [src/core/quote_period.py](../../src/core/quote_period.py) | Classify and label quote-backed daily P&L periods. | classify_quote_period, summarize_daily_pnl_period | 0 |
| [src/core/quote_snapshots.py](../../src/core/quote_snapshots.py) | 快照行情 1 分钟桶落库(批次2 2/2, 2026-09-10)。 | in_trading_window, collect_market_symbols, persist_quote_snapshots, collect_once | 0 |
| [src/core/rally_analysis.py](../../src/core/rally_analysis.py) | 盘中顺势拉升段分析: 判断"放量上涨(真拉升)"还是"拉高出货(假拉升)"。 | analyze_rallies, analyze_swings, format_rally_report | 0 |
| [src/core/recipe_versions.py](../../src/core/recipe_versions.py) | 配方版本历史（P2-2.1, 2026-10-25）。 | sync_versions, history | 0 |
| [src/core/report_generator.py](../../src/core/report_generator.py) | SIDA 内置报告生成器(盘前/盘后)。 | generate_market_report | 0 |
| [src/core/report_scheduler.py](../../src/core/report_scheduler.py) | SIDA 内置报告调度器: 交易日(周一至五) 8:30 盘前报告 / 15:30 盘后报告。 | ReportScheduler | 0 |
| [src/core/resonance.py](../../src/core/resonance.py) | 三指标共振状态机(阶段1 五件套 ⑤)。 | evaluate_state, fund_flow_label, state_action_label | 0 |
| [src/core/resonance_ai.py](../../src/core/resonance_ai.py) | 三指标共振 AI 判定(2026-09-11, 老板"要接入ai分析, 给出是否共振; 现在没办法知道到底有没有共振")。 | build_user_content, parse_ai_verdict, build_verdict_triggers, build_verdict_evidence, build_batch_content, parse_batch_verdicts, run_daily_verdicts | 0 |
| [src/core/resonance_scan.py](../../src/core/resonance_scan.py) | 三指标共振判定 + 全市场扫描(2026-09-11, 老板"都做")。 | resonance_level, evaluate_one, scan, latest, symbol_detail, daily_job, activity_series | 0 |
| [src/core/risk_limits.py](../../src/core/risk_limits.py) | 组合级风控闸门(W3/B3.1-B3.2, KI-038)。 | env_float, RiskLimits, load_risk_limits, check_entry | 0 |
| [src/core/scenario_compose.py](../../src/core/scenario_compose.py) | 情景矩阵的工具级组装（2026-10-23）：矩阵 + 本人成本锚定 + 相似形态频次。 | owner_cost, build_for_tool | 0 |
| [src/core/scenario_cost_anchor.py](../../src/core/scenario_cost_anchor.py) | 持仓成本锚定情景（2026-10-23）。 | enrich_with_cost, render_cost_anchor | 0 |
| [src/core/scenario_engine.py](../../src/core/scenario_engine.py) | 情景规则引擎（2026-10-21）。 | build_scenarios, render, build_matrix, build | 0 |
| [src/core/schedule_parser.py](../../src/core/schedule_parser.py) | Schedule parsing helpers. | normalize_cron_day_of_week_field, parse_cron, parse_interval, parse_schedule, preview_schedule, count_runs_within | 0 |
| [src/core/scheduler.py](../../src/core/scheduler.py) | 无模块说明 | AgentScheduler | 0 |
| [src/core/scheduler_leader.py](../../src/core/scheduler_leader.py) | 调度器选主(2026-08-23 Q1; 2026-09-08 风险方案1.3/A3 改 fail-closed + 租约丢失真停)。 | leader_state, worker_id, try_acquire | 0 |
| [src/core/scheduler_registry.py](../../src/core/scheduler_registry.py) | 运行中调度器的轻量注册表,供系统自检读取健康状态。 | register, get_all, clear | 0 |
| [src/core/seal_quality.py](../../src/core/seal_quality.py) | 封单成色检测器(批次A, 2026-09-06 28号)。 | compute_from_series | 0 |
| [src/core/seal_sampler.py](../../src/core/seal_sampler.py) | 封单成色盘中采样任务(批次A A1, 2026-09-06 28号)。 | set_event_eval_hook, default_limit_up_items, sample_symbol, store_samples, sample_tick, get_recent_samples | 0 |
| [src/core/secret_rotation.py](../../src/core/secret_rotation.py) | JWT 密钥自动轮换 (P0 安全加固, 2026-09-18) | get_previous_records, get_grace_secrets, get_last_rotated_at, days_until_next_rotation, rotate_jwt_secret, maybe_auto_rotate, get_rotation_status, register_rotation_job | 0 |
| [src/core/sector_filter.py](../../src/core/sector_filter.py) | 题材/板块筛选支持:ftsahre 板块工具封装 + 缓存。 | list_concept_boards, search_boards, board_constituents, resolve_sector_codes | 0 |
| [src/core/selfcheck.py](../../src/core/selfcheck.py) | 系统自检(Doctor):一键体检 数据源 / AI / 通知,带中文修复提示。 | classify_hint, probe_datasource, probe_ai_model, probe_notify_channel, probe_db, probe_disk, probe_scheduler, list_selfcheck_items, run_selfcheck | 0 |
| [src/core/sentiment.py](../../src/core/sentiment.py) | 情绪因子(B6.4): 新闻/公告文本 → 可聚合的情绪分。 | SentimentResult, score_text, recency_weight, sentiment_for_items, sentiment_series | 0 |
| [src/core/sentiment_cycle.py](../../src/core/sentiment_cycle.py) | A 股短线情绪周期判别器 | classify_sentiment_cycle, format_cycle | 0 |
| [src/core/sentiment_freshness.py](../../src/core/sentiment_freshness.py) | 市场情绪序列(market_sentiment_daily)数据新鲜度自检 + 自动补数(2026-10-11 断链修复)。 | sc_enhancement_enabled, latest_sentiment_date, check, backfill_missing, guard, run_sentiment_freshness_job | 0 |
| [src/core/sentiment_selfcalc.py](../../src/core/sentiment_selfcalc.py) | 情绪序列**本地自算**(2026-10-14, 替代已弃用的云 SC 序列)。 | is_sealed_close, zt_metrics, streak_count_from_events, derive_ratios, target_trade_date, fetch_universe, fetch_snapshot, fetch_prices, fetch_events, collect_and_compute …（共 13 个） | 0 |
| [src/core/sentiment_thermometer.py](../../src/core/sentiment_thermometer.py) | 情绪温度计(2026-10-11 胜率证据层 ①): 把 SC15/SC3 派生的『封板成功率 / 炸板比』 | derive_thermometer, current_thermometer, clear_thermometer_memo, asof_today | 0 |
| [src/core/shadow_account/__init__.py](../../src/core/shadow_account/__init__.py) | Shadow Account — 从用户自己的交割单提炼交易影子。 |  | 0 |
| [src/core/shadow_account/backtester.py](../../src/core/shadow_account/backtester.py) | 影子回测 + 差值归因(移植自 HKUDS/Vibe-Trading, MIT,按数智分析 A 股收敛)。 | run_shadow_attribution, summarize_result | 0 |
| [src/core/shadow_account/extractor.py](../../src/core/shadow_account/extractor.py) | Shadow Account — 从盈利回合提炼交易规则(移植自 HKUDS/Vibe-Trading, MIT)。 | extract_shadow_profile | 0 |
| [src/core/shadow_account/journal.py](../../src/core/shadow_account/journal.py) | 交割单分析: FIFO 配对 + 行为画像(移植自 HKUDS/Vibe-Trading, MIT)。 | pair_trades_fifo, compute_profile, compute_behavior | 0 |
| [src/core/shadow_account/models.py](../../src/core/shadow_account/models.py) | Shadow Account 数据契约(frozen dataclasses)。 | ShadowRule, ShadowProfile, AttributionBreakdown, ShadowBacktestResult | 0 |
| [src/core/shadow_account/parsers.py](../../src/core/shadow_account/parsers.py) | 交割单格式适配器(移植自 HKUDS/Vibe-Trading, MIT)。 | TradeRecord, load_dataframe, detect_format, parse_guotou, parse_tonghuashun, parse_eastmoney, parse_futu, parse_generic, parse_file, records_to_dataframe | 0 |
| [src/core/shadow_account/reporter.py](../../src/core/shadow_account/reporter.py) | Shadow Account HTML 报告(移植自 HKUDS/Vibe-Trading, MIT,简化为单页)。 | render_shadow_report | 0 |
| [src/core/signal_explain.py](../../src/core/signal_explain.py) | 信号可解释化(Phase 3):rank_score → 1-10 AI Score + 正负因子拆解。 | to_ai_score, explain_factors, build_signal_evidence, enrich_signal | 0 |
| [src/core/signal_quality.py](../../src/core/signal_quality.py) | 用户自选池专属信号质量报告(2026-10-11 胜率证据层 ②): 纯账本聚合, 零预测。 | watchlist_signal_quality | 0 |
| [src/core/signal_review.py](../../src/core/signal_review.py) | 信号→复盘闭环(批次D, 2026-09-06 28号)。 | record_signal, nightly_review, hit_rate | 0 |
| [src/core/signal_summary.py](../../src/core/signal_summary.py) | 今日系统信号摘要(设计稿 §7.3「被动注入」, 2026-09-01)。 | build_signal_summary, render_signal_summary, run_signal_summary_job, read_latest_summary_text | 0 |
| [src/core/signals/__init__.py](../../src/core/signals/__init__.py) | 无模块说明 |  | 0 |
| [src/core/signals/signal_pack.py](../../src/core/signals/signal_pack.py) | 无模块说明 | PositionSnapshot, NewsSnapshot, EventsSnapshot, SignalPack, SignalPackBuilder | 0 |
| [src/core/signals/structured_output.py](../../src/core/signals/structured_output.py) | 无模块说明 | try_parse_action_json, try_extract_tagged_json, strip_tagged_json | 0 |
| [src/core/source_health.py](../../src/core/source_health.py) | L4 数据源健康检查(设计稿 v2.1 §12)。 | check_tck, check_img, check_wencai, check_shadow, check_tq_moreinfo, check_source, check_all, infer_status_from_stats, summarize_source, clear_health_cache | 0 |
| [src/core/startup_check.py](../../src/core/startup_check.py) | 启动配置自检模块(2026-08-21)。 | CheckResult, check_db_dialect_explicit, run_startup_checks | 0 |
| [src/core/startup_signals.py](../../src/core/startup_signals.py) | 启动早期信号 —— 读 'tq_formula_signal_daily'（客户端条件选股全市场扫描结果）。 | read_startup_signals, render_text | 0 |
| [src/core/stock_factors.py](../../src/core/stock_factors.py) | 个股因子读取与渲染（2026-10-21）。 | load, render, build | 0 |
| [src/core/stock_l2.py](../../src/core/stock_l2.py) | 单股 L2 字段接入(v0.5.89, 通达信 get_market_snapshot + get_more_info)。 | fetch_snapshot, fetch_more, fetch_stock_l2_ex, fetch_stock_l2, fetch_stock_l2_batch, seal_quality_tag, is_dive | 0 |
| [src/core/stock_link.py](../../src/core/stock_link.py) | 股票外部链接生成工具：根据股票代码、市场和用户选择的平台生成行情页 URL。 | stock_link_markdown | 0 |
| [src/core/stock_names.py](../../src/core/stock_names.py) | 代码 → 股票名称的统一解析(2026-10-21)。 | get_stock_list, normalize_symbol, names_for, reset_cache | 0 |
| [src/core/stock_persona.py](../../src/core/stock_persona.py) | 标的画像判定器(2026-10-21)。 | Persona, detect, render | 0 |
| [src/core/strategy_catalog.py](../../src/core/strategy_catalog.py) | 策略目录与权重读取。 | StrategySpec, ensure_strategy_catalog, list_strategy_catalog, get_strategy_profile_map, get_effective_weight_map | 0 |
| [src/core/strategy_engine.py](../../src/core/strategy_engine.py) | 策略层：信号生成、后验评估、调权与统计。 | validate_entry_plan, refresh_strategy_signals, list_strategy_signals, evaluate_strategy_outcomes, rebalance_strategy_weights, get_strategy_stats, list_market_regime_snapshots, list_portfolio_risk_snapshots, get_strategy_factor_snapshot, list_strategy_weight_history | 0 |
| [src/core/strategy_library.py](../../src/core/strategy_library.py) | 策略库核心(W4.2/KI-039): YAML 策略的求值与打分实现。 | strategy_params, effective_config, rounding_safe | 0 |
| [src/core/suggestion_pool.py](../../src/core/suggestion_pool.py) | 建议池管理 - 汇总各 Agent 建议 | save_suggestion, get_suggestions_for_stock, get_latest_suggestions, cleanup_expired_suggestions | 0 |
| [src/core/summary_cache.py](../../src/core/summary_cache.py) | 新增: summary_cache 表(PG/SQLite 兼容) + summary 落库/读库 | get_cached_summary, put_cached_summary, clear_summary_cache | 0 |
| [src/core/symbol_guard.py](../../src/core/symbol_guard.py) | 标的与取数护栏（2026-10-21 线上幻觉事故后加）。 | is_stock_question, mention_candidates, resolve_mentions, first_code_from_names, correction_note, zero_tool_refusal | 0 |
| [src/core/tdx_boards.py](../../src/core/tdx_boards.py) | 通达信客户端(TQ 网关)板块数据源 — 方案B(2026-09-10 老板拍板)。 | is_tdx_block_code, block_type_of, tdx_code_to_symbol, compute_speed, sector_items, name_map, data_fresh, latest_trade_date, market_breadth, board_quotes …（共 18 个） | 0 |
| [src/core/tdx_calendar.py](../../src/core/tdx_calendar.py) | 通达信交易日历/复权因子交叉校验(P1, 2026-09-13)。 | tdx_trading_dates, calendar_mismatch, tdx_divid_factors | 0 |
| [src/core/tdx_corporate.py](../../src/core/tdx_corporate.py) | 通达信 TQ 个股「异动资金档案」：大宗交易 / 增减持 / 北向持股 / 质押 / 财务指标。 | block_trades, holder_changes, northbound_holding, pledge, financials, dossier, render_lines | 0 |
| [src/core/tdx_dark_fund.py](../../src/core/tdx_dark_fund.py) | 通达信 TQ 暗盘资金接口(2026-09-15)。 | fetch_tq_l2_summary, fetch_tq_l2_amo, compute_tdx_dark_fund | 0 |
| [src/core/tdx_fundamental.py](../../src/core/tdx_fundamental.py) | 个股基本面/股本/次新(P2, 2026-09-13, 通达信)。 | fetch_gb, fetch_listing, is_sub_new | 0 |
| [src/core/tdx_img_parser.py](../../src/core/tdx_img_parser.py) | 通达信 .img 十档盘口 + 委托队列解析器(**真实样本校准版**, 2026-10-02)。 | ImgFormat, ImgParseError, ImgSnapshot, decode_container, iter_records, iter_fields, decode_snapshot, parse_img, frames_from_img, snapshots_to_frames …（共 12 个） | 0 |
| [src/core/tdx_lhb.py](../../src/core/tdx_lhb.py) | 通达信龙虎榜(客户端页面缓存)解析 — 2026-09-10 老板要求"龙虎榜也可以接入吧"。 | parse_board, parse_seats, board, seats | 0 |
| [src/core/tdx_tick_parser.py](../../src/core/tdx_tick_parser.py) | 通达信 .tck 逐笔解析器 — 36 字节委托号级(官方方向 2B/2S + 主动侧委托号 + 撤单)。 | parse_tck, ticks_from_tck | 0 |
| [src/core/theme_candidate_mapper.py](../../src/core/theme_candidate_mapper.py) | 突发事件题材与全市场标的映射器 (Theme Candidate Mapper) | extract_event_themes, resolve_theme_candidates, build_fullmarket_theme_candidates | 0 |
| [src/core/theme_mood.py](../../src/core/theme_mood.py) | 题材情绪分(2026-09-12 老板口径; 设计见 docs/research/题材情绪分_设计方案_20260912.md)。 | is_meta_board, is_pool_eligible, anchor_map, percentile_rank, dim_structure, dim_diffusion, core_stock_score, continuation_prob, dim_core, dim_relay …（共 27 个） | 0 |
| [src/core/theme_rotation.py](../../src/core/theme_rotation.py) | 题材轮动统计(v0.5.81)。 | daily_top_sets, rotation_series, membership_flags | 0 |
| [src/core/thresholds.py](../../src/core/thresholds.py) | 决策阈值统一配置层(数智决策 P1-3, 2026-10-10)。 | value, source, life_line, strong_line, bull_line, top_line, snapshot | 0 |
| [src/core/thsdk_alert.py](../../src/core/thsdk_alert.py) | thsdk_alert.py — 基于同花顺 SDK(thsdk) 的三类盘口预警算法。 | CircuitBreaker, ThrottledCaller, close_auction_surge, auction_snapshot, ai_candidate_pool, run | 0 |
| [src/core/thsdk_board.py](../../src/core/thsdk_board.py) | thsdk 板块数据采集与轮动计算(阶段2.1/2.2, v0.3.0)。 | clear_cache, fetch_ths_industry, fetch_ths_concept, fetch_block_detail, fetch_block_constituents, fetch_block_snapshots, sync_boards_to_db, compute_rotation, register_board_sync_job | 0 |
| [src/core/thsdk_breaker.py](../../src/core/thsdk_breaker.py) | thsdk 进程级熔断器 + 并发信号量 + 硬超时(2026-09-03 v0.4.73 / 2026-09-18 并发治理)。 | call_with_hard_timeout, thsdk_call, breaker_status, reset_for_tests | 0 |
| [src/core/tick_archive.py](../../src/core/tick_archive.py) | 逐笔日存档(#3 回测底座 + #4 跨日结论序列, 2026-09-04)。 | upsert_day, maybe_archive_day, read_series | 0 |
| [src/core/tick_minute_flow.py](../../src/core/tick_minute_flow.py) | 逐笔 → 分钟资金（2026-10-21）。 | aggregate, summarize, render, build | 0 |
| [src/core/timezone.py](../../src/core/timezone.py) | 时区处理工具 - 统一时间存储和显示。 | utc_now, beijing_now, beijing_now_naive, to_utc, to_beijing, format_beijing, to_iso_utc, to_iso_with_tz, format_app_tz | 0 |
| [src/core/tq_chips.py](../../src/core/tq_chips.py) | 个股筹码/主力体检 —— 用通达信客户端**现成公式**（不是我方拟合）。 | normalize_code, build_chips, render_text | 0 |
| [src/core/tq_formula_signal_scheduler.py](../../src/core/tq_formula_signal_scheduler.py) | TQ 条件选股信号定时采集(单写者): 每交易日收盘后扫一遍全市场条件选股公式。 | TqFormulaSignalScheduler | 0 |
| [src/core/tq_rpc_observability.py](../../src/core/tq_rpc_observability.py) | TQ JSON-RPC 直连消费者的失败可观测性(2026-10-08 可观测性审计 P1-6)。 | tq_kind, install_tq_rpc_guard, is_installed | 0 |
| [src/core/tq_sentiment_scheduler.py](../../src/core/tq_sentiment_scheduler.py) | TQ 情绪序列定时同步 —— **SC 云序列已弃用, 15:35 改为静默跳过(expected_gap)**。 | TqSentimentScheduler | 0 |
| [src/core/trade_phase.py](../../src/core/trade_phase.py) | 交易阶段 / 数据日期标注(2026-10-18, 审计 A-03 修复)。 | cn_now, trade_phase, phase_label, phase_meta, parse_date, resolve_data_date, data_date_iso, is_live_now, as_of_iso | 0 |
| [src/core/trader_playbook.py](../../src/core/trader_playbook.py) | 交易模式画像(2026-10-22) —— 用**证据**说清主人的交易思维: 长项/短板各带样本量与置信度。 | build, render, block, to_json | 0 |
| [src/core/trading_calendar.py](../../src/core/trading_calendar.py) | A股交易日历工具(2026-08-23 S6 修复)。 | TradingCalendarError, is_trading_day, add_trading_days, next_trading_day, prev_trading_day, trading_days_between, is_auction_time, is_trading_session, trading_day_anchor, cache_day | 0 |
| [src/core/trend_pilot_line.py](../../src/core/trend_pilot_line.py) | 趋势操盘线(决策先锋辅助指标, P3 补差, 2026-10-10)。 | compute_trend_line, fetch_trend_line | 0 |
| [src/core/trend_profile.py](../../src/core/trend_profile.py) | 多周期走势画像（2026-10-21）。 | compute, render, build, normalize_day, compute_range_summary, render_range_summary, build_range_summary | 0 |
| [src/core/unit_check.py](../../src/core/unit_check.py) | 量价金额恒等式出口校验(风险整改 3.5/B5)。 | UnitInconsistencyError, unit_deviation, assert_unit_consistency | 0 |
| [src/core/unit_recon.py](../../src/core/unit_recon.py) | 每日单位对账 job(风险整改 3.5/B5)。 | run_unit_reconciliation | 0 |
| [src/core/universe.py](../../src/core/universe.py) | PIT 股票池快照(B0.6, KI-036)。 | is_st_name, upsert_universe, universe_as_of, filter_symbols, backfill_from_entry_candidates, backfill_from_stock_table | 0 |
| [src/core/update_checker.py](../../src/core/update_checker.py) | 应用升级检测模块（基于 Docker Hub tag）。 | check_update | 0 |
| [src/core/usage_latency.py](../../src/core/usage_latency.py) | Skill 调用**耗时**聚合(B7 内部计量, 2026-09-19)。 | percentile, aggregate_latency | 0 |
| [src/core/war_report.py](../../src/core/war_report.py) | 主力资金战报(规格 §4.4『主力资金战报』, 2026-10-10)。 | industry_distribution, split_wash_counts, build_daily_war_report, run_war_report_job | 0 |
| [src/core/webhook_delivery.py](../../src/core/webhook_delivery.py) | Webhook 投递（P2-2.2, 2026-10-25）。 | WebhookRejected, validate_callback_url, build_payload, deliver, deliver_for_run | 0 |
| [src/core/wechat_bot_worker.py](../../src/core/wechat_bot_worker.py) | 微信数智分析BOT 双向对话 worker。 | wechat_bot_worker | 0 |
| [src/core/wechat_ilink.py](../../src/core/wechat_ilink.py) | SIDA 个人微信 iLink 直连客户端(纯 Python, 腾讯官方通道)。 | download_media, fetch_qr, poll_qr, send_text, get_updates, get_config, send_typing | 0 |
| [src/core/wyckoff.py](../../src/core/wyckoff.py) | 威科夫量价结构标注(本土三层滤网 P1: 第三层)。 | WyckoffMark, analyze_wyckoff | 0 |
| [src/db/__init__.py](../../src/db/__init__.py) | src/db — 数据库方言层与中立 ORM 入口(W3.1/D2, 2026-09-09; KI-039 切片 A/B 扩充)。 |  | 0 |
| [src/db/backup.py](../../src/db/backup.py) | 版本化迁移前的备份(W3.1/D2 自 src/web/database.py 收编)。 | backup_db_before_migration, backup_pg_schema_before_migration | 0 |
| [src/db/dialect.py](../../src/db/dialect.py) | 数据库方言层唯一入口: 环境解析 / 方言判定 / 引擎构造 / 语义化 SQL 助手。 | is_postgres, declared_backend, has_read_replica, read_db_url, is_read_sql, is_read_clause, build_engine, acquire_write, upsert_sql, insert_ignore_sql | 0 |
| [src/db/klines_repo.py](../../src/db/klines_repo.py) | klines 数据访问仓库(B4.5/KI-039)。 | load_qfq_bars, load_qfq_bars_cached | 0 |
| [src/db/models.py](../../src/db/models.py) | 无模块说明 | User, AuditLog, ReportSubscription, AIService, UserAIService, UserAiPref, AIModel, AISceneBinding, NotifyChannel, Account …（共 97 个） | 0 |
| [src/db/redis_client.py](../../src/db/redis_client.py) | Redis 客户端单例 (2026-08-17 v0.2.65 — Phase 1 架构升级) | RedisClient | 0 |
| [src/db/session.py](../../src/db/session.py) | ORM 会话入口(中立层, KI-039 切片 A, 2026-09-09; P1 读写分离 2026-09-18)。 | get_read_engine, get_write_engine, RoutingSession, get_db, install_apm_on_engines | 0 |
| [src/db/streams.py](../../src/db/streams.py) | Redis Streams 任务队列 (2026-08-17 v0.2.65 — Phase 1) | publish_kline_backfill, get_stream_stats | 0 |
| [src/harness/__init__.py](../../src/harness/__init__.py) | SIDA Harness 底座：配方（内置智能体）与后续的 trace/eval。 |  | 0 |
| [src/harness/actions.py](../../src/harness/actions.py) | 动作回写(2026-10-20, P1)。 | ActionError, propose, list_pending, apply_action, reject_action, propose_scenario_annotations, preview_diff, rollback_action | 0 |
| [src/harness/bar_cache.py](../../src/harness/bar_cache.py) | 单轮取数缓存（2026-10-23）—— 同一轮里 (symbol, market, days) 只查一次 PG。 | begin_bar_scope, end_bar_scope, scope_active, cached_load_bars, cached_load_bars_async | 0 |
| [src/harness/context.py](../../src/harness/context.py) | 请求级 Harness 上下文：@提及解析、配方注入、工具白名单与预算。 | Mention, mention_stock, resolve_market, resolve_bound_symbol, bind_symbol, sync_conv_symbol, apply_mentions, inject_recipe, extract_code, is_diagnosis_intent …（共 16 个） | 0 |
| [src/harness/pipeline.py](../../src/harness/pipeline.py) | 确定性管线（2026-10-21）。 | post_tool_names, session_context, propose_levels, PipelineRun, symbols_of, run, run_stream, compose_prompt, answer, answer_stream | 0 |
| [src/harness/pipeline_market.py](../../src/harness/pipeline_market.py) | 全市场 / 多标的确定性管线（2026-10-23）。 | MarketRun, tool_requirements, required_args, build_steps, run_market_pipeline, run_market_pipeline_stream, compose_market_prompt, answer_market, answer_market_stream | 0 |
| [src/harness/pipeline_router.py](../../src/harness/pipeline_router.py) | 管线路由器（2026-10-23）：按配方 'scope' 选符号管线或全市场管线。 | is_market_scope, resolve_watchlist, answer, answer_stream | 0 |
| [src/harness/quick_answer.py](../../src/harness/quick_answer.py) | 问价快答：纯行情问题不调模型，直接把工具读数排版返回。 | polish, is_pure_quote, resolve_symbol, answer | 0 |
| [src/harness/recipes.py](../../src/harness/recipes.py) | Harness 配方（内置智能体）加载器。 | Recipe, load_recipes, recipe_fingerprint, recipe_updated_at, catalog, get_recipe, list_recipes, match_recipe, filter_schemas | 0 |
| [src/harness/routing.py](../../src/harness/routing.py) | 请求级路由（2026-10-21）：把"用哪些工具、什么温度"从模型手里拿回来。 | Route, route_for_request | 0 |
| [src/harness/runtime.py](../../src/harness/runtime.py) | 参谋运行时(Agent 内核 P0, 2026-10-20)。 | AgentRuntime, summarize_on_budget, budget_exhausted_hint | 0 |
| [src/harness/skills.py](../../src/harness/skills.py) | 技能手册(2026-10-20, P2)。 | load_skill, skills_block | 0 |
| [src/harness/trace.py](../../src/harness/trace.py) | 数智参谋的执行轨迹(Agent 内核 P0, 2026-10-20)。 | CopilotTrace, start_copilot_run, set_recipe, finish_copilot_run, bind_llm_context | 0 |
| [src/harness/viewstate.py](../../src/harness/viewstate.py) | 页面即上下文(2026-10-20, P1)。 | ViewState, save_view_state, load_view_state, render_view_state_block, extract_selection_range, append_view_state_block | 0 |
| [src/models/__init__.py](../../src/models/__init__.py) | 无模块说明 |  | 0 |
| [src/models/market.py](../../src/models/market.py) | 无模块说明 | MarketCode, TradingSession, MarketDef, StockData, IndexData, is_market_trading_time | 0 |
| [src/web/__init__.py](../../src/web/__init__.py) | 无模块说明 |  | 0 |
| [src/web/api/__init__.py](../../src/web/api/__init__.py) | 无模块说明 |  | 0 |
| [src/web/api/_authz.py](../../src/web/api/_authz.py) | web 层权限依赖工厂 —— 把 core 的 'enforce_perm' 接成 FastAPI 依赖。 | require_perm | 0 |
| [src/web/api/_scope.py](../../src/web/api/_scope.py) | C3(2026-09-09) 统一多用户数据访问 helpers。 | scoped, owned_or_404, writable, allow_cross_user | 0 |
| [src/web/api/abnormal_moves.py](../../src/web/api/abnormal_moves.py) | 异动接近度监控 API(任务 C, 2026-08-24)。 | clear_cache, list_abnormal_moves, cache_clear | 2 |
| [src/web/api/accounts.py](../../src/web/api/accounts.py) | 账户和持仓管理 API | get_hkd_cny_rate, get_usd_cny_rate, AccountCreate, AccountUpdate, AccountResponse, PositionCreate, PositionUpdate, DefenseLineUpdate, PositionResponse, PositionReorderItem …（共 28 个） | 17 |
| [src/web/api/admin_free_tier.py](../../src/web/api/admin_free_tier.py) | 「免费档」管理端点(2026-09-18 老板需求) —— owner 运行时调整免费级别。 | FreeTierPatch, get_free_tier, put_free_tier | 2 |
| [src/web/api/admin_secrets.py](../../src/web/api/admin_secrets.py) | 管理端密钥轮换 API (P0, 2026-09-18) | rotate_secrets, secrets_status | 2 |
| [src/web/api/agents.py](../../src/web/api/agents.py) | 无模块说明 | agents_health, AgentConfigUpdate, AgentConfigResponse, AgentRunResponse, list_agents, list_capabilities, update_agent, preview_schedule_expr, preview_agent_schedule, delete_agent …（共 21 个） | 17 |
| [src/web/api/ai_pattern.py](../../src/web/api/ai_pattern.py) | AI 看图形态 影子证据层 API(2026-10-14 胜率创新 · 主线一并行, **影子运行**)。 | ai_pattern_usage, get_shadow, run_shadow | 3 |
| [src/web/api/api_keys.py](../../src/web/api/api_keys.py) | API Key 管理控制台(2026-09-16): 登录用户自助管理自己的 Skill API Key。 | list_keys, create_key, reset_key, delete_key, key_usage | 5 |
| [src/web/api/auction.py](../../src/web/api/auction.py) | 竞价快照轻接口(竞价快览卡 AuctionSnapshotCard 渲染用, 2026-08-19)。 | get_auction_snapshot, auction_snapshot_endpoint | 1 |
| [src/web/api/auction_pool.py](../../src/web/api/auction_pool.py) | 竞价异动池 API(v0.3.2, 2026-08-24 字段口径二次修正)。 | anomaly, history, sync | 3 |
| [src/web/api/audit.py](../../src/web/api/audit.py) | 操作审计 API(2026-08-15 阶段3): owner 视角审计日志。 | log_audit, list_audit | 1 |
| [src/web/api/auth.py](../../src/web/api/auth.py) | 认证 API - 多用户 JWT 认证(2026-08-10 阶段1) | LoginRequest, SetupRequest, ChangePasswordRequest, RegisterRequest, normalize_email, username_from_email, get_user_by_email, TokenResponse, hash_password, needs_rehash …（共 44 个） | 14 |
| [src/web/api/backtest.py](../../src/web/api/backtest.py) | 回测只读端点(B2.4/KI-039)。 | BacktestIn, run_backtest_endpoint | 1 |
| [src/web/api/boards.py](../../src/web/api/boards.py) | 板块数据 API(阶段2.2, v0.3.0): 板块/概念列表 + 板块详情 + 成分股 + 轮动排序。 | list_boards, board_rotation, board_heatmap, board_detail, board_constituents | 5 |
| [src/web/api/calendar.py](../../src/web/api/calendar.py) | 事件日历 API: 宏观财经日历(百度财经日历, 经 FTShare MCP)。 | calendar_events | 1 |
| [src/web/api/caliber_archive.py](../../src/web/api/caliber_archive.py) | 口径快照档案(B5, 2026-09-18)。 | get_archive_symbols, record_symbol, drift_series | 0 |
| [src/web/api/caliber_compare.py](../../src/web/api/caliber_compare.py) | 口径对照(2026-09-18, 老板需求 A2 第一步) —— 三种"主力资金"口径并排看, 消歧不合并。 | build_caliber_compare, caliber_compare_query, caliber_compare, caliber_drift | 3 |
| [src/web/api/cancel_profile.py](../../src/web/api/cancel_profile.py) | I-3 委托行为画像 API: 撤单率历史基线 + 动态 Z-Score 坐标系. | get_cancel_profile | 1 |
| [src/web/api/catalyst.py](../../src/web/api/catalyst.py) | 埋伏雷达 API(2026-09-05, v0.5.6)。 | catalyst_calendar, catalyst_ambush | 2 |
| [src/web/api/cb_radar.py](../../src/web/api/cb_radar.py) | 可转债 - 正股联动套利雷达 API(I-4 初版, 2026-10-18)。 | snapshot, bond_detail | 2 |
| [src/web/api/channels.py](../../src/web/api/channels.py) | 无模块说明 | ChannelCreate, ChannelUpdate, ChannelResponse, list_channels, list_channel_types, create_channel, update_channel, delete_channel, test_channel | 6 |
| [src/web/api/chat.py](../../src/web/api/chat.py) | AI 对话 API 端点。 | get_web_content, CreateConversationBody, SendMessageBody, suggested_questions, create_conversation, list_conversations, get_conversation, delete_conversation, send_message, send_message_stream | 7 |
| [src/web/api/chat_admin.py](../../src/web/api/chat_admin.py) | 会话管理接口(2026-10-22): 重命名 / 置顶 / 搜索 / 删单条消息 / 会话轨迹。 | UpdateConversationBody, update_conversation, search_conversations, delete_conversation_message, list_conversation_runs | 4 |
| [src/web/api/chat_upload.py](../../src/web/api/chat_upload.py) | AI 对话助手附件上传/解析 API。 | upload_attachment | 1 |
| [src/web/api/context.py](../../src/web/api/context.py) | 上下文与后验评估 API。 | StockContextSnapshotResponse, AgentContextRunResponse, AgentPredictionOutcomeResponse, list_stock_context_snapshots, get_latest_topic, list_context_runs, list_prediction_outcomes, evaluate_predictions, cleanup_context | 6 |
| [src/web/api/copilot.py](../../src/web/api/copilot.py) | src/web/api/copilot.py | get_copilot_brief, get_persona, get_copilot_presets, get_copilot_agents, get_copilot_mentions, post_copilot_context, get_copilot_context, get_copilot_actions, apply_copilot_action, reject_copilot_action …（共 34 个） | 34 |
| [src/web/api/copilot_jobs.py](../../src/web/api/copilot_jobs.py) | 参谋长任务异步化（P0-0.2，2026-10-25）。 | SubmitRunBody, run_copilot_question, submit_run, run_events, cancel_run | 3 |
| [src/web/api/copilot_workbench.py](../../src/web/api/copilot_workbench.py) | 参谋工作台写入型接口（2026-10-25）。 | ModelChoiceBody, get_copilot_model, set_copilot_model, ParallelItem, ParallelBody, submit_parallel, get_parallel, get_run_governance, list_interceptions, DedupeBody …（共 11 个） | 7 |
| [src/web/api/darkflow.py](../../src/web/api/darkflow.py) | 内盘外盘口诀 + 主力意图 轻接口(分时图卡片渲染用, 2026-08-13)。 | build_darkflow_response, build_intent_explain_response, clear_darkflow_ticks_cache, refetch_darkflow, archive_darkflow_day, collect_darkflow_ticks, darkflow_series, dark_flow, dark_flow_path, dark_flow_intent_explain | 8 |
| [src/web/api/dashboard.py](../../src/web/api/dashboard.py) | 首页聚合 API（轻量版：不包含机会消息中心）。 | get_dashboard_overview, CurateCandidate, CurateRequest, curate_today, get_brief, get_dashboard_root | 4 |
| [src/web/api/datasources.py](../../src/web/api/datasources.py) | 数据源管理 API | DataSourceCreate, DataSourceUpdate, DataSourceResponse, list_datasources, get_datasource_types, get_datasources_health, get_vendor_trust, get_configured_sources_health, get_datasource_health, reset_datasources_to_seed …（共 16 个） | 13 |
| [src/web/api/decision.py](../../src/web/api/decision.py) | 决策合成口(2026-09-08 方向2): GET /api/decision/{symbol} → 动手/看看/别碰 + 一行理由。 | get_decision, trigger_precompute | 2 |
| [src/web/api/decision_pioneer.py](../../src/web/api/decision_pioneer.py) | 数智决策三指标 API(盘中实时)。 | get_decision_pioneer_history, get_decision_pioneer | 2 |
| [src/web/api/decisions.py](../../src/web/api/decisions.py) | 决策日志 API(B6, 2026-09-18) —— "信号 → 结果"的账, 只读。 | decisions_stats, decisions_backfill, decisions_log, decisions_backtest, decisions_entry_outcomes, decisions_watchlist_signal_quality, decisions_thresholds | 7 |
| [src/web/api/demon_pool.py](../../src/web/api/demon_pool.py) | 妖股池 API(批次B, 2026-09-06 28号)。 | top_demon_symbols, demon_pool, demon_detail, trigger_backfill | 3 |
| [src/web/api/discovery.py](../../src/web/api/discovery.py) | 无模块说明 | get_hot_stocks, get_hot_boards, get_board_stocks | 3 |
| [src/web/api/email_verify.py](../../src/web/api/email_verify.py) | 邮箱验证码 API (2026-09-16) | EmailCode, generate_code, create_and_send_code, verify_code, hmac_compare, check_send_cooldown, SendCodeRequest, LoginByEmailRequest, send_code, login_by_email …（共 12 个） | 3 |
| [src/web/api/export.py](../../src/web/api/export.py) | 数据导出 API(CSV) — 2026-08-15 完整度评估 P1/P2。 | export_portfolio, export_predictions, export_opportunities | 3 |
| [src/web/api/factors.py](../../src/web/api/factors.py) | 因子权重 API(M5):只读列表 + 手动覆盖(pin / 设权重 / 开关自动标定)。 | FactorWeightUpdate, list_weights, update_weight | 2 |
| [src/web/api/feedback.py](../../src/web/api/feedback.py) | 无模块说明 | FeedbackIn, submit_feedback, feedback_stats | 2 |
| [src/web/api/forecast.py](../../src/web/api/forecast.py) | 预测回测 API:代理到独立预测引擎服务(:8010)。 | forecast_predict, forecast_predict_status, forecast_history, forecast_card, forecast_backtest, forecast_weights, forecast_referee_stats, forecast_models, forecast_health, forecast_report_generate …（共 15 个） | 15 |
| [src/web/api/formula_signals.py](../../src/web/api/formula_signals.py) | TQ 条件选股信号 API(2026-09-25)。 | formula_signals, formula_signals_batch, formula_signal_series | 3 |
| [src/web/api/fund_structure.py](../../src/web/api/fund_structure.py) | 四档资金结构 日序列 API（2026-10-24, exday_daily 消费 MVP）。 | fund_structure_series | 1 |
| [src/web/api/gap_study.py](../../src/web/api/gap_study.py) | 高开分档实证 API(v0.5.80 A7)。 | get_gap_study | 1 |
| [src/web/api/health.py](../../src/web/api/health.py) | 深度健康检查 + Prometheus /metrics (2026-08-17 v0.2.65 — Phase 1) | record_component_status, record_request_metrics, record_datasource_failure, metrics, health | 2 |
| [src/web/api/history.py](../../src/web/api/history.py) | 分析历史 API | HistoryResponse, list_history, get_history_detail, delete_history, RetryResponse, retry_history | 4 |
| [src/web/api/insights.py](../../src/web/api/insights.py) | 无模块说明 | InsightItem, InsightsBatchRequest, insights_batch, AddPositionEvalRequest, add_position_eval, AnnouncementEvalRequest, announcement_eval | 3 |
| [src/web/api/invite_codes.py](../../src/web/api/invite_codes.py) | 邀请码管理(owner only) —— 内部使用模式的准入控制台。 | CreateInviteCodeRequest, invite_mode, create_code, list_codes, code_uses, disable_code | 5 |
| [src/web/api/jobs.py](../../src/web/api/jobs.py) | 后台任务 API(2026-09-12, 借鉴 tick-stock-panel 的作业面板)。 | list_jobs, get_job, cancel_job | 3 |
| [src/web/api/klines.py](../../src/web/api/klines.py) | 无模块说明 | clear_summary_biz_cache, KlineItem, KlineBatchRequest, KlineSummaryItem, KlineSummaryBatchRequest, get_closes_batch, get_scenario_matrix, get_klines, get_klines_batch, build_layer_daily_blocks …（共 17 个） | 11 |
| [src/web/api/limit_strength.py](../../src/web/api/limit_strength.py) | 首封时间×封成比 强度因子 API(2026-10-11 胜率创新 P0-1, **影子运行**)。 | limit_strength_usage, get_shadow, run_shadow | 3 |
| [src/web/api/llm_usage.py](../../src/web/api/llm_usage.py) | LLM 调用统计 API(2026-08-15): 查询 llm_usage 日志的汇总 + 明细。 | llm_usage_stats | 1 |
| [src/web/api/logs.py](../../src/web/api/logs.py) | 日志中心 API | LogEntryResponse, LogListResponse, list_logs, clear_logs, logs_meta, logs_health, FrontendErrorReport, list_error_events, report_frontend_error | 6 |
| [src/web/api/main_flow.py](../../src/web/api/main_flow.py) | 主力意图双源对比 API(腾讯逐笔 vs thsdk L2, v0.5.0)。 | compare, clear | 2 |
| [src/web/api/market.py](../../src/web/api/market.py) | 市场指数 API - 公共数据，无需认证 | get_market_data, clear_indices_cache, get_market_indices, get_index_detail, replay_summary | 3 |
| [src/web/api/market_archive.py](../../src/web/api/market_archive.py) | 数据落库面查询 API(批次3 查询侧收口, 2026-09-10 28号)。 | overview, quote_snapshots, auction_snapshots, chip_daily, dragon_tiger, dragon_tiger_tdx, dragon_tiger_tdx_seats, lhb_institution, lhb_seats | 9 |
| [src/web/api/market_data.py](../../src/web/api/market_data.py) | 市场数据代理 API: 把 marketdata 包的关键方法暴露成只读 HTTP 端点。 | dragon_tiger_range_status, dragon_tiger_proxy, dragon_tiger_status, capital_flow_proxy, board_capital_flow_proxy, market_capital_flow_proxy, market_capital_flow_history, fetch_fundamentals_detail, fundamentals_detail_proxy, anomalies_proxy …（共 15 个） | 14 |
| [src/web/api/market_mainline.py](../../src/web/api/market_mainline.py) | 市场主线识别 API(v0.3.0, 2026-08-24)。 | warm_market_mainline_cache, get_market_mainline, clear_market_mainline_cache | 1 |
| [src/web/api/market_phase.py](../../src/web/api/market_phase.py) | 市场情绪周期 6 阶段 API(2026-08-24)。 | get_phase, sync_phase, backfill_phase, get_phase_segments, get_phase_stats, register_cron | 5 |
| [src/web/api/market_replay.py](../../src/web/api/market_replay.py) | 盘后复盘三池序列 + 连板晋级树 API(创新 1, 2026-10-24)。 | replay_series, replay_tree | 2 |
| [src/web/api/market_scan.py](../../src/web/api/market_scan.py) | 全市场三榜扫描 API(设计稿 §6.1, 2026-09-01 接线)。 | RefreshRequest, get_market_scan_ranks, refresh_market_scan, DarkFundTopRequest, get_dark_fund_top, refresh_dark_fund_top | 4 |
| [src/web/api/my_ai_services.py](../../src/web/api/my_ai_services.py) | 我的服务商(BYOK) — 用户自定义 LLM 服务商 (2026-08-15) | MyModelItem, MyServiceCreate, MyServiceUpdate, list_my_services, create_my_service, update_my_service, delete_my_service | 4 |
| [src/web/api/news.py](../../src/web/api/news.py) | 新闻 API - 基于数据源配置 | NewsItemResponse, get_news, get_news_sources | 2 |
| [src/web/api/notifications.py](../../src/web/api/notifications.py) | 站内消息中心 API。 | NotificationOut, AgentRunDetail, NotificationDetailOut, unread_count, list_notifications, get_notification_detail, get_notification_status, mark_read, mark_all_read, clear_read …（共 11 个） | 8 |
| [src/web/api/orderbook.py](../../src/web/api/orderbook.py) | OB 失衡条 轻接口(分时卡片用, 2026-08-20)。 | get_orderbook_ob, get_img_orderbook, orderbook_ob, orderbook_img, orderbook_ob_path | 3 |
| [src/web/api/paper_trading.py](../../src/web/api/paper_trading.py) | 模拟盘 API 端点。 | ToggleBody, UpdateSettingsBody, get_account, list_positions, list_trades, get_metrics, get_diagnostics, get_backends, toggle_account, reset_account …（共 19 个） | 16 |
| [src/web/api/pioneer_indicators.py](../../src/web/api/pioneer_indicators.py) | 数智决策辅助指标 API(趋势操盘线 / 牛熊线 / 分时突破, P3 补差, 2026-10-10)。 | clear_pioneer_indicators_cache, get_trend_line, get_niuxiong, get_minute_breakthrough | 3 |
| [src/web/api/premarket.py](../../src/web/api/premarket.py) | 盘前分析 Agent API 接线 (B 支线, 2026-10-23): /today 读底稿 + /run 触发 + /evidence 证据层。 | reset_rate_limit, extract_structured, latest_record, render_brief, get_today, run, get_evidence | 3 |
| [src/web/api/presets.py](../../src/web/api/presets.py) | Presets API: 8 个 A 股可用的 swarm preset 团队配置。 | RunPresetRequest, list_all, get_detail, run | 3 |
| [src/web/api/price_alerts.py](../../src/web/api/price_alerts.py) | 无模块说明 | AlertConditionItem, AlertConditionGroup, PriceAlertCreate, PriceAlertUpdate, ToggleBody, list_alert_rules, create_alert_rule, update_alert_rule, toggle_alert_rule, delete_alert_rule …（共 14 个） | 9 |
| [src/web/api/price_distribution.py](../../src/web/api/price_distribution.py) | I-8 分价博弈矩阵 API(2026-10-18)。 | get_price_distribution, get_price_distribution_history, post_price_distribution_archive | 3 |
| [src/web/api/pro_billing.py](../../src/web/api/pro_billing.py) | Pro 付费系统(2026-09-16 任务3.1/3.3): 申请 + 人工审核 + 到期降级查询。 | ApplyRequest, ReviewRequest, submit_application, downgrade_user_keys_to_free, list_tiers, apply_pro, apply_status, admin_list_applications, admin_approve, admin_reject …（共 13 个） | 8 |
| [src/web/api/profile.py](../../src/web/api/profile.py) | 个人中心 API(2026-08-15 SIDA 完整度评估 P1): 个人资料 + 我的数据。 | ProfileUpdate, get_profile, update_profile, prediction_accuracy_board, profile_stats, ProApplyBody, apply_pro | 5 |
| [src/web/api/providers.py](../../src/web/api/providers.py) | 无模块说明 | SceneBindingResponse, SceneBindingUpdate, list_scene_bindings, update_scene_binding, ServiceCreate, ServiceUpdate, infer_capabilities, ModelResponse, ServiceResponse, list_services …（共 24 个） | 13 |
| [src/web/api/quote_stream.py](../../src/web/api/quote_stream.py) | 行情实时推送(2026-08-12): 自选股行情 WebSocket 推送, 免手动刷新/轮询。 | subscribe, unsubscribe, websocket_quote_handler | 0 |
| [src/web/api/quotes.py](../../src/web/api/quotes.py) | 无模块说明 | QuoteItem, QuoteBatchRequest, get_quote, get_quotes_batch, get_more_info, get_dark_flow_tq, get_dark_fund_tdx, get_company_info, get_minute, get_quotes_root | 8 |
| [src/web/api/recommendations.py](../../src/web/api/recommendations.py) | 推荐相关 API（入场候选榜）。 | CandidateFeedbackIn, get_entry_candidates, refresh_candidates, get_candidate_feedback, submit_candidate_feedback, candidate_stats, evaluate_candidate_outcomes, missing_candidate_outcomes, get_strategy_catalog, get_opportunity_sectors …（共 22 个） | 21 |
| [src/web/api/reports.py](../../src/web/api/reports.py) | 报告中心 API: 列出/读取 Hermes cron 输出报告。 | list_reports, get_report_content, get_broker_research_reports, get_reports_root | 4 |
| [src/web/api/resonance.py](../../src/web/api/resonance.py) | 三指标共振 API(2026-09-11, 数智决策升级)。 | get_scan, run_scan, scan_status, activity, symbol_rule, analyze, analyze_daily | 7 |
| [src/web/api/seal_quality.py](../../src/web/api/seal_quality.py) | 封单成色 API(批次A A3, 2026-09-06 28号)。 | seal_quality_usage, seal_quality_metrics, seal_quality_raw, SampleNowBody, sample_now | 4 |
| [src/web/api/sentiment_series.py](../../src/web/api/sentiment_series.py) | 市场情绪日序列 API (2026-09-23)。 | sentiment_health, sentiment_baseline, sentiment_series, sentiment_sync | 4 |
| [src/web/api/service_config.py](../../src/web/api/service_config.py) | 服务间配置下发 (2026-09-08 T7): forecast 容器的唯一配置通道。 | get_forecast_config | 1 |
| [src/web/api/settings.py](../../src/web/api/settings.py) | 无模块说明 | get_app_version, SettingUpdate, SettingResponse, is_mask_placeholder, list_settings, get_avatar, set_avatar, update_setting, get_version, get_update_check | 6 |
| [src/web/api/shadow.py](../../src/web/api/shadow.py) | Shadow Account API — 上传交割单 → 行为画像 + 影子策略 + 归因报告。 | analyze_journal, get_my_trades, get_my_profile, get_report, get_report_pdf | 5 |
| [src/web/api/signals_review.py](../../src/web/api/signals_review.py) | 信号复盘 API(批次D, 2026-09-06 28号)。 | signals_hit_rate, signals_review | 2 |
| [src/web/api/skill_install.py](../../src/web/api/skill_install.py) | 智能体一键安装(2026-09-16)。 | install_sh, skills_config | 2 |
| [src/web/api/skills_gateway.py](../../src/web/api/skills_gateway.py) | Skill Gateway — 对外开放 skill 的 HTTP API(2026-09-15)。 | refresh_tier_configs, downgrade_expired_keys, ensure_downgrade_scheduler, stop_downgrade_scheduler, KeyRegisterRequest, SkillRunRequest, SkillRunResponse, skill_catalog, list_skills, run_skill …（共 25 个） | 16 |
| [src/web/api/stock_pool.py](../../src/web/api/stock_pool.py) | 数智决策选股池 API(盘中实时, 2026-08-30)。 | StockPoolScreenRequest, screen_stock_pool, JbpScreenRequest, jbp_screen, screen_cached | 2 |
| [src/web/api/stocks.py](../../src/web/api/stocks.py) | 无模块说明 | StockCreate, StockUpdate, StockAgentInfo, StockResponse, StockAgentItem, StockAgentUpdate, StockReorderItem, StockReorderRequest, get_market_status, search …（共 23 个） | 15 |
| [src/web/api/strategies.py](../../src/web/api/strategies.py) | 策略库 API: 列出/查看/应用借鉴 alphasift 的 YAML 策略到单只股票。 | list_strategies, get_strategy, ApplyRequest, ScanRequest, scan_strategy, apply_strategy | 4 |
| [src/web/api/subscriptions.py](../../src/web/api/subscriptions.py) | 定时报告订阅 API(2026-08-10 阶段4)。 | SubscriptionUpdate, list_subscriptions, update_subscription | 2 |
| [src/web/api/suggestions.py](../../src/web/api/suggestions.py) | 建议池 API | get_stock_suggestions, get_all_latest_suggestions, cleanup_suggestions | 4 |
| [src/web/api/tdx.py](../../src/web/api/tdx.py) | 通达信问小达 API。 | tdx_ask, tdx_etf_track | 2 |
| [src/web/api/templates.py](../../src/web/api/templates.py) | 无模块说明 | TemplateAgent, TemplateStockAgent, TemplateStock, TemplatePayload, export_template, import_template | 2 |
| [src/web/api/theme_mood.py](../../src/web/api/theme_mood.py) | 题材情绪分 API(2026-09-12): 榜单 / 题材详情 / 手动扫描 / 盘中刷新 / 收盘定型。 | get_ladder, spawn_settle, intraday_refresh_job, settle_refresh_job, warm_caches, spawn_ladder_snapshot, ladder_snapshot_job, get_board, get_detail, run_scan | 4 |
| [src/web/api/ths.py](../../src/web/api/ths.py) | 同花顺账号 API(2026-09-05 精简: 扫码登录整条线已下掉, 只留账号摘要)。 | ths_account | 1 |
| [src/web/api/thsdk_alert.py](../../src/web/api/thsdk_alert.py) | thsdk_alert.run() API 化(v0.3.0 阶段 1.4) | alert | 1 |
| [src/web/api/thsdk_ext.py](../../src/web/api/thsdk_ext.py) | thsdk 深度扩展 API(v0.3.1 选项B):代码补齐 + 市场代码表。 | code_norm, market_list | 2 |
| [src/web/api/thsdk_extended.py](../../src/web/api/thsdk_extended.py) | thsdk 高价值待接能力 API(v0.3.1 目标 A) | api_news, api_corporate_action, api_dde, api_hs300, api_market_data_cn_extended, api_market_data_index, api_market_data_hk, api_market_data_us, api_market_data_bond, api_market_data_fund …（共 11 个） | 11 |
| [src/web/api/thsdk_snapshot.py](../../src/web/api/thsdk_snapshot.py) | thsdk L2 综合快照 API（v0.3.0 阶段 1.3） | snapshot, invalidate_cache | 2 |
| [src/web/api/tradingview_webhook.py](../../src/web/api/tradingview_webhook.py) | TradingView Alert Webhook 接收端点(2026-08-12)。 | TVAlertPayload, tradingview_alert | 1 |
| [src/web/api/user_data.py](../../src/web/api/user_data.py) | 用户数据管理 API(GDPR, P3 2026-09-18): 导出 / 删除。 | DeleteRequest, export_user_data, request_data_delete, data_delete_status, purge_expired_deletions | 3 |
| [src/web/api/users.py](../../src/web/api/users.py) | 用户管理 API — SIDA 权限体系管理端(2026-08-15)。 | UserCreateRequest, UserUpdateRequest, ModelAccessUpdate, get_user_model_access, update_user_model_access, PermissionUpdate, get_my_permissions, get_user_permissions, update_user_permissions, admin_list_users …（共 24 个） | 14 |
| [src/web/api/war_report.py](../../src/web/api/war_report.py) | 主力资金战报 API(规格 §4.4, 2026-10-10)。 | get_daily_war_report, refresh_daily_war_report | 2 |
| [src/web/api/wechat_bind.py](../../src/web/api/wechat_bind.py) | SIDA 扫码绑定个人微信(腾讯官方 iLink 直连) API。 | start_bind, bind_status, get_bind, unbind | 4 |
| [src/web/api/wencai.py](../../src/web/api/wencai.py) | 问财选股 API 端点(薄壳, KI-039 第二阶段, 2026-09-09)。 |  | 1 |
| [src/web/api/ws_notifications.py](../../src/web/api/ws_notifications.py) | 通知 WebSocket 路由 (v0.4.36 P0 派活 1)。 | notifications_ws | 1 |
| [src/web/api/ws_quotes.py](../../src/web/api/ws_quotes.py) | 行情 WebSocket 独立 router(2026-08-12)。 | quote_ws | 1 |
| [src/web/api_version.py](../../src/web/api_version.py) | API 版本化别名 (2026-08-21)。 | ApiVersionAliasMiddleware | 0 |
| [src/web/app.py](../../src/web/app.py) | 无模块说明 | demo_isolation_middleware, version | 1 |
| [src/web/cache/biz_cache.py](../../src/web/cache/biz_cache.py) | 业务缓存层 (2026-08-22) — 把散落在各 API/collector 的内存 dict 缓存统一为 | BizCache, user_scoped_key | 0 |
| [src/web/cache/redis_client.py](../../src/web/cache/redis_client.py) | 兼容 shim(KI-039 第二阶段, 2026-09-09): Redis 客户端已下沉 'src/db/redis_client.py'。 |  | 0 |
| [src/web/cache/streams.py](../../src/web/cache/streams.py) | 兼容 shim(KI-039 第二阶段, 2026-09-09): Redis Streams 已下沉 'src/db/streams.py'。 |  | 0 |
| [src/web/database.py](../../src/web/database.py) | 数据库引擎与会话入口(薄壳, W3.1/D2; KI-039 切片 A 后再薄一层; | init_db | 0 |
| [src/web/log_handler.py](../../src/web/log_handler.py) | Custom logging handler that writes log entries to SQLite. | get_log_handler_stats, DBLogHandler | 0 |
| [src/web/middleware.py](../../src/web/middleware.py) | 统一网关中间件 (2026-08-17 v0.2.65 — Phase 1) | JWTDecodeMiddleware, RateLimitMiddleware, RequestLoggerMiddleware, get_rate_limit_stats, AuditMiddleware, SecurityHeadersMiddleware, issue_csrf_token, CSRFProtectionMiddleware | 0 |
| [src/web/migrations.py](../../src/web/migrations.py) | Versioned database migrations for PanWatch. | Migration, has_pending_migrations, run_versioned_migrations | 0 |
| [src/web/models.py](../../src/web/models.py) | 兼容 shim(KI-039 切片 B, 2026-09-09): ORM 模型已下沉到 'src/db/models.py'。 |  | 0 |
| [src/web/notifications/__init__.py](../../src/web/notifications/__init__.py) | 通知模块 (v0.4.36+): |  | 0 |
| [src/web/notifications/ws_hub.py](../../src/web/notifications/ws_hub.py) | WebSocket 通知 Hub (v0.4.36 P0 派活 1)。 | attach_event_loop, broadcast_notification, broadcast_global, install_pubsub_listener, incr_unread, get_unread, reset_unread, ws_notifications_handler, stats | 0 |
| [src/web/realtime/__init__.py](../../src/web/realtime/__init__.py) | 实时总线包: envelope(统一帧) 为 Hub 独立进程的前置条件。 |  | 0 |
| [src/web/realtime/envelope.py](../../src/web/realtime/envelope.py) | 统一实时信封(2026-09-07 P2 成熟化): WS 下行全部走 envelope, Hub 抽独立进程的前置条件。 | pack, replay_since, reset_for_tests, ring_depth | 0 |
| [src/web/response.py](../../src/web/response.py) | 统一 API 响应格式中间件 | ResponseWrapperMiddleware | 0 |
| [src/web/stock_list.py](../../src/web/stock_list.py) | 兼容 shim(KI-039 第二阶段, 2026-09-09): 股票列表缓存/搜索已下沉 'src/collectors/stock_list.py'。 |  | 0 |
