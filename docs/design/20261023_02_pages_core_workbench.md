# 核心盯盘与个股研判域 — UI/按钮/色调像素级走查

> 基线：**v0.48.0 / 5427af0**（`test: 同步复盘首步结构钉子为 get_market_replay (v0.48.0 门禁修复)`）
> 审计日期：2026-10-23　分支：`docs/design-2`　文档编号：`20261023_02`
> 视角：行情终端 / 看图决策 / 不做数据堆砌 / K 线绝对主角

## 0. 审计路径清单（实际读取）

| 类别 | 路径 | 读法 |
|---|---|---|
| 页面 | `frontend/src/pages/Dashboard.tsx`（1495 行 / 76 KB） | grep 定位 + 分段读 L595–1084 |
| 页面 | `frontend/src/pages/StockWorkbench.tsx`（537 行） | grep 定位 + 分段读 L234–285、L420–537 |
| 页面 | `frontend/src/pages/Stocks.tsx`（56 行） | 全读 |
| 子目录 | `frontend/src/pages/workbench/`（4 文件 + `tabs/` 6 文件） | `ls` + `SuggestTab.tsx` L150–200 |
| 子目录 | `frontend/src/pages/stocks/`（19 文件） | `ls` + `StocksHeader.tsx` / `StocksTabsBar.tsx` / `AccountsSection.tsx` / `WatchlistSection.tsx` grep |
| 关联组件 | `frontend/src/components/ThreeTierVerdictRow.tsx`（257 行） | 全读（三层滤网唯一实现） |
| 关联组件 | `frontend/packages/biz-ui/src/components/workbench/HeaderBand.tsx`（665 行） | grep + 读 L560–600、L625–660 |
| 关联组件 | `frontend/src/components/PageTabs.tsx` / `packages/biz-ui/src/components/KlineChart.tsx` | 读前 60 行 / grep `interval`/`fq`/`showSignal`/`buildPatternMarkers` |
| 实时 | `frontend/src/hooks/useSourceHealth.ts` | 全读（前 50 行 + 常量区） |
| 实时 | `frontend/src/realtime/useQuoteStream.ts` / `envelope.ts` | `ls` + 全库引用 grep |
| 设计令牌 | `frontend/tailwind.config.js` L50–95 | 全读（`stock-up/down` = `#E53935` / `#43A047`，`gs-go/gs-stop` 语义分离注释在 L63–70） |
| 后端 | `src/core/wyckoff.py`（357 行） | grep 结构 + 读 L32–60、L97–125、L289–357 |
| 后端 | `src/core/caliber.py` | 读 L1–40 + `def` 清单 |
| 后端 | `src/web/api/klines.py` | grep `wyckoff` / `main_intent_structured` / `available` |
| 后端 | `src/web/api/chat.py` / `skills_gateway.py` | grep `get_main_intent` 口径条目 |

`src/core/intraday_profile.py`、`src/core/scenario_engine.py` 已确认存在（298 / 209 行），但**未进入本轮判据**——它们服务于面板内部派生，不产出本域任何可见 UI 状态位；本域四类状态位（情绪/催化/量价/意图）的取数链路已在 `klines.py` 侧核实。后续若做「分时轮廓叠加」，需单列一次走查。

## 1. 覆盖范围

**已审（本域核心三角）**
1. Dashboard：指数带 / 涨跌分布 / 板块资金 / 情绪周期 / 顶部状态栏 / 数据源错误横幅
2. StockWorkbench + `workbench/`：K 线舱、速览栏、深度量化舱、HeaderBand、Tab 体系
3. Stocks + `stocks/`：列表结构、批量能力、行内动作、跳转入参

**明确未审（另立文档，不在本次结论内）**
- 页面：`Opportunities.tsx`、`ThemeMood.tsx`、`Auction.tsx`、`Forecast.tsx`、`DecisionLedger.tsx`、`ShadowAccount.tsx`、`PaperTrading.tsx`、`PriceAlerts.tsx`、`AiCopilot.tsx`、`Agents.tsx`、`Admin.tsx`、`DataSources.tsx`、`CaliberCompare.tsx`、`Login/Landing/Profile/Terms`
- 六个 tab 的**内部**版式（`L2Tab` 65 KB / `FundamentalTab` / `NewsTab` / `ResearchTab` / `ForecastTab`）：只审入口、层级、状态位，不审 tab 内像素
- K 线渲染内核（`KlineChart` 封装层：lightweight-charts 实例化、`_build_layer_data`）
- 指标算法正确性（威科夫阈值、意图判定规则）
- 移动端断点与平板档（本次仅桌面 ≥1280px）
- **无运行实测**：本轮禁起服务，所有"占屏比例/列数"均由 className 静态推算，不是 DOM 实测值

## 2. 全局判定（先给结论，后给证据）

**J1｜Dashboard 不是"整页卡片网格堆叠"，但首屏被两层卡片网格夹住 —— 定为 P1 缺陷。**
主体分层（资金流 L733、共振 L818、异动/分布 L826、工作台 L906）**已经**是 hairline 分隔的良性范式（`border-t border-border/60 pt-3`），这部分不该回改。问题集中在首屏最贵的两段：指数 5 卡（L663）+ 情绪/主线/温度 3 卡（L722）。情绪周期——三层滤网第一层——被降为 1/3 宽的第三列卡片，用户首屏读到的是"8 个盒子"，不是"一个结论"。改法见 D1/D3，方向是**指数带压成单行行情带、情绪升为全宽阶段轨道**，不是把卡片做精致。

**J2｜工作台的 K 线被两层卡片包裹、且被 320px 速览栏挤出 80% 线 —— 定为 P0 缺陷。**
`StockWorkbench.tsx:446` `rounded-lg border border-border/80 bg-card/40 p-2.5 shadow-sm` 包一层，`:448` 再 `rounded border border-border/60 bg-background/50 p-2` 包第二层。K 线本体 `height={420}`（L455），宽侧再减 320px rail（L474）。这直接违反"无卡片包裹 + ≥80% 屏宽"。改法见 W1/W2。

**J3｜"三层滤网"数据面齐、三态面缺、位置错。**
三层数据源**真实存在**且被同一个组件消费：情绪层 `/market/phase`、催化层 `/catalyst/calendar?days=14`、量价层 `/klines/{symbol}/wyckoff`（`ThreeTierVerdictRow.tsx:76/87/97`）。但：
- 组件只在 `SuggestTab.tsx:162` 渲染 —— 即工作台第 2 个 tab 内，**首屏不可见**；
- 三层各自**没有**绿通过/黄观察/红否决三态：三层文本一律 `font-medium text-foreground`（L171–191），只有末尾"纪律约束"合成 chip 用 `rose/emerald/muted`（L231–237）；
- 后端**没有**三态聚合端点（`grep three_tier` 仅命中 `klines.py`/`wyckoff.py` 注释与 `src/harness/`），三态若要落地必须新建 BFF 聚合或在组件内派生并显式标注"前端规则"。
→ 判为「后端有原始数据、无聚合结论；三态需新建」，不假设已有。

**J4｜LPS 图层没有后端标记 —— 不许承诺开关。**
`src/core/wyckoff.py:289` 的标记表只有四类：`effort_divergence`（放量滞涨）、`spring_probe`（弹簧试探/Spring）、`dry_up`、`markup_break`。`LPS` 只出现在 `:50` 的规则说明文本里（"须次日缩量企稳(LPS)才确认"），`_verdict()`（`:331`）的 stage 取值是 `A / B / C / D-E / unknown`，**没有 LPS 态**。→ "威科夫图层 Spring/放量滞涨/LPS 三开关"里，前两个能开，LPS 开关必须后端先补 `lps_confirm` mark 才允许上线。

**J5｜主力意图状态位与"拒判"概念错配。**
面板的意图数据来自 `/klines/{s}/summary` → `main_intent_structured`（`L2Tab.tsx:1048`），**不是** `get_main_intent`（后者只注册为对话工具，`skills_gateway.py:112`）。后端无"拒判"字样（`grep 拒判 src/` = 0 命中），等价语义是 `available=false` + `note`（`klines.py:1018` 例）与 `caliber.require_directional()`（`caliber.py:69`）。→ 设计稿写"风控拒判态"时必须落到 `available=false`/`caliber≠tick`/`reconcile_direction` 冲突这三个真实触发条件，不能造"拒判"这个后端不存在的状态。

**J6｜跨页跳转参数几乎为零。** Dashboard 的 `openStock` 把 market 丢掉（`Dashboard.tsx:455-456` 形参 `_market` 未使用），只 `navigate('/stocks/'+symbol)`；Stocks 行跳转固定 `state: navBackState('/portfolio','持仓')`（`useStocksData.ts:461`）。列表→工作台没有任何"带着我在看什么"的入参（period / tab / focus）。详见 §6。

---

## 3. Dashboard.tsx 逐项

### D1｜情绪周期（三层滤网第一层）没有顶部主指示器

- **现状**：情绪周期以三种方式存在，全是次级：(a) `Dashboard.tsx:634-653` 结论行里 `市场情绪 <b>{phaseKpi.label}</b>`，`text-[12px]`；(b) `KpiBand`（L706）里 1/6 格；(c) L722 `grid lg:grid-cols-3` 的第三列 `PhaseGaugeCard`（`SentimentGauge` 154px 高仪表盘，L1417 `GAUGE_BOX_CLS = 'flex h-[154px] ...'`）。`MarketPhaseCard` 与 `MarketMainlineCard` 并列占另两列。
- **问题**：三层滤网第一层的结论，用户首屏要"读一行 12px 小字 + 看 1/3 宽的仪表盘"才能拿到。缺一个 40px 级、全宽、可扫读的阶段轨道（退潮→冰点→发酵→高潮 当前位置）。情绪是"今天能不能出手"的总闸，不该和主线、温度平权分三列。
- **改法**：
  1. 新增首屏第 1 屏块 `MarketPhaseTrack`（全宽，`h-10`，`mt-3`），横排 6 个阶段节点 + 当前节点高亮：容器 `flex items-center gap-1 border-y border-border/60 py-2`；每节点 `text-[10px] tabular-nums px-1.5 text-muted-foreground`；当前节点 `rounded-sm bg-primary/15 px-2 py-0.5 text-[12px] font-semibold text-primary ring-1 ring-primary/40`；已过阶段 `text-muted-foreground/50`。
  2. 阶段色**不用红绿**（情绪不是价格）：退潮/冰点用 `text-amber-500`，发酵/高潮用 `text-primary`，冰点后的修复用 `text-sky-400`。
  3. L722 三卡栅格改为 `lg:grid-cols-12`：`MarketPhaseCard` `col-span-5`、`MarketMainlineCard` `col-span-4`、`SentimentGauge` `col-span-3`；仪表盘高度从 154 → 104（`h-[104px]`），让情绪轨道成为唯一 C 位。
  4. 结论行 L638 的 `市场情绪` 段删除（已被轨道承载），保留涨停/封板率/涨跌/成交/主力净流入/口径。
- **验收线**：1280px 视口首屏（不滚动）能同时看到阶段轨道与 K 线之外的三大分区标题；轨道在情绪接口失败时显示 `情绪 无数据`（灰 `text-muted-foreground`）而非空白；`data-testid="market-phase-track"` 可被 e2e 断言；结论行宽度 ≤ 一行（`flex-wrap` 不换行到第二行）。

### D2｜指数带是 5 卡网格，把首屏最贵的 80px 花在边框和留白上

- **现状**：`Dashboard.tsx:663` `grid grid-cols-2 gap-2.5 md:grid-cols-3 lg:grid-cols-5`；每个指数是 `<button className="relative rounded-md border border-border/40 p-2.5 ... bg-card min-h-[44px]">`（L676），内含 `Stat` 20px 数字（L683 `[&>div:nth-child(2)]:text-[20px]`）、10px 涨跌 chip（L687）、26px sparkline（L695）。
- **问题**：5 个 200px 宽盒子，减去 `p-2.5`×2 与 `gap-2.5`，实际数据密度低；`bg-card` + 圆角 + 边框 = 5 个独立容器，与下方 hairline 范式不一致；上证/深成/创业板/科创/北证本质是"同一条横带上的 5 个数 + 5 条迷你线"，靠网格切割反而削弱可比性。
- **改法**：改为**单行行情带**（ticker strip）：容器 `mt-1 mb-3 flex items-stretch divide-x divide-border/50 border-y border-border/60`；每格 `flex-1 min-w-0 px-3 py-2 text-left hover:bg-accent/20 transition-colors`（去 `bg-card`、去 `rounded`、去 `border`）；数字 `text-[16px] font-semibold font-num tabular-nums`（20 → 16，因为 5 格横排后 20px 会挤压），`change_pct` 用 `text-[10px]` chip 保留但改为无色底仅着色 `text-stock-up`/`text-stock-down`（去掉 `pctChipCls` 的底色，见 D5）；sparkline 高度 26 → 20。保留 `min-h-[44px]` 热区。
- **验收线**：指数带总高 ≤ 56px（当前约 80–88px）；5 格等宽且数字不换行（`truncate` + `title`）；点击仍跳 `/index/{symbol}` 且带 `?period=`（见 §6-1）；`bg-card` 在该区块 grep 结果为 0。

### D3｜缺乏 WS 连接指示灯（Dashboard 完全没有实时通道）

- **现状**：全库 `useQuoteStream` 仅两处引用：`frontend/src/realtime/useQuoteStream.ts:46`（定义）与 `frontend/src/pages/stocks/useStocksData.ts:348`（使用）。`Dashboard.tsx` **未订阅行情推送**，只有 30s 轮询（L432 注释 "30s 自动刷新"，`usePausedInterval`）。
- **问题**：首页没有任何"实时/延迟/断线"状态位。用户看指数闪色（`FlashValue` L686）会误以为在推送，实际是 30s 轮询快照。这是**诚实性缺陷**，不是美化项。
- **改法**：顶部右侧状态区（L624 `flex flex-wrap items-center gap-2 text-[11px]`）在 `refreshedAt` 前插入连接徽标三态：
  - 轮询态（当前唯一真实态）：`inline-flex items-center gap-1 text-[10px] text-muted-foreground` + 圆点 `h-1.5 w-1.5 rounded-full bg-muted-foreground/60`，文案 `轮询 30s`；
  - 若接入 `useQuoteStream`：`bg-emerald-500` + `实时`；`bg-amber-500` + `延迟`；`bg-rose-500` + `断开`。
  - 禁止在未订阅时显示"实时"。文案字号锁 `text-[10px]`，不使用红绿以外的语义色（红绿留给价格）。
- **验收线**：鼠标悬停徽标出 `title` 说明取数方式与最近成功时刻；WS 未接入时代码中不存在"实时"字样（grep 可验证）；断线态不隐藏数字，只降透明度到 `opacity-60` 并保留最后值。

### D4｜没有刷新档位切换（只有单一 30s）

- **现状**：`Dashboard.tsx:432` 固定 30s `usePausedInterval`；刷新入口只有 L599 一个 ghost 图标按钮（`h-7 px-2 min-h-[44px] min-w-[44px]`）。`grep 档位` 无命中，无 5s/15s/60s 选择器。
- **问题**：盘中盯盘需要快档（5s），盘后阅读需要慢档（60s，省配额）。当前无选择权，且"手动刷新"与"自动刷新"没有视觉区分。
- **改法**：在刷新按钮右侧加一个紧凑 select（无边框，`text-[11px] text-muted-foreground`，`bg-transparent`），三档：`5s / 30s / 60s`，默认 30s；档位写 `localStorage['dashboard.pollMs']`。刷新按钮激活态 `text-primary` + `animate-spin`（已有）。档位与 §D3 徽标联读（`轮询 5s`）。
- **验收线**：切档后下一次轮询间隔生效（可在 `usePausedInterval` 入参断言）；页面隐藏时仍暂停（保留 L432 的 visibilitychange 语义）；档位不写 URL（非分享语义）。

### D5｜涨跌 chip 用底色块，价格语义被"徽章化"

- **现状**：`Dashboard.tsx:103-109` `pctChipCls(v)` 返回带底色的 class；使用处 L687（指数）、L883（异动池）、L980（要紧事）、L1056（超额）。`tailwind.config.js:62-65` 定义 `stock.up='#E53935'` / `stock.down='#43A047'`，L66-70 明确 `gs-go/gs-stop` 用于**交易动作**、`stock-*` 用于**价格与 K 线**。
- **问题**：价格涨跌用"带底色的圆角徽章"呈现，会让价格读数像标签而非行情；且同一屏反复出现同形徽章，形成"堆砌"。铁律要求买红卖绿**仅用于价格与 K 线**——现在是"价格用了色块"，方向没错但形式过重。
- **改法**：价格涨跌统一为**纯文字着色 + 前置符号**：`font-num tabular-nums text-[11px] text-stock-up` / `text-stock-down`，删除 `bg-*` 与 `rounded px-1.5`；负号由 `pct()` 自带，正号手补 `+`。保留 `FlashValue` 闪色。**例外**：`超额` chip（L1056）可保留底色（它是绩效标签不是价格）。
- **验收线**：指数带、异动池、要紧事三处不再出现带底色涨跌块；`pctChipCls` 仅剩绩效类调用点；色值只来自 `stock-up/down` token，无硬编码 hex。

### D6｜数据源失败横幅位置正确但缺"健康快照"

- **现状**：`Dashboard.tsx:657` `<ErrorBanner errors={sourceErrors} ... retryAll={load} />`，错误源来自 L217 注释收集的 `{source, message}`。`useSourceHealth`（`hooks/useSourceHealth.ts`）已存在，60s 轮询 `/api/datasources/health`，五逻辑源 `tck/img/wencai/shadow/tq_moreinfo`，但**只在工作台 L21 被 import**，Dashboard 未使用。
- **问题**：失败横幅是"事后告知"，没有"常驻健康度"。用户不知道当前大盘数据来自哪个源、是否 stale。
- **改法**：把 `useSourceHealth` 挂到 Dashboard，在结论行（L650 `ml-auto` 的 CaliberBadge 左侧）加 5 个 3px 健康点：`inline-flex items-center gap-0.5`，每点 `h-1.5 w-1.5 rounded-full` + `title={name + status}`；`connected` → `bg-emerald-500/80`、`degraded` → `bg-amber-500/80`、`down` → `bg-rose-500/80`、`unknown` → `bg-muted-foreground/40`（诚实口径：unknown 按不可用显示，已在 hook 注释中明确）。
- **验收线**：5 点齐全（缺失源显示 unknown 灰点，不隐藏）；点击点展开 `ErrorBanner` 同款明细（复用 L657 组件）；健康点在 1280px 下不换行。

### D7｜板块资金榜的双色渐变条与"做得像图表"

- **现状**：`Dashboard.tsx:776` 双列（流入/流出）；每行 L784 `relative flex justify-between overflow-hidden rounded text-[11px]`，背景 `bg-gradient-to-r from-stock-up/25 to-stock-up/5`（L786）/ `from-stock-down/25 to-stock-down/5`（L803），宽度按 `net_inflow/max` 百分比，`transitionDelay: bi*60ms`。
- **问题**：这是本页少见的"图表主导"实践，方向对。但：`rounded` + 渐变 + 逐行 60ms 延迟入场，在 10 行以上会形成"波浪动画"，与"不做数据堆砌"的克制相冲；且流入/流出标题用 `text-stock-up`/`text-stock-down`（L779/L797）——**板块净流入不是价格**，红绿此处属于语义挪用。
- **改法**：标题色改 `text-foreground`，红绿只留给行尾金额（价格/金额方向）；渐变改纯色 `bg-stock-up/10`（去 gradient、去 delay，仅保留 `transition-[width] duration-300`）；`rounded` → `rounded-none`（与 hairline 语言一致）。
- **验收线**：首屏无逐行错峰动画（`transitionDelay` grep 为 0）；流入/流出标题非红绿色；每行高 ≤ 18px。

### D8｜工作台三列栅格与"今日要紧事"的信息密度

- **现状**：`Dashboard.tsx:906` `grid grid-cols-1 gap-3 lg:grid-cols-12`；agenda `lg:col-span-7 xl:col-span-3`（L909）、portfolio `lg:col-span-5 xl:col-span-6`（L993）、opportunities `col-span-3`。三块均为 hairline（`border-t border-border/60 pt-3`），**无卡片边框**——这是全页最合规的一段。
- **问题**：`lg`（1024–1279）档下 agenda 占 7/12 ≈ 58%、portfolio 5/12，但 `xl` 档 agenda 骤降到 3/12 —— 同一模块在两个断点宽度差 2.3 倍，行内 `text-[13px]` 名称在 3/12 下频繁 `truncate`。密度不连续。
- **改法**：`lg` 档也走 3/6/3（与 xl 一致），中间档改用 `md:grid-cols-2` 让 agenda/portfolio 上下堆叠；`agenda` 名称 `text-[13px]` 保留但加 `min-w-0` + `title`（已有 L977），副行 `text-[11px]` 截断到 1 行（已满足）。不要为塞内容把字号降到 10px。
- **验收线**：1024/1280/1440/1920 四档下，三块标题同一水平线（`items-start`），无横向滚动；agenda 单行最少容纳 12 个汉字不截断（1280 档）。

---

## 4. StockWorkbench.tsx + workbench/ 逐项

### W1｜K 线被两层卡片包裹（P0，直接违反铁律）

- **现状**：`StockWorkbench.tsx:446` `<section className="mt-3 rounded-lg border border-border/80 bg-card/40 p-2.5 shadow-sm">`；L448 内层 `<div className="min-w-0 flex-1 rounded border border-border/60 bg-background/50 p-2">`。
- **问题**：K 线（图表主体）被"卡片 → 卡片"双重包裹：外圆角 8px + 双 1px 边框 + `bg-card/40` + `bg-background/50` + `shadow-sm` + 两层 padding（2.5+2=18px 上下各）。整块可用面积被吃掉约 36px 高度、36px 宽度。铁律：**K 线无卡片包裹**。
- **改法**：
  - 删 L446 的 `rounded-lg border border-border/80 bg-card/40 p-2.5 shadow-sm` → 改 `mt-3 border-t border-border/60 pt-2`（与 Dashboard 同语言）；
  - 删 L448 的 `rounded border border-border/60 bg-background/50 p-2` → 改 `min-w-0 flex-1`（裸容器）；
  - `shadow-sm` 全段删除（`shadow-float` 仅限框选即问浮条，见铁律）。
- **验收线**：`grep -c "shadow-sm" StockWorkbench.tsx` = 0；K 线容器计算宽度 = 视口 − rail − 24px（见 W2）；K 线顶部到 HeaderBand 底边 ≤ 12px。

### W2｜速览栏展开后永久 320px 挤压，且不会自动收回（P1）

- **现状**：`StockWorkbench.tsx:369-371` `railOpen` **默认折叠**，初值读 `localStorage['sida_workbench_rail'] === '1'`（L370）——这一点原设计是对的，注释也写明"默认折叠，把屏宽让给 K 线"。展开态 L474 `<div className="flex w-[320px] shrink-0 flex-col gap-2 rounded border border-border/50 bg-background/40 p-2">`；折叠态 L498 `w-12`（48px）。用户点过展开后被 L373 永久写入 localStorage，之后每次进入都是展开。
- **问题**：默认态合规，但"展开一次 = 永久展开"没有出口：没有"浮层式临时查看"、没有进入工作台时的复位、也没有在窄视口自动收起。一旦展开，1440 视口下 K 线宽 ≈ 1440 − 侧栏 − 320 − gap12 − padding ≈ 1000px ≈ **69%**；1920 下 ≈ 74%；仅 ≥2200px 才回到 80%。`height={420}`（L455）也偏矮（1080p 下约占 39% 高）。
- **改法**：
  1. 宽 320 → **280px**（`w-[280px]`），并改**悬浮抽屉**而非挤压：`absolute right-0 top-0 z-20 h-full w-[280px] border-l border-border/60 bg-background/95 backdrop-blur-sm`，父容器加 `relative`。K 线宽度不随展开变化（这是"临时看一眼"的正确形态）。
  2. 保留 `localStorage` 记忆，但仅记忆"上次是否展开"用于**同会话**；跨天/窄视口（`window.innerWidth < 1600`）强制折叠并在展开按钮上 `title` 说明。
  3. K 线高度 420 → **min(62vh, 640px)**（`height` 传 `Math.round(Math.min(640, window.innerHeight*0.62))`；`KlineChart` 内部 L948 依赖 `props.height` 重算 panes，改值会触发重建，需接受一次重排）。
  4. 折叠条 `w-12` 保留，去 `rounded border`，改 `border-l border-border/50`。
- **验收线**：1280 / 1440 / 1920 三档、**展开态**下 K 线宽度 / 视口宽 ≥ **0.80**；打开速览栏时 K 线宽度变化 = 0px（抽屉覆盖）；K 线高度 ≥ 视口 55%；`window.innerWidth < 1600` 时首帧必为折叠。

### W3｜K 线图层控制栏不存在（周期/复权/威科夫/意图叠加全缺）

- **现状**：`KlineChart` 收到的 props 仅：`symbol / market / initialInterval / initialDays=120 / height=420 / costLines / onIntervalChange / onRangeStats / sourceReady / sourceReason / enableMinute`（L449–471）。周期切换走 URL：`periodFromUrl`（L340 `periodToInterval(sp.get('period'))`）→ `initialInterval`（L453）→ `onIntervalChange`（L460 `setQuery('period', intervalToPeriod(i))`）。**无任何图层开关属性**：`grep 图层|overlay|wyckoff` 在 `StockWorkbench.tsx` = 0 命中。复权（`fq`）无参数、无 UI。
- **问题**：终端最核心的"看图决策"能力缺失——用户无法在 K 线上看威科夫标记、无法叠加逐笔主力意图、无法切前复权/不复权。这些能力后端已有数据（`/klines/{s}/wyckoff`、`/klines/{s}/summary`），前端没有开关。
- **改法**（紧凑 Toolbar Toggle，不占独立行）：
  - 位置：K 线容器顶部与图表同一 block，不另起 section。容器 `flex items-center gap-1 border-b border-border/40 pb-1 mb-1`。
  - 周期段（6 个，已有能力，改为显式按钮组而非图表内置）：`rounded-none px-1.5 py-0.5 text-[11px] tabular-nums`；激活 `border-b border-primary text-primary`（下划线式，不用底色块）；未激活 `text-muted-foreground hover:text-foreground`。
  - 复权段：`不复权 / 前复权`（**只允许两档**，后复权无数据依据，不放进 UI）；同样下划线式。
  - 威科夫段（分隔符 `mx-1 h-3 w-px bg-border/60`）：`Spring` / `放量滞涨` 两个 toggle **可上线**；`LPS` toggle **置灰禁用** `opacity-40 cursor-not-allowed` + `title="后端暂无 LPS 标记（wyckoff.py 仅 effort_divergence/spring_probe/dry_up/markup_break）"`。
    激活态：`rounded-sm bg-violet-500/15 px-1.5 py-0.5 text-[11px] text-violet-300 ring-1 ring-violet-500/40`；未激活：`rounded-sm px-1.5 py-0.5 text-[11px] text-muted-foreground hover:bg-accent/30`。
  - 意图叠加段：`逐笔意图` toggle，激活 `bg-cyan-500/15 text-cyan-300 ring-1 ring-cyan-500/40`；数据源必须标 `tick` 口径，`caliber≠tick` 时禁用并 `title` 说明（口径铁律）。
  - 颜色**不用红绿**（图层是"看什么"，不是"涨跌/买卖"）。紫/青为图层 #7C3AED/#06B6D4 的 15% 底 + 300 级文字，与 `stock-up/down` 不冲突。
- **验收线**：Toolbar 总高 ≤ 28px；所有 toggle 有 `aria-pressed`；LPS 与非法口径意图开关不可点且 `title` 说明原因；切换图层不重挂载图表（`MemoKlineChart` 不因 toggle 变化重取数——需把开关做成图表的 imperative 方法或独立 overlay 层，实现时确认）。

### W4｜本土三层滤网三态面板：位置错、形态缺（P0）

- **现状（核实结果）**：
  - 组件 `frontend/src/components/ThreeTierVerdictRow.tsx`（257 行）**已存在**，四层数据都真实取：情绪 `/market/phase`（L76）、催化 `/catalyst/calendar?days=14`（L87）、量价 `/klines/{s}/wyckoff`（L97）、撤单 `/klines/{s}/summary`（L113）、共振 `/decision-pioneer/{s}`（L129）。
  - 渲染位置：**只有** `pages/workbench/tabs/SuggestTab.tsx:162`（第 2 个 tab："建议"）。工作台首屏、K 线上方均无。
  - 三态：**不存在**。三层值一律 `font-medium text-foreground`（L171/180/189）；唯一有色判定是末尾"纪律约束" chip（L228–250），逻辑为：情绪含"退潮/冰点" → `bg-rose-500/20 text-rose-300 border-rose-500/40`；量价含"突破/弹簧" → `emerald`；其余 `bg-muted/40`。
  - 后端：**无三态聚合端点**。三层数据是三次独立 GET，由前端拼；`grep three_tier` 在 `src/` 仅命中注释与 harness skill。
  - 另注：L209–219 共振色把 `opportunity` 映射为 `text-red-500 animate-pulse`、`warning` 映射为 `text-green-500` —— **语义反置**（红=机会、绿=警告），且用了涨跌色系，本轮必须纠正。
- **问题**：三层滤网的"结论"本应是首屏最该看的一行（情绪能否出手 ➔ 有无催化 ➔ 量价是否右侧），现在藏在第二个 tab 的第四屏位置，且退化为"一句文本 + 一个合成 chip"。同时红绿语义在第 209 行反置，属合规缺陷。
- **改法**：
  1. **位置**：三道闸的紧凑三态面板上提到首屏——放在 HeaderBand 之下、K 线之上（`StockWorkbench.tsx:444` `space-y-3` 内、K 线 section 之前），高度 `h-8`，单行：`flex items-center gap-0 mt-2 divide-x divide-border/50 border-y border-border/60`。
  2. **每层一格，三态显式**（三格等宽 `flex-1 min-w-0 px-2 py-1.5`）：
     - 层名 `text-[10px] text-muted-foreground`（`情绪` / `催化` / `量价`），结论 `text-[11px] font-medium`；
     - **通过**：`text-emerald-400` + 左 2px 竖条 `border-l-2 border-emerald-500/70`，文案示例 `退潮末期·可低吸`；
     - **观察**：`text-amber-400` + `border-l-2 border-amber-500/70`，文案 `弹簧待确认`；
     - **否决**：`text-rose-400` + `border-l-2 border-rose-500/70`，文案 `冰点·禁止加仓`；
     - **无数据**：`text-muted-foreground` + `border-l-2 border-border`，文案 `无数据`（铁律：显式标无数据，不用 `--` 混同）。
  3. **语义红线**：三态色是"闸门/风控"色（emerald/amber/rose），**不是**买卖色——不得改用 `stock-up/down`；交易动作按钮（买入/卖出）在本域仍走 `gs-go/gs-stop`。修正 L209–219 的反置：`opportunity` → `text-emerald-400`（去 `animate-pulse`）、`warning` → `text-amber-400`、`caution` → `text-muted-foreground`（去黄→黄，避免与涨价主题混淆）、`unknown` 保持灰。
  4. **聚合来源**：后端**无聚合端点**，两条路二选一，必须在实现说明里写明选哪条：
     - (A) 新建 `GET /decision/three-tier/{symbol}`（BFF，一次返回 `{sentiment:{state,label}, catalyst:{state,count}, wyckoff:{state,stage}}`），三态由后端规则产出 —— 推荐，因为三态判定是业务规则，应由 `src/core/` 拥有；
     - (B) 前端派生：在 `ThreeTierVerdictRow` 内按阈值映射（如情绪含"退潮/冰点"=否决、含"分歧"=观察；wickoff `stage in (D/E)`=通过、`C`=观察、`unknown`=无数据），**必须**在 UI 标注 `前端规则·非后端结论` 的 `title`。
  5. 原 tab 内的行**删除**（避免两处同一结论、口径漂移）。
- **验收线**：首屏（不滚动）可见三层三态；每层颜色只取 emerald/amber/rose/muted 四值；`grep "text-red-500" ThreeTierVerdictRow.tsx` = 0；任一层接口失败显示 `无数据` 而非隐藏该格（格子数恒为 3）；若走 (B)，DOM 中 `data-caliber="frontend-derived"` 存在。

### W5｜Action Bar 完全缺失（五个高频动作无入口）

- **现状**：HeaderBand（`packages/biz-ui/src/components/workbench/HeaderBand.tsx`，665 行）顶行结构为：返回（L541）→ 名称 `text-[16px] font-semibold`（L553）+ 代码 `font-mono text-[11px]`（L554）→ 右侧 `ml-auto` 一组：类型三按钮（`rounded px-1.5 py-0.5 text-[11px]`，激活 `bg-accent text-foreground`，L573–586）+ 刷新（L591）。下方仅一条"建议·xx"按钮跳 tab（L640 `onClick={() => onGotoTab?.('suggest')}`）。
- **问题**：设计要求的五个动作——TradingAgents 研判 / Kronos 预测 / 记入决策账本 / 参加影子账户 / 设价格预警——在工作台**一个都没有**。用户要离开 K 线去侧栏页面才能做。类型三按钮（个股/指数/板块）占了右上角最好的位置，但它是**低频**导航。
- **改法**：
  - 右上角常驻 **3 个最高频**，其余收进 `⋯`：
    1. `研判`（主按钮，`bg-primary text-primary-foreground h-6 px-2 text-[11px]`）→ 打开右侧抽屉跑 TradingAgents；
    2. `预警`（`variant=outline` 等效：`border border-border/60 h-6 px-2 text-[11px] text-muted-foreground hover:text-foreground`）→ 内联 Popover 设价，不跳页；
    3. `⋯`（`h-6 w-6` 图标）→ 菜单：`Kronos 预测` / `记入决策账本` / `参加影子账户`。
  - 类型三按钮从右上角移至名称左侧（`个股 | 指数 | 板块` 作为 identity 的一部分，`text-[10px] text-muted-foreground`，激活 `text-foreground underline decoration-primary underline-offset-4`）。
  - **不离开 K 线**的展示方式：右侧分栏抽屉，`absolute right-0 top-0 z-30 h-full w-[420px] border-l border-border/60 bg-background/98 backdrop-blur`，`shadow-float` **允许**用在此处（铁律唯一豁免："框选即问浮条"同属临时浮层——若团队严格解读，则本抽屉不用 shadow，改用 `border-l` 单线；**建议按单线处理，保守合规**）。抽屉内流式渲染 SSE 结果，K 线宽度不变。
  - 记入账本 / 影子账户：走既有 `navigate('/shadow?tab=paper')` 同款（Dashboard L478 已有先例），但必须带 `state` 回跳，让用户能回 K 线。
- **验收线**：右上角可见按钮 ≤ 4 个（研判/预警/⋯ + 刷新）；点"研判"后 K 线宽度变化 = 0；抽屉可用 Esc 关闭；每个动作有 `aria-label` 且中文文案固定为 `研判` / `预警` / `Kronos 预测` / `记入决策账本` / `参加影子账户`。

### W6｜"风控拒判态"缺少真实触发条件与统一卡片

- **现状**：
  - 后端**无"拒判"实现**（`grep 拒判 src/` = 0 命中）。
  - 真实降级语义有三处：`/klines/{s}/summary` 的 `available=false` + `note`（`klines.py:1018` 例：`{"available": False, "note": "非交易时段·无可用盘口快照"}`）；`caliber.py:69 require_directional(tag, usage)`（口径非 tick 时禁方向判定）；`caliber.py:82 reconcile_direction`（双口径冲突）。
  - 前端已有部分防御：`L2Tab.tsx:38-43` 注释明确 stale-on-error（保留上次成功值）、不本地编造原因；`:306` `return backendNote ?? '暂无数据'`。
  - 工作台层**无统一"拒判/不可用"卡片**：K 线靠 `sourceReady/sourceReason` 灰显图标（L464-465），盘口靠 tab 内文案。
- **问题**：三种真实降级（意图不可用 / 口径非 tick / 双口径冲突）在 UI 上表现不一致，且 K 线区域没有任何"为什么这块数据不能信"的显式面板。用户看到 `--` 无法区分"无数据"和"数据不可用于判断"。
- **改法**：新增统一 `DataRefusalPanel`（首屏、K 线之上，三层滤网之下），三态触发：
  - 触发 A `available=false`：`border-l-2 border-amber-500/60 bg-amber-500/[0.06] px-3 py-2`，标题 `盘口数据不可用`，正文**原样转述** 后端 `note`（不本地写理由），右侧按钮 `切换数据源重试`（`border border-amber-500/50 px-2 py-1 text-[11px] text-amber-400 hover:bg-amber-500/10`）。
  - 触发 B `caliber ≠ 'tick'`：`border-l-2 border-sky-500/60 bg-sky-500/[0.06]`，文案 `口径 {caliber} 不可用于主力意图判定（仅参考）`，并禁用意图叠加开关（联动 W3）。
  - 触发 C `reconcile_direction` 冲突：`border-l-2 border-rose-500/60`，文案 `逐笔与东财四档方向冲突：见口径对比`，按钮链到 `/caliber-compare`。
  - 禁止用红绿（价格色）；用 amber/sky/rose 三档表示"不可用/口径受限/冲突"。
- **验收线**：三种触发各自可复现（可用 mock 响应断言）；面板文案不含前端自造原因（只允许后端 `note` + 固定说明）；`切换数据源重试` 触发一次真实重取（loading 态 `animate-spin` ≤ 3s 后可再点）；无触发时该面板不占位（不渲染空壳）。

### W7｜深度量化舱仍是卡片，且 PageTabs 为胶囊底色块

- **现状**：`StockWorkbench.tsx:518` `<section className="rounded-lg border border-border/80 bg-card/40 p-3 shadow-sm">`，内部标题 `h-3.5 w-1 rounded-full bg-primary`（L521）+ `text-[12px] font-semibold`（L522）+ `TabBar`（L524）。`PageTabs.tsx:35` 外框 `inline-flex ... rounded-xl border border-border/50 bg-accent/40 p-1`，`:49-52` 激活项 `rounded-lg px-2.5 py-1 text-[12px] bg-primary text-primary-foreground shadow-sm`。
- **问题**：K 线之下还有一条"深度量化"卡片，视觉权重与 K 线舱相当；`rounded-full` 的小色条是装饰元素；`PageTabs` 的 `rounded-xl + bg-primary + shadow-sm` 是典型的 SaaS 胶囊 tab，与行情终端的 hairline 语言冲突。
- **改法**：卡片 → hairline：`mt-3 border-t border-border/60 pt-2`；删 `bg-card/40`/`shadow-sm`/`rounded-lg`；标题色条删除，改 `text-[13px] font-semibold`。`PageTabs` 增加 `variant="underline"`（保留默认 variant 供其他页用，不破坏全局）：外框 `inline-flex items-center gap-3 border-b border-border/40`，项 `pb-1.5 text-[12px] border-b-2 border-transparent text-muted-foreground hover:text-foreground`，激活 `border-primary text-foreground font-semibold`。工作台 `TabBar` 传 `variant="underline"`。
- **验收线**：工作台内 `shadow-sm` 计数 = 0；`rounded-lg` 在 StockWorkbench.tsx 计数 = 0；tab 与 K 线容器左右内边距一致（均 0）；`PageTabs` 旧调用点无回归。

### W8｜两条 Banner 的层级与配色

- **现状**：`StockWorkbench.tsx:425` `<DisciplineCoolDownBanner />`（情绪退潮/冰点冷静期）；L428-430 `DefenseLineBanner`（持仓硬防线击穿）。
- **问题**：两条并列在 HeaderBand 与 K 线之间，均为整宽横条。冷静期是"全局纪律"（低频、不该常驻抢位），防线击穿是"个股紧急"（高频、必须抢位）。当前平权。
- **改法**：防线击穿保持整宽 `border-l-2 border-rose-500 bg-rose-500/[0.08] px-3 py-1.5 text-[11px]`（紧急，可用 rose）；冷静期降级为 HeaderBand 内一枚 chip：`rounded-sm bg-amber-500/15 px-1.5 py-0.5 text-[10px] text-amber-400`，文案 `退潮冷静期`，`title` 展开完整纪律说明。两者不同时占用两行。
- **验收线**：退潮期首屏竖向被 Banner 占用 ≤ 1 行（28px）；防线击穿仍整宽且含"距成本 -x.x%"数字（`tabular-nums`）。

---

## 5. Stocks.tsx + stocks/ 逐项

### S1｜列表页与工作台的入口关系：只有"整页跳走"，没有"就地预览"

- **现状**：`Stocks.tsx:33-38` 结构为 `StocksHeader → PortfolioSummarySection → StocksTabsBar → AccountsSection / WatchlistSection`（两个 section 都渲染，靠 `viewTab` 控制内部显隐）；行点击 `openStockDetail(...)` 最终 `useStocksData.ts:461` `navigate('/stocks/'+symbol, { state: navBackState('/portfolio','持仓') })`。另有行内 `openKlineDialog`（弹窗 K 线，`AccountsSection.tsx:363`、`WatchlistSection.tsx:229`）与 `openMinuteDialog`（分时弹窗，`:362`）。
- **问题**：两套查看方式并存（弹窗 K 线 vs 跳工作台），弹窗与工作台的图层/周期能力不一致，用户不知道自己看的是哪一套；且跳转不带 `period/tab`，工作台永远落默认周期。
- **改法**：
  - 保留行内 K 线弹窗为"快速瞄一眼"（`title` 明确 `快速预览`），行点击（主操作）→ 工作台并带参：`navigate('/stocks/'+symbol+'?period='+lastPeriod+'&tab=suggest', { state: navBackState(route, label) })`。
  - `lastPeriod` 取 `localStorage['kline.lastPeriod']`（工作台切换时写），默认 `d1`。
  - 弹窗底部加一枚 `去工作台 →` 文字链（`text-[11px] text-primary`），把两条路径接起来。
- **验收线**：从列表跳工作台后 `?period=` 存在且图表周期一致；返回按钮回到**来源 page**（持仓/自选各自正确，当前硬编码 `/portfolio` 是缺陷）；弹窗内 `去工作台` 可点。

### S2｜无批量选择/批量操作能力

- **现状**：`grep 批量|checkbox|全选` 在 `pages/stocks/` 仅命中 `AccountsSection.tsx:65`（批量取 sparkline 数据）与 `AgentDialogs.tsx:71`（`isBatchMode ? '批量' : '逐只'`，是"对全部持仓跑一遍"的模式开关，**不是多选**）。列表无 checkbox、无"全选"、无选中计数条。
- **问题**：盯盘场景高频需求"把这几只一起加自选/一起设预警/一起跑研判"无法完成，只能逐行点（每行 2–3 次点击）。`StocksHeader.tsx:88-103` 的 5 个按钮（导出/刷新/扫描/添加账户/添加股票）全是"全局"动作，没有一个是"针对选中项"。
- **改法**：行首加 12px checkbox（`h-3 w-3 rounded-sm border-border/60`，选中 `bg-primary`），表头加全选；选中 ≥1 时 Header 的按钮区**替换**为上下文动作条：`inline-flex items-center gap-1.5 rounded-sm bg-primary/10 px-2 py-1 text-[11px]`，含 `已选 N` + `设预警` + `加入自选` + `批量研判` + `清除`（`text-muted-foreground`）。批量研判复用 `AgentDialogs` 的 batch 通道，把选中集合作为入参。
- **验收线**：选中 3 只后动作条出现且按钮文案如上；`清除` 后回到原 Header；批量研判请求体只含选中 symbol（可在 network 断言）；触屏/1280 档 checkbox 热区 ≥ 24px（不强制 44，避免挤压行高）。

### S3｜Header 五个按钮的层级与尺寸不统一

- **现状**：`StocksHeader.tsx:88-103`：`导出 / 刷新 / 扫描` 三个 `variant="secondary"`（无 `size`，默认），`添加账户` 也是 secondary，`添加股票` 为默认 primary；L107-121 是小屏分支（`size="sm" className="h-8 w-8 p-0"` 纯图标）。
- **问题**：5 个按钮同一行、四个 secondary 一个 primary，"添加股票"是低频（建仓时一次），却被给了最高权重；"刷新/扫描"是高频却与"添加账户"（极低频）平权。
- **改法**：排序改为 `刷新`（图标按钮 `h-7 w-7`）→ `扫描`（`h-7 px-2 text-[11px]`）→ `导出`（并入 `⋯` 菜单）→ `添加股票`（primary，`h-7 px-2 text-[11px]`）→ `添加账户`（收进 `⋯`）。按钮高度从默认（约 32–36px）统一到 `h-7`（28px），与 Dashboard 刷新按钮（L599 `h-7`）一致。
- **验收线**：Header 同级按钮高度方差 = 0（全部 28px）；primary 唯一；`⋯` 菜单含导出与添加账户；小屏分支图标热区 ≥ 40px。

### S4｜列表行的红绿使用混入交易动作

- **现状**：`AccountsSection.tsx:252` 行样式 `hover:bg-s2`、`row-selected`；`:333` / `:510` 的 Agent 入口按钮、`:551` "深度分析(TradingAgents)" 用 `hover:text-primary`；行内删除 `hover:text-destructive`（`:379`）。涨跌色用于 `change_pct` 等价格字段（合规）。
- **问题**：本页暂未发现"买入/加仓/卖出/减仓"按钮误用红绿（当前列表页无交易动作按钮，实盘动作在 `PositionDialog`）。**判定：合规**，但需立规矩防回退——一旦加入"加仓/减仓"快捷动作，必须走 `gs-go`/`gs-stop`（`tailwind.config.js:66-70`），不得用 `text-stock-up/down`。
- **改法**：不加代码；在 `docs` 与本域 ADR 记一条：本域任何交易动作文案色 = `gs-go`(买/加) / `gs-stop`(卖/减)，`stock-up/down` 只允许出现在 价格、涨跌幅、K 线、资金数字 四处。
- **验收线**：`grep -rn "buy\|买入\|加仓" pages/stocks | grep "stock-up"` = 0；代码评审清单含此条。

---

## 6. 跨页联动断点（具体到参数）

| # | 起点（文件:行） | 用户动作 | 现在发生 | 应带参数 | 断点性质 |
|---|---|---|---|---|---|
| 1 | `Dashboard.tsx:675` | 点指数格 | `navigate('/index/'+ix.symbol)` | `/index/{symbol}?period={dashboardLastPeriod ?? 'd1'}` | 无周期记忆，进指数页永远默认 |
| 2 | `Dashboard.tsx:455-456` | 点异动池/要紧事里的美股/港股标的 | `navigate('/stocks/'+symbol)`，形参 `_market` **未使用** | `/stocks/{symbol}?market={market}&period=d1` | **market 被丢弃**，US/HK 标的进 CN 工作台（`StockWorkbench.tsx:81` 硬编码 `const MARKET = 'CN'`）→ 数据错标风险 |
| 3 | `Dashboard.tsx:642` | 想看"涨停 xx 家"的清单 | 纯文本，不可点 | 应可点 → `/opportunities?filter=limit_up&date=today` | 数字是死文本，最该下钻的指标不可点 |
| 4 | `Dashboard.tsx:648` | 点"主力净流入"数字 | 纯文本 | 应可点 → `/dark-fund-top?tab=flow` 或本页锚点滚到资金流块（`#market-phase-anchor` 已有锚点范式） | 同上 |
| 5 | `Dashboard.tsx:733` 资金流块 | 点某板块行（L784 无 onClick） | 纯静态条，不可点 | `/board/{code}` 或 `/stocks?board={name}` | 板块榜不可下钻 |
| 6 | `Stocks.tsx` 行 → `useStocksData.ts:461` | 点持仓行 | `/stocks/{symbol}?`（无 query），`state: navBackState('/portfolio','持仓')` | `/stocks/{symbol}?period={lastPeriod}&tab={lastTab}`，`navBackState(自选页→'/stocks?tab=watchlist')` | 回跳**硬编码 `/portfolio`**：从"关注"tab 进也回持仓页 |
| 7 | `StockWorkbench.tsx:640`（HeaderBand） | 点"建议·xx" | `onGotoTab('suggest')` 只切 tab，不滚 | 切 tab 后 `scrollIntoView` 到三层滤网行；或按 W4 直接首屏化后改为打开研判抽屉 | 切了 tab 但用户不知道结论在哪 |
| 8 | `StockWorkbench.tsx` 全文 | 从 K 线去"记入账本/影子账户/预警" | 无入口（W5） | `/decision-ledger?symbol={s}&from=workbench`、`/shadow?tab=paper&symbol={s}`、预警内联 Popover | 动作断链 |
| 9 | `L2Tab.tsx:1048` 主力意图数字 | 想看口径对比 | 无链接 | `/caliber-compare?symbol={s}&a=tick&b=eastmoney4` | 口径争议无出口 |

**统一改法（一次改到位）**：新增 `frontend/src/lib/nav-args.ts`，导出 `stockPath(symbol, {period, tab, market, from})`，所有跨页跳转改为调用它，杜绝散落字符串拼接。回跳 `navBackState` 一律传**当前真实路由**而非硬编码。

**验收线**：上表 9 条每条可在 DOM 断言 `href`/`navigate` 入参；`grep "'/portfolio'" frontend/src/pages/stocks` 仅剩 1 处（默认兜底）；US/HK 标的跳转后工作台 `market` 参数与来源一致（需同时放开 `MARKET='CN'` 硬编码，属后端契约变更，另立任务）。

---

## 7. 优先级与分片建议

**片子 A（P0，看图本体，1 个 PR）**：W1 去双层卡片 → W2 速览栏抽屉化 + K 线高度 → W3 Toolbar（周期/复权/Spring/放量滞涨/意图；LPS 灰禁）
**片子 B（P0，决策面板，1 个 PR）**：W4 三层滤网三态上首屏（决定走 (A) 后端聚合还是 (B) 前端派生）→ 修 L209 红绿反置 → W6 拒判面板三触发
**片子 C（P1，首页结构，1 个 PR）**：D1 情绪轨道 → D2 指数带 → D3/D4 连接指示与档位 → D5 去徽章化
**片子 D（P1，动作与联动，1 个 PR）**：W5 Action Bar + 抽屉 → §6 九条联动（含 `nav-args.ts`）
**片子 E（P2，列表页）**：S2 多选 → S1 跳转带参 → S3 Header 层级

**依赖**：W3 的意图开关依赖 W6 的口径判定；W4 的 (A) 方案需后端新增端点（不在本次文档范围内排期）；片子 D 的 §6-2 依赖工作台 `MARKET` 硬编码解除（后端契约，需另立）。

## 8. 刻意不改（Frozen Ring 与本域无关项）

以下**不在**本轮建议范围内，任何改动需单独评审：
`symbol_guard` / `number_audit` / `src/core/caliber.py` / 交易 403 / TQ 优先链 / `SYSTEM_PROMPT`。
本域仅**读取** `caliber.py` 的 `require_directional` / `reconcile_direction` 语义用于 UI 状态映射，不修改其实现。

## 9. 未取到 / 需实现时确认

1. `MemoKlineChart`（`packages/biz-ui/src/components/KlineChart.tsx`，116 KB）已读到 L171/490/1422/1465：内部已有 `showSignal`（L1422）与 `buildPatternMarkers`（L1465），说明 marker 叠加机制**已存在**，但未读完其完整 props 类型定义，W3 实施时需确认威科夫 marks 走同一条 `setMarkers` 通道还是拆独立 series。
2. `KlineChart` 的 `height` 为数字 prop（L948 进 dependency 重算 panes），不支持纯 CSS `h-[62vh]`；W2 需由父级 `ResizeObserver` 算数字传入。
3. `src/core/intraday_profile.py` / `scenario_engine.py` 是否已产出可用于"分时轮廓叠加"的结构（本轮仅核实文件存在，未读实现）。
4. 无运行态截图与 DOM 度量：§D2/W2 的屏宽比例为 className 静态推算，实施后需用 `release-visual-verification` 补实测。
