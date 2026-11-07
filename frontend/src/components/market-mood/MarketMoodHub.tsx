/**
 * 市场情绪工作区(MarketMoodHub, 2026-11-07)。
 *
 * 把既有两页收进一个分视图工作区:
 *   · 题材情绪  `/theme-mood`   → 题材×日情绪矩阵 + 全市场情绪温度(收盘确认口径)
 *   · 连板晋级树 `/replay-tree` → market_replay_daily 三池标量 + 梯队晋级树
 *
 * 三条硬约束(本文件即契约):
 *   ① **不合并、不换算、不互相补数**: 两视图各自请求自己的接口、各自渲染,
 *      工作区只做视图切换; 页面上的数字永远来自单一口径, 不做跨口径拼装。
 *   ② **两种来源同屏标注**: 切换条下方常驻两视图的来源/口径边界说明
 *      (ThemeMood = /api/theme-mood/board 落库读侧; ReplayTree = market_replay_daily,
 *       东财三池 eastmoney_zt, TQ 全A自算 tq_pool 仅交叉校验)。
 *   ③ **深链可分享可后退**: 视图写进 URL `?view=theme-mood|replay-tree`,
 *      切换时保留其余查询参数与 hash; 兼容旧 `?tab=` 写法(规范化成 view, replace 不留历史垃圾);
 *      每个视图另有原路由「单独打开」链接, 老书签 /theme-mood、/replay-tree 继续可用。
 *
 * 权限: 两页在 App.tsx 均由 `view_quotes` 守卫; 本组件按同一权限点过滤视图,
 * myPerms 为 null(未加载/拉取失败)时不过滤, 避免闪隐。
 */
import { useId, type ReactNode } from 'react'
import { Navigate, Link, useLocation, useNavigate, useSearchParams } from 'react-router-dom'
import { Activity, ArrowUpRight, GitBranch, type LucideIcon } from 'lucide-react'

import PageTabs from '@/components/PageTabs'
import ThemeMoodPage from '@/pages/ThemeMood'
import ReplayTreePage from '@/pages/ReplayTree'

/** 视图 key 即 URL `?view=` 的规范值 */
export type MarketMoodViewKey = 'theme-mood' | 'replay-tree'

export const DEFAULT_MARKET_MOOD_VIEW: MarketMoodViewKey = 'theme-mood'

/** 旧/别名写法 → 规范 key。`tab` 是既有 TabbedPage 约定的兼容入口。 */
const VIEW_ALIASES: Record<string, MarketMoodViewKey> = {
  'theme-mood': 'theme-mood',
  thememood: 'theme-mood',
  theme: 'theme-mood',
  mood: 'theme-mood',
  'replay-tree': 'replay-tree',
  replaytree: 'replay-tree',
  replay: 'replay-tree',
}

/** 纯函数: 非法/未知值返回 null(调用方据此回退默认视图并规范化 URL) */
export function normalizeMarketMoodView(value: string | null | undefined): MarketMoodViewKey | null {
  if (!value) return null
  return VIEW_ALIASES[value.trim().toLowerCase()] ?? null
}

export interface MarketMoodView {
  key: MarketMoodViewKey
  label: string
  hint: string
  icon: LucideIcon
  /** 数据来源(接口/表), 与页面内数据一致, 不做推断 */
  source: string
  /** 口径边界; 两视图口径不同, 必须各自标注 */
  caliber: string
  /** 原独立路由, 保留老深链 */
  legacyPath: string
  /** 权限点, 与 App.tsx 的 PermGuard 保持一致 */
  perm: string
  render: () => ReactNode
}

export const MARKET_MOOD_VIEWS: readonly MarketMoodView[] = [
  {
    key: 'theme-mood',
    label: '题材情绪',
    hint: '题材×日情绪矩阵(收盘确认口径)',
    icon: Activity,
    source: '/api/theme-mood/board · theme_mood_daily(落库读侧)',
    caliber: '收盘确认口径: 题材×日情绪分; 盘中的 live/定型状态由接口 phase 标注。全市场情绪温度是另一套自算口径, 在该视图内单列展示。',
    legacyPath: '/theme-mood',
    perm: 'view_quotes',
    render: () => <ThemeMoodPage />,
  },
  {
    key: 'replay-tree',
    label: '连板晋级树',
    hint: '三池标量 + 梯队晋级树(东财三池 eastmoney_zt)',
    icon: GitBranch,
    source: '/api/market/replay/tree · market_replay_daily',
    caliber: '东财三池 eastmoney_zt(不含北交所); TQ 全A自算 tq_pool 仅作交叉校验, 与东财口径同屏各自标注、禁止混算。',
    legacyPath: '/replay-tree',
    perm: 'view_quotes',
    render: () => <ReplayTreePage />,
  },
]

export interface MarketMoodHubProps {
  /** 当前用户权限点集合; null/未传 = 未加载, 不过滤(避免闪隐) */
  myPerms?: Set<string> | null
  /** 仅供测试/嵌入: 初始视图(URL 无 view 时生效); URL 优先 */
  initialView?: MarketMoodViewKey
}

function viewAllowed(view: MarketMoodView, myPerms: Set<string> | null | undefined): boolean {
  if (!view.perm) return true
  if (!myPerms) return true
  return myPerms.has(view.perm)
}

export default function MarketMoodHub({ myPerms, initialView }: MarketMoodHubProps) {
  const [params] = useSearchParams()
  const location = useLocation()
  const navigate = useNavigate()
  const idPrefix = useId()

  const visible = MARKET_MOOD_VIEWS.filter((v) => viewAllowed(v, myPerms))
  const rawView = params.get('view')
  const rawTab = params.get('tab')
  const requested = normalizeMarketMoodView(rawView ?? rawTab)
  const fallback = visible.some((v) => v.key === initialView)
    ? (initialView as MarketMoodViewKey)
    : (visible[0]?.key ?? null)
  const resolved: MarketMoodViewKey | null =
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
      <div className="py-10 text-center" data-testid="market-mood-hub-denied">
        <div className="text-[14px] font-medium text-foreground">无可用视图(权限不足)</div>
        <p className="mt-1 text-[12px] text-muted-foreground">
          市场情绪工作区需要 `view_quotes` 权限; 当前账号未开通。
        </p>
      </div>
    )
  }

  const active = visible.find((v) => v.key === resolved) as MarketMoodView
  const panelId = `${idPrefix}-panel`

  return (
    <div className="sida-page-enter" data-testid="market-mood-hub" data-view={active.key}>
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
          data-testid="market-mood-legacy-link"
          className="ml-auto inline-flex min-h-9 items-center gap-1 rounded-md px-2 text-[12px] text-muted-foreground hover:bg-s2 hover:text-foreground"
          title={`在独立页面打开「${active.label}」(原路由书签继续可用)`}
        >
          单独打开{active.label}
          <ArrowUpRight className="h-3.5 w-3.5" />
        </Link>
      </div>

      {/* 两种来源/口径边界: 常驻标注, 明确本工作区不合并 */}
      <div className="mb-3 rounded-lg bg-s2 px-3 py-2 text-[12px]" data-testid="market-mood-caliber-bar">
        <div className="text-muted-foreground">
          口径边界: 两个视图各自独立取数、各自口径; 本工作区不做合并、换算或互相补数。
        </div>
        <div className="mt-1 flex flex-col gap-1">
          {visible.map((v) => (
            <div
              key={v.key}
              data-testid={`market-mood-caliber-${v.key}`}
              className={`flex flex-wrap items-baseline gap-x-2 gap-y-0.5 ${
                v.key === active.key ? 'text-foreground' : 'text-muted-foreground'
              }`}
            >
              <span className="font-medium">{v.label}</span>
              <span className="font-mono text-[12px]">{v.source}</span>
              <span className="w-full leading-relaxed">{v.caliber}</span>
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
        data-testid="market-mood-view-panel"
        data-view={active.key}
      >
        {active.render()}
      </div>
    </div>
  )
}

/** 供主模型接路由时复用的视图元数据(不含 React 渲染) */
export const MARKET_MOOD_NAV_META: ReadonlyArray<{
  key: MarketMoodViewKey
  label: string
  legacyPath: string
  perm: string
}> = MARKET_MOOD_VIEWS.map(({ key, label, legacyPath, perm }) => ({ key, label, legacyPath, perm }))
