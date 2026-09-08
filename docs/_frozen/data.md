# P0冻结-数据层(2026-09-07)
> PG models.py 50表 + klines hypertable + Redis biz:缓存。

## PG核心表(50)
User, AuditLog(独立Session修login卡死), ReportSubscription, AIService/UserAIService/AIModel/AISceneBinding(22工具/BYOK底座), NotifyChannel, Account/Stock/Position, StockAgent/AgentConfig/AgentRun, LogEntry, AppSettings(DB>env, wudao key在此补), DataSource, NewsCache, NotifyThrottle, AnalysisHistory, StockContextSnapshot, NewsTopicSnapshot, AgentContextRun, AgentPredictionOutcome, StockSuggestion, EntryCandidate(+Feedback/Outcome), MarketScanSnapshot/Rank, StrategyCatalog/SignalRun/Outcome/Weight(+History), FactorWeight(+History), MarketRegimeSnapshot, StrategyFactorSnapshot, PortfolioRiskSnapshot, SuggestionFeedback, PriceAlertRule/Hit, PaperTradingAccount/Position/Trade, ChatConversation/Message, Notification, LLMUsage, Board/BoardDaily, AuctionAnomalyRecord, MarketPhaseDaily, SignalSummaryDaily, DarkFundTopSnapshot

## K线
PG hypertable优先(get_klines), 三源幂等(tencent/eastmoney/sina, ON CONFLICT DO NOTHING), 18:00回填 + klines_ingestor --days 800。JWT挂klines致dashboard token 401 → P1拆service token(X-Service-Token)。

## 单位约定(实测校准, 风险方案1.1)
- **腾讯实时行情 `turnover`(Quote.turnover) = 元**。2026-09-08 收盘后实测 qt.gtimg.cn: sh600519 parts[35]="1309.30/17534/2302823753", 恒等式 amt/(price×vol(手)×100)=1.0031≈1; sh601318=1.0069≈1(偏差即VWAP≠收盘)。交叉印证: parts[37]=amt/10000(万元口径)。证据全文见 packages/marketdata vendors/tencent.py 模块 docstring。
- 腾讯 volume/volume_outer/volume_inner = 手。东财 turnover(f48) = 元。

## Quote 完整性标记(2026-09-08, 风险方案1.1)
marketdata.Quote 带 `status`(ok/partial/missing) + `missing_fields`。缺价 current_price=None, **绝不回退 0**; md_quote_rows 输出透传 status/missing_fields, 前端 null/missing 一律显示「无数据」。md_stock_data 跳过缺价 Quote。

## Redis(biz:前缀, L1内存+L2 Redis, 30s降级)
biz:quote:* TTL5s, biz:gs:* TTL60s, biz:ws:seq单调。业务禁裸连Redis, 一律src/web/cache/biz_cache.py。

## 生产铁律(不动)
容器panwatch-postgres(密码见部署机 .env 的 POSTGRES_PASSWORD, --network-alias postgres, SIDA_DB_URL@postgres:5432)。宿主机旧库勿用。
