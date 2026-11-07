/**
 * 大盘观察工作区(MarketWatchHub, 2026-11-07)。
 *
 * 把既有两页收进一个分视图入口, **只做视图切换与边界标注**, 不改两页一行代码:
 *   · 指数全景 `/indexes`    → 八大宽基指数涨跌幅 + 四档资金结构 + 主力净额历史(TQ `get_exday_data`)
 *   · ETF 观察 `/etf-watch` → 20 只宽基 ETF 分钟成交额 vs 同期基线 + 市场级净申购 + 日度 12 类全景
 *
 * 默认视图 = **指数**, ETF 是**独立视图**(不是指数的附属面板、不是第三个数据源)。
 *
 * 四条硬约束(本文件即契约):
 *   ① **默认指数**: URL 无 `view` 时落在指数视图; 指数视图不可见(无 `view_quotes`)时才回退到
 *      第一个可用视图 —— 与现状等价(今天 /etf-watch 在 App.tsx 无 PermGuard, 只有指数页受守卫)。
 *   ② **不合并、不换算**: 两页各自请求各自的接口、各自渲染; 指数是「成分股按单笔金额分档聚合」,
 *      ETF 是「篮子成交额 + 市场级份额」, 两套口径既不同源也不可换算 —— 工作区任何位置都不做
 *      跨视图求和/占比/差值, 只在常驻闸门条里同屏标注来源与边界。
 *   ③ **不为 ETF 背书身份**: ETF 视图是市场级聚合(20 只篮子 + 全市场 SC8 份额), 页面本身不提供
 *      单只 ETF 净申购, 也不代表任何单只 ETF 的身份/持仓/可交易标的。本工作区不得出现
 *      「某只 ETF 净申购 X 亿」这类表述, 也不把篮子当指数成分。
 *   ④ **旧页面保留**: `/indexes`、`/etf-watch` 仍是真实路由, 老书签继续可用; 工作区提供
 *      「单独打开」指向当前视图的原路由。本组件**不做**旧路径重定向(重定向等于下线旧页)。
 *
 * 权限(2026-11-07 复审收口, fail closed): 视图级 `perm` 与 App.tsx 现状逐条对齐
 * (指数 = `view_quotes`, ETF = 无守卫, 与 /etf-watch 现状一致)。`myPerms` 为 null
 * (未加载 / 拉取失败)时**不渲染任何视图、不发任何取数**, 只显示"权限读取中" ——
 * 与 App.tsx `PermGuard` 2026-09-15 的结论一致(未加载 ≠ 放行); 旧实现在这里是
 * fail OPEN(权限没读到就把 view_quotes 视图照常渲染), 属越权可见。
 * **路由级守卫不要用单点 `view_quotes`** —— 那会把「只能看 ETF」的用户挡掉。
 *
 * 指数视图另用指数 K 线接口显示上证指数，ETF 视图保持原有独立成交额口径。
 * 两者不互相换算，不能把 ETF 篮子或市场级 SC8 称为单只 ETF 的净申购。
 */
import { useId, type ReactNode } from 'react'
import { Navigate, Link, useLocation, useNavigate, useSearchParams } from 'react-router-dom'
import { ArrowUpRight, BarChart3, Landmark, type LucideIcon } from 'lucide-react'

import PageTabs from '@/components/PageTabs'
import IndexesPage from '@/pages/Indexes'
import EtfWatchPage from '@/pages/EtfWatch'

/** 视图 key 即 URL `?view=` 的规范值 */
export type MarketWatchViewKey = 'index' | 'etf'

/** 默认视图 = 指数(产品口径: 大盘先看指数, ETF 是独立视图) */
export const DEFAULT_MARKET_WATCH_VIEW: MarketWatchViewKey = 'index'

/** 建议路由(主模型接线用; 组件自身不读它) */
export const MARKET_WATCH_HUB_PATH = '/market-watch'

/** 旧/别名写法 → 规范 key。`tab` 是既有 TabbedPage 约定的兼容入口。只放稳定别名, 不做模糊匹配。 */
const VIEW_ALIASES: Record<string, MarketWatchViewKey> = {
  index: 'index',
  indexes: 'index',
  indices: 'index',
  overview: 'index',
  etf: 'etf',
  etfs: 'etf',
  'etf-watch': 'etf',
  etfwatch: 'etf',
}

/** 纯函数: 非法/未知值返回 null(调用方据此回退默认视图并规范化 URL) */
export function normalizeMarketWatchView(value: string | null | undefined): MarketWatchViewKey | null {
  if (!value) return null
  return VIEW_ALIASES[value.trim().toLowerCase()] ?? null
}

export interface MarketWatchView {
  key: MarketWatchViewKey
  label: string
  hint: string
  icon: LucideIcon
  /** 数据来源(接口/上游), 与页面内实际取数一致, 不做推断 */
  source: string
  /** 口径边界; 两视图口径不同, 各自标注, 全页不合并 */
  caliber: string
  /** 原独立路由: 老书签继续可用, 「单独打开」指这里 */
  legacyPath: string
  /** 权限点, 与 App.tsx 现状对齐; 缺省 = 该页当前无守卫 */
  perm?: string
  render: () => ReactNode
}

export const MARKET_WATCH_VIEWS: readonly MarketWatchView[] = [
  {
    key: 'index',
    label: '指数全景',
    hint: '八大宽基指数涨跌幅 + 四档资金(成分股分档聚合)',
    icon: BarChart3,
    source: '/indexes/overview · /indexes/{symbol}/history · TQ get_exday_data(tdx_index_agg)',
    caliber:
      '指数口径: 成分股按单笔成交金额分档(特大/大/中/小)后求和, 主力 = 特大 + 大;\n与东财四档(eastmoney4)、腾讯逐笔(tick)不同源、不可换算, 只做同口径内横向比较与历史分位。',
    legacyPath: '/indexes',
    perm: 'view_quotes',
    render: () => <IndexesPage />,
  },
  {
    key: 'etf',
    label: 'ETF 观察',
    hint: '20 只宽基 ETF 分钟成交额 + 市场级净申购(独立视图)',
    icon: Landmark,
    source: '/etf-watch/intraday · /etf-watch/support · /etf-watch/daily · 通达信 TQ 网关 + 市场级 SC8',
    caliber:
      'ETF 口径: 20 只宽基 ETF 篮子分钟成交额与同期基线对比; 净申购只有市场级(全市场 ETF 份额变动 × 净值), 单只 ETF 份额在通达信是当前值回填, 故不提供单只净申购。\n本视图是市场级聚合, 不代表任何单只 ETF 的身份、持仓或可交易性; 与指数四档资金不同源, 不做合并或换算。',
    legacyPath: '/etf-watch',
    render: () => <EtfWatchPage />,
  },
]

export interface MarketWatchHubProps {
  /** 当前用户权限点集合; null/未传 = 未加载, 不过滤(避免闪隐) */
  myPerms?: Set<string> | null
  /** 仅供测试/嵌入: 初始视图(URL 无 view 时生效); URL 优先 */
  initialView?: MarketWatchViewKey
}

function viewAllowed(view: MarketWatchView, myPerms: Set<string> | null | undefined): boolean {
  if (!view.perm) return true
  // 未确权 = 不放行(fail closed); 组件在 myPerms==null 时更早就整页停在"读取中"。
  if (!myPerms) return false
  return myPerms.has(view.perm)
}

export default function MarketWatchHub({ myPerms, initialView }: MarketWatchHubProps) {
  const [params] = useSearchParams()
  const location = useLocation()
  const navigate = useNavigate()
  const idPrefix = useId()

  const visible = MARKET_WATCH_VIEWS.filter((v) => viewAllowed(v, myPerms))
  const rawView = params.get('view')
  const rawTab = params.get('tab')
  const requested = normalizeMarketWatchView(rawView ?? rawTab)

  // 权限未加载/拉取失败: 不猜、不放行、不改写 URL(等权限到位再规范化)。
  // 受守卫视图一秒钟都不许先渲染 —— 那正是"权限未就绪期间短暂可见未授权内容"。
  if (!myPerms) {
    return (
      <div
        className="py-10 text-center"
        data-testid="market-watch-hub-perm-pending"
        role="status"
        aria-live="polite"
      >
        <div className="text-[14px] font-medium text-foreground">权限读取中…</div>
        <p className="mt-1 text-[12px] text-muted-foreground">
          正在确认大盘观察视图的访问权限(view_quotes); 确认前不展示指数/ETF 视图。
        </p>
      </div>
    )
  }

  // 默认指数; 指数视图不可见时才回退到第一个可用视图(ETF), 与今天「ETF 页无守卫」一致。
  const preferred: MarketWatchViewKey = initialView ?? DEFAULT_MARKET_WATCH_VIEW
  const fallback: MarketWatchViewKey | null = visible.some((v) => v.key === preferred)
    ? preferred
    : (visible[0]?.key ?? null)
  const resolved: MarketWatchViewKey | null =
    requested && visible.some((v) => v.key === requested) ? requested : fallback

  // 带了非法/别名/旧 tab 参数 → 规范化成规范 view(replace, 不留历史垃圾)。
  if (resolved && (rawView != null || rawTab != null) && (rawView !== resolved || rawTab != null)) {
    const next = new URLSearchParams(params)
    next.delete('tab')
    next.set('view', resolved)
    return (
      <Navigate
        replace
        to={{ pathname: location.pathname, search: next.toString(), hash: location.hash }}
      />
    )
  }

  const onChange = (key: string) => {
    const next = new URLSearchParams(params)
    next.delete('tab')
    next.set('view', key)
    // push: 浏览器后退可回到上一个视图; 保留其余查询参数与 hash(深链不丢)
    navigate({ search: next.toString(), hash: location.hash })
  }

  if (!resolved) {
    return (
      <div className="py-10 text-center" data-testid="market-watch-hub-denied">
        <div className="text-[14px] font-medium text-foreground">无可用视图</div>
        <p className="mt-1 text-[12px] text-muted-foreground">
          大盘观察的指数视图需要 `view_quotes` 权限; 当前账号未开通。
        </p>
      </div>
    )
  }

  const active = visible.find((v) => v.key === resolved) as MarketWatchView
  const panelId = `${idPrefix}-panel`

  return (
    <div className="sida-page-enter" data-testid="market-watch-hub" data-view={active.key}>
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <PageTabs
          tabs={visible.map(({ key, label, icon, hint }) => ({ key, label, icon, hint }))}
          value={active.key}
          onChange={onChange}
          idPrefix={idPrefix}
          panelId={panelId}
        />
        <Link
          to={active.legacyPath}
          data-testid="market-watch-legacy-link"
          className="ml-auto inline-flex min-h-9 items-center gap-1 rounded-md px-2 text-[12px] text-muted-foreground hover:bg-s2 hover:text-foreground"
          title={`在独立页面打开「${active.label}」(原路由书签继续可用)`}
        >
          单独打开{active.label}
          <ArrowUpRight className="h-3.5 w-3.5" />
        </Link>
      </div>

      {/* 两视图来源/口径边界: 常驻同屏, 明确本工作区不合并、不换算、不为单只 ETF 背书 */}
      <div className="mb-3 rounded-lg bg-s2 px-3 py-2 text-[12px]" data-testid="market-watch-caliber-bar">
        <div className="text-muted-foreground">
          口径边界: 两个视图各自独立取数、各自口径; 本工作区不做合并、换算或互相补数。
        </div>
        <div className="mt-1 flex flex-col gap-1">
          {visible.map((v) => (
            <div
              key={v.key}
              data-testid={`market-watch-caliber-${v.key}`}
              className={`flex flex-wrap items-baseline gap-x-2 gap-y-0.5 ${
                v.key === active.key ? 'text-foreground' : 'text-muted-foreground'
              }`}
            >
              <span className="font-medium">{v.label}</span>
              <span className="font-mono text-[12px]">{v.source}</span>
              <span className="w-full whitespace-pre-line leading-relaxed">{v.caliber}</span>
            </div>
          ))}
        </div>
      </div>

      <div
        role="tabpanel"
        id={panelId}
        aria-labelledby={`${idPrefix}-${active.key}`}
        tabIndex={0}
        className="min-w-0"
        data-testid="market-watch-view-panel"
        data-view={active.key}
      >
        {active.render()}
      </div>
    </div>
  )
}

/**
 * 供主模型接路由/导航时复用的视图元数据(不含 React 渲染)。
 *
 * 接线要点(超出本文件职责, 由 App.tsx / navigation.ts 执行):
 *   · 路由 `<Route path={MARKET_WATCH_HUB_PATH} element={<MarketWatchHub myPerms={myPerms} />} />`
 *     **不要**包 `PermGuard perm="view_quotes"` —— ETF 视图当前无守卫, 单点守卫会挡掉只能看 ETF 的账号;
 *     视图级 perm 已在本组件内过滤。
 *   · 导航项 labelKey 需新增词条(建议 `nav.marketWatch`, 中文「大盘观察」);
 *     旧入口 `/indexes`、`/etf-watch` 不要从 NAV_ITEMS 删除 —— 旧页面保留, 是否收敛到一条
 *     入口由导航层决定, 本组件不假设。
 */
export const MARKET_WATCH_NAV_META: ReadonlyArray<{
  key: MarketWatchViewKey
  label: string
  legacyPath: string
  perm: string | null
}> = MARKET_WATCH_VIEWS.map(({ key, label, legacyPath, perm }) => ({
  key,
  label,
  legacyPath,
  perm: perm ?? null,
}))
