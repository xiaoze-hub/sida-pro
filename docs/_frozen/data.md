# P0冻结-数据层(2026-09-07)
> PG models.py 50表 + klines hypertable + Redis biz:缓存。

## PG核心表(50)
User, AuditLog(独立Session修login卡死), ReportSubscription, AIService/UserAIService/AIModel/AISceneBinding(22工具/BYOK底座), NotifyChannel, Account/Stock/Position, StockAgent/AgentConfig/AgentRun, LogEntry, AppSettings(DB>env, wudao key在此补), DataSource, NewsCache, NotifyThrottle, AnalysisHistory, StockContextSnapshot, NewsTopicSnapshot, AgentContextRun, AgentPredictionOutcome, StockSuggestion, EntryCandidate(+Feedback/Outcome), MarketScanSnapshot/Rank, StrategyCatalog/SignalRun/Outcome/Weight(+History), FactorWeight(+History), MarketRegimeSnapshot, StrategyFactorSnapshot, PortfolioRiskSnapshot, SuggestionFeedback, PriceAlertRule/Hit, PaperTradingAccount/Position/Trade, ChatConversation/Message, Notification, LLMUsage, Board/BoardDaily, AuctionAnomalyRecord, MarketPhaseDaily, SignalSummaryDaily, DarkFundTopSnapshot

## K线
PG hypertable优先(get_klines), 三源幂等(tencent/eastmoney/sina, ON CONFLICT DO NOTHING), 18:00回填 + klines_ingestor --days 800。JWT挂klines致dashboard token 401 → P1拆service token(X-Service-Token)。

## Redis(biz:前缀, L1内存+L2 Redis, 30s降级)
biz:quote:* TTL5s, biz:gs:* TTL60s, biz:ws:seq单调。业务禁裸连Redis, 一律src/web/cache/biz_cache.py。

## 生产铁律(不动)
容器panwatch-postgres(密码PanWatch2026PG, --network-alias postgres, SIDA_DB_URL@postgres:5432)。宿主机旧库勿用。
