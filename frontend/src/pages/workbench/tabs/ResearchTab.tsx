import type { ReactNode } from 'react'
import { useInsight } from '@panwatch/biz-ui/components/insight/context'
import { ReportsTab } from '@panwatch/biz-ui/components/insight/ReportsTab'
import { DeepTab } from '@panwatch/biz-ui/components/insight/DeepTab'
import InsightProvider from '@/pages/workbench/InsightProvider'

/**
 * 工作台标签「研究」(工作台 v2 三合一, Task 15)。
 *
 * 落位(spec §三 去重表 / 计划 Task 15):
 *  ① **AI 报告** —— 盘前 / 盘后 / 新闻 三个子页签 + 报告正文(含「分析上下文」折叠);
 *  ② **深度分析** —— TradingAgents 深度分析正文 + 入口(打开 `/analysis/{symbol}/{date}`)。
 * 现价/名称/涨跌归带1 `HeaderBand`, 本标签一概不渲染(去重表)。
 * 去重表锚点: `workbench-tabs.ts` 的 `DATA_OWNERSHIP` 里 `reports → tab.research`、
 * `deep → tab.research` —— 两个数据点的唯一归属就是本标签。
 *
 * 复用 vs 自建(brief 硬要求「REUSE them (don't re-implement)」):
 *  - ① 的**三子页签与正文渲染全部复用**恢复组件 `ReportsTab`(biz-ui): 子页签按钮
 *    (盘前/盘后/新闻 → `setReportTab(premarket_outlook|daily_report|news_digest)`)、
 *    正文头(`AGENT_LABELS` · `analysis_date`)、标题、买/卖标签、Markdown 正文
 *    (`StockReportMarkdown`)与「查看分析上下文」折叠(prompt_stats / 新闻注入明细 /
 *    上下文快照 / Prompt原文)**全部由它自带**, 本文件零重写;
 *  - ② 的**正文与入口全部复用**恢复组件 `DeepTab` + `deep-analysis.tsx`(`DeepAnalysisSection`
 *    / `DeepHistoryComparison`): 决策卡(action_label / 置信度 / 理由)、历史决策 vs 实际涨跌、
 *    4 位分析师报告、看多看空辩论、免责声明与**入口按钮**「打开详情页 ↗」都来自它们。
 *    入口的目标路径由复用组件自己算出(`window.open` 打开 `/analysis/{symbol}/{结果时间戳的日期段}`
 *    —— 见 `DeepTab.tsx`), 本文件**不再造第二个入口**(否则同屏两个按钮指向同一路径)。
 *  - 两个恢复文件**一字未改**: 改它们会让本标签与其它消费方(T9 恢复面)分叉;
 *  - 本文件自建的只有: 一层 `InsightProvider` + 段头(标题/口径/条数, 恢复组件里没有)
 *    + 两段**如实的空态说明**(见下「诚实空态」)。
 *
 * 取数键 `keys={['reports','deep']}`(控制器裁定, 见 progress「Task 15 Ruling」):
 *  - `'reports'` → `insightApi.history({agent_name: premarket_outlook|daily_report|news_digest,
 *    stock_symbol: symbol, limit: 1})` ×3, 三份**全空**时再回退全局记录
 *    (`stock_symbol='*'`, limit 20, 按代码/名称匹配) —— 见 `useInsightData.loadReports`;
 *  - `'deep'` → `tradingAgentsApi.getLatestForStock(symbol)` +
 *    `getHistoryComparison(symbol, market, 90)` —— 见 `useInsightData.loadDeepResult`。
 *  两个 effect 都**只按键**门控(与内部 `tab` 无关; T13 复审已把 `deep` 的同类耦合解掉)⇒
 *  工作台标签(无旧模态标签栏, `tab` 恒 `'overview'`)只传 `keys` 即可取数, **无需任何
 *  `setTab` / 直调取数**(brief 已明示: `keys:['deep']` 单独生效)。
 *
 * `key` 挂在 `InsightProvider` 上(**必要, 非风格**): `reports`/`deepResult` 两个状态的
 * **空值重置**写在 `useInsightData` 的挂载总 effect 里, 而该 effect 第一句就是
 * `if (!isResourceEnabled(enabledKeys,'core')) return` —— 本标签**未启用 `core`** ⇒ 换标的时
 * 旧标的的报告/深度结果不会被清空, 取数 effect 又要等新响应落地才覆盖 ⇒ 中间一段会**把上一只票
 * 的报告与深度结论画在新标的名下**(深度更严重: 取数 effect 有 `if (!deepLoaded && !deepLoading)`
 * 守卫, 不重挂载则新标的**永远不取** `deep`)。整棵子树随 key 重挂载可消除该窗口(与 T13/T14 同法)。
 * **`key` 含 `market`(与 T14 的 `key={symbol}` 不同, 差异理由如下)**: 本标签的 `deep` 端点是
 * **市场维度**的 —— `getHistoryComparison(symbol, market, 90)` 的请求参数里带 market
 * (`tradingagents.ts`), 而同一 `market` 变化时 `loadDeepResult` 身份变化会让 effect 重跑、
 * 却被 `deepLoaded === true` 早退 ⇒ 不重挂载就会把**另一个市场**的历史对比留在屏上。
 * (`reports` 三个请求只带 `agent_name`/`stock_symbol`/`limit`, 与 market 无关 —— 故 market
 * 进 key 纯粹是为 `deep` 服务的。) key 用**归一化后**的 market(见标签入口), 避免 `'cn'`/`'CN'`
 * 被当成两个市场而多一次无收益的重挂载。
 *
 * 诚实空态(never fabricate):
 *  - 恢复组件的空态**不含任何编造内容**: `ReportsTab` 给「暂无报告」, `DeepAnalysisSection`
 *    给「暂无深度分析报告 · 可在持仓 / 自选页点击 🧠 深度分析按钮触发」; 本文件不往段内塞任何
 *    占位/示例条目、不补任何数值。
 *  - `reports` 段(与 T14 的 news 同组问题): `loadReports` 的 `catch { setReports([]) }` 把**取数
 *    失败静默降级为空列表**, 且 hook **未暴露** `reportsLoading`/`reportsLoaded`(无加载位、无失败位)
 *    ⇒ 列表为空时「该标的三个 agent 均无已存报告 / 取数失败 / 首拉在途」**三因不可区分**。故本段
 *    在列表为空时给一行常驻说明(`data-testid="research-reports-caveat"`), **不声称**空列表就是
 *    "没有报告"。
 *  - `reports` 段另有一种**可区分**的情形: 列表非空但当前子页签的 agent 不在其中
 *    (`activeReport === null`, 例如只有盘后日报、却停在盘前页签)—— 这不是"没有报告", 故本文件
 *    给的是**另一句**文案(`data-testid="research-reports-agent-missing"`, 且带上真实的份数),
 *    与上一条并列而非互相冒充。
 *  - `deep` 段: `deepLoaded`/`deepLoading` **有**暴露 ⇒ 加载中与已加载可分(加载中文案由复用
 *    组件给)。但 `loadDeepResult` 用 `Promise.allSettled` 把失败降级为 `null`, **没有失败位**
 *    ⇒ 已加载且无结果时「该标的尚无深度分析 / 取数失败」仍不可区分, 本文件给一行如实说明
 *    (`data-testid="research-deep-caveat"`); 若历史对比有数据(历史部分取到了), 则把该**事实**
 *    一并写在说明里(几个字都不编)。
 *  - 段头条数只在 `> 0` 时渲染: 首拉在途与确无内容不可混为一谈(与 T12/T14 同法)。
 *  - 根治(三因分离)需 provider 给 `reports`/`deep` 两个端点增失败位 + 加载位, 属跨任务 API 面
 *    —— 见 task-15-report(与 Task 11 Finding 1 / Task 14 concern 1 同组问题)。
 *
 * 归属 `src/pages/workbench/tabs/`: 与 T11–T14 同层(标签自带 Provider, 由 Task 17 的 `TabPanel`
 * 按 `?tab=research` 挂载); 本文件不新增 biz-ui 依赖方向, 只消费已有导出。
 */
/** 门控键: 模块级常量 —— 每帧新建数组会换引用(Provider 现按内容签名记忆化, 此为防御性收敛)。 */
const RESEARCH_TAB_KEYS = ['reports', 'deep'] as const

/** 展示原子(hairline 分节, 与 L2Tab/SuggestTab/FundamentalTab/NewsTab 的 Section 同形)。 */
function Section({
  id,
  title,
  hint,
  count,
  children,
}: {
  id: string
  title: string
  hint?: string
  /**
   * 份数(本标签只有「报告」用: 三个 agent 各至多 1 份); 省略或 `<= 0` 时不渲染
   * 「共 N 份」(首拉在途与确无内容不混同; 深度段的历史对比条数不套用该措辞, 只在空态
   * 说明里作事实陈述)。
   */
  count?: number
  children: ReactNode
}) {
  return (
    <section data-testid={`research-section-${id}`} className="border-b border-border/40 pb-3">
      <div className="mb-2 flex flex-wrap items-baseline gap-x-2 gap-y-0.5 text-[11px]">
        <span className="font-medium text-foreground">{title}</span>
        {hint ? (
          <span className="text-muted-foreground" title={hint}>
            {hint}
          </span>
        ) : null}
        {count != null && count > 0 ? (
          <span className="font-mono text-[10px] text-muted-foreground">共 {count} 份</span>
        ) : null}
      </div>
      {children}
    </section>
  )
}

/** 标签正文(在 Provider 内消费 useInsight; 见文件头注)。 */
function ResearchTabBody({ symbol }: { symbol: string }) {
  const { reports, activeReport, deepLoaded, deepResult, deepHistory } = useInsight()
  // 历史对比的条数: 只用于**如实陈述**"历史部分取到了几条", 不参与任何推断。
  const historyCount = deepHistory?.items?.length ?? 0

  return (
    <div className="mt-1 space-y-3 text-[12px]" data-testid="research-tab">
      {/* 条: 口径说明(现价/名称/涨跌归带1 —— 本标签一概不渲染, 去重表) */}
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-border/40 pb-2 text-[11px] text-muted-foreground">
        <span className="text-foreground">研究</span>
        <span className="text-border/60">|</span>
        <span>AI 报告(盘前 / 盘后 / 新闻 三子页签 + 正文) · 深度分析(TradingAgents)</span>
      </div>

      {/* ① AI 报告(三子页签 + 正文由恢复组件 ReportsTab 自带) */}
      <Section
        id="reports"
        title="AI 报告"
        hint={`/history ×3 agent(premarket_outlook / daily_report / news_digest, 个股 limit 1; 三份全空时回退全局记录 stock_symbol=* 按代码/名称匹配) · 标的 ${symbol}`}
        count={reports.length}
      >
        <ReportsTab />
        {/* 空态说明(**只陈述成因不可区分**, 不声称"没有报告"): 列表为空时三因并存 */}
        {reports.length === 0 ? (
          <div
            data-testid="research-reports-caveat"
            className="mt-2 text-[10px] text-muted-foreground/70"
          >
            报告列表为空时「该标的三个 agent 均无已存报告 / 取数失败 / 首拉在途」在此不可区分
          </div>
        ) : null}
        {/* 可区分的情形: 列表非空而当前子页签的 agent 不在其中 —— 这与"没有报告"不是一回事 */}
        {reports.length > 0 && !activeReport ? (
          <div
            data-testid="research-reports-agent-missing"
            className="mt-2 text-[10px] text-muted-foreground/70"
          >
            已取到 {reports.length} 份报告, 但当前子页签对应的 agent 不在其中 —— 换个子页签, 或等该 agent 产出
          </div>
        ) : null}
      </Section>

      {/* ② 深度分析(正文与入口由恢复组件 DeepTab/deep-analysis 自带) */}
      <Section
        id="deep"
        title="深度分析"
        hint={`tradingAgentsApi.getLatestForStock(${symbol}) + getHistoryComparison(90 天) · 入口: 有结果时正文上方「打开详情页 ↗」→ /analysis/${symbol}/{最近一次分析日}(新窗口)`}
      >
        <DeepTab />
        {/* 已加载且无结果: 「确无分析 / 取数失败」不可区分(hook 无失败位) —— 如实说明, 不猜原因 */}
        {deepLoaded && !deepResult ? (
          <div data-testid="research-deep-caveat" className="mt-2 text-[10px] text-muted-foreground/70">
            未取到最近一次深度分析结果{historyCount > 0 ? `(历史决策对比已取到 ${historyCount} 条)` : ''}
            —— 「该标的尚无深度分析 / 取数失败」在此不可区分
          </div>
        ) : null}
      </Section>
    </div>
  )
}

/**
 * 标签入口。`keys={RESEARCH_TAB_KEYS}`(模块级常量 `['reports','deep']`): 只启用 `/history`
 * (三 agent)与 TradingAgents 两个端点 —— quote/moreInfo/darkFlowTq/klineSummary/klines/
 * portfolioSummary/watchlist/suggestions/news/announcements/fundamentals/company 一个都不发
 * (惰性由 `TabPanel` 只渲染激活标签保证)。`key` 与 market 归一化的理由见文件头注。
 * `stockName` 与 `hasPosition` 与兄弟标签同形透传给 Provider: `stockName` 在本标签**有意义**
 * —— 未启用 `core` ⇒ `resolvedName` 拿不到 `quote?.name`, 传入后 `loadReports` 回退全局记录时
 * 才能按**名称**匹配(`hasPosition` 本标签不消费: 报告/深度都不按持仓分支)。
 */
export default function ResearchTab({
  symbol,
  market,
  stockName,
  hasPosition,
}: {
  symbol: string
  market: string
  stockName?: string
  hasPosition?: boolean
}) {
  // market 归一化(provider 内部亦归一化; 这里为了 key 的稳定性 —— 'cn' 与 'CN' 不该各挂一次)
  const mkt = String(market || 'CN').trim().toUpperCase()
  return (
    <InsightProvider
      key={`${mkt}:${symbol}`}
      symbol={symbol}
      market={mkt}
      stockName={stockName}
      hasPosition={hasPosition}
      keys={RESEARCH_TAB_KEYS}
    >
      <ResearchTabBody symbol={symbol} />
    </InsightProvider>
  )
}
