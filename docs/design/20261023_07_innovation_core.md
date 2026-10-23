# 基于 SIDA-Pro 基因的单域纵深创新方向（设计稿 07）

> 基线：v0.48.0 / 5427af0
> 范围：单页面/单领域纵深创新；跨模块处只写「本域新增什么 + 依赖哪条已有能力」，不展开流水线。

## 0. 真实资产引用清单（本稿只引用以下已落地模块/表）

| 资产 | 路径 / 表 | 说明 |
| --- | --- | --- |
| 四档资金结构 | `src/core/exday_structure.py`、`src/collectors/exday_daily.py`、表 `exday_daily`（Migration 220） | 通达信四档 4×4 矩阵（特大/大/中/小 × 买/卖）+ 委托粒度（`gran_hiding`、`order_fill_ratio`、`cancel_skew`、`chip_migration`），每日 15:45 落库，口径 `tdx_exday4` |
| 因子胜率 | `src/core/backtest_lab.py`、表 `factor_winrate_daily`（Migration 221） | 五大因子族（`gs_state`/`ai_activity`/`gs_x_activity`/`wyckoff`/`limit_up_quality`）T+1/3/5 胜率、3% 达标率、盈亏比 + 同池无条件基准（`baseline_win_rate_pct`/`baseline_avg_return_pct`）+ 超额，`untradable_skipped` 记录剔除的一字板样本；参谋工具 `get_factor_winrate`（`src/agents/chat/registry.py:2967`） |
| 盘后复盘 | `src/core/market_replay.py`、表 `market_replay_daily`（Migration 222） | 东财三池同源（涨停/炸板/跌停）+ 连板梯队 + 炸板率/封板率 + 晋级率 + 情绪周期，每日 15:50 落库；参谋工具 `get_market_replay`（`src/agents/chat/registry.py:3019`） |
| 威科夫 | `src/core/wyckoff.py`（`analyze_wyckoff`，四类标：弹簧/量枯竭/努力背离/破位）、`src/core/wyckoff_engine.py`；端点 `GET /klines/{symbol}/wyckoff`（`src/web/api/klines.py:1749`） | K 线图层 `layersVisible.wyckoff` 已存在 |
| 筹码 | `src/core/chip_distribution.py`（`compute_chips`、`compute_main_accumulation_chips`、`fetch_sina_hist_price`）、`exday_daily.chip_migration` 列 | 端点 `GET /price-distribution/{symbol}` 与 `/history`（`src/web/api/price_distribution.py:53,87`） |
| K 线组件 | `frontend/packages/biz-ui/src/components/KlineChart.tsx`（`KlineSubchart = 'vol'\|'macd'\|'active_ratio'\|'phase'\|'activity'\|'dark_pool'`；`layersVisible`；`onRangeSelect`/`onRangeStats`）、`frontend/src/components/RangeStatsCard.tsx`、`frontend/src/pages/StockWorkbench.tsx`、`frontend/src/pages/workbench/`（L2Tab / ResearchTab / IndexBody / DecisionVerdictCard） | 区间拖拽与区间统计回调均已实现 |
| 自选/告警 | `src/web/api/stocks.py`（`POST /stocks`、`PUT /reorder`）、`src/web/api/price_alerts.py` | 复盘回填的落点 |
| 战报页范式 | `frontend/src/pages/WarReport.tsx`（`CaliberBadge`、`sida-page-enter`、缺源显式「无数据」） | 复盘联动页的视觉参照 |

**覆盖范围声明**：以上模块/表/端点均已在 v0.47–v0.48 仓库中验证存在（函数签名与表结构取自源码）；本稿 §1–§6 中的新增端点、新增组件、新增页面均为**设想**，仓库中不存在，落地时按「后端改动点」一节实现。颜色/字体/阴影均引用 `frontend/src/index.css` 已落地令牌：`--stock-up: #E53935`（买/涨红）、`--stock-down: #43A047`（卖/跌绿）、hairline 边框 `border-border/40`、`.shadow-float` 仅限框选即问浮条、`tabular-nums`、字号 10/11/12/13/16/20。

---

## 1. 方案一：四档资金结构副图（exday4 基因）

**现状与数据底座**：`KlineChart` 的 L5 副图档位 `KlineSubchart` 目前 6 档（`vol/macd/active_ratio/phase/activity/dark_pool`），最接近的是 `dark_pool`（暗盘分摊近似，图上线 `L1_approx`）。而 `exday_daily` 自 v0.48 起每日 15:45 落库，`xl/lg/md/sm_buy/sell_amt` + `xl_active_net…sm_active_net` 就是「真实、当日、无分摊」的四档净额，但**没有任何页面消费它**——只在 `exday_structure.render()` 的文本报告里出现。

**创新点**：把通达信四档资金结构做成 K 线主图下方的一档副图「四档结构」，特大/大/中/小 四档主动净额堆叠成带，主动净额线（`main_active_net`）叠画，替代 dark_pool 的「近似」定位成为第四口径真源。

**新增 UI**：
- 页面：`StockWorkbench` 主图区（K 线仍占 ≥80% 屏宽，副图高度约 1/4 视口，hairline 分隔 `border-b border-border/40`）。
- 组件：`KlineChart.tsx` 的 `KlineSubchart` 追加 `'exday4'` 档位；新增副图渲染组 `Exday4Subchart`（`frontend/packages/biz-ui/src/components/` 内与 `MinutePane.tsx` 同层新建）。
- 图层：主图 `layersVisible` 不动；副图切到 `exday4` 时，K 线蜡烛上叠加 `xl` 主动买/卖标记（特大档净流日为实心点，红色 `#E53935` 买 / 绿色 `#43A047` 卖）。
- 按钮文案：副图档位切换器新增「四档」；悬停读数条文案「特大 +x.xx亿 · 大 ±x.xx · 中 ±x.xx · 小 ±x.xx（tdx_exday4）」；当日无数据显式「--（今日未落库）」。
- 视觉：堆叠带买侧 `#E53935` 25% 透明、卖侧 `#43A047` 25% 透明；`main_active_net` 线 `#E53935` 1px；金额走 `toAmount`（亿/万），`tabular-nums`，字号 10。同屏挂 `CaliberBadge` 标签 `tdx_exday4`，与 `eastmoney4`/tick 口径并列不可换算——**不**写「主力」。

**后端改动点**：
- 新建 `src/web/api/exday.py`：`GET /exday/{symbol}/series?days=N`（默认 60，上限 250），返回逐日 `{date, xl_net, lg_net, md_net, sm_net, main_active_net, available}`；读库走 `exday_daily`（`src/collectors/exday_daily.py` 已有读表函数），缺日 `available=false` 不下发 0。
- 新建 `src/core/exday_series.py`：`load_series(symbol, days)` 纯读库聚合函数，`exday_structure.py` 保持纯计算不掺 DB。
- `src/web/app.py` 挂载 router（同 `price_distribution` 挂载方式）。

**数据前提**：已有。`exday_daily` 自 2026-10-23 起逐日累积，**无历史回填**（`get_exday_data` 只返当日 1 行，这是 Migration 220 docstring 明说的红线）——前 30 天内副图左侧为空白并注「结构数据自 YYYY-MM-DD 起」，不做回填、不用 OHLC 分摊伪装。

**验收线**：① 副图切到「四档」，最近 5 个交易日逐日四档净额与 `/exday/{symbol}/series` 返回值一致（`tabular-nums` 对齐）；② 主图宽度占比 ≥80% 不变；③ 缺日显式 `--`，不显示 0；④ 副图上出现 `tdx_exday4` 口径标签。
**不做项**：不回填历史四档（口径不可换算是铁律）；不把四档净额当「主力意图」标签（那是 tick 口径）；不做四档 × 威科夫的自动共振计算（留给跨模块那一路，本域只把数据摆进副图）。

---

## 2. 方案二：区间胜率回显（factor_winrate_daily 基因 + 威科夫基因）

**现状与数据底座**：`KlineChart` 已有 `onRangeSelect`（拖拽选段）与 `onRangeStats`（区间统计），`RangeStatsCard` 只回显资金累计与事件计数。而 `factor_winrate_daily` 已按五大因子族 × 桶 × T+1/3/5 存了胜率、3% 达标率、盈亏比、同池无条件基准与超额（`baseline_win_rate_pct`/`excess_win_rate_pp`），且 `_is_untradable_next_bar` 剔除了 T+1 一字板不可买样本（`untradable_skipped` 列留痕）。威科夫家族桶（`wyckoff`）与区间内 `analyze_wyckoff` 打出的弹簧/量枯竭等标的可以**同屏对表**。

**创新点**：K 线划定区间时，在区间统计卡上实时回显「这段行情所属形态族」的 T+5 胜率与同池基准，把统计结论钉在选段上——不是预测，是「你选里的这种行情，历史上 T+5 表现如何」。

**新增 UI**：
- 页面：`StockWorkbench`，沿用 `RangeStatsCard` 所在槽位（K 线下方区间统计带）。
- 组件：`RangeStatsCard.tsx` 新增一节「形态胜率」：家族徽标（威科夫弹簧/量枯竭…，取自区间内 `GET /klines/{symbol}/wyckoff` 命中标）、`T+5 胜率 xx%（基准 yy%，n=zz，剔除一字板 mm 笔）`；无命中家族时显式「本区间无命中因子族，不显示胜率」，**不**写「无风险」。
- 图层：K 线区间选中带（半透明 `#E53935` 8% 底）上，命中威科夫标的的 K 线柱加 1px 红色顶帽标记（复用 wyckoff 图层已有标记样式，`layersVisible.wyckoff` 控制显隐）。
- 按钮文案：区间统计卡右上角新加「对表」小按钮（`text-muted-foreground hover:text-foreground`，`rounded px-1`），点开浮条（`.shadow-float` 唯一合法场景之一）列 T+1/T+3/T+5 三档全表。
- 字号 11/12，`tabular-nums`，百分比带符号色：胜率 ≥ 基准用 `text-stock-up`，低于基准 `text-stock-down`（比较色语义，不是涨跌色，需加 title 说明）。

**后端改动点**：
- 新建 `src/web/api/range_winrate.py`：`GET /klines/{symbol}/range-winrate?from=&to=&horizon=5`——① 调 `src/core/kline_patterns.detect_pattern_marks` + `src/core/wyckoff.analyze_wyckoff` 取区间内命中桶；② 调 `src/core/backtest_lab.load_winrates(db, factor_family=..., horizon=...)` 取最新一期快照对表；③ 返回 `{hits: [{bucket_key, bucket_label, sample_count, untradable_skipped, win_rate_pct, baseline_win_rate_pct, excess_win_rate_pp}], as_of_date}`。
- 不新建表：`factor_winrate_daily` 已含基准与剔除数，直接读。

**数据前提**：已有。胜率快照每日由 `backtest_lab.run_and_persist`/`run_scheduled` 生成（`as_of_date` 最新一期）；同屏必须标注 `as_of_date`。样本红线沿用 `MIN_SAMPLE = 20`：`sample_count < 20` 的桶不下发胜率，只显「样本 n 不足（n<20，不评级）」。

**验收线**：① 拖拽一个含威科夫弹簧标的的区间，「形态胜率」节显示该桶胜率 + 基准 + `n=` + 剔除一字板笔数，四项齐全才出数；② `as_of_date` 与 `factor_winrate_daily` 最新一期一致；③ 空区间/无命中时该节整体隐藏，不显示 0%。
**不做项**：不预测「本次区间未来 T+5 会怎样」（胜率只描述历史同类）；不做区间 × 多族的交叉推断；不扩展回测宇宙（`limit_symbols` 默认 400 是性能约束，不动）。

---

## 3. 方案三：复盘三池一键回填（market_replay_daily 基因）

**现状与数据底座**：`market_replay_daily` 每日 15:50 落库完整三池（涨停/炸板/跌停）+ 连板梯队（`ge2_count/ge3_count`）+ 炸板率 + 晋级率 + 情绪周期（`phase`），`payload` 存全证据 JSON（`src/core/market_replay.py: build_replay/to_row/persist`）。战报页 `WarReport.tsx` 目前只做当日主力汇总，**没有**「复盘池 → 我的池子」的动线；自选是 `src/web/api/stocks.py` 的 `POST /stocks`，盘中盯盘走 `price_alerts`。

**创新点**：复盘页三池表格每一行加一个「回填」按钮：一键进自选/观察池 + 按池子类型预填次日盯盘条件（涨停池→次日开板即预警；炸板池→尾盘未回封预警；跌停池→仅观察），复盘到盯盘的间隔从「手动抄代码」压到 2 秒。

**新增 UI**：
- 页面：新建 `frontend/src/pages/ReplayPool.tsx`（路由 `/replay-pool`，视觉走 `WarReport.tsx` 范式：`sida-page-enter space-y-4`、`Card`、`CaliberBadge` 标「东财三池同源，与 TQ 自算口径并存不混算」）。
- 组件：三池 `JbpPoolTable` 同款表格（复用它），每行末列新增按钮「回填」；回填成功行级 toast 文案「已入自选 + 已挂『开板即预警』（可撤）」；情绪周期横幅显示 `phase_label`（`text-[16px] font-medium`），炸板率/封板率两枚 chip `text-[11px] tabular-nums`。
- 浮层：批量选择时顶部出现汇总条「已选 N 只 · 涨停 X / 炸板 Y / 跌停 Z · [一键回填]」（hairline 顶边，不叠阴影；「框选即问」浮条才用 `.shadow-float`，本条不用）。
- 颜色：涨停行左缘 2px `#E53935`，跌停行 2px `#43A047`，炸板 `#E53935` 半透明（`--stock-up-tint`）。

**后端改动点**：
- 新建 `src/web/api/replay.py`：
  - `GET /replay/pools?date=&kind=limit_up|broken|limit_down`——读 `market_replay_daily.payload`（经 `market_replay.read_latest`）返回逐只 `{symbol, name, streak, first_time, open_times, promo_note}`；
  - `POST /replay/backfill`——body `{date, symbols, target: 'watchlist', alerts: 'auto'}`，内部**只编排已有能力**：`POST /stocks`（`stocks.py:create_stock` 语义）+ `price_alerts` 建预警（开板预警/未回封预警/观察三型按池子类型映射，映射表写死在本文件头注释）；幂等：已在自选的跳过并回 `skipped` 名单。
- 不做独立新表：三池明细已在 `payload` JSON 里，不复制。

**数据前提**：已有（`market_replay_daily` 每日 15:50）。边界：仅 CN 市场（`market='CN'`）；非交易日复盘缺期时页面显式「最近一期为 YYYY-MM-DD」。

**验收线**：① 三池表格数字与 `market_replay_daily` 当行 `limit_up_count/broken_count/limit_down_count` 一致；② 点「回填」后自选列表出现该票且预警已建（`price_alerts` 可查）；③ 重复回填不产生重复自选（幂等）。
**不做项**：不做「三池 → 次日盘前报告 → 盘中推荐」联动流水线（那是跨 3 模块的路，本域只到「入池 + 挂预警」）；不给回填票加权重/评分；炸板池回填默认**不**挂买卖预警，只挂「未回封」观察预警——不替用户下单意图。

---

## 4. 方案四：拆单隐匿度健康度仪表（exday_daily 委托粒度基因）

**现状与数据底座**：`exday_daily` 已落委托粒度字段：`gran_hiding`（拆单隐匿度）、`b_order/s_order`（委托笔数）、`cjbs`（笔数比）、`avg_order_lots` vs `avg_trade_lots`、`cancel_buy_rate/cancel_sell_rate/cancel_skew`（撤单不对称）、`order_fill_ratio`。`exday_structure._conclusions()` 已产出文本级结论（`render()` 渲染），但**没有单票仪表化的健康度**；盘中撤单画像已有（`/market/cancel-profile/{symbol}`，`CancelProfilePanel`，L2Tab 消费，60 日基线 + z 值）。

**创新点**：把委托粒度 6 字段压缩成一张「主力拆单伪装」三灯仪表——拆单隐匿 / 撤单偏见 / 挂单兑现，各给 0–100 分与一句话，和盘中撤单画像（60 日基线）并列，回答「这只票的主力今天是不是在拆单藏着做」。

**新增 UI**：
- 页面：`L2Tab`（盘口资金标签）内新增区块「委托粒度健康」（`@/pages/workbench/` 下新建 `ExdayHealthCard.tsx`，与 `CancelProfilePanel`、`PriceDistributionPanel` 并列，L2Tab 已有该标签取数节奏 60s，盘后区块自动降为日更）。
- 组件：三行灯（每行：灯点 + 指标名 `text-[11px]` + 分数 `text-[13px] tabular-nums` + 一句话 `text-[10px] text-muted-foreground`）：
  - 拆单隐匿（`gran_hiding` 高分=拆单越细=越像在藏）：灯色 `#E53935`（藏得深）/ 中性灰 / `#43A047`（敞亮）；
  - 撤单偏见（`cancel_skew` 绝对值 + 方向）；
  - 挂单兑现（`order_fill_ratio` 低 + 笔数比异常 = 挂而不成）。
- 按钮文案：区块头右侧「明细」折叠出 6 字段原始值表（hairline 分隔）；缺日显「--（未落库）」。
- 图层：无 K 线改动；本域只占 L2Tab 一个 slot。

**后端改动点**：
- 新建 `src/core/exday_health.py`：`score(symbol, days=5) -> {rows: [{metric, score, light, note}], raw: {...}, quality}`——纯函数，读 `exday_daily` 最近 N 日（N<可用天数时按实际天数给「样本 d/N」注记）；阈值表写在本文件内并注释来源（沿用 `exday_structure._conclusions` 的同套判据，不另发明阈值）。
- `src/web/api/exday.py`（方案一已建）追加 `GET /exday/{symbol}/health?days=5`。

**数据前提**：已有（`exday_daily` 逐日累积，无回填）；分位类判据依赖同票 ≥20 日历史，不足时三灯全部显「样本不足，仅展示原始值」——仪表降级为明细表，不打分。

**验收线**：① 三灯分数与后端 `score()` 返回逐字一致；② `quality != 'ok'` 的日整行灰显不打分；③ 与 L2Tab 既有撤单画像并列时两口径各带标签（盘中 tck 60 日基线 vs 盘后 tdx_exday4 当日粒度），**不互相换算**。
**不做项**：不推「主力在出货/吸筹」结论（灯只描述行为形态，结论句一律「现象：… 供参考」句式）；不做多票横排（那是扫描域的事）；不动 `cancel_profile` 既有端点。

---

## 5. 方案五：一字板/连板筹码峰迁移（chip + wyckoff 基因）

**现状与数据底座**：`chip_distribution.py` 有 `compute_chips`（300 日 Sina 价格史 → 筹码分布）与 `compute_main_accumulation_chips`，端点 `/price-distribution/{symbol}`（30s 轮询）与 `/history`、`/archive` 已存在；`exday_daily.chip_migration` 列每日落一个筹码迁移标量；威科夫标（`wyckoff.py` 弹簧/量枯竭/破位）与涨停梯队（`market_replay_daily` 的 `ge2_count`）都在。缺的是「一字板/连板股筹码峰如何一天天内上移」的**时序动画**。

**创新点**：给连板股做「筹码峰迁移」副图：按日切片的重叠直方图 + 逐帧播放（1 日 1 帧），看获利峰/套牢峰随连板整体上移、连板断层日出现双峰——这是量价形态之外的第二证据层。

**新增 UI**：
- 页面：`StockWorkbench`，副图档位 `KlineSubchart` 追加 `'chip'`（切到该档时主图正常，副图区渲染 `ChipMigrationPane`，`frontend/packages/biz-ui/src/components/` 新建，与 `MinutePane` 同层）。
- 组件：堆叠直方图（价格轴与主图 K 线共轴，右端逐日密度带）；播放控件「⏮ 连板日 1/N ▶」（`text-[10px]`，按钮 `rounded border-border/40 px-1`）；帧间 300ms，可暂停；连板日（`market_replay_daily` 梯队或本地 9.5% 收盘=最高判据，沿用 `backtest_lab` 连板计数口径）在时间轴上画 1px 红色刻度。
- 图层：主图叠加「峰位线」两条（获利峰/主力集中位，`#E53935` 虚线 / `#43A047` 实线）；威科夫标（弹簧等）在帧上保持同步显示（受 `layersVisible.wyckoff` 控制）。
- 按钮文案：档位「筹码」；播放条「播放迁移」；无连板日时整档位灰显并注「本股近 N 日无连板日，仅日度切片」。

**后端改动点**：
- `src/web/api/price_distribution.py` 追加 `GET /price-distribution/{symbol}/chip-migration?days=60`：返回 `{frames: [{date, peaks: [{price, weight}], limit_up_streak, chip_migration}], source: 'sina_hist300' | 'exday_chip_migration'}`——帧由 `chip_distribution.compute_chips` 逐日切片重算（读库缓存：结果按 `(symbol, days)` 存 `price_distribution` 既有归档表，盘中不重算）；`chip_migration` 标量列作旁证列展示。
- `src/core/chip_distribution.py` 新增 `slice_frames(klines, limit_up_dates)` 纯函数（不改既有 `compute_chips` 行为）。

**数据前提**：已有但有限：`compute_chips` 依赖 `fetch_sina_hist_price`（300 日 Sina 历史价），连板日判据用 K 线本地可算；`exday_daily.chip_migration` 只有新数据。Sina 源不可用时整档位显「Sina 历史价不可用」，**不**用腾讯分价补帧（口径不同，宁缺毋假）。

**验收线**：① 至少 1 只连板股（≥3 连板）逐帧播放，峰位线与 K 线共轴无错位（帧日期 = K 线日期逐一对上）；② 峰位置信度注记（300 日切片窗口）显示在档位旁；③ 播放不阻塞主图交互（播放中仍可拖拽选段）。
**不做项**：不做筹码 → 压力位/支撑位的自动换算；不做 300 日以外窗口的帧；不做连板预测（迁移图只回放，不预测「下一板」）。

---

## 6. 方案六：形态失效倒计时（factor_winrate_daily T+5 衰减基因）

**现状与数据底座**：`factor_winrate_daily` 每个桶存了 `horizon ∈ {1,3,5}` 的三档胜率（`win_rate_pct`、`hit3_rate_pct`、`profit_loss_ratio`）与基准/超额，`MIN_SAMPLE = 20`、`untradable_skipped` 留痕。`KlineChart` 的 `chartNotes`/威科夫标已能把「某形态出现在哪根 K 线」定位。缺的是把 T+1→T+3→T+5 的**衰减曲线**回显成可执行的「离场窗口提示」——注意：提示离场窗口，**不是**预测价格方向。

**创新点**：K 线选中某形态日（拖拽选段自动命中，或威科夫标点击）时，在主图该日下方挂一枚「失效倒计时」徽标：该形态族 T+5 胜率相对 T+1 的衰减幅度 + 3% 达标率，衰减越快「离场窗口」越靠前提前。

**新增 UI**：
- 页面：`StockWorkbench`。
- 组件：新建 `frontend/src/components/WinRateCountdownBadge.tsx`（与 `DisciplineCoolDownBanner` 同槽位体系，挂 `StockWorkbench` 既有 banner 带）：单行 `text-[11px]`，文案范式「威科夫·量枯竭 T+1→T+5 胜率 58%→44%（基准 41%，n=37，剔除一字板 6 笔）· 衰减 14pp · 离场窗口前移」；衰减 ≥10pp 时徽标底色 `#E53935` 10%（tint），<5pp 时 `#43A047` 10%；中间态中性灰。
- 图层：K 线该日蜡烛下方 3px 红/绿底条（与 GS 买卖点箭头区分：点=买卖信号，底条=失效窗口，两种标记永不同义）；悬停浮条（`.shadow-float`）展开 T+1/T+3/T+5 三行小表。
- 按钮文案：banner 右侧「看基准」展开同池无条件基准对照列；无命中形态日时 banner 不渲染（整条消失，不留灰壳）。

**后端改动点**：
- 新建 `src/core/winrate_decay.py`：`decay_for(bucket_key, as_of_date) -> {h1, h3, h5, decay_pp, baseline, n, skipped}`——读 `factor_winrate_daily`（`backtest_lab.load_winrates` 已有），三档齐全才出 decay，缺档返回 `available=false`。
- `src/web/api/klines.py` 追加 `GET /klines/{symbol}/winrate-countdown`：复用 `/wyckoff` + `/patterns` 的命中定位逻辑，返回「本股近期命中桶 × 各自衰减」；不新表。

**数据前提**：已有。三档快照按日生成，`as_of_date` 必须回显；`n < 20` 的桶整个不渲染（前端也硬校验，双保险）。

**验收线**：① 徽标数字 = 表内三档原值（T+1/T+3/T+5 各列一致）+ 基准列独立可查；② 衰减公式 `decay_pp = h1.win − h5.win` 写进响应（可重算）；③ 缺任一档时该桶不渲染、不插值；④ 全页不出现「预测」「抄底」「追涨」字样——本方案只输出统计窗口，不下单建议（Frozen Ring：`SYSTEM_PROMPT` 不动，交易 403 边界不动）。
**不做项**：不做「自动在 T+3 止损」的指令型功能（无交易接口，交易域 403 红线）；不做跨桶合成衰减；不预测衰减本身会怎样（「离场窗口前移」仅描述统计结构，文案固定含「供参考」）。

---

## 附：六方案落地优先级建议（本稿观点，非承诺）

1. 方案一（四档副图）：数据端点未建但最轻，副图档位已有先例（`dark_pool` 即懒取数范式）；
2. 方案二（区间胜率回显）：`onRangeSelect`/`RangeStatsCard` 基建全在，只差一个对表端点；
3. 方案三（复盘回填）：动线价值最高但牵动自选/预警两端点编排，需幂等验收；
4. 方案五（筹码迁移）：依赖 Sina 300 日史，降级面最大，排后；
5. 方案四（拆单健康度）与 方案六（失效倒计时）：依赖 `exday_daily` / 胜率快照的**样本积累天数**（各自需 ≥20 日/三档齐全），随时间自然解锁，可最后做。

共性红线（全方案适用）：统计胜率一律「样本数 + 同池基准 + 一字板剔除数」三件套，缺一项不渲染；四个资金口径（tick / eastmoney4 / tdx_exday4 / L2 TQ）同屏必各带标签；K 线主角位 ≥80% 宽、买红卖绿、`tabular-nums`、hairline 分隔；`.shadow-float` 只给框选即问浮条；不做大盘方向预测；Frozen Ring 八件不动。
