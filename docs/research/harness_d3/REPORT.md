# Harness D3 评测（内置智能体 vs 默认配方）

| 智能体 | 问题 | 工具调用 | 耗时 | 结构合规 | 字数 |
|---|---|---|---|---|---|
| premarket_strategist | 今天开盘前有什么值得盯的？ | 7 | 39.5s | — | 1293 |
| premarket_strategist | 隔夜有什么事件可能影响 A 股？ | 3 | 55.8s | — | 716 |
| premarket_strategist | 我的自选股今天优先级怎么排？ | 10 | 29.0s | — | 1396 |
| auction_hunter | 今天集合竞价有什么异常？ | 4 | 39.8s | — | 1721 |
| auction_hunter | 9:25 谁在爆量抢筹？ | 2 | 9.9s | ✅ | 752 |
| auction_hunter | 竞价撤单率高的票有哪些？ | 3 | 60.6s | ✅ | 700 |
| intraday_watcher | 我的持仓现在什么状态？ | 2 | 10.0s | — | 279 |
| intraday_watcher | 有没有触发防守位的？ | 2 | 4.5s | — | 228 |
| intraday_watcher | 主力资金今天在流入还是流出？ | 3 | 11.6s | — | 425 |
| postmarket_reviewer | 今天涨停梯队怎么样？ | 6 | 34.0s | — | 795 |
| postmarket_reviewer | 今天情绪温度多少？ | 2 | 10.5s | ✅ | 478 |
| postmarket_reviewer | 今天的主线题材是什么？ | 3 | 13.5s | — | 732 |
| risk_judge | 我的持仓风险有多大？ | 2 | 5.5s | — | 271 |
| risk_judge | 有没有已经破位的票？ | 6 | 34.6s | ✅ | 1227 |
| risk_judge | 现在的仓位结构有什么风险？ | 2 | 24.5s | — | 352 |
| chip_analyst | 600111 的筹码结构怎么样？ | 3 | 33.3s | — | 1101 |
| chip_analyst | 600111 主力今天在拿货还是派发 | 5 | 47.0s | ✅ | 1430 |
| chip_analyst | 600111 底仓成本和获利盘多少？ | 4 | 26.0s | ✅ | 1117 |
| default | 今天涨停梯队怎么样？ | 3 | 20.7s | — | 410 |
| default | 我的持仓风险有多大？ | 2 | 10.5s | — | 193 |
| default | 600111 的筹码结构怎么样？ | 2 | 10.9s | ✅ | 642 |
