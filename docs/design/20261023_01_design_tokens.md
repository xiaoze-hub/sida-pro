# SIDA-Pro 全站设计令牌（Design Tokens）与语义徽标体系

> **基线**：`v0.48.0 / 5427af0`（分支 `docs/design-1`，worktree `/home/ubuntu/wt/design-1`）
> **定位**：本文是**令牌与语义徽标的唯一权威定义**。其余页面走查文档引用本文的 token 名 / class 名，不重复定义；若本文与页面文档冲突，以本文为准。
> **性质**：诊断 + 规范增量。所有条目按「现状（文件:行）/ 问题 / 改法（token 名 + Hex + class）/ 验收线」四段式。
> **不触碰 Frozen Ring**：`symbol_guard` / `number_audit` / `caliber.py` / 交易 403 / TQ 优先链 / `SYSTEM_PROMPT` 约束段。本文只定义**展示层**，不改任何口径判定逻辑。

---

## 0. 审计路径清单

| # | 路径 | 读到的行 | 用途 |
|---|---|---|---|
| 1 | `frontend/tailwind.config.js` | 全文 1–96 | 颜色/圆角/动效映射，定位第 61–71 行现有语义规则 |
| 2 | `frontend/src/index.css` | 全文 1–438 | CSS 变量、三主题档、surface 阶梯、组件类 |
| 3 | `frontend/src/realtime/useQuoteStream.ts` | 全文 1–124 | WS 行情流、重连、页签挂起 |
| 4 | `frontend/src/realtime/envelope.ts` | 全文 1–50 | 帧信封 `{seq,ts,topic,user_id,payload}` 与 `last_seq` 补发 |
| 5 | `frontend/packages/biz-ui/src/components/CaliberBadge.tsx` | 全文 1–55 | 现有口径徽章（中性色铁律） |
| 6 | `frontend/src/lib/format.ts` | 1–175（关键段） | `safeMoney`/`toAmount`/`safeNetInflow` 亿/万换算与 NaN 守卫 |
| 7 | `docs/_frozen/caliber_matrix.md` | 1–60 | 口径 × 数据源权威矩阵 |
| 8 | `src/core/caliber.py` | 1–100（关键段） | `Caliber = Literal["tick","eastmoney4","ths","unknown"]` |
| 9 | `src/web/cache/biz_cache.py` | 索引 + 关键段（`BizCache` 64、`get_or_fetch` 180、`stats` 231） | L1/L2 缓存 TTL 与降级 |
| 10 | `frontend/src/App.tsx` | 340 / 485 / 489 / 617 / 635–637 | 外壳、侧栏、焦点模式、免责条占位 |
| 11 | `frontend/src/components/ThreeTierVerdictRow.tsx` | 1–80 | 三层滤网（情绪/催化/威科夫）现状 |
| 12 | `frontend/src/components/DefenseLineBanner.tsx` | 1–101（关键段） | `-8%` 硬防线现状 |
| 13 | 目录/计数探针 | `frontend/src/components/`、`frontend/packages/{api,base-ui,biz-ui}`、`grep` 计数 | class 存量统计（`bg-white/` 39、`shadow-float` 14、`.card*` 15、`text-[10px]` 461） |

**未取到**：K 线渲染组件内部实现（`frontend/src/components/` 下无独立 KLine 组件文件命中，图表实现在页面内或 `@sida/biz-ui` 内，本轮未读）；`JobPanel.tsx` 内部实现（仅从目录清单确认存在）。凡涉及这两处，本文只写约束与验收线，不描述实现细节。

---

## 1. 覆盖范围

### 1.1 已由其他 5 份文档覆盖（本文引用其页面结论，**不逐页审**）

Dashboard、StockWorkbench、Stocks、Auction、DarkFundTop、Opportunities、Forecast、FactorIC、Agents、AiCopilot、DecisionLedger、ShadowHub、ShadowAccount、PaperTrading、WarReport、ReportsHub、Reports、PriceAlerts、DataSources、ApiKeys、SettingsHub、Settings（共 **22** 个页面）。

### 1.2 本轮**未审**（不在本文结论范围内，需后续补审）

Admin、AnalysisDetail、Audit、CaliberCompare、CopilotGovernance、Developers、ErrorLog、Heatmap、History、Help、Landing、Login、NotificationsHub、Notifications、Profile、System、Terms、ThemeMood、Tiers（共 **19** 个页面）。

> 说明：`ThemeMood` 与主题档（`data-theme='dim'`）强相关，`CaliberCompare` 与口径徽标强相关，`Tiers` 与模型徽标强相关——这三页是本文规范最可能被违反的地方，列入补审优先级 P0。

### 1.3 本文覆盖的四类对象

1. Surface 层级（画布 / 分栏 / 行 / 浮层 / 斑马纹）
2. 语义色板与徽标（涨跌竞价 / 威科夫三层滤网 / 硬防线守卫 / 双源与模型）
3. 工程与实时体验（WS、闪烁、重绘、跨页状态、长任务）
4. 落地清单（文件 / 变量名 / 迁移顺序 / 存量 class 替换）

---

## 2. 现有基线的三条硬事实（先读这段，后面全部增量都建立在此）

**F1｜surface 阶梯只表达空间层级，不表达涨跌。** `tailwind.config.js:41-48` 已声明 `s0…s4` + `canvas`，且用 `<alpha-value>` 支持 `bg-s2/60`，但注释明确「新代码不该再手调透明度（门禁 R12 棘轮）」。→ 本文所有新层级**必须**从 `--s*` 派生或新增命名 token，禁止 `bg-white/[0.03]` 这类写法。

**F2｜涨跌色与交易动作色已分离。** `tailwind.config.js:61-71`：

```
stock-up/down 只用于**价格与K线**；"买入/加仓""卖出/减仓"这类交易动作
走 gs-go/gs-stop —— 同值不同名, 让"价格涨跌"与"买卖动作"两种语义可分别演进。
```

本文**继承并扩展**该规则：语义轴从「价格 vs 动作」两条，扩到「价格 / 动作 / 判定 / 出处 / 模型」五条互不换算的轴。

**F3｜全站只允许一档阴影。** `index.css:431-438` 注释「全站只有**一档**阴影，只给真正的浮层」，`.shadow-float` 是唯一实现。产品铁律进一步收紧为：`.shadow-float` **只允许**用于「框选即问浮条」一处。

### 2.1 五条语义轴（本文的元规则 R0）

| 轴 | 语义 | 允许的色族 | 禁止 |
|---|---|---|---|
| A 价格轴 | 涨/跌/平 | `--stock-up` 红 / `--stock-down` 绿 / `--flat-color` 灰 | 任何非价格数字用红绿 |
| B 动作轴 | 买/卖、加/减 | `--gs-go` 红 / `--gs-stop` 绿 | 用 `stock-*` 表达动作 |
| C 判定轴 | 通过/观察/否决 | 青 / 琥珀 / 玫红（见 §4.2） | 用纯绿表达「通过」 |
| D 出处轴 | 口径 / 数据源 | 中性灰 + 等宽 10px（见 §4.4） | 出处徽章填充涨跌色 |
| E 模型轴 | Kronos / Chronos-Bolt / XGBoost / TradingAgents | 紫 / 天蓝 / 青绿 / 玫紫（见 §4.5） | 模型色与判定色混用 |

**验收线 R0**：任一页面截图内，若同一屏出现 ≥2 种「红」且语义不同（价格红 + 判定红 + 出处红），必须靠**形态**区分（填充/描边/竖条/等宽字），不得只靠色相。

---

## 3. 模块一｜Surface Hierarchy（五层）

### 3.1 层级总表（亮 / 暗 / Dim 三档实测 Hex）

Hex 由 `index.css` 的 HSL 值精确换算（`hsl(h s% l%)` → 6 位 Hex），**不是估算值**。

| 层 | Token（CSS 变量） | Tailwind | 亮色 Hex | 暗色 Hex | Dim Hex | 使用约束 |
|---|---|---|---|---|---|---|
| L0 主画布（K 线区） | `--chart-canvas` | `bg-canvas` / `.s-canvas` / `bg-kline` | `#FDFDFE` | `#07070B` | `#050508` | **必须**比外壳「更空一档」：亮更白、暗更黑。K 线区 ≥80% 屏宽、**无卡片包裹**、无圆角遮罩 |
| L0' 工作区页底 | `--s0` | `bg-s0` / `.s-workspace` | `#F1F1F6` | `#0A0A0F` | `#08080C` | 只做页底；不得用于任何 panel 内部填充 |
| L1 分栏 Pane | `--s1` | `bg-s1` / `.s-panel` | `#FCFCFD` | `#14141F` | `#11111A` | Pane 之间**只准** 1px hairline 分隔；禁止 `border + rounded-xl + shadow` 三件套 |
| L1.5 行 hover | `--s2` | `bg-s2` / `.row-hover` | `#F3F3F7` | `#1C1C29` | `#191924` | 行高 ≥`--row-h-32`（`.row-hit` 32px）；hover 只改底色不改边框 |
| L1.5 行 selected | `--s3` | `bg-s3` / `.row-selected` | `#E8E8EE` | `#272735` | `#21212E` | selected 与 hover 必须可同时表达（selected 优先），不得叠加出第三色 |
| L2 悬浮抽屉 | `--s3` + `--s4` | `bg-s3` / `bg-s4` | `#E8E8EE` / `#FFFFFF` | `#272735` / `#181825` | `#21212E` / `#151521` | 抽屉头部 fixed 48px，正文可滚；**不加阴影**（除框选浮条） |
| L3 Tooltip | `--s4` | `bg-s4` | `#FFFFFF` | `#181825` | `#151521` | 10px/11px 文字，最大宽 320px |

### 3.2 冲突项 C1｜暗色阶梯 `--s4` 反向下沉（**必须修**）

- **现状**：`index.css:152` 暗色 `--s3: 240 16% 18%`（`#272735`），`--s4: 240 22% 12%`（`#181825`）。
- **问题**：阶梯声明为「越靠前越浮」，但暗色 `s4` 明度 12% **低于** `s3` 的 18% —— 最高浮层（tooltip / 命令面板）比 selected 行还暗，层级语义在暗色下反向。亮色 `#FFFFFF > #E8E8EE` 是对的，暗色是错的。
- **改法**：暗色 `--s4` 提为 `240 20% 22%`（`#2D2D43`），Dim 档 `--s4` 提为 `240 18% 19%`（`#282839`）。同步在 `index.css` 注释里补一句「暗色 s4 必须 > s3，由测试锁定单调性」。
- **验收线**：单测断言三档主题下 `L(s0) < L(s1) < L(s2) < L(s3) < L(s4)`（`L` 取 oklch 明度）；暗色 tooltip 背景对比 `s1` panel 的 ΔL ≥ 4。

### 3.3 新增｜hairline 分隔体系（去卡片化的执行件）

- **现状**：`index.css:223` 全局 `* { @apply border-border }`，`--border` 亮 `#E2E2E9` / 暗 `#1E1E29`；`.card` 用 `border-border/60`（`index.css:247`）。
- **问题**：全站没有「分隔线」与「容器边框」的区分，导致分隔线偏重、卡片偏多；且 `border-border/60` 靠 alpha 手调，违反 R12。
- **改法**（新增 3 个 token）：

| Token | 亮 | 暗 | Tailwind | 用途 |
|---|---|---|---|---|
| `--hairline` | `#E9E9F0` | `#1F1F2B` | `border-hairline` | Pane / 行 / 表头分隔（无需交互的线） |
| `--hairline-strong` | `#D8D8E2` | `#2C2C3A` | `border-hairline-strong` | 分组边界、抽屉顶边、粘性表头下沿 |
| `--hairline-accent` | `#C7C7D4` | `#3A3A4A` | `border-hairline-accent` | 焦点/当前分组（**不用** primary 彩色线） |

- **验收线**：`grep -c "border-border/6\?0"` 在迁移后为 0；任何 1px 分隔线不得带 `shadow`。

### 3.4 新增｜高密度表格斑马纹

- **现状**：无斑马纹 token；`index.css:401` 只有 `.col-band`（`foreground/0.035` 列带），`index.css:403-405` 用 `content-visibility` 做长列表（`.list-window`）。
- **问题**：`col-band` 用 `--foreground` alpha，亮暗两侧观感不一致（亮色偏灰、暗色偏青），且没有行级斑马，>12 列的行情表靠 hover 才能跟行。
- **改法**：新增 `--row-zebra`（亮 `#F7F7FA` / 暗 `#0F0F16` / Dim `#0D0D14`），class `.row-zebra:nth-child(even)`；**仅在列数 ≥12 的只读行情表启用**，且与 `.row-hover` 不叠加（hover 覆盖 zebra）。
- **验收线**：斑马纹与相邻行 ΔL ≤ 1.5%（不产生「条纹卡片」感）；开启 `content-visibility` 的长表滚动时无条纹错行。

### 3.5 冲突项 C2｜`.card` 三件套（去卡片化的最大存量）

- **现状**：`index.css:246-248` `.card { bg-card rounded-xl border border-border/60 shadow-[0_1px_2px_…] }`；`{15}` 处使用 `className="card|card-hover"`。
- **问题**：`border + rounded-xl + shadow` 同时出现，既是卡片又是浮层，直接违反「hairline 取代边框与卡片」「全站一档阴影」两条铁律；且 `rounded-xl = calc(var(--radius)+2px)` = 8px，与「行情终端微圆角」不符。
- **改法**：`.card` 降级为 `.pane`（新 class）：`background: var(--s1); border: 1px solid var(--hairline); border-radius: var(--radius)`（`--radius` 已 `0.375rem`=6px），**删除 shadow**；`.card-subtle` → `.pane-inset`（`bg-s2`，无边框）。
- **验收线**：`grep -rn "className=\"card"` 为 0；全站 `shadow-*` 只剩 `.shadow-float` 一处定义 + 1 处使用（框选即问浮条）。

### 3.6 冲突项 C3｜`shadow-float` 14 处 vs 铁律 1 处

- **现状**：`index.css:435-437` 定义，`grep` 命中 `frontend/src` **14** 处。
- **问题**：阴影被当作「浮起来」的万能表达，与「浮不浮起来与结构无关的信息不该用阴影」直接冲突；14 处里多数是卡片/面板，属于 C2 的同一病根。
- **改法**：保留 `.shadow-float` 仅给框选即问浮条；其余 13 处改为 `border-hairline-strong` + `bg-s3`；下拉/菜单/命令面板改为 `bg-s3 + 1px hairline`（暗色靠亮度差，不靠投影）。
- **验收线**：`grep -rn "shadow-float" frontend/src | wc -l` = 1；视觉走查「浮层是否可辨」以 hairline 亮度差为准。

### 3.7 冲突项 C4｜`bg-white/*` 39 处绕开阶梯

- **现状**：`index.css:61-65` 注释记录「`bg-white/[0.0x]` 30 处」，实测已涨到 **39** 处。
- **问题**：R12 棘轮要求「只减不增」，现在净增 9 处 → 棘轮失效；且 `bg-white` 在暗色靠叠加才可见，是「同层不同色」的复发点。
- **改法**：全部映射到阶梯：`bg-white/5`→`bg-s2`，`bg-white/10`→`bg-s3`，`bg-white/[0.03]`→`bg-s1`；仅在**图像蒙版/渐变遮罩**场景允许 `bg-white`（且需注释 `mask`）。
- **验收线**：`grep -rn "bg-white/" frontend/src --include=*.tsx | wc -l` ≤ 5，且每处带 `/* mask */` 注释。

### 3.8 字号与数字（token 化，收口 461 处任意值）

- **现状**：`grep` 命中 `text-[10px]` **461** 处；`index.css:235-238` 已有 `.font-num`（`--font-num` + `tabular-nums`）。
- **问题**：铁律规定字号只用 10/11/12/13/16/20，但代码里靠任意值字面量表达，无法审计；`tabular-nums` 只存在于 `.font-num`，价格单元格若忘记加会抖动。
- **改法**：新增字号 token 与 utility：`--fs-10:10px / --fs-11:11px / --fs-12:12px / --fs-13:13px / --fs-16:16px / --fs-20:20px`，utility `text-fs-10 … text-fs-20`；`tailwind.config.js` 的 `fontSize` 增加同名键。价格/涨跌幅/成交额**必须**同时挂 `.font-num`。
- **验收线**：`grep -rn "text-\[[0-9]\+px\]" frontend/src | wc -l` = 0（除 10/11/12/13/16/20 的迁移中间态）；任一价格单元格 DOM 上 `font-variant-numeric: tabular-nums` 生效（DevTools 断言）。

---

## 4. 模块二｜语义色板与徽标体系

### 4.1 涨跌与竞价强弱（价格轴 A）

| Token | 亮 | 暗 | Tailwind | 约束 |
|---|---|---|---|---|
| `--stock-up` | `#E53935` | `#E53935` | `text-stock-up` | **仅**价格、涨跌幅、K 线 |
| `--stock-down` | `#43A047` | `#43A047` | `text-stock-down` | 同上 |
| `--flat-color` | `#808EA3` | `#97A3B4` | `text-flat`（需新增映射） | 平盘；不参与排序着色 |
| `--stock-up-tint` | `#FDECEC` | 暗 `hsl(0 50% 14%)`=`#361212` | `bg-stock-up-tint` | 涨停行/强势标记**底色**，不抢 K 线主色 |
| `--stock-down-tint` | `#E9F7ED` | 暗 `hsl(140 40% 12%)`=`#122B1B` | `bg-stock-down-tint` | 跌停行底色 |

新增（涨停/跌停高亮，**当前没有专用 token**）：

| Token | 亮 | 暗 | 用途 |
|---|---|---|---|
| `--limit-up-bg` | `#FDE7E7` | `#3A1418` | 涨停行底 |
| `--limit-up-fg` | `#C62828` | `#FF8A80` | 涨停价文字（比 `stock-up` 深，表示「到板」） |
| `--limit-down-bg` | `#E6F4EA` | `#10281A` | 跌停行底 |
| `--limit-down-fg` | `#2E7D32` | `#81C784` | 跌停价文字 |
| `--limit-bar` | `#E53935`（涨停）/`#43A047`（跌停） | 同亮 | 行首 2px 竖条（与 `.role-strip` 同构，宽度同为 2px） |

- **现状**：`Auction.tsx:47` 只有 `text-red-500` 一个原始调色板色，无涨停/跌停专用色；`index.css:104-110` 已备 tint 但未覆盖「到板」态。
- **问题**：①「涨 3%」与「涨停」同色，扫描时无法区分强弱；②直接写 `text-red-500`（`#EF4444`）与 `--stock-up`（`#E53935`）不同值，同屏两个红。
- **改法**：涨停/跌停态用 `--limit-up-fg`/`--limit-down-fg` + 行首 2px `--limit-bar`；**不得**加背景填充以外的任何阴影/缩放。
- **验收线**：同一表格内「涨停行」与「+9.8% 未封板行」颜色必须可辨（ΔE ≥ 6）；禁用色 `text-red-500`/`text-green-500` 在 `src` 内计数为 0。

#### 4.1.1 竞价 09:20 前后：可撤 vs 真封单（分组色）

- **现状**：`ThreeTierVerdictRow.tsx:44-51` 已消费 `cancel_order_intent`（`cancel_bias` / `b_cancel_ratio` / `s_cancel_ratio`），但**没有任何视觉规范**；`AuctionAnomalyTab.tsx:222` 只挂了 `CaliberBadge caliber="ths"`。
- **问题**：09:15–09:20 可撤单与 09:20 后真封单在榜上混排，用户无法一眼分辨「虚」与「实」；这是竞价页最容易误判的地方。
- **改法**：竞价列表按「09:20 前（可撤）/ 09:20 后（真封）」**分组**，组头用 11px 中性文字 + 组色竖条：

| Token | 亮 | 暗 | 语义 |
|---|---|---|---|
| `--auc-cancel` | `#B45309` | `#FBBF24` | 09:20 前，可撤（虚） |
| `--auc-sealed` | `#9F1239` | `#FB7185` | 09:20 后，真封单（实） |
| `--auc-sealed-bg` | `#FDF2F4` | `#2A1119` | 真封组行底（极淡） |

分组标题文案：`09:20 前 · 可撤` / `09:20 后 · 封单`；组底色仅 `--auc-sealed-bg` 一档，可撤组**不加底色**（虚的要显得轻）。
- **验收线**：竞价页任一行能在不看数字的情况下，靠「竖条色 + 组头」判断虚实；`cancel_order_intent.available=false` 时整块显示 `口径不足 · 不判定`（灰 `#64748B`），不得用任何组色伪装。

### 4.2 威科夫 + 三层滤网（判定轴 C）

#### 4.2.1 威科夫形态徽标

- **现状**：`ThreeTierVerdictRow.tsx:11` 注释列出 `Spring/LPS/SOS/放量滞涨`，数据来自 `/klines/{symbol}/wyckoff`（`verdict.stage_label` + `effort_result`），但渲染只有文字，无 token。
- **问题**：形态名靠中文长文本（「弹簧试探」4 字），在 32px 行里挤压价格列；且无形态间区分，扫不出「哪个是买点形态」。
- **改法**：徽标 = `10px 等宽 + 1px hairline 描边 + 无填充 + 2 字缩写`，色只用在描边与文字：

| 形态 | Token | 亮 | 暗 | 缩写 |
|---|---|---|---|---|
| Spring（弹簧试探） | `--wyck-spring` | `#0891B2` | `#22D3EE` | `SPR` |
| 放量滞涨 | `--wyck-stall` | `#B45309` | `#FBBF24` | `STL` |
| LPS（最后支撑确认） | `--wyck-lps` | `#4F46E5` | `#818CF8` | `LPS` |
| SOS（强势信号） | `--wyck-sos` | `#0F766E` | `#2DD4BF` | `SOS` |

- **验收线**：徽标宽度 ≤ 30px；hover tooltip 给中文全称 + `evidence`（`markers_count`）；`stage` 缺失时显示 `--`，**不得**回退到 SOS/Spring。
- **冲突说明**：`--wyck-spring`(#0891B2) 与 `--success`(#2BAB6F)、`--stock-down`(#43A047) 同属冷色族，但 Spring 是**判定**不是涨跌；靠「描边徽标 vs 实心文字」形态区分，符合 R0。

#### 4.2.2 三层滤网三态：【情绪周期 ➔ 题材催化 ➔ 量价结构】

- **现状**：`ThreeTierVerdictRow.tsx:1-80` 三层分别取 `/market/phase`、`/catalyst/calendar`、`/klines/{symbol}/wyckoff`，UI 只有图标 `Activity / Flame / ShieldAlert / Sparkles` + 文本，无三态 token。
- **问题（**关键冲突项 C5**）**：若按直觉把三态做成「通过=绿 / 观察=黄 / 否决=红」，会与价格轴（红=涨、绿=跌）**直接相撞**——「否决」显示成红色会被读成「涨」，「通过」显示成绿色会被读成「跌」。这是 A 股终端最典型的语义污染。
- **改法**：三态**避开价格红绿**，改用「青 / 琥珀 / 玫红」：

| 态 | Token | 亮 | 暗 | 背景 | 图标（lucide） | 形态 |
|---|---|---|---|---|---|---|
| 通过 | `--verdict-pass` | `#0E7490` | `#22D3EE` | `#E8F6F8` / `#0C2A2E` | `Check` | 实心 1px 描边 chip |
| 观察 | `--verdict-watch` | `#B45309` | `#FBBF24` | `#FDF3E3` / `#2E2408` | `Eye` | 虚线 1px 描边 chip |
| 否决 | `--verdict-veto` | `#9F1239` | `#FB7185` | `#FCE9EE` / `#2E1220` | `X` | 实心 chip + 左 2px 竖条 |

- **验收线**：三态 chip 内**必须**同时有图标 + 中文二字（`通过`/`观察`/`否决`），任何一态缺图标即视为不合格（色盲可用性）；与 `--stock-up/#stock-down` 同屏时不得出现「同色不同义」（按 R0 检查）。
- **链路语义**：三层之间用 `ChevronRight` 10px `#8B8B9C` 连接，表示**顺序依赖**（情绪 → 催化 → 结构），不是并列；第三层否决时前两层置 60% 不透明度（表示「被下游否决」）。

### 4.3 `-8%` 硬防线与物理守卫拒判（风险轴）

- **现状**：`DefenseLineBanner.tsx:64` 计算硬防线（默认买入成本 −8%）；第 76–88 行样式为 `border-red-600/80 bg-red-950/40 text-red-100 animate-pulse`（击穿）/ `border-amber-500/50 bg-amber-950/20 text-amber-200`（警戒）；`StockWorkbench.tsx:427` 挂载。
- **问题**：①用的是 Tailwind 原始调色板（`red-600`=#DC2626、`amber-500`=#F59E0B），与 `--stock-up`(#E53935) 形成同屏两个红；②`bg-red-950/40` 在**亮色主题下也生效**（未加 `.dark` 前缀），亮色页面会出现深红底+浅红字，对比度不足；③`animate-pulse` 不在 `--dur-*` 令牌层，`prefers-reduced-motion` 的降级（`index.css:7-12`）管不到它。
- **改法**：新增风险轴三档，与价格红**刻意拉开明度**：

| 档 | Token | 亮 | 暗 | 背景（亮/暗） | 动效 |
|---|---|---|---|---|---|
| 预警黄线（距防线 <3%） | `--warn-line` | `#B45309` | `#FBBF24` | `#FEF6E7` / `#2B2210` | 无 |
| 硬防线（跌破 −8%） | `--guard-line` | `#B91C1C` | `#F87171` | `#FDECEC` / `#331315` | `--dur-flash` 一次 |
| 守卫拦截 | `--guard-block` | `#57534E` | `#A8A29E` | `#F5F5F4` / `#1C1B1A` | 无 |

- `--guard-line`(#B91C1C) 比 `--stock-up`(#E53935) 深 2 档，且**只出现在横幅/竖条**，不出现在价格数字上 → 满足 R0。
- `animate-pulse` 改为 `animation: guard-breathe var(--dur-slow) var(--ease-in-out) 2`，并在 `@media (prefers-reduced-motion)` 内归零（复用 `--dur-slow` 令牌层降级）。
- **验收线**：亮色主题下横幅文字对比度 ≥ 4.5:1（`#B91C1C` on `#FDECEC` 实测 5.66:1）；reduced-motion 下无动画；`grep "animate-pulse" frontend/src` = 0。

#### 4.3.1「数据异常不硬猜」拦截徽标 + caliber 滞后标注

- **现状**：无统一拦截徽标；`CaliberBadge.tsx:39` 只用中性 `border-border text-muted-foreground`，`caliberOf()` 拿不到就 `unknown`（正确：不猜）。后端 `caliber.py:19` 的 `Caliber = Literal["tick","eastmoney4","ths","unknown"]` 是权威枚举。
- **问题**：当数据源降级（Redis 掉线见 `biz_cache.py:116 _redis_down`、TQ 断链、tick 不足）时，页面仍渲染数字，只在 tooltip 里说口径 —— 「数字看起来很确定，其实不可判定」是本项目最危险的展示态。
- **改法**：两个新徽标（都不使用红绿）：

| 徽标 | Token | 亮 / 暗 | 文案 | 触发 |
|---|---|---|---|---|
| 拦截「不硬猜」 | `--guard-block` | `#57534E` / `#A8A29E` | `数据异常 · 不判定` | `require_directional` 抛 `CaliberViolationError`、`insufficient` 状态、字段缺失 |
| 口径滞后 | `--caliber-lag` | `#64748B` / `#94A3B8` | `口径滞后 · T-1` | 缓存命中且 `age > ttl/2`；`biz_cache.stats()` 可给 `age` |

- 拦截态**必须替换数字本身**为 `--`（`format.ts:33 safeFixed` 的 fallback 已是 `'--'`），并在单元格右上角挂 10px 徽标；**禁止**显示数字 + 灰徽标并存（会被读成「数字可信」）。
- **验收线**：任一 `unknown` 口径单元格，屏幕上不存在可读数值；口径徽章一律中性色（继承 `CaliberBadge` 铁律：出处不随涨跌上色）。
- **冻结边界**：本项只改展示，**不得**修改 `caliber.py` / `require_directional` / `reconcile_direction`。

### 4.4 双源与模型徽标（出处轴 D）

- **现状**：`CaliberBadge` 已实现口径徽章（10px 等宽 + hairline + 中性色），但**没有数据源（vendor）角标**：`DarkFundTop.tsx:168` 只标了口径，没标「这条数是 TDX 还是同花顺」。
- **问题**：口径（tick / eastmoney4 / ths）与来源（TDX / 同花顺 / 腾讯 / 东财）是**两个维度**——`eastmoney4` 可能来自腾讯四档或东财四档，`caliber_matrix.md` 的表格正是二维的。现在只标一维，冲突归因时无法定位。
- **改法**：新增 `SourceBadge`（与 `CaliberBadge` 并排，**先源后口径**），形态：`10px 等宽 + 1px hairline + 无填充 + 2–4 字母`；色只在 hover/tooltip 出现：

| 来源 | Token | 亮 | 暗 | 角标文字 |
|---|---|---|---|---|
| 通达信 | `--src-tdx` | `#0F766E` | `#2DD4BF` | `TDX` |
| 同花顺 | `--src-ths` | `#B45309` | `#FBBF24` | `THS` |
| 腾讯 | `--src-tencent` | `#1D4ED8` | `#60A5FA` | `TX` |
| 东方财富 | `--src-em` | `#7C3AED` | `#A78BFA` | `EM` |
| 未知 | `--src-unknown` | `#71717A` | `#A1A1AA` | `?` |

- **验收线**：任何资金流数字必须能回答「哪个源 + 哪个口径」；`source ∈ {THS, EM}` 与 `caliber=tick` 的组合必须由 `caliberOf()` 归一到 `unknown`（防错标），并在徽标上加 `!` 前缀。
- **一致性约束**：`CaliberBadge.tsx:11` 的 TS 类型与 `caliber.py:19` 的 `Literal` **必须同步**；建议加一行注释指向 `docs/_frozen/caliber_matrix.md`，并把 `src/` 侧枚举列为唯一权威。

### 4.5 模型专属识别色（模型轴 E）

- **现状**：`Forecast` / `FactorIC` 页展示 Kronos / Chronos-Bolt / XGBoost / TradingAgents 多模型结果（页面清单已覆盖，本文不逐页审），但无模型色 token。
- **问题**：多模型同表对比时靠文字识别，无法在图上快速归属（哪条线是 Kronos）。
- **改法**：

| 模型 | Token | 亮 | 暗 | 线型（图表） |
|---|---|---|---|---|
| Kronos | `--model-kronos` | `#7C3AED` | `#A78BFA` | 实线 2px |
| Chronos-Bolt | `--model-chronos` | `#0EA5E9` | `#38BDF8` | 实线 1.5px |
| XGBoost | `--model-xgb` | `#0D9488` | `#2DD4BF` | 虚线 4/2 |
| TradingAgents | `--model-agents` | `#DB2777` | `#F472B6` | 点线 1.5px |

- 徽标形态同上（10px 等宽 + hairline），**必须**与线型配套（色盲兜底：线型是主区分，色是辅）。
- 冲突说明：`--model-xgb` 若用 `#059669` 会与 `--scenario-down`(#059669) 撞值 → 改 `#0D9488`。
- **验收线**：任一张多模型图在灰度截图下仍可按线型区分四模型；模型色不得用于任何判定 chip。

---

## 5. 模块三｜工程与实时体验

### 5.1 行情闪烁目前**实际失效**（P0 缺陷）

- **现状**：`index.css:340-343`

```css
@keyframes sida-flash-up { 0% { background-color: hsl(var(--stock-up) / 0.22); } … }
```

而 `index.css:27-28` 定义 `--stock-up: #E53935`（**Hex，不是 HSL 分量**）。
- **问题**：`hsl(#E53935 / 0.22)` 是**非法 CSS**，关键帧里该声明被丢弃 → `sida-flash-up/down` 实际不产生任何背景色，涨跌闪「静默失效」。同一错误影响 `index.css:341/345`。
- **改法**：新增 `--stock-up-rgb: 229 57 53; --stock-down-rgb: 67 160 71;`（亮暗同值），关键帧改为 `rgb(var(--stock-up-rgb) / 0.22)`；或统一走 `color-mix(in oklab, var(--stock-up) 22%, transparent)`。同时补一个 `--flash-alpha: 0.22` token，避免魔法数。
- **验收线**：DevTools 里对价格单元格触发一次 tick，`animation-name` 生效且 `getComputedStyle().backgroundColor` 在 0ms 时非 `rgba(0,0,0,0)`；加一条 CSS 变量单测（解析 `--stock-*` 格式，防止再次混用 hsl/rgb 语法）。

### 5.2 闪烁强度与动效降级

- **现状**：`index.css:413-418` `.sida-flash { animation-duration: var(--flash-dur, var(--dur-flash)) }`，注释称由 `FlashValue` 按 |Δ| 给 `--flash-dur`，上限 `--dur-flash-max`（700ms）。但 `.sida-flash` 在 348 与 418 行**重复定义**（前者含 `border-radius:4px`）。
- **问题**：①重复定义导致 `border-radius` 与 `animation-duration` 分散两处，改一处易漏；②`--flash-dur` 由 JS 注入，若某页面传字符串（`'0.65s'`）会覆盖 token；③`4px` 圆角硬编码，与 `--radius` 体系脱钩。
- **改法**：合并为单一定义；`border-radius: var(--flash-radius, 3px)`，`--flash-radius` 进 token 表；`--flash-dur` 一律由 JS 写 `clamp()` 值（`min(calc(|Δ| * 60ms), var(--dur-flash-max))`），不接受字符串单位。
- **验收线**：`--dur-flash` 在 `prefers-reduced-motion` 下为 0ms（`index.css:9-10` 已覆盖）；`--flash-dur` 的注入值恒 ≤ `--dur-flash-max`；`grep -c "\.sida-flash {"` = 1。

### 5.3 WS 合帧：每帧同步回调，无节流

- **现状**：`useQuoteStream.ts:84-93` `onmessage` 内 `parseFrame` → `extractTicks` → 直接 `handlerRef.current(data)`；`useQuoteStream.ts:52-53` 用 `lastSeqRef` 单调推进；断线退避 1s→30s（`:106`），4401 不重连（`:104`）。
- **问题**：一帧可含 N 个 symbol（`QuoteTickMap`），页面层通常每 symbol 一次 `setState` → 高频 tick 下 React commit 次数 ≫ 帧数，是「行情卡顿」的主因；且 `handlerRef.current` 直接调用，跨多个消费页时无共享广播。
- **改法**：在 hook 内做 **250ms 合帧**（`--tick-coalesce` 放进契约常量而非 CSS），用 `Map` 累积每 symbol 最新 tick，到点一次性 flush；flush 只触发**一次** `setState`（整表替换）。合帧窗口与 K 线重绘节奏对齐（K 线 250ms 一次重绘，价格数字 250ms 一次刷新）。
- **验收线**：单测注入 50 帧/秒 × 20 symbol，React commit ≤ 4 次/秒；WS 帧序 `last_seq` 补发在合帧后仍单调（不丢帧）；页面卸载时 pending flush 被取消（无 setState-after-unmount 警告）。

### 5.4 K 线重绘性能（约束，不含实现结论）

- **现状**：`index.css:73/154` 已为 K 线准备 `--chart-canvas`（亮 `#FDFDFE` / 暗 `#07070B`），并把图表区做得比外壳「更空」；`.sida-shimmer`（`index.css:325`）为图表骨架扫光。
- **问题**：K 线是绝对主角（≥80% 屏宽），任何 re-render 都会导致画布重绘；而 `useQuoteStream` 的逐帧回调会连带触发父级重渲染。此处**未取到** K 线组件实现，无法给出具体 DOM/Canvas 结论。
- **改法（约束级）**：①WS tick 只更新「最新价/涨跌」这层叶子节点，K 线数据流**独立订阅**（不与行情列表共享 store）；②K 线容器固定 `--canvas` 背景，重绘期间不切 `--s*`（避免闪白）；③骨架扫光只在首屏（有数据后立即卸载 `.sida-shimmer`）。
- **验收线**：Performance 面板录制 30s，K 线画布 `Paint` 次数 ≤ 4/秒；切换股票时无白屏（`--chart-canvas` 常驻）；骨架屏在数据首帧后 1 帧内移除。

### 5.5 连接状态可视化（当前无 token）

- **现状**：`useQuoteStream.ts:29` 状态 `'connecting' | 'live' | 'offline'`，`:103` 断线即 `offline`，`:104` 4401 直接 return；`index.css:400` 有 `.session-dot`（6px，`currentColor`）。
- **问题**：状态只回传给页面，**没有全站统一指示**；用户看到「数字不动」时无法区分「没行情」与「断线」。
- **改法**：页头固定 `.session-dot` + 10px 文字：

| 状态 | Token | 亮/暗 | 文案 |
|---|---|---|---|
| live | `--conn-live` | `#10B981` | `实时` |
| connecting | `--conn-wait` | `#F59E0B` | `重连中` |
| offline | `--conn-off` | `#71717A` | `离线 · 已退回轮询` |

- **R0 例外声明**：`--conn-live` 用绿，是**唯一**允许的「非价格绿」——因为连接态固定出现在页头状态区，永不与价格单元格同排；且必须配文字，不允许纯色点。
- **验收线**：拔网线后 2s 内状态变 `offline` 且文案含「退回轮询」；4401 时文案为「登录失效」，不显示「重连中」（避免无限重试误导）。

### 5.6 跨页状态保持

- **现状**：`App.tsx:489` 侧栏 `fixed … bg-card/80 backdrop-blur`；`:340/635-637` 焦点模式用 `data-focus-mode`；已有 `lib/workbench-tabs.ts`、`lib/tab-shell.ts`、`lib/nav-back.ts`。
- **问题**：①侧栏 `bg-card/80 + backdrop-blur` 在 80% 宽度下透出底层行情，滚动时产生「背后有字在动」的噪声，违反「看图决策」的静默要求；②`data-focus-mode` 存在但无持久化，刷新后丢失；③tab 状态存在内存，跨页返回丢失滚动位置。
- **改法**：①侧栏改 `background: var(--s1)`（`#FCFCFD` / `#14141F`）**去 alpha、去 blur**，右边 1px `--hairline`；②`data-focus-mode` 与 `data-density`、`data-theme` 一起持久化到 `localStorage`（键 `sida.ui.prefs`），首帧前由 inline script 应用避免闪白；③列表页滚动位置存 `sessionStorage`（key = 路由 + 筛选哈希），返回时恢复。
- **验收线**：焦点模式刷新后仍生效；侧栏区域无 `backdrop-filter`（`grep backdrop-blur` = 0）；列表→详情→返回，滚动偏移误差 ≤ 32px。

### 5.7 长耗时 AI 任务非阻塞

- **现状**：`components/JobPanel.tsx`、`ScanJobButton.tsx` 存在（目录清单），`biz_cache.py` 有 L1/L2 双级缓存 + Redis 降级（`:116 _redis_down`）。内部实现**未取到**。
- **问题**：长任务（扫描、Kronos 预测、TradingAgents 辩论）若占住主线程或阻塞提交，会让 K 线停止刷新——对一个「看图决策」终端是硬伤。
- **改法（约束级）**：①长任务一律走后端 job + 轮询/推送，前端只渲染进度，**不 await**；②进度条 token：`--job-track`（`#E8E8EE`/`#272735`）、`--job-fill`（`--primary`）、`--job-stall`（`#B45309`，>30s 无进展）；③任务面板与 K 线区**分栏**，不得用全屏遮罩。
- **验收线**：触发扫描期间，K 线仍保持 ≥1 次/秒刷新（WS 未被阻塞）；任务失败时显示 `--warn-line` 文案 + 重试入口，不弹全屏 modal；`--job-stall` 触发后 30s 内必须有可见反馈。

---

## 6. 模块四｜落地清单

### 6.1 变量新增（一次性写进 `frontend/src/index.css` 的 `:root` / `.dark` / `[data-theme='dim']`）

```css
/* 分隔与密度 */
--hairline / --hairline-strong / --hairline-accent
--row-zebra
/* 字号 */
--fs-10 --fs-11 --fs-12 --fs-13 --fs-16 --fs-20
/* 价格轴补充 */
--limit-up-bg --limit-up-fg --limit-down-bg --limit-down-fg --limit-bar
--stock-up-rgb --stock-down-rgb --flash-alpha --flash-radius
/* 竞价 */
--auc-cancel --auc-sealed --auc-sealed-bg
/* 威科夫 */
--wyck-spring --wyck-stall --wyck-lps --wyck-sos
/* 判定三态 */
--verdict-pass --verdict-watch --verdict-veto
--verdict-pass-bg --verdict-watch-bg --verdict-veto-bg
/* 风险 */
--warn-line --guard-line --guard-block --caliber-lag
/* 出处 */
--src-tdx --src-ths --src-tencent --src-em --src-unknown
/* 模型 */
--model-kronos --model-chronos --model-xgb --model-agents
/* 连接 */
--conn-live --conn-wait --conn-off
/* 任务 */
--job-track --job-fill --job-stall
```

### 6.2 Tailwind 映射增量（`frontend/tailwind.config.js` → `theme.extend`）

需要新增三个映射组（**不得**在 `colors` 里硬编码 Hex，一律 `var(--x)`，参考现有 `gs` 写法）：

```js
colors: {
  hairline: { DEFAULT: 'var(--hairline)', strong: 'var(--hairline-strong)', accent: 'var(--hairline-accent)' },
  verdict: { pass: 'var(--verdict-pass)', watch: 'var(--verdict-watch)', veto: 'var(--verdict-veto)' },
  wyck: { spring: 'var(--wyck-spring)', stall: 'var(--wyck-stall)', lps: 'var(--wyck-lps)', sos: 'var(--wyck-sos)' },
  limit: { upBg: 'var(--limit-up-bg)', upFg: 'var(--limit-up-fg)', downBg: 'var(--limit-down-bg)', downFg: 'var(--limit-down-fg)' },
  auc: { cancel: 'var(--auc-cancel)', sealed: 'var(--auc-sealed)', sealedBg: 'var(--auc-sealed-bg)' },
  metric: { warn: 'var(--warn-line)', guard: 'var(--guard-line)', block: 'var(--guard-block)', lag: 'var(--caliber-lag)' },
  src: { tdx: 'var(--src-tdx)', ths: 'var(--src-ths)', tencent: 'var(--src-tencent)', em: 'var(--src-em)' },
  model: { kronos: 'var(--model-kronos)', chronos: 'var(--model-chronos)', xgb: 'var(--model-xgb)', agents: 'var(--model-agents)' },
}
fontSize: { 'fs-10': '10px', … , 'fs-20': '20px' }
```

### 6.3 迁移顺序（按「风险低 → 收益高」，每步可独立回滚）

| 步 | 文件 | 动作 | 替换的存量 class | 验收 |
|---|---|---|---|---|
| 1 | `src/index.css` | 修 `--stock-up-rgb` 与 flash 关键帧（§5.1 静默失效） | 无（纯缺陷修复） | 闪烁可见；CSS 单测通过 |
| 2 | `src/index.css` | 暗色 `--s4` 提亮（C1）+ hairline 三档 + `--row-zebra` | — | 阶梯单调性单测 |
| 3 | `tailwind.config.js` | 加 hairline/verdict/wyck/limit/auc/metric/src/model 映射 + `fontSize` | `stock.up:'#E53935'` 改为 `var(--stock-up)`（**冲突项 C6**） | 主题切换后 `text-stock-up` 跟随变量 |
| 4 | `src/index.css` | `.card`→`.pane`、`.card-subtle`→`.pane-inset`，删阴影 | `card` / `card-hover` / `card-subtle`（15 处） | `grep 'className="card'` = 0 |
| 5 | 全站 `src/**/*.tsx` | `bg-white/*` → `bg-s*` | 39 处 | ≤5 处且带 `mask` 注释 |
| 6 | 全站 | `shadow-float` 收敛到 1 处 | 13 处 | `grep -c shadow-float` = 1 |
| 7 | `src/components/DefenseLineBanner.tsx` | 用 `--warn-line/--guard-line` 替换 `red-*/amber-*`，去 `animate-pulse` | `border-red-600/80`、`bg-red-950/40`、`text-red-100`、`animate-pulse` | 亮色对比度 ≥4.5:1；pulse=0 |
| 8 | 新增 `components/biz-ui/SourceBadge.tsx` | 与 `CaliberBadge` 并排，先源后口径 | — | 每个资金数字可答「源 + 口径」 |
| 9 | 新增 `components/VerdictChip.tsx` / `WyckoffBadge.tsx` | 三态与形态徽标 | 现有 `ThreeTierVerdictRow` 内联文本 | 图标+文字齐全；C5 不撞色 |
| 10 | `src/pages/Auction.tsx` 及 `AuctionAnomalyTab.tsx` | 09:20 前后分组 + `--auc-*` | `text-red-500`（`Auction.tsx:47`） | 虚实可辨；`text-red-500` 归零 |
| 11 | `src/App.tsx` | 侧栏去 `bg-card/80 backdrop-blur`；prefs 持久化 | `bg-card/80`、`backdrop-blur` | `grep backdrop-blur` = 0；焦点模式跨刷新 |
| 12 | `src/realtime/useQuoteStream.ts` | 250ms 合帧 flush | — | commit ≤4 次/秒 |
| 13 | 全站 | `text-[Npx]` → `text-fs-*` | 461 处 `text-[10px]` 等 | 任意值计数 = 0 |

### 6.4 需替换的存量 class 汇总（可直接当 grep 清单）

```
className="card            → .pane
card-hover / card-subtle   → .pane / .pane-inset
bg-white/[0.0x]            → bg-s0|s1|s2|s3
shadow-lg|xl|2xl|md        → border-hairline-strong + bg-s3
border-border/60|/40       → border-hairline
bg-card/80 backdrop-blur   → bg-s1 + border-hairline
text-red-500|green-500     → limit-up-fg / limit-down-fg / stock-*
border-red-600/80 bg-red-950/40 animate-pulse → metric-guard + guard-breathe
text-[10px]                → text-fs-10
```

### 6.5 冲突项清单（本文与现有代码的全部矛盾点，供评审逐条签字）

| ID | 冲突 | 位置 | 处理 |
|---|---|---|---|
| C1 | 暗色 `--s4` 明度低于 `--s3`，浮层反向下沉 | `index.css:152` | 提亮至 `#2D2D43` |
| C2 | `.card` 同时用 border+圆角+阴影，违反去卡片化 | `index.css:246-248` | 降级为 `.pane` |
| C3 | `shadow-float` 14 处 vs 铁律 1 处 | `index.css:435` + 14 调用点 | 收敛到框选浮条 |
| C4 | `bg-white/*` 净增到 39 处，R12 棘轮失效 | 全站 | 映射到 `--s*` |
| C5 | 三层滤网三态若用红/绿将与涨跌语义相撞 | `ThreeTierVerdictRow.tsx` | 改青/琥珀/玫红 + 图标 |
| C6 | `tailwind.config.js` 硬编码 `stock.up/down` Hex，与 `--stock-up` 重复定义 | `tailwind.config.js:61-64` | 改 `var(--stock-up)` |
| C7 | flash 关键帧 `hsl(var(--stock-up)/…)` 语法非法，闪烁静默失效 | `index.css:341,345` | 改 `rgb(var(--stock-up-rgb)/…)` |
| C8 | `.sida-flash` 重复定义（348 / 418），`border-radius` 与时长分散 | `index.css:348,418` | 合并单一定义 |
| C9 | `DefenseLineBanner` 用原始调色板 + `animate-pulse`，绕开 token 与降级层 | `DefenseLineBanner.tsx:76-88` | 见 C7 步 7 |
| C10 | `CaliberBadge`(TS) 与 `caliber.py`(Py) 双份枚举，易漂移 | `CaliberBadge.tsx:11` / `caliber.py:19` | 注释互指 + 契约测试 |

---

## 7. 与产品铁律的对齐自检

| 铁律 | 本文如何满足 |
|---|---|
| 行情终端 / 看图决策 / 不做数据堆砌 | 所有徽标 ≤30px、10px 等宽；形态优先（竖条/描边/线型），色为辅 |
| K 线绝对主角（≥80% 屏宽、无卡片包裹） | L0 `--chart-canvas` 独立成层，明令禁止圆角/阴影/边框包裹；§5.4 保证重绘不闪白 |
| 去卡片化、hairline 取代边框与卡片 | 新增 3 档 hairline；`.card`→`.pane`；C2/C3/C4 三条冲突项 |
| `.shadow-float` 只用于框选即问浮条 | C3 收敛到 1 处，其余改亮度差 |
| 买红卖绿（`stock-up=红`/`stock-down=绿`） | §4.1 保持 `#E53935`/`#43A047` 不动；新增涨停/跌停更深的同族色，不改主角 |
| 字号只用 10/11/12/13/16/20 | `--fs-*` 六档，无第七档 |
| 数字必须 `tabular-nums` | `.font-num`（`index.css:235`）为唯一入口，价格单元格强制挂载 |
| 金额/量自动亿/万 | 复用 `format.ts` `safeMoney/toAmount/safeNetInflow`，不在展示层重写；缺数据走 `'--'` |
| 不做大盘涨跌方向预测 | 本文只定义色与层级，**不引入任何预测类色标**；模型徽标只标身份不标结论 |
| 指标缺失显式「无数据」 | §4.3.1 拦截徽标要求数字位替换为 `--` |
| 四口径不可换算、同屏各带标签 | §4.4 双徽标（源 + 口径）；`caliber_matrix.md` 为唯一权威表 |
| Frozen Ring 禁止改动 | 全文只改 `index.css` / `tailwind.config.js` / 展示组件；`caliber.py`、`symbol_guard`、`number_audit`、交易 403、TQ 优先链、`SYSTEM_PROMPT` 一律不动 |

---

## 8. 待办与未取到

1. **未取到**：K 线渲染组件实现（决定 §5.4 能否从「约束」升级为「改法」）。
2. **未取到**：`JobPanel.tsx` 内部实现（决定 §5.7 的进度条 token 是否能直接落地）。
3. **未审 19 页**（见 §1.2），其中 `ThemeMood` / `CaliberCompare` / `Tiers` 与本文强相关，建议下一轮优先。
4. 本文的 Hex 全部由 `index.css` 的 HSL 精确换算或显式指定，**无占位色**；新增色在亮/暗两档都给了值，Dim 档沿用暗档（仅 `--s*` 与 `--chart-canvas` 单独下调对比度，符合 `index.css:13-22` 的「只降对比度、不动色相与涨跌语义」）。
