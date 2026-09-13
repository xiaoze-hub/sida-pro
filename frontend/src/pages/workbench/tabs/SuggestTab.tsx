import { useMemo, useState } from 'react'
import { Loader2, Radar } from 'lucide-react'
import { useInsight } from '@panwatch/biz-ui/components/insight/context'
import { SuggestionsTab } from '@panwatch/biz-ui/components/insight/SuggestionsTab'
import InsightProvider from '@/pages/workbench/InsightProvider'

/**
 * 工作台标签「建议」(工作台 v2 三合一, Task 12)。
 *
 * 落位(spec §4.3 / 计划 Task 12): **AI 建议列表**(后端建议池下发, 源含盘中监测
 * `intraday_monitor` —— 每条建议的「来源」由 `SuggestionBadge` 按后端 `agent_label` 原文渲染)
 * · **技术指标基础建议**(无 AI 建议时的回退) · **建议列表含「包含过期」开关**
 * · 顶部「触发盘中监测」按钮(真实提交后端 AI 作业)。
 *
 * 复用 vs 自建(brief 硬要求「不要再实现一遍」):
 *  - 列表正文**全部复用**恢复组件 `SuggestionsTab`(biz-ui) —— 它自带「显示过期建议」开关
 *    (`includeExpiredSuggestions` 走 `useLocalStorage`)、建议卡片列表、以及空态回退链;
 *    本文件**不重写**其中任何渲染逻辑, 只包一层 Provider + 一行头部(说明 + 触发按钮);
 *  - `SuggestionBadge` 的内部实现一字未动(徽章/来源/详情弹窗由它持有);
 *  - 技术指标基础建议**不自行计算**: 由 `useInsightDerived` 的 `technicalFallbackSuggestion`
 *    (内部即 `buildKlineSuggestion(klineSummary, hasHolding)`)产出, 经 `SuggestionsTab`
 *    在 AI 建议为空时回退渲染。
 *
 * 取数键(本标签 `keys`, 说明「为什么是这两个」):
 *  - `'suggestions'` —— `/suggestions/{s}`(AI 建议列表 + `include_expired`, 必取);
 *  - `'core'` —— **必须**带上: 技术指标基础建议的入参 `klineSummary` 来自 `/klines/{s}/summary`,
 *    而它只在 `core` 键内取数(`useInsightData` 的挂载总取数 effect); 不带 `core` 时
 *    `technicalFallbackSuggestion` 恒为 `null`, 空态就只剩一句「暂无建议」—— 与 brief
 *    「空态回退技术指标基础建议」不符。`SuggestionBadge` 的 `stockName`(报价名)与
 *    `kline` 对照同样取自 core。
 *  - 带 `core` 的代价(已知, 见 task-12-report concern): 会连带取 quote/moreInfo/darkFlowTq/
 *    klines(36d)/portfolioSummary 五个本标签不渲染的端点, 且与带1 `HeaderBand` 自取的
 *    `/klines/{s}/summary` 重复一次。若要收敛, 需给 `InsightProvider` 增加更细的键(如
 *    `klineSummary` 单键), 属跨任务改动, 本任务不擅自扩 API 面。
 *
 * 「触发盘中监测」的真实链路(经 Provider 的 actions, 非本文件自建):
 *  按钮 → `useInsight().handleSetAlert`(`insight/useInsightActions.ts`)→
 *  `stocksApi.list()` 找到/`create()` 建关注 → `updateAgents` 确保绑定 `intraday_monitor` →
 *  `stocksApi.triggerAgent(stock.id, 'intraday_monitor', { bypass_throttle: true, bypass_market_hours: true })`
 *  → 提交成功后 5s 轮询 `loadSuggestions()` 最长 120s(新建议带「来源: 盘中监测」)。
 *  即与恢复组件的「一键设提醒」**同一个动作**, 故有**同形副作用**: 标的未关注时会先加入自选
 *  并绑定该 Agent —— 复审 Finding 1: 该副作用**在可见 UI 里明示**(按钮左侧一行 note
 *  「未关注时会先加入自选并绑定盘中监测」, 不只藏在 `title` tooltip 里, 否则点击/触摸用户看不到);
 *  本标签 `keys` 未启用 `watchlist`(不为此额外取数), 无法判定"当前是否已关注", 故 note 用
 *  **条件句**陈述, 不假装知道当前绑定状态。
 *  失败态: 该 action 内部 `toast` 原始错误(不吞错、不伪装), 并回传
 *  `SetAlertOutcome{ ok, watchlistEnsured, agentBound }`(Task 12 复审增量; 既有调用方均忽略
 *  返回值 ⇒ 行为不变)。本标签据此渲染一行**事实性**失败提示, 并**分别**陈述触发前的两步写入
 *  是否已落库(部分成功不得被隐去, 未落库的也不许声称已写入) —— **不猜**失败原因, 也**不假定**
 *  存在错误提示(`symbol` 缺失的早退是静默的, 复审 Minor)。
 *  busy 态: Provider 的 `alerting`(禁用按钮 + 「提交中…」)。
 *  挂载时**不额外触发**(除 Provider 自身 `suggestions` 键的自动建议逻辑外); 本文件无
 *  mount 副作用(无 useEffect), 也**不动** `triggerAutoAiSuggestion` 的自动路径。
 *  UI 文案**不出现**内部 agent 名 `intraday_monitor`(一律用用户可见名「盘中监测」, 复审 Minor)。
 *
 * 降级(never fabricate): 无 AI 建议且技术指标可得 → 复用组件回退「技术指标基础建议」;
 *  两者都没有(如 klineSummary 不可用)→ 「暂无建议」; 自动建议在途 → 「正在自动生成 AI 建议
 *  (通常 5-15 秒)...」。均无 `--`/编造数据 —— 本文件**不渲染任何**数值或建议文案。
 *
 * 归属 `src/pages/workbench/tabs/`: 与 Task 11 同层(标签自带 Provider, 由 Task 17 的
 * `TabPanel` 按 `?tab=` 挂载); 本文件不新增 biz-ui 依赖方向, 只消费已有导出。
 */
/** 门控键: 模块级常量 —— 每帧新建数组会换引用, 若被 Provider 按引用消费会让取数 effect 重跑。 */
const SUGGEST_TAB_KEYS = ['suggestions', 'core'] as const

/** 「盘中监测」的用户可见名(内部 agent 名 `intraday_monitor` 只出现在代码/契约里, 不进 UI 文案)。 */
const INTRADAY_LABEL = '盘中监测'

function SuggestTabBody() {
  const { suggestions, alerting, handleSetAlert } = useInsight()
  // 失败态(Finding 1/复审): 除"未提交"外, 还要如实陈述触发**之前**的持久化写入是否已落库
  // (`watchlistEnsured`/`agentBound` 由 `handleSetAlert` 回传) —— 部分成功不得被隐去, 也不得夸大。
  const [triggerFailure, setTriggerFailure] = useState<null | { watchlistEnsured: boolean; agentBound: boolean }>(null)

  // 列表里**真实出现**的来源标签(后端 `agent_label` 原文, 如「盘中监测」); 空列表不渲染该行。
  const sources = useMemo(
    () =>
      Array.from(
        new Set(suggestions.map((s) => String(s.agent_label || s.agent_name || '').trim()).filter(Boolean)),
      ),
    [suggestions],
  )

  const onTrigger = () => {
    setTriggerFailure(null)
    void handleSetAlert().then((outcome) => {
      if (!outcome.ok) {
        setTriggerFailure({ watchlistEnsured: outcome.watchlistEnsured, agentBound: outcome.agentBound })
      }
    })
  }

  return (
    <div className="mt-1 space-y-3 text-[12px]" data-testid="suggest-tab">
      {/* 条: 口径说明 + 来源/条数 + 「触发盘中监测」(现价/名称/评分归带1, 本标签不渲染) */}
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-border/40 pb-2 text-[11px] text-muted-foreground">
        <span className="text-foreground">建议</span>
        <span className="text-border/60">|</span>
        <span>
          AI 建议(后端建议池, 源含「{INTRADAY_LABEL}」等 Agent) · 无 AI 建议时回退技术指标基础建议
        </span>
        {suggestions.length > 0 ? (
          <span className="font-mono text-[10px]">
            共 {suggestions.length} 条
            {sources.length > 0 ? <span className="font-sans"> · 来源: {sources.join(' · ')}</span> : null}
          </span>
        ) : null}
        {/* Finding 1: 该按钮的**持久化副作用**必须在可见 UI 里写明(点击/触摸用户看不到 title tooltip)。
            本标签 keys 未启用 `watchlist`(不额外取数), 故无法判定当前是否已关注 —— 用**条件句**如实陈述。 */}
        <span className="ml-auto text-[10px]" data-testid="suggest-trigger-note">
          未关注时会先加入自选并绑定{INTRADAY_LABEL}
        </span>
        <button
          type="button"
          onClick={onTrigger}
          disabled={alerting}
          title={`立即向后端提交一轮「${INTRADAY_LABEL}」AI 作业; 与「一键设提醒」同一个动作: 标的未关注时会先加入自选并绑定该 Agent(持久写入, 不会自动回滚)。提交成功后新建议通常 5-15 秒出现(来源标「${INTRADAY_LABEL}」)。`}
          className="inline-flex h-6 items-center gap-1 rounded border border-border/50 px-2 text-[11px] text-muted-foreground hover:text-foreground disabled:opacity-60"
        >
          {alerting ? <Loader2 className="h-3 w-3 animate-spin" /> : <Radar className="h-3 w-3" />}
          {alerting ? '提交中…' : `触发${INTRADAY_LABEL}`}
        </button>
      </div>

      {triggerFailure ? (
        <div className="rounded border border-border/50 px-3 py-2 text-[11px] text-muted-foreground" data-testid="suggest-trigger-failed">
          触发失败: {INTRADAY_LABEL}作业未提交成功 —— 可重试; 失败原因不在此处推断(若出现错误提示, 以其为准)。
          {/* 部分成功必须如实陈述: 触发前的写入已落库且不回滚, 不因后续失败而消失 */}
          {triggerFailure.agentBound
            ? ` 注意: 触发前的「加入自选 + 绑定${INTRADAY_LABEL}」已写入(不会自动回滚), 可在右栏「关注」核对。`
            : triggerFailure.watchlistEnsured
              ? ` 注意: 该标的已加入自选(不会自动回滚), 但${INTRADAY_LABEL}绑定未完成。`
              : ' 本次未发生自选 / 绑定写入。'}
        </div>
      ) : null}

      {/* 建议列表(含「包含过期」开关 + 空态回退技术指标基础建议) —— 全部复用恢复组件 */}
      <SuggestionsTab />
    </div>
  )
}

/**
 * 标签入口。`keys={SUGGEST_TAB_KEYS}`(模块级常量, 见上): 只启用建议端点 + 技术指标基础建议
 * 所需的 core(watchlist/news/announcements/reports/deep/fundamentals 一个都不发);
 * 惰性由 `TabPanel` 只渲染激活标签保证。两个键的理由见文件头注。
 */
export default function SuggestTab({
  symbol,
  market,
  hasPosition,
}: {
  symbol: string
  market: string
  hasPosition?: boolean
}) {
  return (
    <InsightProvider symbol={symbol} market={market} hasPosition={hasPosition} keys={SUGGEST_TAB_KEYS}>
      <SuggestTabBody />
    </InsightProvider>
  )
}
