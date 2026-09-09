# P0冻结-数据层(2026-09-07)
> PG models.py 50表 + klines hypertable + Redis biz:缓存。

## PG核心表(50)
User, AuditLog(独立Session修login卡死), ReportSubscription, AIService/UserAIService/AIModel/AISceneBinding(22工具/BYOK底座), NotifyChannel, Account/Stock/Position, StockAgent/AgentConfig/AgentRun, LogEntry, AppSettings(DB>env, wudao key在此补), DataSource, NewsCache, NotifyThrottle, AnalysisHistory, StockContextSnapshot, NewsTopicSnapshot, AgentContextRun, AgentPredictionOutcome, StockSuggestion, EntryCandidate(+Feedback/Outcome), MarketScanSnapshot/Rank, StrategyCatalog/SignalRun/Outcome/Weight(+History), FactorWeight(+History), MarketRegimeSnapshot, StrategyFactorSnapshot, PortfolioRiskSnapshot, SuggestionFeedback, PriceAlertRule/Hit, PaperTradingAccount/Position/Trade, ChatConversation/Message, Notification, LLMUsage, Board/BoardDaily, AuctionAnomalyRecord, MarketPhaseDaily, SignalSummaryDaily, DarkFundTopSnapshot

## K线
PG hypertable优先(get_klines), 三源幂等(tencent/eastmoney/sina, ON CONFLICT DO NOTHING), 18:00回填 + klines_ingestor --days 800。JWT挂klines致dashboard token 401 → P1拆service token(X-Service-Token)。

## 单位约定(实测校准, 风险方案1.1)
- **腾讯实时行情 `turnover`(Quote.turnover) = 元**。2026-09-08 收盘后实测 qt.gtimg.cn: sh600519 parts[35]="1309.30/17534/2302823753", 恒等式 amt/(price×vol(手)×100)=1.0031≈1; sh601318=1.0069≈1(偏差即VWAP≠收盘)。交叉印证: parts[37]=amt/10000(万元口径)。证据全文见 packages/marketdata vendors/tencent.py 模块 docstring。
- 腾讯 volume/volume_outer/volume_inner = 手。东财 turnover(f48) = 元。

## K线日K 各源单位(2026-09-09, B5/3.5 补测冻结)

| 源 | volume 原始 | 归一 | amount | close 复权 | 锚点 |
|---|---|---|---|---|---|
| 东财 push2his | 手(A股) | ×100→股(仅 `0./1.` secid) | f57=元,**原始不复权值**(B5 新透传 Bar.amount) | fqt=1 前复权 | marketdata/vendors/kline.py fetch_eastmoney_kline |
| 腾讯 ifzq(qfq) | 手 | ×100→股(2026-09-04) | **不提供**(None) | qfq | 同文件 fetch_tencent_kline_raw |
| 新浪 CN_MarketData | 股(原生) | 无需 | **不提供**(接口无字段) | 不复权 | 同文件 fetch_sina_index_kline / kline_collector._sina_fallback |
| TQ 通达信 | 股(原生) | 无需 | 不提供 | 原始 | vendors/tq.py |
| Yahoo chart v8 | 股 | 无需 | 不提供 | 无复权 | vendors/kline.py YahooKlineVendor |

→ 恒等式校验(volume×price≈amount)只有东财三要素齐; 其余源 amount=None 按 1.1 缺失语义跳过。

### 恒等式 dev 实测(2026-09-09 本机直连, 30 股×20 日)
- **不复权 fqt=0(校验口径, n=120)**: max=1.28496% / p99=1.19454% / p95=1.02187% / p50=0.29391%(top: 600276 08-13 1.285%)。
- **前复权 fqt=1(n≈200)**: max=5.61850%, 超阈全部为 000651(格力)除息前柱(08-14 implied=40.0294 vs close=37.90) —— **qfq 调整 close 而 amount/volume 恒原始, 恒等式在复权柱上天然破缺**(dev=除权因子)。
- 结论: **校验只在不复权数据上做**; 对账 job 显式拉 fqt=0。

### tol 校准(B5 禁止拍脑茼 2%)
DEFAULT_TOL_PCT=5.0(≈4× 不复权实测 max 1.28%; 20× 低于最小单位错 ×2→dev≈100%)。env 覆盖 `SIDA_UNIT_TOL_PCT`; fail-closed `SIDA_STRICT_UNITS=1`。

### 校验落点(B5/3.5)
- `src/core/unit_check.assert_unit_consistency`: 超阈 → error 日志 + `record_datasource_failure(source, kind="parse")`(复用 0.2 计数) + strict 抛 `UnitInconsistencyError`。
- 每日对账 job: `src/core/unit_recon.run_unit_reconciliation` 挂 KlineBackfillScheduler **18:35** cron(交易日), 抽样 20 股×20 柱 fqt=0, 报告落 `DATA_DIR/reports/unit_recon/YYYY-MM-DD.json`。
- 历史教训: P1-13(2026-09-08 审计) main_net_inflow_pct 东财/腾讯跨源差 100 倍, 已修, 见 types.CapitalFlow 注释。

### 结算路径 Decimal 分界(B5/3.5)
金额运算 Decimal 化(src/core/money.py to_dec/q2/q4/q6): backtest/cost_model.py fill/round_trip_pnl 内部、paper_trading_engine.py 平仓结算/compute_market_cash/账户净值回撤。**留 float(注明分界)**: 行情展示/报价路径、DB 列(模型 Float)、SQL 侧聚合(market_realized_open)。入库值 = Decimal 精确结算→量化→float 转存(无损); Float→Numeric 迁移另行评估。

## Quote 完整性标记(2026-09-08, 风险方案1.1)
marketdata.Quote 带 `status`(ok/partial/missing) + `missing_fields`。缺价 current_price=None, **绝不回退 0**; md_quote_rows 输出透传 status/missing_fields, 前端 null/missing 一律显示「无数据」。md_stock_data 跳过缺价 Quote。

## Redis(biz:前缀, L1内存+L2 Redis, 30s降级)
biz:quote:* TTL5s, biz:gs:* TTL60s, biz:ws:seq单调。业务禁裸连Redis, 一律src/web/cache/biz_cache.py。

## 生产铁律(不动)
容器panwatch-postgres(密码见部署机 .env 的 POSTGRES_PASSWORD, --network-alias postgres, SIDA_DB_URL@postgres:5432)。宿主机旧库勿用。
