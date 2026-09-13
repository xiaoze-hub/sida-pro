import { useParams, useSearchParams } from 'react-router-dom'
import KlineChart from '@panwatch/biz-ui/components/KlineChart'
import HeaderBand from '@panwatch/biz-ui/components/workbench/HeaderBand'
import QuickRail from '@panwatch/biz-ui/components/workbench/QuickRail'
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
 * **Ruling B 临时占位**(保证本任务单独可编译/可走查, 后续任务逐一替换):
 *  - `IndexBoardHost` —— 现渲染「指数/板块正文建设中」面板; Task 7 换成 `IndexBody`/`BoardBody`;
 *  - `TabPanel`       —— 现渲染「{label} 建设中」; Task 17 换成 6 个真实标签组件。
 *  `TabBar` 是本任务的**正式**产物(6 键 + `?tab=` 深链), 非占位。
 *
 * 真数据: 本页不取数(mock 零容忍) —— 取数全在 `HeaderBand`/`QuickRail`/`KlineChart` 内。
 */

/** 工作台当前只服务 A 股口径(CN); 非 CN 标的的 market 由后续路由/参数再议。 */
const MARKET = 'CN'

/**
 * 指数/板块正文宿主(**临时占位**, Task 7 替换为真实 `IndexBody`/`BoardBody`)。
 * `type === 'board'` 走板块正文(spec §1.3: 同一路由内切, 正文复用既有页).
 */
function IndexBoardHost({ type, symbol }: { type: WorkbenchType; symbol: string }) {
  return (
    <div className="mt-3 rounded border border-border/60 p-4 text-[12px] text-muted-foreground">
      {type === 'index' ? '指数' : '板块'}正文建设中
      <span className="ml-2 font-mono text-[11px]">{symbol}</span>
    </div>
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

  /** 写单个 query(保留其它键, 如 ?type / ?tab 并存), 不跳页。 */
  const setQuery = (key: 'type' | 'tab', value: string) =>
    setSp((prev) => ({ ...Object.fromEntries(prev), [key]: value }))

  if (!symbol) return <div className="p-4 text-[12px] text-muted-foreground">缺少代码</div>

  return (
    <div className="mx-auto max-w-[1500px] p-3">
      {/* 带1: 顶部信息带(吸顶, 三类型共享) */}
      <HeaderBand
        symbol={symbol}
        market={MARKET}
        type={type}
        onTypeChange={(t) => setQuery('type', t)}
        onGotoTab={(t) => setQuery('tab', t)}
      />

      {type !== 'stock' ? (
        /* 指数/板块: 只留带1 + 正文(spec §1.3 —— 无右栏/无 6 标签/无建议条) */
        <IndexBoardHost type={type} symbol={symbol} />
      ) : (
        <>
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
        </>
      )}
    </div>
  )
}
