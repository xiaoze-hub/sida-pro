# 量化交易/回测框架精品开源项目调研

> 调研日期: 2026-08-25 | 模型: DeepSeek v4 Pro 0813
> 背景: SIDA (PanWatch) 已缝合 TradingAgents、Kronos、Chronos-Bolt、XGBoost，
> 内置轻量事件式回测引擎 (`src/core/backtest`)，预留 `quant_adapters.py` 适配器协议。
> 本报告扫描 9 个候选框架，输出 Top5 缝合推荐。

---

## 现有架构定位

SIDA 已提供:
- **回测内核**: `src/core/backtest/engine.py` — 纯 Python 事件式回测，零外部依赖
- **适配器协议**: `src/core/quant_adapters.py` — `BacktestAdapter` Protocol，已预留 `vectorbt`/`rqalpha`/`qlib` 三个可选后端
- **策略引擎**: `src/core/strategy_engine.py` + `strategies/panwatch_strategies.yaml`
- **模拟盘**: `src/core/paper_trading_engine.py` → `paper_trading.py` API
- **预测引擎**: `forecast_server.py` (Kronos + Chronos + XGBoost)

缺口方向:
- 缺少向量化批量回测（当前事件式逐信号，大批量策略筛选慢）
- 缺少因子研究管线（IC/IR 分析、因子库管理）
- 策略执行/下单层仅 MiniQMT 实验性，缺正式交易网关
- 缺少 A 股高保真成本模型（当前仅基础印花税+佣金）

---

## 1. Qlib — Microsoft AI 量化平台

| 维度 | 评估 |
|------|------|
| **GitHub** | https://github.com/microsoft/qlib |
| **Stars** | 47,922 ★ |
| **许可证** | MIT ✅ |
| **语言** | Python |
| **最后更新** | 2026-07 (活跃) |
| **核心能力** | AI-oriented 量化研究平台: 因子表达式引擎(Alpha158/360)、模型训练管线(LightGBM、GRU、Transformer)、自动超参搜索、组合优化、在线预测 |
| **A股适配** | 数据无关 — 需自行接入 A 股数据源。社区有 `qlib-data` 支持 A 股日频/分钟频，但非官方捆绑 |
| **与 SIDA 互补点** | 因子研究管线 (IC/IR 分析) 填补 SIDA 空白；ML 模型库与现有 Kronos/XGBoost 互补；自动超参搜索可优化策略参数 |
| **缝合成本** | **中** — 已在 `quant_adapters.py` 预留为可选后端；需实现 `qlib.init()` 数据适配器将 SIDA 的 `marketdata` 层接到 Qlib 的 Dataset；模型训练需额外 GPU 资源 |
| **风险** | 相当重量级（~300+ 依赖）；学习曲线陡；Microsoft 主导但社区活跃度尚可 |
| **结论** | ✅ 推荐缝合 — 作为因子研究后端，不取代现有回测内核 |

---

## 2. Backtrader — 成熟事件驱动回测框架

| 维度 | 评估 |
|------|------|
| **GitHub (上游)** | https://github.com/backtrader2/backtrader (268 ★, GPL-3.0) |
| **GitHub (社区增强版)** | https://github.com/cloudquant/backtrader (156 ★, GPL-3.0, 45%+ 性能提升) |
| **许可证** | GPL-3.0 ⚠️ |
| **语言** | Python |
| **核心能力** | 事件驱动回测引擎，50+ 内置技术指标，多数据源(CSV/Pandas/IB/Yahoo/CCXT/CTP)，Tick-to-Bar 多频段，交互式图表，一码回测/实盘 |
| **A股适配** | 无原生支持。通过 `PandasData` 馈入 A 股 K 线数据可行；CTP 数据源支持期货；无涨跌停/印花税/交易日历等 A 股专用逻辑 |
| **与 SIDA 互补点** | 成熟的事件驱动回测引擎（SIDA 已有轻量版，Backtrader 提供更完整的订单簿/滑点/佣金模型）；Live trading 支持 IB/CTP/CCXT |
| **缝合成本** | **低** — 数据通过 PandasData 接入；策略可包装为 SIDA 的 `BacktestAdapter`。但 GPL-3.0 拷贝左许可证与 SIDA 的 MIT 项目存在法律冲突风险 |
| **风险** | GPL-3.0 许可证是最大障碍（除非 SIDA 也改 GPL 或独立部署）；上游 (`backtrader2`) 维护频率低，`cloudquant` 分支活跃但社区小 |
| **结论** | ⚠️ 待定 — 技术上易缝合，法律上 GPL 冲突；仅适合作为独立部署的服务形态引用 |

---

## 3. Zipline-reloaded — 华尔街级回测管线

| 维度 | 评估 |
|------|------|
| **GitHub** | https://github.com/stefan-jansen/zipline-reloaded |
| **Stars** | 1,927 ★ |
| **许可证** | Apache-2.0 ✅ |
| **语言** | Python |
| **核心能力** | Quantopian 遗产 — 基于数据 bundle 的日频/分钟频回测，交易所日历，资产元数据，完整交易成本模型，绩效分析工具(tear sheet) |
| **A股适配** | 极弱 — 中心化 bundle 系统绑定 US 股票/ETF。需写自定义 `A股Calendar` + `A股Exchange` + `A股Bundle` 全套适配 |
| **与 SIDA 互补点** | 最成熟的回测数据管线设计（bundle ingest → pipeline API → 策略跑 → 绩效分析）；Tear sheet 报告可与 SIDA 的 WeCom 报告联合 |
| **缝合成本** | **高** — 需要写完整的 A 股 bundle adapter（交易日历、停复牌、涨跌停、送转除权）、自定义 bundle 数据下载工具；`apache-2.0` 许可证友好 |
| **风险** | 社区较小（1.9k ★），维护者单一；数据 bundle 体系对 A 股特殊规则（T+1、涨跌停、印花税）支持差；分钟频回测极慢 |
| **结论** | ❌ Phase 0 不建议 — 缝合成本高，边际收益低；Apache-2.0 是优点，但 A 股适配工作量> 收益 |

---

## 4. vnpy — 全栈开源量化交易平台

| 维度 | 评估 |
|------|------|
| **GitHub** | https://github.com/vnpy/vnpy |
| **Stars** | 44,749 ★ |
| **许可证** | MIT ✅ |
| **语言** | Python |
| **核心能力** | 全栈交易平台: CTP/迷你QMT/IB/易盛等 20+ 交易接口，CTA/套利/期权/组合策略引擎，回测+实盘，RPC 分布式，K 线图表，社区生态完整 |
| **A股适配** | ✅ **原生 A 股** — 支持 CTP（期货）、xt（迅投/迷你QMT，股票）、tushare/ifind/wind/rqdata 等数据源，A 股交易日历/涨跌停/手续费已内置 |
| **与 SIDA 互补点** | **交易执行层** — SIDA 缺正式下单接口，vnpy 的 `xt` 网关可直接对接迷你QMT 实现 A 股实盘；其 CTA 引擎为 SIDA 策略信号提供执行骨架；策略回测结果可直接用于实盘 |
| **缝合成本** | **中** — vnpy 是独立应用框架（有自己的 Event Engine、日志、GUI），非 Python 库式调用。最佳缝合方式: 将 SIDA 的信号通过 vnpy 的 `XTP` 或 `xt` 网关执行，或跨进程 RPC 调用。 |
| **风险** | 重量级安装（需要 C++ 编译的 CTP 接口等）；其 Event Engine 与 SIDA 的 asyncio 架构冲突，需跨进程/消息队列隔离 |
| **结论** | ✅ 推荐缝合（执行层）— 作为交易执行网关，不侵入 SIDA 主架构 |

---

## 5. zvt — 模块化 A 股量化框架

| 维度 | 评估 |
|------|------|
| **GitHub** | https://github.com/zvtvz/zvt |
| **Stars** | 4,284 ★ |
| **许可证** | MIT ✅ |
| **语言** | Python |
| **核心能力** | 模块化量化框架: 多数据源（akshare/tushare/eastmoney 等）、基本面/技术面/资金面因子、证券选股、回测引擎、可视化、因子研究 |
| **A股适配** | ✅ **原生 A 股** — 通过 akshare/tushare 直接获取 A 股行情、财务、资金流、龙虎榜、板块数据，内置 A 股交易日历 |
| **与 SIDA 互补点** | **因子研究管线** — zvt 的 `Factor` 体系（`TechnicalFactor`/`FundamentalFactor`）可直接复用 SIDA 的 `marketdata` 层；其选股 + 回测流程可补充 SIDA 的策略评估；`zvt.draw` 可视化模块可增强报告 |
| **缝合成本** | **低** — 模块化设计 (`zvt.contract`/`zvt.factor`/`zvt.api`)，可单独引入因子模块而不搬整个框架；数据层可替换为 SIDA 的 `marketdata`；MIT 许可证无风险 |
| **风险** | 社区活跃度一般（2026-07 最后更新）；部分模块依赖 akshare 的稳定性；代码质量参差不齐 |
| **结论** | ✅ **推荐缝合（优先）** — 最低风险、最高 A 股适配、模块化设计、MIT 许可证，与 SIDA 天然互补 |

---

## 6. DualAlpha-Lite — 双轨集成学习 A 股研究系统

| 维度 | 评估 |
|------|------|
| **GitHub** | https://github.com/motto-debug/dualalpha-lite |
| **Stars** | 12 ★ |
| **许可证** | **无** ⚠️ |
| **语言** | Python |
| **核心能力** | TX/BS 双轨集成学习系统: 结合技术形态、资金流、成本偏离、VWAP、量价共振等因子，使用 XGBoost/LightGBM/CatBoost/Transformer 多模型集成 |
| **A股适配** | ✅ **原生 A 股** — 设计目标即为 A 股研究，使用东方财富等国内数据源 |
| **与 SIDA 互补点** | 双轨集成学习思路（TX=技术/BS=资金流）与 SIDA 的 AI 分析管线逻辑一致；其因子集可融入 SIDA 的因子库 |
| **缝合成本** | **低** — 代码量小（~12 star 项目，只有一个核心文件），可提取其因子工程逻辑。但**无许可证**，需联系作者确认 |
| **风险** | ⭐ 仅 12 star，2026-06-24 创建，项目极新且无 license；无社区、无文档、无测试；可能随时弃坑 |
| **结论** | ❌ Phase 0 不建议 — 可关注其因子思路，但代码质量和许可状态不可用于生产 |

---

## 7. QuantStock (多因子选股系统)

| 维度 | 评估 |
|------|------|
| **GitHub** | https://github.com/xiaosicau/quantstock-selection-system |
| **Stars** | 22 ★ |
| **许可证** | NOASSERTION ⚠️ |
| **语言** | Python |
| **核心能力** | 多因子选股系统: 多数据源、实时行情、智能因子分析、风险监控 |
| **A股适配** | ✅ **原生 A 股** — 设计为 A 股选股 |
| **与 SIDA 互补点** | 多因子选股流水线思路可参考 |
| **缝合成本** | **低** — 代码量小，可提取因子逻辑。但无许可证 |
| **风险** | 2025-08 最后更新，已近一年无维护；22 star，NOASSERTION 许可证不可用于商业项目 |
| **结论** | ❌ 不建议 — 已经停更，无正规许可证 |

---

## 8. T1.AI — 未找到

| 维度 | 评估 |
|------|------|
| **GitHub** | 经 GitHub 搜索，未找到名称为 `T1.AI` 或 `T1AI` 的量化交易开源项目 |
| **结论** | ❌ 不存在或非公开项目 — 无法评估 |

---

## 9. Swell Quant — A 股日频 AI 预测工具

| 维度 | 评估 |
|------|------|
| **GitHub** | https://github.com/18355166248/swell-quant |
| **Stars** | 0 ★ |
| **许可证** | **无** ⚠️ |
| **语言** | Python |
| **核心能力** | 面向个人的 A 股日频 AI 量化预测: 拉取行情、构建因子、训练 LightGBM、Top N 回测、LLM 生成研究解释报告 |
| **A股适配** | ✅ **原生 A 股** — 设计目标即为 A 股日频预测 |
| **与 SIDA 互补点** | LightGBM 预测 + LLM 报告生成管线与 SIDA 现有架构重叠；其因子构建思路可供参考 |
| **缝合成本** | **低** — 极少量代码，可提取因子逻辑。但**无许可证**，0 star |
| **风险** | 0 star，2026-07-02 创建，无 license，无文档，个人 hobby 项目 |
| **结论** | ❌ 不建议 — 生产质量不可用，无许可证，无社区 |

---

## Top5 推荐清单

| 排名 | 框架 | GitHub | 许可证 | 推荐理由 | 一句话缝合建议 |
|:---:|------|--------|:------:|----------|----------------|
| 🥇 | **zvt** | https://github.com/zvtvz/zvt | MIT ✅ | 模块化 A 股原生、低缝合成本、因子研究管线与 SIDA 天然互补 | 实现 `BacktestAdapter` 包装 zvt 的 Factor 体系，用 SIDA 的 `marketdata` 替换 akshare 数据源，快速获得因子 IC/IR 分析能力 |
| 🥈 | **vnpy** | https://github.com/vnpy/vnpy | MIT ✅ | A 股交易执行层标准答案，44k star 生态 | 通过 `xt` 网关将 SIDA 策略信号转为迷你QMT 实盘订单，跨进程 RPC 通信，不侵入 SIDA 主架构 |
| 🥉 | **Qlib** | https://github.com/microsoft/qlib | MIT ✅ | 头部 AI 量化平台，ML 因子研究领域最强 | 按 `quant_adapters.py` 预留接口实现 Qlib 数据适配器，用其 Alpha158/360 因子集 + LightGBM 模型管线增强 SIDA 预测能力 |
| 4 | **Backtrader** | https://github.com/backtrader2/backtrader | GPL-3.0 ⚠️ | 成熟事件驱动回测，指标丰富 | 以独立 Docker 容器部署 Backtrader 服务，通过 HTTP/gRPC 接收 SIDA 信号回测请求，避免 GPL 传染 |
| 5 | **Zipline-reloaded** | https://github.com/stefan-jansen/zipline-reloaded | Apache-2.0 ✅ | 回测数据管线设计最优雅，Tear sheet 报告 | 仅推荐在需要标准化绩效分析报告（Tear sheet）时缝入，需写 A 股 bundle adapter，成本较高 |

---

## 缝合优先级建议

```
Phase 1 (当前)  → zvt    — 因子研究管线（低风险，高回报）
Phase 2 (近期)  → vnpy   — 交易执行层（实盘核心需求）
Phase 2 (近期)  → Qlib   — ML 因子研究（增强 AI 管线）
Phase 3 (远期)  → Backtrader — 独立回测服务（填补高保真回测缺口）
Phase 3 (远期)  → Zipline-reloaded — 标准化绩效报告（可选）
```

---

## 框架矩阵对比

| 框架 | Stars | 许可证 | A股原生 | 回测引擎 | 因子研究 | 实盘执行 | 缝合成本 | 与 SIDA 现有架构重叠 |
|------|:-----:|:------:|:-------:|:--------:|:--------:|:--------:|:--------:|:--------------------:|
| **zvt** | 4.3k | MIT | ✅ | 事件式 | ✅ 因子 | ❌ | 低 | 中（数据层可替换） |
| **vnpy** | 44.7k | MIT | ✅ | 事件式 | ❌ | ✅ 20+接口 | 中 | 低（独立架构） |
| **Qlib** | 47.9k | MIT | ❌ | 向量化 | ✅ AI 因子 | ❌ | 中 | 低（ML 管线新增） |
| **Backtrader** | 268 | GPL-3.0 | ❌ | 事件式 | ❌ | ✅ 部分 | 低 | 高（已有内置回测） |
| **Zipline-r** | 1.9k | Apache-2.0 | ❌ | 事件式 | Pipeline API | ❌ | 高 | 中（数据管线设计） |
| **DualAlpha-Lite** | 12 | 无 | ✅ | ❌ | 双轨集成 | ❌ | 低 | 低（因子思路可借） |
| **QuantStock** | 22 | NOASSERTION | ✅ | ❌ | 多因子 | ❌ | 低 | 中（已停更） |
| **T1.AI** | — | — | — | — | — | — | — | 未找到对应项目 |
| **Swell Quant** | 0 | 无 | ✅ | ❌ | LightGBM | ❌ | 低 | 高（与 SIDA 预测管线重叠） |

---

## 附录: 已有预留

`src/core/quant_adapters.py` 已声明 `_OPTIONAL_BACKENDS`:
```python
("vectorbt", "vectorbt"),   # 向量化批量回测 ← 可在 zvt/qlib 之后缝入
("rqalpha", "rqalpha"),     # A 股高保真成本撮合 ← 替代 Backtrader 的 GPL 选项
("qlib", "qlib"),           # ML 因子研究
```

**rqalpha** (https://github.com/ricequant/rqalpha, 6.7k ★, Apache-2.0) 是 Ricequant 开源的 A 股回测框架，原生支持 A 股规则（T+1、涨跌停、印花税、交易日历），可作为 Backtrader 的 Apache-2.0 替代品。建议在 Phase 3 评估是否将 rqalpha 加入备选清单。