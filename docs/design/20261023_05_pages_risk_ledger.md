# 交易风控闭环 / 决策账本 / 战报域 — UI·按钮·交互走查

> 基线：`v0.48.0 / 5427af0`（worktree `docs/design-5`，分支 `docs/design-5`）
> 性质：纯静态读码走查（未运行 build / 未跑 pytest / 未截图）。所有结论均给出文件:行号，凡未读到实现的一律写「未取到」，不做推断式描述。

## 0. 审计路径清单

| 域 | 文件 | 行数 | 关键锚点 |
|---|---|---|---|
| 硬防线 UI | `frontend/src/components/DefenseLineBanner.tsx` | 106 | `:65` `cost*0.92`、`:70` `-5%` 门槛、`:76-80` 配色 |
| 硬防线挂载 | `frontend/src/pages/StockWorkbench.tsx` | — | `:429` 唯一挂载点 |
| 硬防线后端 | `src/agents/intraday_monitor.py` | 2290 | `:1880-1906` 浮亏 ≤ -8% 判定 + 外发 |
| 硬防线后端 2 | `src/agents/daily_report.py` | — | `:484-486` 日报打标 |
| 止损常量 | `src/core/paper_trading_engine.py` | — | `:49-50` `DEFAULT_STOP_LOSS_PCT = 0.92`、`:479-491` 兜底 |
| 口径反例 | `src/core/scenario_cost_anchor.py` | — | `:11` 「绝不默认 -5%/-8%」 |
| 推送落库 | `src/core/notify_center.py` | — | `:77` `push_notification`、`:123` `do_push`、`:201-202` WS 广播 |
| 推送通道 | `src/core/notifier.py` | — | `:147` `build_apprise_url`、`:154-218` 六类协议 |
| 通道解析 | `src/bootstrap/runtime.py` | — | `:364` `resolve_notify_channels` |
| 决策账本 | `frontend/src/pages/DecisionLedger.tsx` | 591 | `:144` 根容器、`:194/330/384/434/506` 五段 |
| 影子枢纽 | `frontend/src/pages/ShadowHub.tsx` | 55 | `:18-41` 两 Tab |
| 影子账户 | `frontend/src/pages/ShadowAccount.tsx` | 498 | `:384-416` 拖拽区、`:219-235` PDF 下载 |
| 模拟盘 | `frontend/src/pages/PaperTrading.tsx` | 863 | `:365-391` 六按钮、`:554/574` 止损列 |
| 战报 | `frontend/src/pages/WarReport.tsx` | 269 | `:191` 重扫、`:199-259` 四段 |
| 投研枢纽 | `frontend/src/pages/ReportsHub.tsx` | 168 | `:133-164` 立即分析/问参谋 |
| 底稿归档 | `frontend/src/pages/Reports.tsx` | 239 | `:112` 唯一按钮=刷新 |
| 价格提醒 | `frontend/src/pages/PriceAlerts.tsx` | 389 | `:285-298` 顶部、`:327-333` 行按钮、`:380` 裸 JSON |
| 颜色令牌 | `frontend/src/index.css` | — | `:27-28` `--stock-up:#E53935` / `--stock-down:#43A047` |

**覆盖范围（已审）**：上表 16 个文件中的 UI 结构、按钮族、状态表达、空/错态、口径文案。
**明确未审**：
1. `@sida/biz-ui` 内 `price-alert-form-dialog`、`CaliberBadge`、`TabbedPage` 三个外部组件内部实现（本次只看到调用方传参）；
2. `src/agents/war_report*` 战报数据生产侧（只审了消费端渲染）；
3. 决策账本写入侧（cron 回填 T+1/3/5 的作业）与 `GET /api/decisions/log` 的 SQL；
4. 移动端真机布局（只读 class，未渲染）；
5. 全站其余页面（个股详情除 `DefenseLineBanner` 外、AI Copilot、通知中心页）不在本次范围。

---

## 1. 结论速览（落地度矩阵）

| 需求项 | 判定 | 依据 |
|---|---|---|
| `-8%` 硬防线·**触发判定** | **已落地（三处重复实现）** | 前端 `DefenseLineBanner:65`、后端 `intraday_monitor:1889`、`paper_trading_engine:50` |
| `-8%` 硬防线·**浮亏水位进度条** | **未落地** | 全仓无进度条组件，`DefenseLineBanner` 是纯文字条 |
| `-8%` 硬防线·**预警黄线→硬防线红线阶梯** | **部分落地** | 只有两态（`-5%` 黄 / `≤-8%` 红），无刻度阶梯、无中间水位 |
| `-8%` 硬防线·**一键纪律平仓** | **未落地** | 防线条无任何动作按钮；平仓仅在 `PaperTrading:579` 手工逐条 |
| 决策三态徽标（兑现/证伪/违背纪律） | **未落地** | `DecisionLedger` 无该三态字段与徽标，只有「信号类型/命中率」 |
| 决策详情抽屉 | **未落地** | 591 行全为纵向堆叠表格，无抽屉、无 row→detail 交互 |
| 新建决策 / 关联快照 / 一键复盘归因 三按钮 | **未落地** | 页内仅 5 个按钮：刷新、回测、过滤、上一页、下一页 |
| 交割单拖拽区 | **已落地** | `ShadowAccount:384-416` |
| 交割单 OCR 解析校对弹窗（确认入账/修正识别/重生成画像） | **未落地** | `:402` 选文件即 `upload(f)` 直连，无中间校对态 |
| 战报 8:30 / 15:30 分栏 | **未落地** | `WarReport` 单快照 `snapshot_date`，无时段维度 |
| 战报核心标的跳转 | **已落地** | `:142-145` `navigate('/stocks/:symbol', {state: navBackState('/war-report','主力战报')})` |
| 战报顶部三按钮（生成/导出 PDF/推送） | **未落地（仅有 1 个）** | `:191` 仅「重新扫描」，无导出、无推送 |
| 推送渠道（真实可用） | **已落地：apprise 六类 + 站内落库 + WS** | 见 §5.4；**PushDeer 全仓 0 命中，未落地** |
| 价格提醒批量启停 | **未落地** | `:327-333` 仅逐条 `toggle`，无 checkbox / 无批量接口 |

---

## 2. DecisionLedger 决策账本

### 2.1 列表形态：纵向表格瀑布，缺「决策」主体

- **现状**：`DecisionLedger.tsx:144` 根容器 `mx-auto max-w-[1200px] p-3 md:p-4`，往下是五个 `mt-4/6` 的 `<section>`：命中率（`:194`）、自选池信号质量（`:249`）、信号对账（`:330`）、入场候选后验（`:384`）、共振回测（`:434`）、信号明细（`:506`）。全部为 `table` + `divide-y divide-border/40`。
- **问题**：页面主语是**信号**不是**决策**。用户要回答「我为什么买、当时承诺什么、事后兑现没有」——现页面无法回答第二、第三问，因为没有任何「决策记录」实体。且六段等权堆叠，首屏（`max-w-[1200px]`，视口 900px 高）只看到「命中率」一张空表 + 「自选池信号质量」空态，信息密度与重要性倒挂。
- **改法**：
  1. 首屏改为「决策账本列表」：一行一条决策，`grid grid-cols-[88px_1fr_96px_96px_72px]`，字段＝`日期 | 标的+一句话逻辑 | 承诺周期 | 结果 | 三态徽标`；hairline 用 `border-b border-border/40`，**不加卡片边框**。
  2. 现有六段降级为列表下方的「统计附录」，用 `<details>` 或分段 Tab（`命中率 / 对账 / 后验 / 回测`），默认折叠。
  3. 排序默认「未结算在前」，空态文案沿用诚实口径（现 `:213-216` 文案可复用）。
- **验收线**：1200px 宽下首屏可见 ≥8 条决策记录；无 `rounded-xl border` 包裹的统计块出现在首屏；`data-testid="decision-ledger-list"` 存在。

### 2.2 详情抽屉：需从零建

- **现状**：无抽屉。行点击无事件（`:560-580` 的 `<tr>` 无 `onClick`）。`it.context` 只通过 `title=` 悬浮显示（`:569`）。
- **问题**：证据（`context`）是决策账本的核心资产，却塞进 `title` 属性——移动端不可达、不可复制、不可比对。
- **改法**：新增 `Sheet`（右侧 480px，`md:max-w-[480px]`）四段式：
  - 头部：标的 `StockLabel`（复用 `:566`）+ 三态徽标 + 决策时间（`tabular-nums`）；
  - ① 当时证据：`context` 全文（`text-[12px] leading-relaxed whitespace-pre-line`），附「主力意图 / 资金流 / L2」三行口径标签，缺项显式「无数据」；
  - ② 承诺：目标价 / 止损价 / 周期（T+n）；
  - ③ 结果：T+1/3/5 三列（复用 `retText`/`retClass`，null → `--` 中性色）；
  - ④ 归因：一行结论 + 「一键复盘归因」按钮（§2.4）。
- **验收线**：抽屉可 Esc 关闭、URL 可深链（`?decision=<id>`）；`context` 为空时显示「当时未记录证据」而非空白。

### 2.3 三态徽标：配色给到 hex

- **现状**：**无此三态**。全仓无「逻辑兑现 / 逻辑证伪 / 违背纪律」字段，`DecisionLedger` 只有 `signal_kind` 与 `hit/miss`。
- **问题**：把「赚了但违背纪律」和「亏了但守纪律」压成同一个「命中率」，等于把纪律执行度从账本里抹掉——这正是风控闭环最该留痕的一维。
- **改法**：新增 `verdict: 'fulfilled' | 'falsified' | 'undisciplined'`，徽标统一 `inline-flex h-5 items-center gap-1 rounded-sm border px-1.5 text-[11px] font-medium`（`rounded-sm` 而非全圆角，避免糖果感）：
  | 态 | 文案 | 文字色 | 边框 | 底色 |
  |---|---|---|---|---|
  | 逻辑兑现 | 兑现 | `#0D9488` | `rgba(13,148,136,.40)` | `rgba(13,148,136,.10)` |
  | 逻辑证伪 | 证伪 | `#EA580C` | `rgba(234,88,12,.40)` | `rgba(234,88,12,.10)` |
  | 违背纪律 | 违纪 | `#DC2626` | `rgba(220,38,38,.45)` | `rgba(220,38,38,.12)` |
  - **刻意避开** `--stock-up #E53935` / `--stock-down #43A047`：涨跌色只归价格（`index.css:105` 已定此铁律），三态若复用会与 K 线语义撞车。
  - 「违纪」额外加 `border-l-2` 左侧 2px 实线 + 图标（不靠颜色单独承载，色弱可辨）；有盈亏数字的行，数字仍走 `retClass` 涨跌色。
- **验收线**：三态在灰度截图下仍可区分；「兑现但亏损」的样本能同时出现 teal 徽标 + 绿色收益数字，不打架。

### 2.4 三个按钮：表单减负

- **现状**：无。页面按钮仅 `:150` 刷新、`:448` 回测、`:527-535` 过滤/上一页/下一页——**全是查询动作，零写入动作**。账本目前是纯只读视图（后端 `decision_log` 由 cron 写）。
- **问题**：需求要「新建决策记录 / 关联快照 / 一键复盘归因」，当前一个都没有，账本缺「人的判断」这一半。
- **改法**（表单降到 1 屏内、必填 ≤3 项）：
  1. **新建决策记录**（主按钮，右侧 `:150` 旁）：`Dialog` 三字段——标的（带 `search` 的 Combobox，自动带出当前价）、一句话逻辑（`textarea rows=3`，placeholder「因为__，所以__，若__则作废」）、纪律三件套（目标 / 止损**默认带出 `-8%` 折算价且可改**、周期 T+3/T+5 胶囊）。**不要求填证据**——证据由「关联快照」自动带。
  2. **关联快照**：不是表单，是**一个按钮 + 一次确认**。点击即刻抓当前 `get_main_intent` / `get_capital_flow` / `get_decision_pioneer` 三口径快照并挂在决策下，弹 `toast('已关联 3 项证据')`；三项任一失败时按钮内联显示「证据 2/3（L2 无数据）」。
  3. **一键复盘归因**（详情抽屜内，`variant="outline"`）：触发后端归因，返回前显示 `Loader2` + 「归因中…（约 8s）」，返回后**只给一行事实结论**（如「T+3 收益 +4.2%，同期行业 +1.1%，超额 +3.1p」），禁止输出「建议加仓」类预测。
- **验收线**：新建决策 ≤3 次输入完成；快照按钮失败也有明确「无数据」标注；归因文案不含方向预测词（用 `grep -E '看涨|看跌|建议买|预计涨'` 卡）。

---

## 3. ShadowHub / ShadowAccount / PaperTrading 影子盘与模拟盘

### 3.1 ShadowHub：枢纽页对风控零贡献

- **现状**：`ShadowHub.tsx:18-41` 两 Tab（影子账户 `manage_shadow` / 模拟盘 `manage_paper_trading`），`:51-55` 直接 `TabbedPage`。
- **问题**：枢纽页没有任何「当前风险水位」摘要。用户进「影子」是想知道「我模拟盘现在亏到哪了」，却要先点 Tab。
- **改法**：Tab 标签右侧加计数/风险点：`影子账户` / `模拟盘` + 当有仓位触及 `-5%` 时标签后挂 `<span className="ml-1 rounded-full bg-amber-500/15 px-1.5 text-[10px] text-amber-500">2</span>`；`-8%` 击穿用 `#DC2626` 同款。**不加卡片、不加 banner**。
- **验收线**：有击穿仓位时，未点进 Tab 即可见红色计数。

### 3.2 ShadowAccount：三段式结构已成型，但顺序反了

- **现状**：`:238` 根容器 `max-w-5xl px-4 py-6 space-y-6`；顺序为 K 线复盘（`:250-289`）→ 我的画像（`:291-381`）→ 上传区（`:384-416`）→ 错误（`:418`）→ 结果（`:424`）。
- **问题**：① 首次用户进页面，**看到的第一块是空 K 线**（`:275-278` 无成交明细提示），而动作入口「上传」被推到第三屏——空态与 CTA 分离。② `:321` 空态用了 `border border-dashed` + `bg-accent/20` 的卡片式容器，与去卡片化冲突。③ 画像 `StatCard`（`:341-371`）`grid-cols-5`，其中「胜率」在 `:352` 前端自算 `(win/total*100).toFixed(0)` —— 与「百分比只取后端值」的账本口径（`DecisionLedger:52` ④）不一致。
- **改法**：
  1. 无画像时（`!myProfile`）把上传区提到首位，K 线区显示一行 `text-[12px] text-muted-foreground`「上传后这里会标出你的真实买卖点」，**不渲染空图表容器**。
  2. 空态容器改 `border-l-2 border-border/60 pl-3`（hairline 竖线），去掉 dashed 边框与底色。
  3. 胜率改为后端字段（`win_rate`）优先，拿不到才 `--`，不在前端 `toFixed`。
- **验收线**：新用户首屏 100% 出现「点击或拖拽上传交割单」；页面内 `rounded-xl`/`dashed` 计数为 0（除拖拽区本体）。

### 3.3 PaperTrading：按钮族同级、无风险分层

- **现状**：`:344` 根容器；`:356` 标题 `text-[16px] font-bold`；`:365-391` **六个** `variant="outline" size="sm" className="h-8"` 同级按钮：已平仓记录 / 通知 / 扫描 / 刷新 / 启停 / 重置（`:388` 重置虽加 `text-destructive` 但仍是 outline 同尺寸）。持仓表 `:542` 有「止损/止盈」列（`:554-555`、`:574` `safeFixed(p.stop_loss, 2, '-')`）。摘要区 `:429-470` 已去卡片化（`border-l border-border/40 pl-3`，数字 `tabular-nums`）——**这块是全站最好的示范，应作为基准**。
- **问题**：① 六个同级按钮里「重置账户」与「刷新」视觉权重相同，误触代价极高。② 持仓表只给止损**价格**，不给「距止损还有多远」——用户要自己算。③ 摘要「最大回撤」用 `text-emerald-500`（`:461`）——**回撤是风险，不该用涨跌绿**，且与「买红卖绿」中绿色=下跌的语义混用。
- **改法**：
  1. 按钮分层：主操作「扫描」→ `variant="default"`；「已平仓记录 / 通知 / 刷新 / 启停」→ `variant="ghost"`；「重置」移到页脚 `text-[11px] text-destructive/70 hover:text-destructive underline`，并加二次确认输入账户名。
  2. 持仓表在「止损」列右侧加水位条（见 §4.2）：`w-16 h-1 rounded-full bg-border` 内嵌 `bg-amber-500`/`bg-red-600` 填充，`title="距止损 3.1%"`。
  3. 回撤数字改中性 `text-foreground`，仅当 `max_drawdown_pct > 15` 时用 `text-amber-500`、`> 25` 用 `text-red-500`。
- **验收线**：重置按钮不再与刷新同尺寸同级；每个持仓行可见水位条；回撤色不与涨跌色重合。

---

## 4. `-8%` 硬防线（核心）

### 4.1 判定逻辑：已落地，但**同一规则三处各写一遍**

- **现状**：
  - 前端 `DefenseLineBanner.tsx:65` `const defaultDefense = cost * 0.92`，`:66` `isBreached = currentPrice <= defaultDefense`，`:67` `lossPct = (price-cost)/cost*100`，`:70` 「`lossPct > -5` 则不渲染」。
  - 后端盘中 `intraday_monitor.py:1888-1889` `loss_ratio = (cur-cost)/cost`；`loss_ratio <= -0.08` 触发，`:1890` 拼 `🚨【触发硬防线警报】…已触及 -8% 硬止损防线`，`:1896-1904` 走 `push_notification(category="risk_warning", level="critical", also_push=True)`。
  - 模拟盘引擎 `paper_trading_engine.py:49-50` `DEFAULT_STOP_LOSS_PCT = 0.92`，`:479-491` 优先用信号自带止损、不合理才兜底 8%。
  - 日报 `daily_report.py:484-486` 同样打标。
- **问题**：
  1. **阈值三处硬编码**（`0.92` / `-0.08` / `0.92`），改一处必漏两处。`DecisionLedger:175-191` 已有「当前阈值(只读)」展示机制，却没把 `-8%` 纳入。
  2. **与产品口径直接冲突**：`scenario_cost_anchor.py:11` 明写「没有纪律止损就**不出现**纪律位，**绝不默认 -5% / -8%** 这类行业惯例」。而 `DefenseLineBanner:65` 恰恰是「没配置就默认 8%」。
  3. `:98` 文案「距离硬防线仅剩不到 3%」是**写死的字符串**，与 `:70` 的 `-5%` 门槛只是碰巧对应（-5% 处距离正好 3%），实际水位 3.7% 时也会说「不到 3%」。
  4. 判定用 `currentPrice <= defaultDefense` 的**价格比较**，未处理复权/除权，也未处理当日跌停无法卖出的情况。
- **改法**：
  1. 阈值收敛到单一来源：新增后端 `GET /api/risk/defense-line`（或复用 `decisionsApi.thresholds()`），返回 `{ stop_loss_pct: -8, warn_pct: -5, source: 'env'|'default', symbol_overrides: {...} }`；前端**只读不写死**，删除 `:65` 的 `0.92` 与 `:70` 的 `-5`。
  2. 落实 `scenario_cost_anchor` 的裁决：**未配置止损时，防线条不显示「-8% 硬防线」，改显示中性提示**「未设纪律止损 · 点击设置」，点击打开设置（预填 -8% 但需用户确认）。已配置才走红线逻辑。
  3. `:98` 改为计算值：`距硬防线 ${safeFixed((currentPrice/defaultDefense-1)*100, 1)}%`。
  4. 文案去 emoji：`:90` 的 `🚨` / `⚠️` 与 `:97` 的「不听任何故事！」属喊单体，改为中性纪律陈述（见 §4.3）。
- **验收线**：全仓 `grep -rn '0\.92\|-0\.08' frontend/src` 仅剩 0 处业务常量；未设止损的持仓**不出现**任何「-8%」字样。

### 4.2 告警 UI：两态文字条 → 水位阶梯

- **现状**：`DefenseLineBanner:74-105` 单行 flex：左图标 + `font-bold` 标题 + 一句长文案 + 右侧「纪律高于预测」。容器 `rounded border px-3 py-2 text-[12px]`，
  - 击穿：`border-red-600/80 bg-red-950/40 text-red-100 animate-pulse`（`:78`）；
  - 预警：`border-amber-500/50 bg-amber-950/20 text-amber-200`（`:79`）；
  - `animate-pulse` 常驻（`:78`）——页面静止时它一直在闪。
- **问题**：① 无水位可视化，用户看不出「亏了 4% 还是 7.8%」，只能读数字。② 常驻脉冲动画在盯盘场景是噪声源（多处同时闪会盖过 K 线）。③ `rounded + bg-*-950/40` 是典型卡片底，与去卡片化冲突。④ 只在 `StockWorkbench:429` 挂载一处，模拟盘/影子盘看不到——**风控最强的信号只出现在最不需要提醒的页面**。
- **改法（三阶水位条）**：
  ```
  容器：border-l-2 pl-3 py-1.5（无圆角、无底色）
  左竖线色：预警 #F59E0B ／ 击穿 #DC2626
  第一行：[图标 14px] 浮亏 -6.4%  ·  硬防线 -8.00（6.42 元）  ·  距防线 1.6p
          ↑ 数字统一 text-[13px] font-medium tabular-nums；-6.4% 用 #F59E0B
  第二行：刻度条 h-1 w-full rounded-full bg-border/60，轨道左 0% 右 -10%
          -5% 处 1px 竖线 #F59E0B（预警黄线）
          -8% 处 1px 竖线 #DC2626（硬防线红线，线宽 2px）
          当前水位填充：< -5% 用 #F59E0B；≤ -8% 用 #DC2626
  第三行：文字动作「按纪律减仓 · 记一笔」（链接样式，text-[11px] underline）
  ```
  - 动画：仅在**首次进入击穿态**时 `animate-[pulse_1.2s_ease-in-out_3]`（3 次即停），不做常驻。
  - 击穿态文案（去掉喊单体）：「浮亏 -8.3%，已触及 -8% 纪律线。执行减仓/清仓，今日不再开新仓。」——陈述 + 动作，不喊「不听任何故事」。
  - 挂载扩展：`PaperTrading` 持仓表行内复用紧凑版（§3.3-2），`ShadowAccount` 顶部汇总一条（「影子账户 2 只标的跌破纪律线」）。
- **验收线**：灰度下黄线与红线可通过线宽（1px vs 2px）区分；页面静止 5s 内无持续动画；`grep -c 'animate-pulse' DefenseLineBanner.tsx` 为 0。

### 4.3 「一键纪律平仓」：**未落地，且不宜做成真·一键**

- **现状**：`DefenseLineBanner` 内无任何按钮（`:74-105` 结构仅图标/文字/尾标）。平仓动作只存在于 `PaperTrading:579-587`：持仓行末尾一个 `Button`，`:212-227` 走 `handleClose` → `toast('平仓成功')`。
- **问题**：防线条喊「请无条件执行纪律止损」，但不给执行入口——用户读完还得自己翻到模拟盘找标的。**喊了不做，等于把纪律变成道德指责**。
- **改法**：
  - 按钮文案：`按纪律减仓`（不是「一键平仓」——A股 T+1 与涨跌停使「一键」不可靠，且真实监管语境下自动平仓有合规风险）。
  - 位置：防线条第三行右端，`variant="outline" size="sm"` + `border-red-600/60 text-red-400 hover:bg-red-950/30`，`h-7 text-[11px]`。
  - 点击 → `Dialog`（宽 400）：「将按市价卖出 {qty} 股 {name}，预估回收 {amount}。此操作不可撤销。」+ 两个按钮 `取消`（ghost）/ `确认减仓`（destructive）；**默认焦点在取消**。
  - 平仓后该行原地替换为「已按纪律减仓 · 10:23 · 均价 6.38」，`text-[11px] text-muted-foreground`，不弹模态、不再脉冲。
  - **禁止**：全自动无人值守平仓（与「不做方向预测」铁律同源——系统只执行用户预设的纪律，不替用户决策）。
- **验收线**：防线击穿 → 3 次点击内完成减仓；确认框默认焦点在「取消」；平仓成功后原处留痕时间戳。

### 4.4 数据新鲜度：一次性 fetch，不跟盘

- **现状**：`DefenseLineBanner:37-58` `useEffect` 依赖 `[symbol, cost, hasPosition]`，`fetchAPI('/quotes/...')` 只取一次；异常静默 `setCurrentPrice(null)`（`:51-53`），而 `:60` 的卫语句会让整条**直接消失**。
- **问题**：① 盘中价格不刷新——9:32 拉到的价用到 14:55，防线判定等于失效；巡游一次页面不重进就不会更新。② 取价失败时警报**无声消失**（`return null`），这是最危险的失败模式：看不到警报会被误读为「安全」。
- **改法**：① 盘中（9:30-15:00）用 `setInterval` 30s 轮询，或接现有 WS 行情推送；离开交易时段停轮。② 取价失败渲染**降级态**：`border-l-2 border-muted-foreground/40` + 「行情获取失败，防线未知（不是安全）· 重试」，绝不返回 `null`。
- **验收线**：Mock 418 后页面出现「防线未知」而非空白；进入页面 30s 后价格发生变化。

---

## 5. 交割单 OCR 与解析校对

### 5.1 拖拽区：可用，但缺「解析前预览」

- **现状**：`ShadowAccount.tsx:384-416`：`border-2 border-dashed rounded-xl p-8 text-center`，`dragOver` 切 `border-primary bg-primary/5`（`:386`），`onDrop` 取 `files[0]` 直接 `upload(f)`（`:390-394`），`accept=".csv,.xlsx,.xls,.pdf"`（`:400`），`upload` 超时设 180000ms（`:149`）。转圈文案「正在分析交割单，生成行为画像...」（`:407`）。
- **问题**：① `upload()` 一步到「生成画像」，**中间没有『我识别到了 N 笔、M 笔存疑』这一步**。券商 CSV 列名漂移时，用户拿到的是一份错误画像而毫不知情——这是影子盘最致命的静默错误。② `:149` 注释自承「586 笔实测 75s」，180s 转圈期间无进度、无取消。③ 多次上传会怎样（覆盖/追加）未在 UI 说明。
- **改法**：拆成三步流水（`上传 → 校对 → 入账`），进度条 + 取消：
  - 转圈态加 `进度：解析中 3/5 步（识别成交 → 配对回合 → 生成画像）`（若后端无法给进度，退化为「已用 12s / 预计 75s」的计时器，诚实优于假进度）；
  - 右上角 `取消`（`text-[11px] text-muted-foreground underline`）。
- **验收线**：上传后 **必先** 出现校对弹窗，不得直接出画像；>60s 时可见已用时长。

### 5.2 解析校对弹窗：**未落地，必须补**

- **现状**：不存在。`upload()` 成功即 `setResult(...)` 渲染画像（`:425-449`），无 `review`/`confirm` 中间态。
- **问题**：无校对＝无「修正识别」入口，也无「确认入账」语义边界——用户不知道这笔数据是否已进画像库。
- **改法**：`Dialog`（`max-w-4xl`，`h-[80vh]`）三区：
  1. **头部摘要**（一行）：`识别 586 笔 · 覆盖 12 只标的 · 时间 2025-01-02 ~ 2025-12-31 · 3 笔存疑`（存疑数用 `#F59E0B`）。
  2. **表格**：`日期 | 标的 | 方向(买红/卖绿) | 数量 | 价格 | 金额 | 状态`；存疑行整行 `bg-amber-500/5` + 左 `border-l-2 border-amber-500`，末列可点击「修正」（内联 `input`）。
  3. **底部三按钮**（右对齐）：`重生成画像`（ghost，仅存疑清零后可用）/ `修正识别`（outline，打开字段映射面板：列名 → 字段下拉）/ `确认入账`（primary，`确认入账 586 笔`）。
  - 存疑未处理时「确认入账」点击给出 `toast('还有 3 笔存疑，请先修正或忽略', 'error')` + 行内高亮，**不允许静默放过**。
- **验收线**：弹窗内能看到「笔数/存疑数」；存疑 >0 时无法直接入账；三条按钮文案与本文一致。

### 5.3 「记录进账」后的去向要可见

- **现状**：`:330-333` 画像展示「更新时间 / 交易区间」，`:413` 上传区提示 `.csv/.xlsx/.pdf（同花顺、国投、东财格式自动识别）`。PDF/HTML 导出存在（`:219-235` `downloadPdf` 走 `fetch + blob`，`:232` 注释 KI-010 鉴权 401 的旧坑）。
- **问题**：上传→画像之间存在「这批成交有没有落库到 `shadow_trades`」的黑箱。`:275-278` 的 K 线复盘依赖 `trades`，如果只是生成了画像但成交没入，两处会不一致。
- **改法**：校对弹窗「确认入账」成功后，`toast('已入账 586 笔成交，画像已更新')`，并在「我的画像」标题右侧加一行 `text-[11px] text-muted-foreground`：`成交 586 笔 · 已配对回合 214 个`。
- **验收线**：画像页可见成交笔数与回合数，且与 K 线标记笔数一致（`:283` 已有 `pickedMarkers.length`）。

---

## 6. WarReport 战报

### 6.1 紧凑感：现在是「四个卡片」，不是战报

- **现状**：`WarReport.tsx:173` 根容器 `sida-page-enter space-y-4`；四段各为 `<section className="overflow-hidden rounded-xl border border-border/60 bg-card">`（`:199`、`:207`、`:215`、`:239`），标题 `text-[13px] font-medium`；表头 `bg-muted/60`（`:59`）。
- **问题**：① `rounded-xl border bg-card` × 4 = 典型卡片堆，与「去卡片化 / 数字为主角」直接冲突——战报最该「像一张纸」，现在像仪表盘拼图。② 表格 `px-3 py-2`（行高约 36px），一屏只能看 ~10 行；战报的价值在「一眼扫完 TOP20」。③ 标题同级无层级（四个 13px 等重）。
- **改法**：
  1. 去掉四个 `rounded-xl border bg-card`，改 `border-t border-border/60 pt-2` 分节，节标题：
     `<h3 className="text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">净流入 TOP</h3>` —— 与小节正文字号拉开 11/13 对比。
  2. 行高收紧到 `py-1`（约 26px），`text-[12px]`；表头去掉 `bg-muted/60` 底色，改 `border-b border-border/60`。
  3. 金额列宽固定（`w-20 text-right`），保证数字纵列对齐；数字 `tabular-nums`（`:88/91/226` 已是 `font-mono`，可保留）。
  4. 行业分布（`:221-230` `<ul>`）改为双列 `grid grid-cols-2 gap-x-6`，一屏可看 10 个行业。
- **验收线**：900px 高视口内 TOP 净流入表可完整见 20 行；页面 `rounded-xl` 计数为 0。

### 6.2 8:30 盘前 / 15:30 盘后 分栏：**未落地**

- **现状**：单一 `data.snapshot_date`（`:182`），一次 `warReportApi.daily()` 取当日报表；无 `session`/`phase` 字段，无时段切换控件。全站唯一「盘前」语义入口在 `ReportsHub:134-150` 的「立即分析」（`premarketApi.run()`），与战报无关。
- **问题**：盘前关注「隔夜/竞价前的资金意图」，盘后关注「全天净额与拆单」，两者指标口径与情绪完全不同的，混成一张快照会让用户拿盘后数据当盘前依据。
- **改法**：
  - 页头右侧加**分段控件**（`inline-flex rounded-md border border-border/60 p-0.5`）：`08:30 盘前` / `15:30 盘后`，选中项 `bg-accent text-foreground`，未选 `text-muted-foreground`；URL 同步 `?session=pre|post`。
  - 盘前态表格隐藏「净额变化/采样数」两列（盘前无采样，`:94-95` 会全是 `--`），改显示「隔夜大单占比」；盘后态显示全列。
  - 时段标签写在 `snapshot_date` 后：`2026-10-23 · 15:30 盘后快照`。
  - **数据缺失时**该时段显示「本时段快照未生成 · 08:30 后自动生成」（复用 `:151-167` 的 unavailable 卡，但此处可保留卡片——空态卡是允许的）。
- **验收线**：`?session=pre` 与 `?session=post` 表格列数不同；盘前态不出现成片的 `--`。

### 6.3 核心标的跳转：已落地，缺「返回语义」验收

- **现状**：`:142-145` `pick()` → `navigate('/stocks/${symbol}', { state: navBackState('/war-report','主力战报') })`；行 `cursor-pointer hover:bg-accent/20`（`:81`）+ `title={r.symbol}`（`:83`）。
- **问题**：① 整行 `onClick` 但 `<tr>` 无 `tabIndex`/`role`，键盘与读屏不可达。② 代码列已 `text-primary`（`:86`）看似链接，但实际点击热区是整行，视觉与行为不一致。③ `%` 无 `aria-label` 说明「点击进入个股」。
- **改法**：`<tr tabIndex={0} role="link" aria-label={`查看 ${r.name||r.symbol} 详情`} onKeyDown={Enter/Space}`；代码列加 `hover:underline`；行尾补一个 `ChevronRight` `h-3 w-3 text-muted-foreground/50`（仅 hover 显形）。
- **验收线**：Tab 可聚焦、Enter 可跳转；返回后回到战报且保留 session 参数。

### 6.4 顶部三按钮（生成 / 导出 PDF / 推送）：**只有一个，且推送渠道必须写实**

- **现状**：`:191` 唯一按钮「重新扫描」→ `warReportApi.refresh()`（`:129-140`，同步约 16s）。**无「导出 PDF」、无「推送」按钮**。导出能力在别处存在（`ShadowAccount:219-235` 的 `downloadPdf` 走 `fetch+blob` 带 `Authorization`），战报未接。
- **问题**：① 缺两个按钮＝战报只能「看」，不能「交付」。② `refresh` 是同步阻塞（`:136` 注释 16s），按钮 `disabled={refreshing}` 但无进度，用户会重复点。③ 末尾 `:263` 直出 `/api/war-report/daily` 裸 API 链接给用户——对普通用户是噪声，且暴露内部路径。
- **改法（三按钮视觉层级）**：
  | 按钮 | variant / class | 位置 |
  |---|---|---|
  | `生成战报` | `variant="default"` `h-7 text-[12px]`（主） | 页头最右 |
  | `导出 PDF` | `variant="ghost"` `h-7 text-[11px]` | 主按钮左 |
  | `推送` | `variant="ghost"` `h-7 text-[11px]` + `Send` 图标 | 次左 |
  - 生成中：按钮内 `Loader2` + 文案「生成中 12s…」（计时器，因同步接口无进度），并禁用导出/推送（数据未就绪）。
  - 导出：复用 `ShadowAccount:222-235` 的 `fetch + blob + a.download` 模式（**不要** `window.open`，KI-010 会 401）。
  - 推送：`Dialog` 列出**真实可用渠道**（见 §6.5），默认勾选上次选择，底部 `推送到 2 个渠道`。
  - 删掉 `:263` 的裸 API 链接，改为 `text-[11px]` 的「数据源 thsdk_dde · 口径 eastmoney4」纯文本注脚。
- **验收线**：三按钮同屏、主次可辨（主按钮为实心）；生成中导出/推送为 disabled；不存在裸 `/api/` 链接。

### 6.5 推送渠道的真实清单（必须按此写，不得写 PushDeer）

- **现状（已验证）**：
  - `notify_center.py:77` `push_notification(title, body, category, level, also_push, source, user_id)` → `:123` `do_push = also_push if also_push is not None else (level in _PUSH_LEVELS)` → `:126/142/163` `_after_push_hook` → `:201-202` `broadcast_notification(user_id, payload, category=category)`（**WS Hub 实时推送到在线端**）。
  - `notifier.py:147-218` `build_apprise_url` 支持的 `channel_type`：`telegram`、`bark`、`dingtalk`、`lark`、`discord`、`pushover`（其余抛 `ValueError: 不支持的 Apprise 渠道类型`）。
  - `runtime.py:364` `resolve_notify_channels`：传 `user_id` 时取「该用户渠道 ∪ 全局共享(`user_id IS NULL`)渠道」，且仅 `enabled == True`。
  - **PushDeer：全仓 0 命中，未落地。** 任何文档/UI 不得将其列为可选渠道。
- **改法**：推送弹窗渠道列表 = ① `站内通知`（必选，不可取消，说明「写入通知中心 + 在线端实时弹出」）② 上述六类 apprise 渠道（从 `GET /channels` 拉，`PriceAlerts:110` 已有同款调用）③ 无可用渠道时显示「未配置外部渠道 · 前往『通知设置』添加」，附链接，**不造假按钮**。
- **验收线**：渠道弹窗内不出现 PushDeer；无渠道时给出去处而非灰按钮；推送后 `notify_success` 状态可回查（对齐 `PriceAlerts:375` 的成功/失败着色）。

---

## 7. ReportsHub / Reports 报告域

### 7.1 ReportsHub：入口已扶正，但只有盘前

- **现状**：`:25-47` 两 Tab：`分析`（History，`icon: Clock`）/ `底稿归档`（Reports，`perm: view_reports`）；`:133-164` 顶部「立即分析」（`premarketApi.run()` 入队 + 30s 轮询 `getToday`，`RUN_POLL_INTERVAL_MS = 30_000`，`:92-112`）+「问参谋」（跳 `/ai-copilot?agent=premarket_strategist&q=...`，`:124-126`）。按钮用了**裸 button** 而非 `Button` 组件（`:134-150`，`h-8 rounded-md border border-border/60 text-[12px]`），与 `WarReport:191` 的 `<Button>` 不一致。
- **问题**：① 只有盘前 8:30 一键触发，15:30 盘后无对应入口——「分析」Tab 里盘前盘后混排，无法区分。② 两个按钮一个实心边框一个纯文字，层级尚可，但组件族不统一（裸 `button` vs `<Button>`），主题切换会掉队。③ 30s 轮询期间按钮显示「分析生成中…」但不显示已等多久；`runState==='queued'` 最长可无限等（`:96` 只在非空时停）。
- **改法**：① 「立即分析」拆成两枚：`盘前分析 08:30`（主）/ `盘后复盘 15:30`（outline），各自绑定对应 API；② 统一改用 `<Button size="sm" variant=...>`；③ queued 态文案带计时：`生成中 · 已等 1m20s`，并在 5 分钟未出结果时切 `runState='error'` + 「超过 5 分钟未生成，可重试」；④ 清掉 `:139` 的 `disabled:pointer-events-none` 之外的冗余（`disabled:opacity-50` 保留即可）。
- **验收线**：盘前/盘后两入口并存且文案含时间；无裸 `<button>`；5 分钟超时给出显式错误。

### 7.2 Reports（底稿归档）：纯列表，无导出/无推送

- **现状**：`:98-112` 页头 + 刷新（`:112` 用 `<Button variant="ghost" size="sm" className="min-h-[44px]">`）；`:62-83` `filtered`/`grouped` 按 `job_id` 分组；`:85-96` `openItem` 拉 `reportsApi.content` 进 `selected`；`:30` 骨架用 `<Card>`。
- **问题**：① 与 WarReport 同样缺「导出/推送」。② 44px 高的 ghost 刷新按钮与列表 `text-[12px]` 密度不匹配（`:112`）。③ `:76-83` 按 job 分组后无组内计数，长列表难定位。④ 纯 `<Card>` 骨架（`:30`）与去卡片化不一致（骨架可容忍，但组容器不应是卡片）。
- **改法**：① 页头右侧与战报对齐：`导出`（ghost）/`推送`（ghost），复用 §6.5 渠道弹窗；② 刷新降至 `h-7`；③ 组标题行加 `文本数`（`text-[11px] text-muted-foreground`）：`盘前分析 · 42 篇`；④ 列表行改 `border-b border-border/40`，去掉 `<Card>` 包裹。
- **验收线**：组标题可见篇数；导出/推送与战报同款交互（同一个 Dialog 组件，不重复实现）。

---

## 8. PriceAlerts 价格预警

### 8.1 触发条件表单：能力齐但藏在 Dialog 里，主页面无摘要

- **现状**：表单在外部包 `@sida/biz-ui/components/price-alert-form-dialog`（`:8` 引入，`:349-360` 调用），支持 `price / change_pct / turnover / volume / volume_ratio` 五类型 + `and/or` 组合（`conditionText:71-83` 的映射即证据）；`DEFAULT_FORM`（`:51-62`）默认 `cooldown_minutes: 30`、`max_triggers_per_day: 3`、`repeat_mode: 'repeat'`、`market_hours_mode: 'trading_only'`。**该组件内部结构本次未取到，不做评价。**
- **问题**：① `:320` 列表页把条件拼成一行文本 `价格 >= 6.5 且 涨跌幅% >= 3`，读起来是代码而非自然语言（`>=` `.join(' 且 ')`）。② 冷却/日上限（`:322-324`）是一串「冷却 30 分钟 · 日上限 3 次 · 最近触发 --」，`--` 与真时间同色，扫不出「从未触发」。③ 预填逻辑（`:126-159`）从 URL 自动建股票，但失败时静默 fallback 到手动选择（`:145-147` `catch {}`），用户会以为预填坏了。
- **改法**：① 条件文案自然化：`价格 ≥ 6.50`、`涨幅 ≥ 3%`、区间 `价格 6.00–7.00`（`between` 已在 `:79-81` 处理，只需换 `∈ [a,b]` 为 `a–b`）；② 「最近触发」为空时用 `text-muted-foreground/60` + 文案「未触发」；③ 预填失败给一次 `toast('未能自动带入该标的，请手动选择', 'info')`。
- **验收线**：列表条件行不出现 `>=`/`∈` 符号；「未触发」与真时间视觉可辨。

### 8.2 已触发 / 待触发 状态色：现在只有「启用/暂停」

- **现状**：`:317` 状态徽标仅两态：`启用`（`bg-emerald-500/15 text-emerald-700 dark:text-emerald-500`）/ `暂停`（`bg-muted text-muted-foreground`）。**无「已触发」态**——触发信息只体现为 `last_trigger_at` 文本与 `trigger_count_today`（`:39-40`，页面未展示后者）。
- **问题**：「启用」是配置态，「已触发」是运行态，两者混用会让用户以为「启用了就等于会被通知」。真实失望点在于：规则触发了但通知失败（`AlertHit.notify_success=false`，`:375`），主页面上完全看不出来。
- **改法**：状态列拆两枚小签（`text-[10px] px-1.5 py-0.5 rounded-sm`，不用全圆角）：
  | 态 | 文案 | 文字 / 底 |
  |---|---|---|
  | 待触发（enabled 且今日未触发） | 待触发 | `#6B7280` / `rgba(107,114,128,.12)` |
  | 已触发（今日 `trigger_count_today > 0`） | 已触发 ×2 | `#F59E0B` / `rgba(245,158,11,.14)` |
  | 已触发但通知失败 | 通知失败 | `#DC2626` / `rgba(220,38,38,.14)` |
  | 暂停 | 已暂停 | `text-muted-foreground` / `bg-muted` |
  - **不使用** `--stock-up/down`：预警不是涨跌。
- **验收线**：一条今日已触发的规则在列表上不必展开命中历史即可看出「已触发/通知失败」。

### 8.3 批量启停：**未落地**

- **现状**：`:327-333`（桌面）/ `:336-343`（移动）每行 5 个按钮：`测试 / 命中历史(图标) / 编辑 / 停用|启用 / 删除(图标)`；启停走 `:224-231` `POST /price-alerts/{id}/toggle`，逐条 `await load()` 重拉全表。顶部（`:286-298`）只有「立即扫描」+「新建规则」，**无全选、无批量**。
- **问题**：① 每条启停都 `await load()` 全量刷新，10 条规则连点 10 次 = 10 次列表请求，且每行按钮在此期间无 loading，可重复点。② 「停用」用 `variant="destructive"`（`:331` `r.enabled ? 'destructive' : 'default'`）——红色通常代表危险操作，但「停用提醒」不是破坏性动作；反而真正的破坏性动作「删除」只是 `variant="secondary"`（`:332`）。**危险色用反了**。③ 无批量。
- **改法**：
  1. 行首加 `<input type="checkbox" className="mt-1 h-3.5 w-3.5 accent-primary">`，选中后顶部条切换为批量条：`已选 3 条 · 批量启用 · 批量停用 · 取消`；`rules.length > 0` 时全选框在表头。
  2. 批量接口：本仓**未见** `/price-alerts/batch`；实现方式二选一并写明：①（推荐）后端加 `POST /price-alerts/batch-toggle {ids, enabled}`，一次事务；② 前端 `Promise.all` 串行 `toggle`，合并 `toast('已启用 3 条，失败 0 条')`，且**只 `load()` 一次**。禁止逐条 `await load()`。
  3. 危险色纠正：`删除` → `variant="ghost" className="text-destructive hover:bg-destructive/10"`；`停用` → `variant="ghost"`；`启用` → `variant="outline"`。行内同时保留 `测试/编辑` 为 ghost。
  4. 单行 toggle 加局部 pending（`disabled` + 图标转圈），不要整表 spinner。
- **验收线**：勾选 3 条可一次启停且仅 1 次列表请求；删除是页面唯一的红色操作。

### 8.4 命中历史：裸 JSON 不是给人看的

- **现状**：`:362-386` Dialog，`max-w-3xl`，每条命中显示时间 + 通知结果 + `JSON.stringify(h.trigger_snapshot, null, 2)` 塞进 `font-mono text-[12px] bg-accent/20 rounded p-2`（`:379-381`）。
- **问题**：`trigger_snapshot` 直接 dump 原始 JSON，字段名是英文 key，用户读不懂「当时触发的是什么条件、值多少」。
- **改法**：把 snapshot 渲染为键值对表：`条件 | 触发值 | 阈值`；保留「查看原始 JSON」折叠（`<details>`）。通知失败时在条目顶部给 `border-l-2 border-red-600 pl-2` + 错误原因全文（现在只 `h.notify_error` 一行拼接，长文会溢出）。
- **验收线**：默认视图无 `{`/`"` 字符；失败原因完整可读。

---

## 9. 分级整改清单

**P0（风控正确性，本轮必须做）**
1. `-8%` 阈值单一来源化；未配置止损时**不得**默认显示 8%（与 `scenario_cost_anchor.py:11` 对齐）。§4.1
2. `DefenseLineBanner` 取价失败禁止静默消失，改「防线未知」降级态。§4.4
3. 防线击穿提供「按纪律减仓」执行入口（二次确认，禁止全自动）。§4.3
4. 交割单上传必须先经校对弹窗才入账。§5.2

**P1（闭环缺环，建议本轮或紧随）**
5. 决策三态徽标 + 详情抽屉 + 「关联快照」。§2.2/2.3
6. `DefenseLineBanner` 挂到 `PaperTrading` 持仓行 + `ShadowHub` Tab 计数。§3.1/3.3
7. 战报 8:30/15:30 分栏 + 导出 PDF + 推送（真实渠道）。§6.2/6.4
8. PriceAlerts 已触发/待触发状态色 + 危险色纠正。§8.2/8.3-3

**P2（观感与一致性）**
9. 去卡片化：`WarReport` 四段、`ShadowAccount:321`、`Reports` 组容器。§6.1/§3.2/§7.2
10. 按钮族统一（`ReportsHub` 裸 `button` → `Button`）、按钮层级重排（`PaperTrading` 六同级）。§7.1/§3.3
11. 批量启停 + 命中历史可读化。§8.3/8.4
12. 战报表格行高压缩、数字列对齐。§6.1

**验收门槛（提交前自查）**
- `grep -rn '0\.92\|-0\.08' frontend/src/components/DefenseLineBanner.tsx` → 0 处（注：`frontend/src` 全量搜会命中若干 `opacity: 0.92` 的分享卡样式，与风控阈值无关，验收须限定文件）；
- `grep -rn 'PushDeer\|pushdeer' .` → 0 处（除本文件说明「未落地」）；
- 新写按钮文案与 §6.5 渠道列表一致，无「PushDeer / 微信机器人 / Server酱」等未接渠道；
- 所有新增状态色仅用本文给出的 hex，不引 `--stock-up/--stock-down`（`#E53935/#43A047`）表达非价格语义；
- 字号仅 10/11/12/13/16/20，金额统一 `tabular-nums` + 亿/万（`toAmountFromWan` 已在 `WarReport:31` 使用）。

---

## 10. 风险与遗留

- **未验证项**：本文所有「未落地」结论基于读码，未在运行态点验；`WarReport` 的 unavailable 分支（`:151-167`）与 `refresh` 16s 同步阻塞未实测耗时。
- **口径风险**：`-8%` 到底是「成本价 × 0.92」还是「最高价回撤 8%」未在代码中统一表达（三处实现均为前者）；若产品意图含后者，P0-1 的收敛方案需扩展为 `basis` 字段，否则会把两套纪律混为一谈。
- **未审依赖**：`price-alert-form-dialog` 内部若已含批量/校对能力，§8.3 结论需下调；本次未取到其源码。
