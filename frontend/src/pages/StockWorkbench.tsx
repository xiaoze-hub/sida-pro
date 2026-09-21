import { useEffect, useState } from 'react'
import { ChevronLeft, ChevronRight } from 'lucide-react'
import { useParams, useSearchParams } from 'react-router-dom'
import { dashboardApi } from '@panwatch/api'
import KlineChart, { type KlineRangeStats } from '@panwatch/biz-ui/components/KlineChart'
import HeaderBand from '@panwatch/biz-ui/components/workbench/HeaderBand'
import QuickRail from '@panwatch/biz-ui/components/workbench/QuickRail'
import BoardBody from '@panwatch/biz-ui/components/workbench/BoardBody'
import IndexBody from '@/pages/workbench/IndexBody'
import PageTabs from '@/components/PageTabs'
import RangeStatsCard from '@/components/RangeStatsCard'
import { intervalToPeriod, periodToInterval } from '@/lib/kline-period'
import { useBackTarget } from '@/lib/nav-back'
// v2.1 §12: 事件图标的数据源健康裁决(不可用 → K线上灰显, 悬停标"数据源不可用")
import { useSourceHealth } from '@/hooks/useSourceHealth'
import L2Tab from '@/pages/workbench/tabs/L2Tab'
import SuggestTab from '@/pages/workbench/tabs/SuggestTab'
import FundamentalTab from '@/pages/workbench/tabs/FundamentalTab'
import NewsTab from '@/pages/workbench/tabs/NewsTab'
import ResearchTab from '@/pages/workbench/tabs/ResearchTab'
import ForecastTab from '@/pages/workbench/tabs/ForecastTab'
import {
  normalizeType,
  parseTab,
  WORKBENCH_TABS,
  type WorkbenchTab,
  type WorkbenchType,
} from '@/lib/workbench-tabs'

/**
 * 个股/指数/板块 **三类型唯一详情页**(工作台 v2 三合一, spec §1.2 三带结构)。
 *
 * 三带(自上而下):
 *  带1 `HeaderBand`           —— 名称/现价/涨跌 + 类型三按钮 + 快照行 + 个股建议条(吸顶, 三类型共享);
 *  带2 主图 `KlineChart`(flex-1) + 右栏 `QuickRail`(320px) —— 个股专属(spec §1.3: 指数/板块无右栏);
 *  带3 `TabBar`(`WORKBENCH_TABS` 6 键, ?tab= 深链) + `TabPanel`(选中标签正文, 整宽单层)。
 *
 * 类型切换**不跳页**: `?type=` 只改本路由的 query(spec §1.3「工作台内切类型」); 标签同理走 `?tab=`。
 * 指数/板块在本路由内渲染 `IndexBoardHost`, **不渲染**右栏与 6 标签。
 *
 * **Ruling B**(保证本任务单独可编译/可走查, 后续任务逐一替换):
 *  - `IndexBoardHost` —— Task 7 已换成真实 `IndexBody`/`BoardBody`(正文从 `IndexDetail`/`BoardDetail` 原样搬移);
 *  - `TabPanel`       —— Task 17 已换成 6 个真实标签组件(见下「标签惰性」)。
 *  `TabBar` 是本任务的**正式**产物(6 键 + `?tab=` 深链), 非占位。
 *
 * **标签惰性(Task 17, spec §4.3)**: `TabPanel` 按 `?tab=` **只渲染当前激活的那一个**标签 ——
 * 每个标签内部自带 `InsightProvider`(各自的 `keys`), 挂载才实例化取数 hook ⇒ 切标签 = 惰性取数,
 * 进工作台**不**触发下部接口风暴。六个标签**不得**同时挂载(否则首屏打满全部标签的端点)。
 *
 * **持仓上下文 `hasPosition`(T19 接真源)**: 见 `useHasPosition` —— 页面挂载时取
 * `GET /portfolio/summary`, 按 `market:symbol` 判定当前标的是否在真实持仓里; 三态
 * (`undefined`=未知/在途/失败), 未知时**不猜** `false`:
 *  ① 带1 建议条评分按非持仓口径 + 显式「持仓态未知」小标注(`HeaderBand positionUnknown`);
 *  ② 基本面标签的**加仓计算器**: 未知时不渲染(与未持仓同处置), 但在标签口径行显式标注未知。
 * 未持仓/未知都只是**不渲染**该块, **不产生假数据**。
 *
 * 真数据: 本页只取一件自己消费的数据 —— 持仓汇总(`/portfolio/summary`, 上条); 其余取数全在
 * `HeaderBand`/`QuickRail`/`KlineChart`/`IndexBody`/`BoardBody`/六个标签组件内。
 *
 * **页面级刷新**(Task 7 复审 Finding 1, 控制器裁定): 带1 `HeaderBand` 的刷新按钮除刷自身行情外,
 * 还回调 `onRefresh` → 本页 `refreshKey + 1`。`refreshKey` 的两种用法(遗留⑦ 起分开):
 *  - **个股分支**: 仍作正文子树的 `key` —— `KlineChart`/`QuickRail`/激活标签都是"挂载即取数"且
 *    没有 token 入参, 重挂载是它们唯一的整棵重取数手段;
 *  - **指数/板块分支**: 作 `IndexBody`/`BoardBody` 的 **`refreshToken` prop**(不再是 `key`)——
 *    token 变化只让正文**重跑取数 effect**, 组件不卸载 ⇒ 不重放 `sida-page-enter` 入场动画,
 *    也不丢正文自己的内部 UI 状态。
 * 页面外壳与带1 **不挂 key**(吸顶带不因刷新丢焦点/滚动位置, 也不重发它自己的请求)。
 */

/** 工作台当前只服务 A 股口径(CN); 非 CN 标的的 market 由后续路由/参数再议。 */
const MARKET = 'CN'

/** 持仓态轮询间隔: 盘中买卖会变, 否则建议条评分/加仓计算器要等整页刷新才更新。 */
const POSITION_POLL_MS = 60000

/**
 * 持仓上下文 `hasPosition`(T19 接**真源**)。
 *
 * 三态:
 *  - `undefined` —— **未知**(取数在途 / 失败 / **未启用**)。调用方**不得**把它当 `false`
 *    (那等于断言"未持仓", 对持仓用户是假陈述);
 *  - `true`/`false` —— 已从真实持仓接口判定。
 *
 * 真源: `dashboardApi.portfolioSummary({ include_quotes: false })`(`GET /portfolio/summary`)——
 * 与 `DiscoveryPanel` 判定 `holdingSet` 用的是**同一个接口同一口径**(`accounts[].positions[]` 的
 * `market:symbol`)。不编造: 取数失败即保持 `undefined`, 由调用方展示「持仓态未知」而不是猜 `false`。
 *
 * **`enabled` 闸门(Finding 3)**: 只有**个股视图**(`type === 'stock'`)才需要持仓态 —— 指数/板块
 * 分支既不渲染右栏/标签, 也不消费 `hasPosition`(见 `StockWorkbench` 的 `type !== 'stock'` 早分支)。
 * 若不闸门, `?type=index`/`?type=board` 会白发一次 `GET /portfolio/summary` 且结果**永不被读**。
 * `enabled=false` 时本 hook **不发请求**且恒为 `undefined`(未知), 与"未启用"同态。个股视图的三态
 * 语义(在册 true / 不在册 false / 在途失败 undefined)**逐字节不变**。
 */
function useHasPosition(symbol: string, market: string, enabled: boolean): boolean | undefined {
  const [held, setHeld] = useState<boolean | undefined>(undefined)
  useEffect(() => {
    let alive = true
    // 换标的/关闸门先回到"未知", 避免把上一只票(或已离开的个股视图)的持仓态画到当前标的上。
    setHeld(undefined)
    // `enabled=false`(指数/板块)⇒ **不发** /portfolio/summary, 恒为未知(结果本就无人消费)。
    if (!enabled || !symbol) return () => { alive = false }
    const want = `${market}:${symbol}`
    const load = async () => {
      try {
        const r = await dashboardApi.portfolioSummary({ include_quotes: false })
        if (!alive) return
        const has = (r?.accounts || []).some((acc) =>
          (acc.positions || []).some((p) => `${p.market}:${p.symbol}` === want),
        )
        setHeld(has)
      } catch {
        // 失败**保留上次值**(stale-on-error); 首次即失败则仍为 `undefined`(未知) —— 不静默当未持仓。
      }
    }
    void load()
    // 盘中持仓会变(买入/卖出) ⇒ 轮询刷新, 否则评分/加仓计算器要等整页刷新才更新。
    const t = window.setInterval(() => void load(), POSITION_POLL_MS)
    return () => {
      alive = false
      window.clearInterval(t)
    }
  }, [symbol, market, enabled])
  return held
}

/**
 * 指数/板块正文宿主(Task 7 换成真实正文)。
 * `type === 'index'` 走指数正文(`IndexBody`, 取数/渲染来自 `IndexDetailPage`);
 * `type === 'board'` 走板块正文(`BoardBody`, 来自 `BoardDetailPage`; 同路由内切, spec §1.3)。
 * 两组件均按 spec §1.3 去掉了旧页骨架(返回/标题/刷新 —— 头部并入带1 `HeaderBand`; 旧页各自的
 * 「刷新」按钮改由带1 的 `onRefresh` 统一广播, 见头注「页面级刷新」, 正文仍能手动更新)。
 *
 * **`refreshToken`(v0.6.0 遗留⑦)**: 页面把 `refreshKey` 作为 **prop** 传进正文, 而不再挂
 * `key={refreshKey}` —— 换 key 会卸载并重建整棵子树, 于是每次点刷新都重放正文根节点的
 * `sida-page-enter` 入场动画(视觉"闪一下"), 还会丢掉正文自己的内部 UI 状态(如已展开的块)。
 * 正文把 token 放进取数 effect 的依赖 ⇒ token 变化**只重跑取数**, 组件实例与 DOM 节点都不动。
 */
function IndexBoardHost({
  type,
  symbol,
  refreshToken,
}: {
  type: WorkbenchType
  symbol: string
  refreshToken?: number
}) {
  return type === 'index' ? (
    <IndexBody symbol={symbol} refreshToken={refreshToken} />
  ) : (
    <BoardBody code={symbol} refreshToken={refreshToken} />
  )
}

/**
 * 带3 标签栏: `WORKBENCH_TABS` 六键(设计稿 §4.3), 复用通用件 `PageTabs`(既有视觉语言,
 * 不引新样式), 点击写 `?tab=` 深链。本组件是本任务的正式产物, Task 17 不动它。
 */
function TabBar({ value, onChange }: { value: WorkbenchTab; onChange: (t: WorkbenchTab) => void }) {
  return (
    <PageTabs
      tabs={WORKBENCH_TABS.map((t) => ({ key: t.id, label: t.label }))}
      value={value}
      // 点击回传的是普通 string → 用 Task 1 的 parseTab 收敛回联合类型(非法值落 'l2')
      onChange={(key) => onChange(parseTab(key))}
      className="mt-3"
    />
  )
}

/**
 * 带3 正文: 按 `?tab=` **只渲染当前激活的那一个**真实标签(Task 17)。
 *
 * 为什么是 `switch`(而不是六个都渲染 + CSS 隐藏): 每个标签自带 `InsightProvider`(各自 `keys`),
 * 取数 hook 在**挂载**时实例化 ⇒ 只渲染激活项才等价于"切标签才惰性取数"(spec §4.3)。
 * 六个同时挂载会在进工作台首屏把六组端点一次打满 —— 本任务的核心约束。
 * `switch` 覆盖联合类型全部六值后 TS 收敛为 `never`(下面 `neverTab` 的穷尽性守卫),
 * 将来 `WorkbenchTab` 加键时 `tsc -b` 会在此处报错, 逼人补分支(不静默漏渲染)。
 *
 * 传参: `symbol`/`market` 一律 `MARKET`('CN'); `hasPosition` 见头注「持仓上下文」。
 * `ResearchTab` 另收可选 `stockName`, 本页无名称来源(带1 自己取)故不传 —— 其兜底链会回退到
 * symbol 匹配, 不编造名称。`ForecastTab` 不消费 `symbol`/`market`(签名同形, 只为统一接线)。
 */
function TabPanel({
  tab,
  symbol,
  hasPosition,
}: {
  tab: WorkbenchTab
  symbol: string
  hasPosition: boolean | undefined
}) {
  switch (tab) {
    case 'l2':
      return <L2Tab symbol={symbol} market={MARKET} hasPosition={hasPosition} />
    case 'suggest':
      return <SuggestTab symbol={symbol} market={MARKET} hasPosition={hasPosition} />
    case 'fundamental':
      return <FundamentalTab symbol={symbol} market={MARKET} hasPosition={hasPosition} />
    case 'news':
      return <NewsTab symbol={symbol} market={MARKET} hasPosition={hasPosition} />
    case 'research':
      return <ResearchTab symbol={symbol} market={MARKET} hasPosition={hasPosition} />
    case 'forecast':
      return <ForecastTab symbol={symbol} market={MARKET} />
  }
  const neverTab: never = tab
  return <div className="mt-3 text-[12px] text-muted-foreground">未知标签: {neverTab}</div>
}

export default function StockWorkbench() {
  const { symbol = '' } = useParams()
  const [sp, setSp] = useSearchParams()
  const type = normalizeType(sp.get('type'))
  const tab = parseTab(sp.get('tab'))
  /**
   * 页面级刷新计数器(Finding 1; 用法自遗留⑦ 起**分成两种**, 详见文件头注「页面级刷新」):
   *  - **个股分支**: 仍作正文子树的 `key` → 自增即重挂载, 主图/右栏/激活标签在挂载副作用里重新取数;
   *  - **指数/板块分支**: 作 `IndexBody`/`BoardBody` 的 **`refreshToken` prop**(不再是 `key`)
   *    → 只重跑正文的取数 effect, 组件不卸载(不重放入场动画、不丢正文内部状态)。
   */
  const [refreshKey, setRefreshKey] = useState(0)
  /**
   * 持仓态(T19 真源): `undefined` = 未知(在途/失败), 见 `useHasPosition` 头注。
   * 未持仓只是**不渲染**持仓专属块, **不编造**数据; 未知时由带1/标签处显式标注(不猜 `false`)。
   * **闸门(Finding 3)**: 只有个股视图才取 —— 指数/板块(`type !== 'stock'`)不消费 `hasPosition`,
   * 不该为其白发一次 `GET /portfolio/summary`(见 `useHasPosition` 的 `enabled`)。
   */
  const hasPosition = useHasPosition(symbol, MARKET, type === 'stock')

  /** 返回入口(2026-09-20 用户报"从持仓进行情页没有返回按钮"): 必须在任何 early return **之前**
   *  调用(hooks 顺序固定) —— 所以放在这里, 不放渲染前。 */
  const back = useBackTarget()

  /**
   * 设计稿 v2.1 §10.2①: K 线周期落在 URL(`?period=d1`), 刷新/分享不丢。
   * 解析不出(缺失/非法/`intra` 分时) → `undefined` → 图表用自身默认周期(不假装支持)。
   */
  const periodFromUrl = periodToInterval(sp.get('period'))

  /**
   * 设计稿 v2.1 §10.2④: 区间统计(拖拽选段/缩放后, 资金面板顶部那一行)。
   * 统计由 `KlineChart` 算好回调上来(它同时持有 K线/资金柱/事件), 本页只决定渲染位置与收起。
   */
  const [rangeStats, setRangeStats] = useState<KlineRangeStats | null>(null)
  // §12: 数据源健康(60s 轮询; 失败/未知一律按不可用 → 灰显, 不假装有数据)
  const { isReady: sourceReady, reasonOf: sourceReason } = useSourceHealth()
  const [statsDismissed, setStatsDismissed] = useState(false)
  /** P0-1(2026-09-18): 右栏可折叠 —— 默认折叠, 把屏宽让给 K 线(铁律: K线是绝对主角, ≥80% 屏宽)。
   *  用户显式点过就记住他的选择(与侧边栏同一个套路), 不跟用户较劲。 */
  const [railOpen, setRailOpen] = useState(() => {
    try { return localStorage.getItem('sida_workbench_rail') === '1' } catch { return false }
  })
  const toggleRail = () =>
    setRailOpen((v) => {
      try { localStorage.setItem('sida_workbench_rail', v ? '0' : '1') } catch { /* 隐私模式忽略 */ }
      return !v
    })

  /** 写单个 query(保留其它键, 如 ?type / ?tab / ?period 并存), 不跳页。 */
  const setQuery = (key: 'type' | 'tab' | 'period', value: string) =>
    setSp((prev) => ({ ...Object.fromEntries(prev), [key]: value }))

  if (!symbol) return <div className="p-4 text-[12px] text-muted-foreground">缺少代码</div>

  return (
    <div className="mx-auto max-w-none p-3">
      {/* 带1: 顶部信息带(吸顶, 三类型共享)。**不挂 key** —— 刷新时它自己只重取自身行情(tick), 不重挂载 */}
      <HeaderBand
        symbol={symbol}
        market={MARKET}
        type={type}
        hasPosition={hasPosition === true}
        positionUnknown={hasPosition === undefined}
        onTypeChange={(t) => setQuery('type', t)}
        onGotoTab={(t) => setQuery('tab', t)}
        onRefresh={() => setRefreshKey((k) => k + 1)}
        back={back}
      />

      {type !== 'stock' ? (
        /* 指数/板块: 只留带1 + 正文(spec §1.3 —— 无右栏/无 6 标签/无建议条)。
           遗留⑦: **不再**挂 `key={refreshKey}`(那会重挂载 ⇒ 重放入场动画 + 丢正文内部状态),
           改为把 `refreshKey` 当 `refreshToken` prop 传下去 —— 正文据此重跑取数 effect。
           `mt-3`: 与个股分支的带1↔带2 间距对齐(Finding 2)。 */
        <div className="mt-3">
          <IndexBoardHost type={type} symbol={symbol} refreshToken={refreshKey} />
        </div>
      ) : (
        /* 个股分支**保留** `key={refreshKey}`: `KlineChart`/`QuickRail`/六个标签都没有
           `refreshToken` 这类入参(各自"挂载即取数"), 重挂载是它们唯一的整棵重取数手段;
           给它们逐个加 token 属跨组件改造, 不在本批范围(见 tranche-4 报告 concern)。 */
        <div key={refreshKey}>
          {/* 带2: 首屏主体 —— 大 K 线(4 图层 + 副图) + 右栏 320px 速览卡
              走查 2026-09-18: 右栏更高时左图下方会大片空白 —— 右栏限高与主图同高并可滚,
              两列底部对齐, 不再被最长列撑出死白。 */}
          <div className="mt-3 flex items-stretch gap-3">
            <div className="min-w-0 flex-1 rounded border border-border/60 p-2">
              <KlineChart
                symbol={symbol}
                market={MARKET}
                /* §10.2①: 周期以 URL 为准; URL 无/非法 → 图表默认 '1d' */
                initialInterval={periodFromUrl ?? '1d'}
                initialDays={120}
                height={420}
                /* §10.2①: 用户切周期 → 写 ?period=, 链接可分享/刷新不丢 */
                onIntervalChange={(i) => setQuery('period', intervalToPeriod(i))}
                /* §10.2④: 可视区间统计回调(月/周/日/分钟级都同一口径) */
                onRangeStats={(s) => {
                  setRangeStats(s)
                  // 新区间 = 新读数: 之前手动收起过的卡在区间变化后重新出现
                  if (s) setStatsDismissed(false)
                }}
                /* §12: 数据源不可用的事件图标灰显(+悬停说明原因), 不隐藏也不装作有数据 */
                sourceReady={sourceReady}
                sourceReason={sourceReason}
              />
            </div>
            {railOpen ? (
              <div className="flex w-[320px] shrink-0 flex-col gap-2">
                <div className="flex items-center justify-between">
                  <span className="text-[11px] font-medium text-muted-foreground">速览</span>
                  <button
                    type="button"
                    onClick={toggleRail}
                    title="收起速览栏(把宽度还给 K 线)"
                    aria-label="收起速览栏"
                    data-testid="rail-toggle"
                    className="flex h-5 w-5 items-center justify-center rounded border border-border/60 text-muted-foreground hover:text-foreground"
                  >
                    <ChevronRight className="h-3 w-3" />
                  </button>
                </div>
                {/* §10.2④: 区间统计(资金面板顶部那一行) */}
                {rangeStats && !statsDismissed && (
                  <RangeStatsCard stats={rangeStats} onClear={() => setStatsDismissed(true)} />
                )}
                <div className="scrollbar max-h-[436px] overflow-y-auto">
                  <QuickRail symbol={symbol} market={MARKET} />
                </div>
              </div>
            ) : (
              /* 折叠态: 56px 图标条。只留"展开"入口 —— 速览内容原样保留在展开态, 不删功能。 */
              <div className="flex w-14 shrink-0 flex-col items-center gap-2 rounded border border-border/60 py-2">
                <button
                  type="button"
                  onClick={toggleRail}
                  title="展开速览栏"
                  aria-label="展开速览栏"
                  data-testid="rail-toggle"
                  className="flex h-6 w-6 items-center justify-center rounded border border-border/60 text-muted-foreground hover:text-foreground"
                >
                  <ChevronLeft className="h-3.5 w-3.5" />
                </button>
                <span className="[writing-mode:vertical-rl] text-[10px] tracking-wide text-muted-foreground">
                  速览
                </span>
              </div>
            )}
          </div>
          {/* 带3: 下部单层标签(整宽, ?tab= 深链) */}
          <TabBar value={tab} onChange={(t) => setQuery('tab', t)} />
          <TabPanel tab={tab} symbol={symbol} hasPosition={hasPosition} />
        </div>
      )}
    </div>
  )
}
