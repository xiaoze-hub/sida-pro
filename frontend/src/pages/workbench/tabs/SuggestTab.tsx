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
 * 「触发盘中监测」的真实链路(v0.6.0 遗留③ 改为**无自选/绑定副作用**路径, 经 Provider 的 actions):
 *  按钮 → `useInsight().triggerIntradayOnce`(`insight/useInsightActions.ts`)→
 *  `stocksApi.triggerAgent(0, 'intraday_monitor', { allow_unbound: true, symbol, market, name,
 *  bypass_throttle: true, bypass_market_hours: true })` → 提交成功后 5s 轮询 `loadSuggestions()`
 *  最长 ~120s(新建议带「来源: 盘中监测」)。
 *  **不改用户的自选/绑定状态**: 后端 `src/web/api/stocks.py:461-533`(`trigger_stock_agent`)对
 *  `stock_id<=0 + allow_unbound=true` 有专门的"不落库"分支 —— 标的不在当前用户自选时用
 *  `SimpleNamespace(id=0, …)` 顶替 Stock 行(:523-533 注释原文「不落库：…一次性分析」), 已在自选时
 *  也只**读**既有行(:515-522), 两条分支都不 `create`/不写 `StockAgent`。
 *  故按钮左侧的 note 改为如实陈述"一次性触发, 不加入自选、不绑定盘中监测"
 *  —— 原先那句「未关注时会先加入自选并绑定盘中监测」随持久化路径一起去掉(不再发生的事不许留在 UI 上)。
 *  ⚠️ **但这不等于"零写入"**(2026-09-14 复审 Finding 1 证伪了本文件原先的"不发站内通知 —— 无任何
 *  持久化写入"措辞): 一轮真实 Agent 运行本身就会落库, 本路径与 `handleSetAlert` 在这点上**没有区别** ——
 *  `record_agent_run`(`src/core/agent_runs.py:40`)写一条**运行记录**; API 层收尾 `_notify`
 *  (`stocks.py:589-630`, **无 `suppress_notify` 判断**)→ `notify_task_done`(`notify_center.py:97-111`
 *  恒 `db.add(Notification)`+`commit()`)写一条**站内「任务完成」通知**, 且不传 `user_id` ⇒ 按
 *  `notify_center.py:338-341` **兜底推给 owner 账号**。`suppress_notify=stock_id<=0`(:485)只让
 *  `trigger_agent_for_stock` 内部 `channels=[]`(`runtime.py:790`), 即"**不外发 Agent 自己的渠道**"。
 *  ⇒ 文案只许声称"不加自选/不绑 Agent/不外发渠道", **不得**声称"无任何持久化写入"或"不发通知"。
 *  `handleSetAlert`(「一键设提醒」: list→create→updateAgents→trigger, 回传 `SetAlertOutcome` 陈述
 *  部分成功)**原样保留但当前零生产调用方** —— 它唯一的入口(旧详情模态壳里的按钮)已随 v0.6.0 退役,
 *  本标签也不调它 ⇒ 全站现在**没有**任何"持久化设提醒/绑定 Agent"的 UI 入口(登记为 **KI-058**,
 *  补显式按钮还是删死代码待老板拍板)。保留而不接线的理由: 遗留③ 的既定范围明确写了"保留它"。
 *  失败态: 该 action 内部 `toast` 原始错误(不吞错、不伪装), 回传 `{ ok: false }`; 本标签据此渲染
 *  一行**事实性**失败提示 —— **不猜**失败原因, 也**不假定**存在错误提示(`symbol` 缺失的早退是静默的)。
 *  本路径没有"部分成功"(触发前无任何写入), 故失败行固定陈述「本次未发生自选 / 绑定写入」。
 *  busy 态: Provider 的 `alerting`(禁用按钮 + 「提交中…」; 与「一键设提醒」共用同一 busy 位)。
 *  挂载时**不额外触发**(除 Provider 自身 `suggestions` 键的自动建议逻辑外); 本文件无
 *  mount 副作用(无 useEffect), 也**不动** `triggerAutoAiSuggestion` 的自动路径
 *  (它本来就走同一条 `allow_unbound` 无绑定链路, 只是多了持仓/5 分钟去重门控)。
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
  const { suggestions, alerting, triggerIntradayOnce } = useInsight()
  // 失败态: 本路径**无前置写入**(触发前不加自选、不绑 Agent)⇒ 不存在"部分成功", 只需如实说"未提交成功"
  // + "本次未发生自选 / 绑定写入"; 失败原因**不在此处推断**(错误原文由 action 内部 toast)。
  // 注: "无前置写入" ≠ "零写入" —— 提交成功后那轮运行仍会落运行记录与站内通知(见文件头注 ⚠️ 段)。
  const [triggerFailed, setTriggerFailed] = useState(false)

  // 列表里**真实出现**的来源标签(后端 `agent_label` 原文, 如「盘中监测」); 空列表不渲染该行。
  const sources = useMemo(
    () =>
      Array.from(
        new Set(suggestions.map((s) => String(s.agent_label || s.agent_name || '').trim()).filter(Boolean)),
      ),
    [suggestions],
  )

  const onTrigger = () => {
    setTriggerFailed(false)
    void triggerIntradayOnce().then((outcome) => {
      if (!outcome.ok) setTriggerFailed(true)
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
        {/* 可见 note(不只藏 title): 本按钮是**一次性**触发 —— 后端 stock_id<=0 + allow_unbound 走
            "不落库"分支(src/web/api/stocks.py:523-533), 既不加自选也不绑 Agent, 故如实这么写。
            需要持久化设提醒的语义仍由 Provider 保留的 `handleSetAlert`(「一键设提醒」)承担。 */}
        <span className="ml-auto text-[10px]" data-testid="suggest-trigger-note">
          一次性触发: 不加入自选、不绑定{INTRADAY_LABEL}
        </span>
        <button
          type="button"
          onClick={onTrigger}
          disabled={alerting}
          title={`立即向后端提交一轮「${INTRADAY_LABEL}」AI 作业(一次性触发): 不加入自选、不绑定该 Agent、不外发该 Agent 的通知渠道。注意: 运行本身仍会留一条运行记录与一条站内「任务完成」通知(通知中心可见)。提交成功后新建议通常 5-15 秒出现(来源标「${INTRADAY_LABEL}」)。`}
          className="inline-flex h-6 items-center gap-1 rounded border border-border/50 px-2 text-[11px] text-muted-foreground hover:text-foreground disabled:opacity-60"
        >
          {alerting ? <Loader2 className="h-3 w-3 animate-spin" /> : <Radar className="h-3 w-3" />}
          {alerting ? '提交中…' : `触发${INTRADAY_LABEL}`}
        </button>
      </div>

      {triggerFailed ? (
        <div className="rounded border border-border/50 px-3 py-2 text-[11px] text-muted-foreground" data-testid="suggest-trigger-failed">
          触发失败: {INTRADAY_LABEL}作业未提交成功 —— 可重试; 失败原因不在此处推断(若出现错误提示, 以其为准)。
          {/* 如实陈述写入状况: 本路径触发前后都没有任何持久化写入(不加自选/不绑 Agent), 故固定这一句。 */}
          {' 本次未发生自选 / 绑定写入。'}
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
