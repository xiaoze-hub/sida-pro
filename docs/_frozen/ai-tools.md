# P0冻结-AI工具/后端接口清单(2026-09-07)
> 后端: src/web/api下68模块。Agent: 9个注册。

## 9 Agents(server.py)
DailyReportAgent, NewsDigestAgent, ChartAnalystAgent, IntradayMonitorAgent, PremarketOutlookAgent, TradingAgentsAgent, ThemeLaunchDetectorAgent, StockAttributionAgent, AuctionReviewAgent(+chat/tools_thsdk.py)

## 68 API模块(路由数>0)
accounts16, agents17, forecast14, providers13, datasources11, stocks11, thsdk_extended11, market_data9, price_alerts9, notifications8, chat7, quotes7, settings6, channels6, darkflow6, context6, users5, klines5, shadow4, dashboard4, boards4, market_scan4, my_ai_services4, logs4, wechat_bind4, paper_trading16(交易核心), recommendations20(推荐核心)
单路由模块(1-3个, P1合并候选): auction, audit, auth8(保留), boards, calendar, catalyst, demon_pool, discovery, export, factors, feedback, health2(扩healthz/readyz), history, insights, llm_usage, main_flow, market_mainline, market_phase, market2, news2, orderbook2, presets, profile3, seal_quality4, signals_review2, stock_pool, strategies4, subscriptions2, suggestions4, tdx, templates2, ths*, tradingview_webhook, wencai, ws_notifications, ws_quotes(→Hub), abnormal_moves2, auction_pool3, reports3, tdx(1个, 与datasources合并候选)

## WS现状(问题)
ws_quotes + ws_notifications各1路由, 与FastAPI同进程 → P2抽services/ws-hub(独立进程+Redis biz:ws:*)。
