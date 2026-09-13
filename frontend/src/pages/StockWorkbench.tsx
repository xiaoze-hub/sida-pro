import { useState } from 'react'
import { useParams, useSearchParams } from 'react-router-dom'
import KlineChart from '@panwatch/biz-ui/components/KlineChart'
import HeaderBand from '@panwatch/biz-ui/components/workbench/HeaderBand'
import QuickRail from '@panwatch/biz-ui/components/workbench/QuickRail'
import BoardBody from '@panwatch/biz-ui/components/workbench/BoardBody'
import IndexBody from '@/pages/workbench/IndexBody'
import PageTabs from '@/components/PageTabs'
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
 *  - `TabPanel`       —— 现渲染「{label} 建设中」; Task 17 换成 6 个真实标签组件。
 *  `TabBar` 是本任务的**正式**产物(6 键 + `?tab=` 深链), 非占位。
 *
 * 真数据: 本页不取数(mock 零容忍) —— 取数全在 `HeaderBand`/`QuickRail`/`KlineChart`/`IndexBody`/`BoardBody` 内。
 *
 * **页面级刷新**(Task 7 复审 Finding 1, 控制器裁定): 带1 `HeaderBand` 的刷新按钮除刷自身行情外,
 * 还回调 `onRefresh` → 本页 `refreshKey + 1`。`refreshKey` 只作**正文子树**的 `key`(两个分支
 * 各一个), 于是指数/板块正文与个股带2(`KlineChart`/`QuickRail`)整棵重挂载 → **各自重新取数**;
 * 页面外壳与带1 **不挂 key**(吸顶带不因刷新丢焦点/滚动位置, 也不重发它自己的请求)。
 */

/** 工作台当前只服务 A 股口径(CN); 非 CN 标的的 market 由后续路由/参数再议。 */
const MARKET = 'CN'

/**
 * 指数/板块正文宿主(Task 7 换成真实正文)。
 * `type === 'index'` 走指数正文(`IndexBody`, 取数/渲染来自 `IndexDetailPage`);
 * `type === 'board'` 走板块正文(`BoardBody`, 来自 `BoardDetailPage`; 同路由内切, spec §1.3)。
 * 两组件均按 spec §1.3 去掉了旧页骨架(返回/标题/刷新 —— 头部并入带1 `HeaderBand`; 旧页各自的
 * 「刷新」按钮改由带1 的 `onRefresh` 统一广播, 见头注「页面级刷新」, 正文仍能手动更新)。
 */
function IndexBoardHost({ type, symbol }: { type: WorkbenchType; symbol: string }) {
  return type === 'index' ? <IndexBody symbol={symbol} /> : <BoardBody code={symbol} />
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

/** 选中标签的正文(**临时占位**, Task 17 替换为 6 个真实标签组件)。 */
function TabPanel({ tab, symbol }: { tab: WorkbenchTab; symbol: string }) {
  const label = WORKBENCH_TABS.find((t) => t.id === tab)?.label ?? tab
  return (
    <div className="mt-3 rounded border border-border/60 p-4 text-[12px] text-muted-foreground">
      「{label}」建设中
      <span className="ml-2 font-mono text-[11px]">{symbol}</span>
    </div>
  )
}

export default function StockWorkbench() {
  const { symbol = '' } = useParams()
  const [sp, setSp] = useSearchParams()
  const type = normalizeType(sp.get('type'))
  const tab = parseTab(sp.get('tab'))
  /**
   * 页面级刷新计数器(Finding 1): 只作**正文子树**的 `key`。带1 刷新 → 自增 → 两个分支的
   * 内容块各自重挂载一次 → 正文/主图/右栏在挂载副作用里重新取数(各自组件本就"挂载即取数")。
   */
  const [refreshKey, setRefreshKey] = useState(0)

  /** 写单个 query(保留其它键, 如 ?type / ?tab 并存), 不跳页。 */
  const setQuery = (key: 'type' | 'tab', value: string) =>
    setSp((prev) => ({ ...Object.fromEntries(prev), [key]: value }))

  if (!symbol) return <div className="p-4 text-[12px] text-muted-foreground">缺少代码</div>

  return (
    <div className="mx-auto max-w-[1500px] p-3">
      {/* 带1: 顶部信息带(吸顶, 三类型共享)。**不挂 key** —— 刷新时它自己只重取自身行情(tick), 不重挂载 */}
      <HeaderBand
        symbol={symbol}
        market={MARKET}
        type={type}
        onTypeChange={(t) => setQuery('type', t)}
        onGotoTab={(t) => setQuery('tab', t)}
        onRefresh={() => setRefreshKey((k) => k + 1)}
      />

      {type !== 'stock' ? (
        /* 指数/板块: 只留带1 + 正文(spec §1.3 —— 无右栏/无 6 标签/无建议条)。
           `key={refreshKey}`: 刷新时正文重挂载重取数; `mt-3`: 与个股分支的带1↔带2 间距对齐(Finding 2)。 */
        <div key={refreshKey} className="mt-3">
          <IndexBoardHost type={type} symbol={symbol} />
        </div>
      ) : (
        <div key={refreshKey}>
          {/* 带2: 首屏主体 —— 大 K 线(4 图层 + 副图) + 右栏 320px 速览卡 */}
          <div className="mt-3 flex gap-3">
            <div className="min-w-0 flex-1 rounded border border-border/60 p-2">
              <KlineChart
                symbol={symbol}
                market={MARKET}
                initialInterval="1d"
                initialDays={120}
                height={420}
              />
            </div>
            <div className="w-[320px] shrink-0">
              <QuickRail symbol={symbol} market={MARKET} />
            </div>
          </div>
          {/* 带3: 下部单层标签(整宽, ?tab= 深链) */}
          <TabBar value={tab} onChange={(t) => setQuery('tab', t)} />
          <TabPanel tab={tab} symbol={symbol} />
        </div>
      )}
    </div>
  )
}
