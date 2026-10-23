# 盘前/盘中雷达与资金异动域 · UI/按钮/色调走查

**基线**：`v0.48.0 / 5427af0`（分支 `docs/design-3`，worktree `/home/ubuntu/wt/design-3`）
**性质**：只读走查 + 改法建议。本文不含任何代码改动，不改采集口径、不改 Frozen Ring。

---

## 0. 审计路径清单

| # | 文件 | 行数 | 角色 |
|---|---|---|---|
| 1 | `frontend/src/pages/Auction.tsx` | 63 | 集合竞价页壳（时间轴说明 + 嵌 `AuctionAnomalyTab`） |
| 2 | `frontend/packages/biz-ui/src/components/AuctionAnomalyTab.tsx` | 386 | 竞价异动榜单实体（Auction 页 90% 的可视内容在这） |
| 3 | `frontend/src/pages/DarkFundTop.tsx` | 336 | 暗盘资金 TOP 榜（桌面表格 + 移动卡片） |
| 4 | `frontend/src/pages/Opportunities.tsx` | 2305 | 机会页（双视图 + 7 项筛选 + 机会卡） |
| 5 | `src/collectors/auction_collector.py` | 618 | 竞价快照采集（TQ + thsdk 合并） |
| 6 | `src/core/auction_tick_sampler.py` | 561 | 竞价逐帧采样（未匹配量/撤单率推导） |
| 7 | `src/core/dark_flow.py` | 1637 | tick 级资金（big/mid 分档、拆单簇、主力意图） |
| 8 | `src/core/market_scan.py` | 535 | 全市场扫描（resonance_pick / scan） |
| 9 | `src/core/tdx_corporate.py` | — | TQ 个股异动档案（GP04 大宗交易）——仅用于核实数据可得性 |
| 10 | `src/web/api/auction.py`、`auction_pool.py`、`resonance.py`、`decisions.py`、`src/core/decision_log.py` | — | 接口面核实 |

## 1. 覆盖范围

**已审**：
- Auction 页的信息层级、时间轴语义、榜单列集、9:25 意图判定色带
- DarkFundTop 的服务端口径徽章、数值列、排序交互、移动端降级
- Opportunities 的机会卡结构、动作色调、因子 chip 色语义、顶部操作按钮
- 上述三页所依赖的后端字段是否真实存在（逐项核到函数/列名）

**未审（明确排除，勿据此判断已验收）**：
- `StockWorkbench`（`/stocks/:symbol`）页面本体 —— 仅审跳转参数
- `DecisionLedger` 页本体 —— 仅审「是否有写入通路」
- `AbnormalMovesCard` 内部实现（Opportunities `viewMode==='abnormal'` 分支，第 1241 行）
- 暗色/亮色双主题真机对比、移动端真机触控热区实测
- i18n 键完整性（`t('darkFundTop.*')` 只核了调用形态，未核 locale 文件）
- TQ / thsdk / DDE 采集网络层与时序调度

---

## 2. 结论先行：三项特色能力的数据可得性（不许把设想当现状）

| 诉求 | 后端是否有采集 | 证据 | 判定 |
|---|---|---|---|
| 竞价**未匹配量 M** | **有**（逐帧推导） | `auction_tick_sampler.py:152-175` `unmatched = bid1_vol - ask1_vol` 写入 `unmatched_vol`；表 `src/db/models.py:2081` `unmatched_vol`（正=买剩） | **数据在库，前端/接口未出** → 加字段+加列即可，不需新采集 |
| 竞价**撤单率** | **有**（两种） | `auction_pool` 的 `withdraw_rate`(部分填充) 与 `withdraw_rate_full`(全程近似, `src/web/api/auction.py:13`)；另有 `withdraw_rate_pre0920`(9:20 前, `market_archive.py:51`) | 已可见，但**前端只显示单一值**，9:20 前/后不分离 → 见 §3.3 |
| **暗盘四档**（小/中/大/特大） | **只有两档** | `dark_flow.py:1341` `big_net`(≥100万) / `1352` `mid_net`(20万–100万)；`main_net` 来自 `get_main_intent` | **四档不存在** → 需在 tick 聚合里补 4 档阈值，不许用现有两档冒充四档 |
| **拆单伪装吸筹强度** | **有检测，无强度分** | `dark_flow.py:754` `_detect_split_orders`、`867` `_classify_split`；页面侧用 `.tck` 拆单簇 `tck_dark_net_wan` | 半可用：簇净额已有，**「强度」是 0–100 分标度，需新增计算** |
| **主力控盘度** | **无该指标** | 仅 `tq_chips.py:116` `cyw`（通达信 CYW 客户端公式口径）与 `dark_flow.py:1097` 「控盘洗盘」形态名 | **无页面级数据源** → 需先补；若接 CYW 必须单独标 TQ 口径，禁止与 tick 混算 |
| **09:05 双轨事件雷达（大宗供给收缩 vs 科技突发）** | **无** | `tdx_corporate.py:134` 只有**单票** GP04 大宗笔数/均价/成交额，无「供给收缩」派生；全库无「双轨/事件雷达/科技突发」分类器 | **无数据源，需先补采集**（方案见 §6.1） |
| **三引擎共振** | **口径名不存在** | `resonance.py:77` 是「三指标共振」（机构活跃度 1.56/3/6 三线），`103` AI 共振判定；Opportunities 侧 `resonanceOf`/`resonanceSourcesLabel`(`Opportunities.tsx:128-131`) | **可复用，但必须改名**：现有是「三指标共振」+ 多源徽章，不是「三引擎」 |
| **一键生成盘前计划写 DecisionLedger** | **无写入端点** | `src/web/api/decisions.py` 只有 `GET /stats`(24)、`POST /backfill`(37)、`GET /log`(53)、`GET /backtest`(80)…；`decision_log.py:70 record_signal` 仅供系统信号 | **需补 `POST /api/decisions/record`**，否则按钮只能写死「暂不可用」 |

**总判**：三项诉求里，只有「竞价未匹配量」是真·现成数据；「暗盘四档 / 控盘度 / 双轨雷达」都属于**无数据源，需先补采集/计算**。任何把它们画成已上线的稿子都是假现状。

---

## 3. `Auction.tsx` + `AuctionAnomalyTab.tsx`：09:15–09:25 高扫视效率

**场景约束**：真实决策窗口 10 分钟，9:24:30 之后用户在看倒计时而不是看表。所以这一页唯一 KPI 是「**一次扫视能否分出真封单和诱多**」，不是信息密度。

### 3.1 页面壳是双卡片，违反去卡片化

- **现状**：`Auction.tsx:27` 头部 `rounded-lg border border-border/50 bg-card/60 p-4`；`Auction.tsx:58` 又套一层 `rounded-lg border border-border/40 bg-card/30 p-2` 包住 `AuctionAnomalyTab`（第 59 行）。等于「卡片里放卡片」，且列表区被 `p-2` 二次内缩，9:25 时可视行数少 1–2 行。
- **问题**：违反产品铁律「去卡片化（hairline 分隔取代卡片边框）」。两张卡片叠起来后，榜单与数字不再是主角，边框抢了视线。
- **改法**：
  - 头部容器 → `border-b border-border/40 pb-3`（与 `DarkFundTop.tsx:152` 一致，同域内统一）；
  - `Auction.tsx:58` 外包层 → 直接 `<AuctionAnomalyTab … />`，或用 `divide-y divide-border/30` 的裸容器，去掉 `bg-card/30`；
  - 标题 `text-[16px]` 保留（符合 16 号阶），副标题 `text-[12px] text-muted-foreground` 保留。
- **验收线**：9:25 窗口下 1440×900 首屏可见榜单行数 ≥ 14 行（现约 12 行）；页面 DOM 中 `border` + `bg-card` 同时出现的矩形容器 ≤ 1 个。

### 3.2 时间轴是纯文字，不可交互

- **现状**：`Auction.tsx:41-53`，三段 legend 用 `Clock/ShieldCheck/Zap` + 文字（「9:15-9:20 试盘撤单」「9:20-9:25 不可撤单(真金)」「9:25-9:30 撮合定型」），颜色分别是 `text-primary` / `text-red-500` / `text-yellow-500`。
- **问题**：① 三色无系统含义（红=涨停买盘，这里却被用作"真金"，与「买红」撞车但语义不同）；② 不可点击 → 用户无法「只看 9:20 后」；③ 与下方表格无视觉连接，看不出当前处在哪一段。
- **改法**：
  - 改成三段**连续进度条**（一根 100% 高的 4px 轨，三段按 5/5/5 分钟等宽），已过去段 `bg-foreground/15`，当前段 `bg-[#DC2626]`，未来段 `bg-foreground/5`；
  - 9:20 分界处加 2px 竖线 + 小标签「不可撤」`text-[10px] font-semibold text-[#DC2626]`；
  - 三段本身变成筛选 chip：点击「9:20 后」→ 表格按 `frame_ts >= 09:20:00` 过滤（数据侧需回传帧时间，见 §3.5）；
  - 非竞价时段整条轨降为 `text-muted-foreground` 并写「非竞价时段 · 无数据」，不要显示 0。
- **验收线**：点击任一段后 URL 带 `?phase=pre0920|post0920|match`，榜单行数随之变化；当前段在 09:19:59 与 09:20:01 采样两次，高亮段必须切换。

### 3.3 09:20 前可撤（诱多）vs 09:20 后真封单：现在只用同一列同一种颜色

- **现状**：表格只有一列「撤单率」（`AuctionAnomalyTab.tsx:242`，渲染在 `315` 行，`withdraw == null ? '—' : …`，颜色 `text-muted-foreground`）。后端其实有 `withdraw_rate_pre0920`（`market_archive.py:51`、`thsdk_alert.py:382`）和 `withdraw_rate_full`（`auction.py:13`），前端只取了后者或其中一个。
- **问题**：这是本页**最严重的信息缺失**。「9:15 挂了 8000 手、9:19 撤掉 7000 手」与「9:21 才挂上 8000 手」在现表里可能显示同一个撤单率数字，而交易含义完全相反（前者诱多、后者真封）。用户在 9:25 前用同一灰度文字判断这两种情形，等于没有区分。
- **改法（色块必须截然不同）**：
  - 撤单率列拆成**两个上下叠放的小格**：上格 `9:20前` 22px 高，下格 `9:20后` 22px 高；
  - 9:20 前格：底色 `bg-[#64748B]/12` + 1px 虚线边 `border-dashed border-[#64748B]/40` + 数值 `text-[#64748B]`，术语用「**试盘撤单**」（禁用"洗盘""诱空"等主观词）；
  - 9:20 后格：底色 `bg-[#DC2626]/10` + 左侧 2px 实线 `border-l-2 border-l-[#DC2626]` + 数值 `text-[#DC2626] font-semibold`，术语用「**真封单**」；
  - 9:20 后撤单率 > 15% 时，在该格右上角加 `▲` 与 `bg-[#B45309]/15 text-[#B45309]` 的「撤单偏高」10px 标签（注意：这是**事实标记**，不是方向预测）；
  - 缺 9:20 前序列时（`auction_collector.py:403` 明确说过 TQ 单点快照算不出撤单率）→ 上格写「**无数据**」`text-muted-foreground/60`，不要留 `—`（`—` 与"0%"读起来一样）。
- **验收线**：随机抽 20 行，9:20 前后两格底色在灰度打印下仍可区分；构造「前 70% / 后 3%」与「前 3% / 后 70%」两只票，肉眼 1 秒内能指认哪只是诱多。

### 3.4 未匹配量 M 完全没有露出（数据在库，白扔）

- **现状**：`AuctionAnomalyTab.tsx:236-244` 共 9 列：`# / 代码 / 名称 / 竞价涨幅 / 竞价成交额 / 竞价量比 / 撤单率 / 9:25 盘口意图判定 / 操作`。**没有未匹配量列**。而 `unmatched_vol` 已被 `auction_tick_sampler.py:152-175` 写入、`models.py:2081` 落库。
- **问题**：未匹配量是「9:25 那一刻还挂着没成交的委托」，是判断封单厚度的**唯一**硬数；现在用户只能看竞价成交额（=已成交），把"没成交的"信息全丢了。
- **改法**：
  - 后端 `src/web/api/auction.py` 响应加 `unmatched_vol` / `unmatched_side`（`buy`=买剩 / `sell`=卖剩），源取 `auction_tick_sampler.load_frames` 最后一帧；
  - 前端在「竞价量比」后插一列「**未匹配 M**」：`text-[12px] tabular-nums text-right`，值 `<span>{abs/100 手→万手}</span>` + 方向标记：
    - 买剩（正）→ 数字 `text-[#DC2626]`，前缀 `M买` 10px `text-[#DC2626]/80`；
    - 卖剩（负）→ 数字 `text-[#16A34A]`，前缀 `M卖` 10px `text-[#16A34A]/80`；
    - 0 或 null → 「无数据」；
  - 列宽固定 `w-[76px]`，禁止换行（避免 9:25 抖动）。
- **验收线**：任意交易日 09:24:00 抓一帧，M 买剩绝对值 + 竞价成交额 之和 ≤ 竞价时段总委托量（口径自洽校验）；列在 `text-[12px]` 下不折行。

### 3.5 时间轴筛选 / 异动强度排序 / 行内一键跳，三件都缺

- **现状**：
  - 无时间轴筛选（`AuctionAnomalyTab.tsx` 全文无 `phase` / `frame_ts` 过滤）；
  - 排序不可控：`AuctionAnomalyTab.tsx` 无 `sort` state（对比 `DarkFundTop.tsx:69-86` 已有三列可排序），行序完全由后端给；
  - 行内跳转：只有「名称」单元格可点（`AuctionAnomalyTab.tsx:270-274` `onClick={() => onOpenDetail?.(code, market, name)}`）；「操作」列（`:244` 表头）只有一枚「入池」`Button variant="ghost" size="sm" className="h-6 px-1.5 text-[10px]"`（`:337` 起）—— **24px 高，低于 44px 触控线，10px 字也低于榜单可读下限**；无「工作台」按钮，且**无键盘上下键切换**。
- **改法**：
  - 表头三列（竞价成交额 / 撤单率 / 未匹配 M）加 `cursor-pointer select-none hover:text-foreground` + `▼/▲`（复用 `DarkFundTop.tsx:85-86` 的 `sortMark` 写法）；
  - 默认排序改为「**异动强度**」：`score = 量比 × log10(成交额) × (1 − 9:20后撤单率)`，排序 chip 文案「按强度」`text-[11px]`，旁挂 `bg-foreground/5` 的说明 tooltip；
  - 「操作」列改两枚图标按钮，并把现有「入池」按钮（`:337` `h-6`）抬到 `min-h-[44px] w-[44px]`：`ExternalLink` → `onOpenDetail`（跳 `StockWorkbench`），`Plus` → 保留 `addToWatchlist`；图标下不挂文字，`title` 写「工作台」「入池」；
  - 键盘：表格容器 `tabIndex={0}` + `onKeyDown` 处理 `ArrowUp/ArrowDown` 移动 `focusIdx`，`Enter` 触发第一枚按钮。焦点行 `bg-accent/30` + 左侧 2px `border-l-2 border-l-[#DC2626]`。
- **验收线**：按 `↓` 连按 5 次，焦点行必须下移 5 行且不滚动错位（`scrollIntoView({block:'nearest'})`）；`Enter` 后路由为 `/stocks/<code>` 且 `location.state.from === '/auction'`（现 `Auction.tsx:18` 已传 `navBackState('/auction')`）。

### 3.6 9:25 意图判定的五色带太杂，且绿色被当成风险

- **现状**：`AuctionAnomalyTab.tsx:317-330`，`verdict_level` 五档：`strong_buy`→`bg-red-500/20 text-red-500 font-bold`；`buy`→`bg-amber-500/20 text-amber-500`；`warning`→`bg-yellow-500/20 text-yellow-600`；`risk`→`bg-green-500/20 text-green-500`；其余 `bg-muted`。
- **问题**：① `amber` 与 `yellow` 两档在小面积 10px 上是**不可分辨**的（#F59E0B vs #EAB308 差 2 个色阶）；② `risk` 用纯 `green-500`(#22C55E) 做底色，与全站「卖绿」`#16A34A` 不是同一支绿，同一页出现两种绿；③ 用 fill 底色 × 5 档，违背「数字是主角」，chip 抢了竞价涨幅的注意力。
- **改法**：
  - 压到**三档语义 + 中性兜底**：做多 `#DC2626`、观察 `#64748B`、回避 `#16A34A`；`warning`（数据可疑）改中性 `#64748B` + 虚线边；
  - 形态从「实心底」改「**左 2px 竖条 + 无底**」：`border-l-2 pl-1.5`，文字 `text-[10px] font-medium` 用对应色，背景透明 —— 与 `DarkFundTop` 的 hairline 语言一致；
  - `strong_buy` 用字重而非色块强调（`font-bold` + 竖条 3px）。
- **验收线**：全页出现的绿色 token 只有 `#16A34A` 一支（grep `green-500` 在 biz-ui 该文件应为 0）；三档 chip 在 10px 下仍能靠竖条长短（2/3px）+ 字重区分。

### 3.7 高开溢价率的色调：现在是「涨了就红」，但没告诉你溢价在历史什么位置

- **现状**：`AuctionAnomalyTab.tsx:110-111` `gapColor(gap)` 返回 `text-stock-up` / `text-stock-down` / `muted`；`:277` 异动/妖股徽章 `bg-red-500/10 text-red-500`；`:301` 追高徽章 `text-red-600`；`:288-296` 异动幅度小字 `text-muted-foreground/80`。
- **问题**：`+7.2%` 的红色对一只常年 10% 高开的票毫无信息量，对一只常年平开的票却是极端信号。当前配色**只编码符号，不编码异常度**。而且 9:25 时红色密集会让人误判"全场都在抢"。
- **改法**：
  - 保留符号色（买红卖绿）作为**文字色**：`text-[#DC2626]` / `text-[#16A34A]`；
  - 溢价**异常度**单独用 6px 高的**下划线**编码：`gap_pct` 落在该股近 60 日竞价溢价分布 `>P90` → 下方 2px `bg-[#DC2626]`；`<P10` → `bg-[#16A34A]`；中间不画。数据源用 `cancel_profile.py` 同款「60 日基线 + z/分位」思路，新增 `auction_premium_profile`（见 §6.2）；
  - 无历史基线时**不画线**，并在表尾 legend 写「溢价分位：无数据」。
- **验收线**：同一 `+5.0%` 在两只票上呈现不同下划线（一只 P95 一只 P50）；无基线时页面不出现任何下划线且 legend 显示"无数据"。

---

## 4. `DarkFundTop.tsx`：把百分比改成双向能量条

### 4.1 服务端口径徽章挂错成 `eastmoney4`

- **现状**：`DarkFundTop.tsx:168` `<CaliberBadge caliber="eastmoney4" label={t('darkFundTop.caliberTag')} … />`，而同页 `:164` 数据源写死显示 `thsdk_dde`，字段注释（`:5-8`）说 `main_net_wan` 来自「同花顺官方主力净流入」。
- **问题**：**口径红线的直接违反**。页面自称 `thsdk_dde`，徽章却声明 `eastmoney4`（= 按单金额分档资金流）。两种口径不可换算，用户看到 `eastmoney4` 徽章会把 DDE 主力净流入当成"东财分档"，或反过来把分档当主力意图。这是 §5 铁律「资金方向只能走 tick、eastmoney4 只能叫分档」的落地失败。
- **改法**：`caliber` 改为与数据实际来源一致的枚举（新增 `thsdk_dde` 或 `ths`），文案「口径：同花顺 DDE 主力净流入（非 tick 意图，非东财分档）」；若枚举不可扩展，则至少 `caliber="ths_dde"` 并在 `CaliberBadge` 兜底渲染为中性灰。**禁止**保留 `eastmoney4`。
- **验收线**：`grep -n 'eastmoney4' frontend/src/pages/DarkFundTop.tsx` 结果为 0；页面徽章 tooltip 文案与 `data.source` 字符串一致。

### 4.2 「大单/特大单净流入」只有两档，却按"四档"的视觉预期排版

- **现状**：表格列（`:188-208`）只有 `主力净流入(main_net_wan)`、`净量比(main_net_vol/main_net_ratio)`、`总成交额(total_amount_wan)`，外加可选 `.tck` 暗盘列（`:209-216`）。后端 `dark_flow.py:1341/1352` 只有 `big_net`(≥100万) 与 `mid_net`(20万–100万)。
- **问题**：栏目标题叫「暗盘资金 TOP」，但一个"大单 vs 特大单"都分不开，"拆单伪装"更看不见。用户以为在看四档资金结构，实际是一个标量 + 一个比值。
- **改法**：
  - 明确**不伪造四档**。先在表格加一组**双档微型条**（见 4.3），列名改「超大单(≥100万)」「大单(20–100万)」并在表头 tooltip 注明「两档，非四档；四档需补采集」；
  - 若产品坚持四档 → 走 §6.3 采集方案，在数据到位前**禁止**在 UI 上出现"四档"字样。
- **验收线**：表头文案与后端实际档数一致；页面不出现"四档/四类单"字样（除非 `dark_flow` 已实现四档）。

### 4.3 核心改法：百分比 → 双向正负能量条（diverging bar）

- **现状**：占比/比值以裸数字呈现 —— `:243-247` `safeFixed(r.main_net_vol ?? r.main_net_ratio, 2, '-')` 灰度 `text-muted-foreground`，`:248-250` 成交额 `toAmountFromWanUnsigned`。全部是 `font-mono` 纯文本，无长度编码。
- **问题**：20 行的榜单里，用户要读 20 个数字才能比较"谁更强"。数值列既是主角（合规），又完全没有可扫视性 —— 这是"数字是主角"被误读成"只有数字"。
- **改法**（单行 200px 宽，中线固定 50%）：
  - 容器 `<div className="relative h-[14px] w-[200px]">`，中线 `absolute left-1/2 top-0 h-full w-px bg-border`；
  - 正（净流入，买红）：`absolute left-1/2 top-[3px] h-2 rounded-r-sm bg-[#DC2626]`，`width = 50% * min(1, |net|/maxAbs)`，最小值 2px（避免 0 宽消失）；
  - 负（净流出，卖绿）：`absolute right-1/2 top-[3px] h-2 rounded-l-sm bg-[#16A34A]`，同宽算法；
  - 数值仍**保留在条右侧**：`text-[12px] tabular-nums` + 符号色，条不替代数字，只做排序视觉；
  - `maxAbs` 取当前视口内 `top` 的 `max(|main_net_wan|)`，换了排序/过滤要重算 —— 禁用全局常数（否则一只 50 亿的票会把其余压成 1px）；
  - 条高固定 8px，行高固定 30px，20 行总高 600px，保证 1080p 首屏不滚。
- **验收线**：任一行，条的像素宽度 / 100px 与该行 `|net| / maxAbs` 误差 ≤ 2px；`net = 0` 时条宽 0 且不渲染（不能出现 1px 假阳线）；色值只能是 `#DC2626` 或 `#16A34A`。

### 4.4 「主力控盘度」在页面上无处可依

- **现状**：无该列、无该字段。`dark_flow.py:1097` 只有形态标签「控盘洗盘」，`tq_chips.py:116` 有 `cyw`（TQ 客户端公式口径）。
- **问题**：需求里的"主力控盘度"是**无数据源**项（见 §2）。硬画一个 0–100 的环或条，只能靠 `main_net_wan / total_amount_wan` 拍脑袋，等于把"净流入占比"冒充"控盘度"。
- **改法**：本期**不上控盘度**。若必须给，只能：
  - 新列名「**净流入/成交额**」`text-[11px] text-muted-foreground`，值 `main_net_wan / total_amount_wan`，标注「非控盘度」；
  - 或接 `cyw` 时单独列 `TQ CYW` 徽章 + 独立 tooltip，且**禁止**与 `main_net_wan` 同格显示。
- **验收线**：页面不出现"控盘度/控盘率"字样（除非后端新增 `control_degree` 字段并标注口径）。

### 4.5 时间窗口 / 行业过滤 / 导出批量入池：三项全缺

- **现状**：无窗口切换（`snapshot_date` 单日，`:160`）；无行业列也无行业筛选；导出仅有表尾一个**指向 API 的裸链接**（`:326-328` `<a href="/api/market-scan/dark-fund-top">`），点开是 JSON，不是文件；无批量入池。
- **问题**：用户拿到榜单后无法「按 5 日持续性筛」「按行业收敛」「整批扔观察池」，TOP 榜成了只能看不能用的展板。
- **改法**：
  - **窗口切换**：头部右侧一组 segmented chip `当日 | 3日 | 5日 | 10日`，`h-7 px-2.5 text-[11px]`，选中 `bg-foreground/10 text-foreground`，未选 `text-muted-foreground hover:text-foreground`；后端需支持 `window` 参数聚合（当前 `refresh` 只传 `top_n`，`:114`）；
  - **行业过滤**：表头「名称」右侧加一个 `Popover` 多选（复用 `Opportunities.tsx:1249-1275` 的 Popover 草稿模式），chip 上带数量角标；
  - **导出**：按钮文案「导出 CSV」`variant="outline" size="sm"`，点击下载 `dark-fund-top-YYYYMMDD.csv`（前端由已加载的 `top` 生成，不新增接口）；表尾裸链接改为 `text-[11px] text-muted-foreground hover:text-primary` 的「原始 JSON」次级入口；
  - **批量入池**：首列加 checkbox（`w-8`，行高 30px 内 16px 方框），选中后顶部出现浮条 `sticky top-0`「已选 N 只」+ 按钮「加入观察池」`bg-[#DC2626] text-white`（主操作）与「清空」。
- **验收线**：切到「5日」后 `snapshot_date` 与行集合都变；导出文件行数 = 表格行数；选中 3 行点入池后 `watchlist_add` 接口被调 3 次且页面 toast「已加入 3 只」。

### 4.6 移动端回退成 Card，与去卡片化冲突

- **现状**：`:280-318` `md:hidden` 分支用 `<Card variant="hover" className="p-3 min-h-[44px]">` 逐行卡片。
- **问题**：桌面 hairline、移动卡片，两个端两套语言；且卡片把「净量比」整列丢了（`:301-308` 只有 `main_net_wan` 与 `total_amount_wan`）。
- **改法**：移动端改为 `divide-y divide-border/40` 的单列列表，行内两行：第一行 `代码 名称` + 右侧净流入（`text-[13px] font-semibold`），第二行 `text-[10px]` 灰字「净量比 x.xx · 成交 x.xx亿」；保留 `min-h-[44px]` 热区。
- **验收线**：移动分支 DOM 中不出现 `Card`；`main_net_vol/ratio` 在 375px 宽下可见。

---

## 5. `Opportunities.tsx`：双轨雷达 + 三引擎徽标 + 三按钮

### 5.1 09:05 双轨事件雷达：**无数据源，需先补采集**

- **现状**：页面无任何"09:05 双轨"结构。`viewMode` 只有 `candidates | abnormal` 两态（`:1213-1236` 的 toggle，文案「机会 / 异动」）。`AbnormalMovesCard`（`:1241`）是异动股列表，不是事件雷达。
- **问题**：「大宗供给收缩」与「科技突发」是**两条完全不同的证据链**：前者要大宗交易折价率+解禁/减持窗口（`tdx_corporate.py:134` 只给单票笔数/均价/额，无折价率、无供给收缩判定），后者要新闻/公告的事件分类器（全库无「科技突发」分类）。当前把它们塞进现有候选池，只能得到"混在一起的 300 只"。
- **改法**（分两步，UI 必须在数据到位后才上线）：
  - **采集侧**（§6.1）：新建 `src/collectors/event_radar_0905.py`，产出统一结构 `{track: 'block'|'tech', symbol, title, evidence_url, surprise, ts}`；
  - **UI 侧**：09:00–09:30 时段在页面顶部插入一条**双轨跑道**（不是卡片）：
    - 左轨「大宗 / 供给收缩」色 `#0E7490`（cyan-700），右轨「科技 / 突发」色 `#7C3AED`（violet-600）；
    - 每轨一条 2px 横线 + 事件按 `surprise` 排在线上（x = 时间，y = 强度），点按弹出证据原文；
    - 非 09:00–09:30 时段整块 `hidden`，不占空间；
  - 数据为空时显式写「**今日无事件数据（采集未就绪）**」，不得显示空跑道。
- **验收线**：在无 `event_radar` 表的情况下，页面不出现两条空跑道；仅在接口返回 `track` 字段时渲染，且每轨首条事件必须可点开 `evidence_url`。

### 5.2 三引擎共振徽标：先改名，再谈配色

- **现状**：`Opportunities.tsx:128-131` `resonanceOf` / `resonanceSourcesLabel`；`:2146` 与 `:2186-2193` 的来源徽章用 `bg-primary/10 text-primary`（蓝紫）圆角 pill，超过 2 个折叠为 `+N`；`resonance.py:77` 的"三线"是机构活跃度生命线 1.56 / 强势线 3 / 大牛线 6。
- **问题**：① 文案叫「三引擎」但代码里叫「三指标共振」—— 上 UI 前必须统一，否则用户拿"引擎"去问客服，客服在代码里找不到；② 所有来源徽章同色 `primary`，看不出共振是强是弱（3 源 vs 2 源长得一样）；③ 共振徽标和普通来源徽标混在一个 flex 里，强弱无层级。
- **改法**：
  - **命名**：统一为「**三指标共振**」（与 `resonance.py` 对齐），tooltip 写「机构活跃度 · 生命线 1.56 / 强势线 3 / 大牛线 6」；
  - **独占徽标**（放在代码最左侧、名称之前，28px 高）：
    - 强共振（≥3 源且 AI 判定 `strong`）→ 实心 `bg-[#C2410C]`，白字 `text-[10px] font-bold`，文案「共振·强」；
    - 弱共振 → 描边 `border border-[#C2410C]/60 text-[#C2410C] bg-[#C2410C]/8`，文案「共振·弱」；
    - 普通多源 → 现有 `bg-primary/10 text-primary` 保持不变；
  - 三轨色（与 §5.1 双轨区分开、也不与买红卖绿撞车）：机构活跃 `#C2410C`、资金 `#0E7490`、事件 `#7C3AED`，全部只作**左竖条**（`border-l-2`），不做底色；
  - 徽标必须带 `title`，写明「非买卖建议」。
- **验收线**：页面不出现"三引擎"字样（grep 0）；强/弱共振徽标在灰度下可区分（实心 vs 描边）；每枚徽标 `title` 含"非买卖建议"。

### 5.3 机会卡应有三枚按钮：现在只有「洞察」

- **现状**：卡尾（`:2156-2164`）只有一枚纯文字按钮「洞察」（`text-[11px] text-muted-foreground hover:text-primary`，`openInsight(item)`）；桌面扫读行（`:2145-2154`）显示入场/止损/目标三价。整页顶部只有「策略库 / 导出 / 刷新」三枚（`:1128-1164`）。
- **问题**：用户从"看到机会"到"能执行"之间缺三步：查原文、判持续性、落计划。现在只能点「洞察」打开一个笼统抽屉。
- **改法**：卡展开区（`:2165` `{expanded && …}`）底部加一行**三枚按钮**，等高 28px、间距 6px：
  1. 「**查催化原文**」`variant="outline" size="sm" text-[11px]`，icon `FileText`；无 `evidence_url` 时 `disabled` 且文案「查催化原文（无原文）」；
  2. 「**AI 评题材持续性**」`variant="outline"`，icon `Sparkles`；点击调 `POST /api/ai/theme-durability`（需新增，见 §6.4），返回「强/中/弱 + 一句理由 + 依据条数」，结果以 `text-[11px]` 灰底条回填在按钮下方，**禁止**给买卖结论；
  3. 「**生成盘前计划**」`bg-[#DC2626] text-white text-[11px]`（主操作），icon `ClipboardList`；点击调 `POST /api/decisions/record`（需新增），写入 `DecisionLedger` 后 toast「已写入决策台账」并给「查看台账」链接 `navigate('/decision-ledger')`。
  - 三枚按钮在 `md` 以下折成两行（1+2），主操作独占一行。
- **验收线**：三按钮 `min-h-[44px]` 触控热区（移动端）；第 3 枚在 `decisions.py` 未加 POST 前必须 `disabled` + 文案「生成盘前计划（接口未就绪）」，**不得**做成点了没反应。

### 5.4 因子 chip 的绿红与「买红卖绿」直接冲突

- **现状**：`:2205` 正向因子 `bg-green-500/15 text-green-700 dark:text-green-400`；`:2210` 负向因子 `bg-red-500/15 text-red-700 dark:text-red-400`；`:2234` / `:2248` 同款（另一处因子区）。即"加分=绿、减分=红"（西方惯例）。
- **问题**：同页 `:2143` 目标价 `text-stock-up`(红)、`:2139` 止损 `text-stock-down`(绿)、`:2168-2169` 也是红目标绿止损。**同一屏里红色既是"止盈方向"又是"扣分"**，用户必须每次重新解释颜色。这是本域最典型的色调自相矛盾。
- **改法**：
  - 正向因子 → 中性底 + 红字：`bg-[#DC2626]/10 text-[#DC2626]`，前缀 `+`；
  - 负向因子 → 中性底 + 绿字：`bg-[#16A34A]/10 text-[#16A34A]`，只保留数值符号；
  - 如担心"绿=好"的直觉冲突，改用**无彩色方案**：正向 `bg-foreground/8 text-foreground`，负向 `bg-foreground/5 text-muted-foreground line-through`（更安全，推荐）。
- **验收线**：全页 `bg-green-500` / `bg-red-500` 出现次数为 0；任一屏内红色只表达"多头/买入/流入"一种含义。

### 5.5 `toneClass` 仍是旧调色板（rose/emerald）

- **现状**：`:203-214` `toneClass` 返回 `border-l-rose-600/70`（buy）/ `border-l-emerald-600/70`（add）/ `border-l-primary/70`（score≥85）。而 `:225-232` `actionTone` 已在 2026-09-22 迁到 `SignalChip`（go=红 / stop=绿 / neutral）。
- **问题**：**加仓用 emerald（绿）**，与买红卖绿相反，且与 `actionTone` 的 go=红 冲突 —— 同一张卡上"加仓"左竖条是绿、chip 是红。
- **改法**：`toneClass` 直接复用 `actionTone` 的 tone → 颜色映射，删掉 `rose/emerald` 字面量；加仓用 `border-l-2 border-l-[#DC2626]/70`（弱于 buy 的 `/100`）。
- **验收线**：`grep -n 'emerald\|rose-' Opportunities.tsx` 结果为 0；`action==='add'` 与 `action==='buy'` 两行的左竖条同色相、buy 更深。

### 5.6 大盘方向：只描述状态，不做预测

- **现状**：`:2192` 展开区显示 `marketRegime.regime_label || marketRegime.regime`；`regimeTone`（`:349-354`）给 tone。
- **问题**：若 `regime_label` 文案含"看涨/看跌/将反弹"等预测词，就违反「不做大盘涨跌方向预测」。
- **改法**：UI 侧对 label 做白名单——只允许「普涨/普跌/分化/缩量/放量/修复/退潮」等**状态词**；命中预测词时降级显示原始 `regime` 代码 + `text-muted-foreground`。
- **验收线**：构造 `regime_label = "明日看涨"` 的 mock，页面必须不显示"看涨"，而显示代码或「状态未知」。

---

## 6. 数据缺口与采集方案（不许把设想当现状）

### 6.1 双轨事件雷达（大宗供给收缩 / 科技突发）

- **新增** `src/collectors/event_radar_0905.py`，每日 08:55 与 09:03 各跑一次（09:05 前落库）：
  - **大宗轨**：`tdx_corporate` GP04（`tdx_corporate.py:134`）逐票拉近 20 交易日大宗 → 计算 `discount = (close - avg_px) / close`、`block_amt_ratio = 大宗额 / 20日均额`；「供给收缩」定义为 `discount <= -5%` 且 `block_amt_ratio >= 2` 且 未来 30 日有解禁/减持窗口（接现有解禁接口）；
  - **科技轨**：公告+新闻流按关键词/实体命中，产出 `surprise ∈ [0,1]`（新词率 × 权威源权重），**不做情感预测**；
  - 落库 `event_radar(trade_date, track, symbol, title, surprise, evidence_url, ts)`；`evidence_url` 必填（否则不入库，避免"传闻"上桌）。
- **口径**：`track` 只有 `block` / `tech` 两值，禁止扩展成"消息面"泛称；`surprise` 是"信息新颖度"，UI tooltip 必须写明，别叫"利好强度"。

### 6.2 竞价溢价分位（§3.7）

- 复用 `cancel_profile.py` 的 60 日基线范式：新增 `auction_premium_profile(symbol, trade_date, gap_pct, p10, p50, p90)`，收盘后由 `auction_collector` 落库；查询走单票缓存。**只做分位，不做概率。**

### 6.3 资金四档（§4.2）

- 在 `dark_flow.compute_dark_flow`（`:1266`）已有 tick 循环里加阈值分档：`>=100万` 特大、`20万–100万` 大、`4万–20万` 中、`<4万` 小；输出 `xl_net / lg_net / md_net / sm_net` 四字段。
- **红线**：四档全部来自 tick 聚合，**不得**混入 `eastmoney4` 分档值；两套数字不可出现在同一列、不可换算、不可求和对比。页面并列时必须各自 `CaliberBadge`。

### 6.4 决策台账写入（§5.3）

- `src/web/api/decisions.py` 新增 `POST /record`：入参 `{symbol, action, entry_low, entry_high, stop_loss, target, reason, source:'opportunity_card'}`，内部调 `decision_log.record_signal`（`:70`），落库后返回 `plan_id`；写入前必须过 `number_audit`（Frozen，不改）。
- **红线**：该端点只登记"用户自拟计划"，不得自动下单、不得进入 403 交易链路。

---

## 7. 优先级与验收清单

| 优先级 | 项 | 位置 | 类型 |
|---|---|---|---|
| **P0** | `CaliberBadge` 挂成 `eastmoney4` 与 `thsdk_dde` 数据源自相矛盾 | `DarkFundTop.tsx:168` | 口径红线 |
| **P0** | 因子 chip 绿=加分 / 红=减分，与买红卖绿冲突 | `Opportunities.tsx:2205,2210,2234,2248` | 色调 |
| **P0** | `toneClass` 加仓用 emerald | `Opportunities.tsx:203-214` | 色调 |
| **P0** | 9:20 前/后撤单率不分离（诱多 vs 真封无法区分） | `AuctionAnomalyTab.tsx:242,315` | 信息架构 |
| **P1** | 未匹配量 M 未露出（数据已在库） | 表头 `AuctionAnomalyTab.tsx:236-244` + `auction.py` | 功能 |
| **P1** | 双重卡片容器，9:25 首屏少 2 行 | `Auction.tsx:27,58` | 去卡片化 |
| **P1** | 资金列由裸数字改双向 diverging bar | `DarkFundTop.tsx:236-250` | 视觉编码 |
| **P1** | 排序 / 时间轴筛选 / 键盘上下键全缺 | `AuctionAnomalyTab.tsx` 全局 | 交互 |
| **P1** | 三枚 CTA 只有「洞察」，且计划写入无接口 | `Opportunities.tsx:2156-2164` | 交互+后端 |
| **P2** | 窗口切换 / 行业过滤 / CSV 导出 / 批量入池 | `DarkFundTop.tsx:152-177,326` | 功能 |
| **P2** | 五色意图 chip 压到三档 + 竖条化 | `AuctionAnomalyTab.tsx:317-330` | 色调 |
| **P2** | 移动端 Card 改 hairline 列表 | `DarkFundTop.tsx:280-318` | 去卡片化 |
| **P3** | 「三引擎」改名「三指标共振」+ 强弱徽标 | `Opportunities.tsx:128-131,2146` | 命名+色调 |
| **阻塞** | 暗盘四档 / 控盘度 / 双轨雷达 —— **无数据源，需先补采集** | §6.1 / §6.3 / §2 | 数据 |

**整体验收线（本域）**：
1. 三页 `border` + `bg-card` 并存矩形 ≤ 1 个/页；列表一律 hairline 分隔。
2. 绿色仅出现 `#16A34A`（卖/流出），红色仅出现 `#DC2626`（买/流入），语义型强调色只用 `#C2410C / #0E7490 / #7C3AED` 且只做竖条。
3. 所有资金列带 `tabular-nums`；金额一律「亿/万」；缺值一律「无数据」，不得 `—` / `0` / `--` 混用。
4. 页面不存在"预测/看涨/看跌/将反弹"类文案；不存在"控盘度/四档/三引擎"等无数据支撑的名词。
5. 09:24:00 与 09:26:00 分别截图，竞价页在两次采样中当前时段高亮与列表内容均正确切换。
