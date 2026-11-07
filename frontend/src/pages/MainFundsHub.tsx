/**
 * 主力资金工作区(MainFundsHub): 暗盘资金 TOP + 主力资金战报 并排两 Tab。
 *
 * 为什么合并: 两页都在讲「当日主力资金」, 入口却在侧栏各占一行(机会组), 用户要在两个
 * 一级入口之间来回跳才能拼出一张资金面图。合并只做**入口与视图容器**层面的并排,
 * 两个页面组件一行不改(原样懒加载), 因此各自的取数、刷新按钮、空态、口径徽章、
 * 列排序、行点击下钻等既有功能全部原样保留。
 *
 * ⚠️ 无损前提(接线时必须遵守, 由 App.tsx 侧执行):
 *   1. 路由守卫用 `canEnterMainFundsHub(myPerms)`/`MAIN_FUNDS_HUB_PERMS`: 两页的权限点
 *      **不同**(暗盘 = `view_dark`, 战报 = `view_forecast`)。用单点守卫会直接丢掉只有另一个
 *      权限的用户 —— 这是并排入口唯一的真实回归风险, 故守卫取「任一」, 再按 Tab 各自 perm 过滤。
 *   2. 旧路径 `/dark-fund-top` `/war-report` 必须重定向到 `?tab=`, 见
 *      `LEGACY_MAIN_FUNDS_ROUTES` / `legacyMainFundsRedirect()`。两页内部的行点击下钻用
 *      `navBackState('/war-report', …)` 这类旧路径做返回落点 —— 重定向**不带 tab** 时,
 *      用户从个股返回会落回默认 Tab, 上下文就丢了。
 *   3. 侧栏/移动端导航项改指本页时, 一并带上 `?tab=`; 标签文案沿用两页原名, 不改名的话
 *      老用户按名字找得到。
 *
 * 数据源与时间口径差异: 两页都是同花顺 DDE 大单口径(ths), 但**快照各自独立生成**(各自有
 * snapshot_date / updated_at, 各自的手动刷新按钮), 行业维度只在战报里且单位是亿元, 个股是
 * 万元 —— 页首常驻「口径差异」条把这点写明, 避免用户把两页数字当成同一时点、直接相加。
 *
 * Tab 状态写 URL `?tab=`, 并兼容旧写法别名(?tab=war-report 等), 别名先归一再交给 TabbedPage。
 */
import { Suspense } from 'react'
import { Navigate, useSearchParams } from 'react-router-dom'
import { ListOrdered, Loader2, Swords } from 'lucide-react'

import TabbedPage, { type TabDef } from '@/components/TabbedPage'
import { lazyWithRetry } from '@/lib/lazy-with-retry'

const DarkFundTopPage = lazyWithRetry(() => import('@/pages/DarkFundTop'), 'dark-fund-top-page')
const WarReportPage = lazyWithRetry(() => import('@/pages/WarReport'), 'war-report-page')

/** 本工作区的路由(主模型接线用; 组件自身不读它) */
export const MAIN_FUNDS_HUB_PATH = '/funds'

/** canonical tab key —— URL 一律写这两个值 */
export const MAIN_FUNDS_HUB_TABS = { dark: 'dark', war: 'war' } as const
export type MainFundsTabKey = (typeof MAIN_FUNDS_HUB_TABS)[keyof typeof MAIN_FUNDS_HUB_TABS]

/** 两页各自的权限点, 路由守卫取「任一」放行(见文件头 1) */
export const MAIN_FUNDS_HUB_PERMS = ['view_dark', 'view_forecast'] as const

/**
 * 路由级放行判定: 任一权限点即可进工作区; 具体 Tab 由 TabbedPage 按各自 perm 过滤。
 * myPerms 为 null(权限未加载/拉取失败)时返回 true —— 与 TabbedPage 的 null 语义一致,
 * 交给页面内过滤, 不在这里预判拒绝。
 */
export function canEnterMainFundsHub(myPerms?: Set<string> | null): boolean {
  if (!myPerms) return true
  return MAIN_FUNDS_HUB_PERMS.some((p) => myPerms.has(p))
}

/** 旧路径 → canonical tab; App.tsx 接线时用它做重定向(必须带 ?tab=, 见文件头 2) */
export const LEGACY_MAIN_FUNDS_ROUTES: Record<string, MainFundsTabKey> = {
  '/dark-fund-top': 'dark',
  '/war-report': 'war',
}

/** 旧路径 → `?tab=` 重定向目标; 非旧路径返回 null(调用方据此决定是否重定向) */
export function legacyMainFundsRedirect(pathname: string): string | null {
  const tab = LEGACY_MAIN_FUNDS_ROUTES[pathname]
  return tab ? `${MAIN_FUNDS_HUB_PATH}?tab=${tab}` : null
}

/**
 * 旧 ?tab= 别名 → canonical。分享链接/推送里出现过这些写法, 换 key 不能让链接失锚。
 * 只放稳定别名, 不做模糊匹配(避免 /reports 之类的语义串台)。
 */
const TAB_ALIASES: Record<string, MainFundsTabKey> = {
  dark: 'dark',
  'dark-fund': 'dark',
  darkfund: 'dark',
  'dark-fund-top': 'dark',
  darkfundtop: 'dark',
  war: 'war',
  'war-report': 'war',
  warreport: 'war',
  battle: 'war',
}

const TABS: TabDef[] = [
  {
    key: 'dark',
    label: '暗盘资金 TOP',
    icon: ListOrdered,
    perm: 'view_dark',
    hint: '全市场主力净流入榜单(同花顺 DDE)',
    render: () => (
      <Suspense fallback={<TabLoading />}>
        <DarkFundTopPage />
      </Suspense>
    ),
  },
  {
    key: 'war',
    label: '主力战报',
    icon: Swords,
    perm: 'view_forecast',
    hint: '当日主力动向叙事 + 行业分布',
    render: () => (
      <Suspense fallback={<TabLoading />}>
        <WarReportPage />
      </Suspense>
    ),
  },
]

function TabLoading() {
  return (
    <div className="flex h-[40vh] items-center justify-center text-[12px] text-muted-foreground">
      <Loader2 className="mr-2 h-4 w-4 animate-spin" /> 加载中…
    </div>
  )
}

/**
 * 页首常驻口径差异条(两个 Tab 都可见)。
 * 只陈述两页**已经写死**的口径事实(数据源 / 时点 / 更新方式 / 覆盖范围), 不推算、不合并数字。
 */
function CaliberDiff() {
  const rows = [
    {
      key: 'dark',
      name: '暗盘资金 TOP',
      source: 'thsdk_dde(同花顺 DDE 大单, 全市场批量约 5200 只)',
      timing: '盘后快照日(snapshot_date); 时点由各自快照决定',
      update: '页内「重新扫描」独立触发, 全市场约 16s',
      scope: '个股榜单; .tck 暗盘对照仅持仓股有值',
    },
    {
      key: 'war',
      name: '主力战报',
      source: 'thsdk_dde + TQ SUPAMO 行业子块',
      timing: '当日快照(独立 snapshot_date, 与暗盘榜不同步)',
      update: '页内「重新生成」独立触发, 全市场约 16s',
      scope: '吸筹/派发 TOP-BOTTOM + 行业分布(亿元) + 拆单计数(对倒无源)',
    },
  ]
  return (
    <section
      data-testid="main-funds-caliber"
      className="rounded-lg border border-border bg-s2/60 px-3 py-2"
      aria-label="两页数据源与时间口径差异"
    >
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
        <h2 className="text-[12px] font-medium text-foreground">两页口径不完全一致</h2>
        <p className="text-[12px] text-muted-foreground">
          同为同花顺 DDE 大单口径, 但快照各自生成, 不保证同日同刻; 个股是万元、行业是亿元, 不可跨页相加。
        </p>
      </div>
      <div className="mt-2 overflow-x-auto">
        <table className="w-full min-w-[640px] text-left text-[12px]">
          <thead>
            <tr className="text-muted-foreground">
              <th className="w-[110px] py-1 pr-2 font-medium">页面</th>
              <th className="py-1 font-medium">数据源</th>
              <th className="py-1 font-medium">时间口径</th>
              <th className="py-1 font-medium">更新方式</th>
              <th className="py-1 font-medium">覆盖范围</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border/40">
            {rows.map((r) => (
              <tr key={r.key} data-fund-row={r.key} className="align-top">
                <th scope="row" className="py-1 pr-2 text-left font-medium text-foreground">
                  {r.name}
                </th>
                <td className="py-1 pr-2 text-muted-foreground">{r.source}</td>
                <td className="py-1 pr-2 text-muted-foreground">{r.timing}</td>
                <td className="py-1 pr-2 text-muted-foreground">{r.update}</td>
                <td className="py-1 text-muted-foreground">{r.scope}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  )
}

export default function MainFundsHubPage({
  myPerms,
  isOwner,
}: {
  myPerms?: Set<string> | null
  isOwner?: () => boolean
}) {
  const [params] = useSearchParams()
  const requested = params.get('tab')
  const canonical = requested ? TAB_ALIASES[requested] : undefined

  // 别名归一: 一次性 replace 成 canonical key, 之后不会再命中别名(无循环)。
  // 未知 tab 值不在这里处理, 交给 TabbedPage 统一归一(它按可见 Tab 回退)。
  if (requested && canonical && canonical !== requested) {
    const next = new URLSearchParams(params)
    next.set('tab', canonical)
    return <Navigate to={{ search: next.toString() }} replace />
  }

  return (
    <div className="space-y-3">
      <CaliberDiff />
      <TabbedPage
        tabs={TABS}
        defaultTab={MAIN_FUNDS_HUB_TABS.dark}
        myPerms={myPerms}
        isOwner={isOwner}
      />
    </div>
  )
}
