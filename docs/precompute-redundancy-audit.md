# 冗余设计审计: /decision 预落库 + 前端热路径端点读库/现算分类

- **基线标识**: `main` = v0.13.55 (commit `339d2f6`)。分支 `feat/decision-precompute-20261010`。
- **审计对象(完整路径)**:
  - 后端: `src/web/api/decision.py`、`src/core/decision.py`、`src/core/dark_pool_flow.py`、
    `src/collectors/kline_collector.py`、`src/core/resonance_scan.py`、`src/web/api/resonance.py`、
    `src/web/api/klines.py`、`src/web/api/theme_mood.py`、`src/web/api/quotes.py`、
    `src/web/api/dashboard.py`、`src/web/api/stock_pool.py`、`src/web/cache/biz_cache.py`、
    `src/core/summary_cache.py`、`src/web/migrations.py`。
  - 前端调用面: `frontend/packages/api/src/insight.ts`、`frontend/src/pages/workbench/**`
    (研究标签首屏 `DecisionVerdictCard` → `GET /api/decision/{symbol}`)。
- **覆盖范围**: 审了**工作台/研究首屏 + 数智决策/选股池 + 首页**所调用的热路径端点(下表)。
  **没审**: 交易/模拟盘下单链路、管理后台(admin/pro/invite)、邮件/企业微信推送、chat LLM 链路、
  情报/新闻页、`/forecast/*` 预测引擎内部实现(仅按"远端计算、不含在本仓缓存面"归类)。

## 端点分类(前端热路径)

| 端点 | 前端热路径 | 分类 | 证据 |
|---|---|---|---|
| `GET /api/decision/{symbol}` | 工作台「研究」首屏 | **B**(本次改造)→ A(命中) | `src/core/decision.py:decide` 现拉 120 日 K 线 + `compute_pool_flow`(明/暗盘);`insight.ts` 标"慢接口"、超时放宽 30s |
| `GET /api/klines/{symbol}` | 工作台 K 线 | A | PG hypertable 优先(`KlineCollector._pg_read`),~25ms |
| `GET /api/klines/{symbol}/summary` | 工作台技术面 | A(缓存) | `summary_cache`: biz L1+L2 + PG `summary_cache` 表(冷启动 20-30s 已缓) |
| `GET /api/resonance/scan` | 数智决策/选股池 | A | `resonance_scan.latest()` 只读 `resonance_scan` 表 |
| `GET /api/resonance/activity/{symbol}` | 活跃度副图 | A | `resonance_scan.activity_series` 读 PG |
| `GET /api/resonance/symbol/{symbol}` | 三灯 | A | `symbol_detail` 读 PG klines(日线, 不依赖 L2) |
| `GET /api/theme-mood/*` | 题材情绪 | A | `theme_mood_daily` 表(盘中刷新 + 收盘 15:05 定型) |
| `GET /api/decisions/log`·`/backtest` | 决策账本页 | A | `decision_log` 表 |
| `GET /api/klines/{symbol}/l2-ticks` | L2 页 | A(采样落库) | `query_l2_ticks` 读表(5min cron 落库) |
| `GET /api/stock-pool` | 选股池 | A | `resonance_scan` 表 |
| `GET /api/quotes/{symbol}` | 全站现价 | B(**不可预落库**) | 实时行情(腾讯), 属实时数据 |
| `GET /api/quotes/{symbol}/dark-flow-tq` | 暗盘 | B(**不可预落库**) | 实时 L2 口径 |
| `GET /api/orderbook-ob` | L2 盘口 | B(**不可预落库**) | 实时 20 档(thsdk) |
| `GET /api/seal-quality/{symbol}` | 封单成色 | B(已 60s 采样) | 盘中 60s 采样(`seal_quality_samples`) |
| `GET /api/forecast/*` | 预测 | B(**不可预落库**) | 独立预测引擎(远端计算) |
| `GET /api/dashboard/overview` | 首页 | 混合 | 聚合多源, 已有局部缓存 |

## 改造清单(本次)

- **B 类首选 `/decision/{symbol}`**: 新增 `decision_cache` 表(迁移 v183)+ `src/core/decision_cache.py`
  (全局基底落库 + 读时个性化叠加)+ `src/core/decision_precompute.py`(盘后批算调度)。
- **B 类其余**(quotes/dark-flow-tq/orderbook-ob/seal-quality/forecast): **明确标注属实时数据,
  不可预落库**(seal-quality 已 60s 采样落库), 不改。
- 前端**零改动**(UI 不变, 仅透传 `cached`/`computed_at`)。

## 实测(本地隔离 DB + 预置 120 根真实日线 + 真实 `compute_pool_flow`)

- BEFORE(每次现算)中位 **~664ms** → AFTER-hit(命中)中位 **~13ms**(**~51×**); miss ≈ BEFORE。
- 说明: 本地无 `thsdk`, `compute_pool_flow` coverage=`dark_only` 且 L2 熔断后快速失败 ⇒ 生产含 L2 的
  慢路径更重, 提速不低于此。命中响应 `cached=True`、`computed_at` 与 miss 一致、`cache_source=ttl`。
