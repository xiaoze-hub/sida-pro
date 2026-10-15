import { X } from 'lucide-react'
import type { LucideIcon } from 'lucide-react'
import {
  Activity,
  Bell,
  ClipboardList,
  Code2,
  FileText,
  FlaskConical,
  KeyRound,
  LayoutDashboard,
  LayoutGrid,
  LineChart,
  List,
  ServerCog,
  Settings,
  Shield,
  ShieldCheck,
  Sparkles,
  TrendingUp,
  User,
} from 'lucide-react'

import { useI18n } from '@/hooks/useI18n'
import { HOME_TAB_KEY, parseTabPath, tabLabel, type WorkspaceTab } from '@/lib/tab-shell'

/**
 * 工作台页签栏(Tab Shell, 2026-10-15)。
 *
 * UI 铁律落法:
 *   · **hairline 分隔取代边框** —— 整条栏只有一条 1px 下分隔线(`border-b border-border/60`),
 *     页签之间不画竖线、不做卡片化包裹。
 *   · 层级只靠 Surface 阶梯(`bg-s2` 选中 / `hover:bg-s2` 悬停 / `bg-s3` 关闭键悬停),
 *     不手调透明度(R12 棘轮)、不用阴影(R13)。
 *   · 选中态 = 唯一的 accent 权重(`border-b-2 border-primary` 选择指示条 + 前景色),
 *     与侧栏「当前项」同一套语言。
 *   · 暗色优先: 全部走 token(--s0..s4 / --border / --primary), 亮暗两套自动成立。
 */
const TAB_ICONS: Record<string, LucideIcon> = {
  [HOME_TAB_KEY]: LayoutDashboard,
  '/heatmap': LayoutGrid,
  '/theme-mood': Activity,
  '/opportunities': Sparkles,
  '/dark-fund-top': TrendingUp,
  '/war-report': TrendingUp,
  '/caliber-compare': Activity,
  '/decision-ledger': ClipboardList,
  '/factor-ic': FlaskConical,
  '/reports': FileText,
  '/portfolio': List,
  '/shadow': Shield,
  '/notifications': Bell,
  '/profile': User,
  '/api-keys': KeyRound,
  '/system': ServerCog,
  '/settings': Settings,
  '/developers': Code2,
  '/admin': ShieldCheck,
}

function iconOf(path: string): LucideIcon | null {
  const { pathname } = parseTabPath(path)
  if (pathname.startsWith('/stocks/')) return LineChart
  return TAB_ICONS[pathname] ?? null
}

export default function WorkspaceTabs({
  tabs,
  activeKey,
  onActivate,
  onClose,
}: {
  tabs: WorkspaceTab[]
  /** 当前页签 key(= 当前路由的页签身份); null 或不在 tabs 里时视为无选中 */
  activeKey: string | null
  onActivate: (tab: WorkspaceTab) => void
  onClose: (tab: WorkspaceTab) => void
}) {
  const { t } = useI18n()
  if (tabs.length === 0) return null

  return (
    <div
      data-testid="workspace-tabs"
      role="tablist"
      aria-label={t('tabs.bar')}
      // 桌面端吸附在内容区顶部: 页签是"我在哪儿"的常驻坐标; 移动端顶部已有 sticky header, 不叠一层
      className="mb-3 -mx-4 flex items-stretch overflow-x-auto border-b border-border/60 px-4 md:sticky md:top-0 md:z-30 md:-mx-6 md:px-6 bg-s0"
    >
      {tabs.map((tab) => {
        const active = tab.key === activeKey
        const label = tabLabel(tab.path, t)
        const Icon = iconOf(tab.path)
        return (
          <div
            key={tab.key}
            data-testid="workspace-tab-item"
            data-tab-key={tab.key}
            data-active={active ? 'true' : 'false'}
            className={`group flex shrink-0 items-stretch rounded-t-lg border-b-2 transition-colors ${
              active ? 'border-primary bg-s2' : 'border-transparent hover:bg-s2'
            }`}
          >
            <button
              type="button"
              role="tab"
              data-testid="workspace-tab"
              aria-selected={active}
              title={label}
              onClick={() => onActivate(tab)}
              className={`flex min-w-0 items-center gap-1.5 py-1.5 pl-3 text-[12px] font-medium transition-colors ${
                tab.closable ? 'pr-1' : 'pr-3'
              } ${active ? 'text-foreground' : 'text-muted-foreground group-hover:text-foreground'}`}
            >
              {Icon && <Icon className={`h-3.5 w-3.5 shrink-0 ${active ? 'text-primary' : ''}`} />}
              <span className="max-w-[160px] truncate">{label}</span>
            </button>
            {tab.closable && (
              <button
                type="button"
                data-testid="workspace-tab-close"
                data-close-for={tab.key}
                aria-label={t('tabs.close', { name: label })}
                title={t('tabs.close', { name: label })}
                onClick={(e) => {
                  e.stopPropagation()
                  onClose(tab)
                }}
                className="my-1 mr-1 flex w-5 items-center justify-center rounded-md text-muted-foreground/70 transition-colors hover:bg-s3 hover:text-foreground"
              >
                <X className="h-3 w-3" />
              </button>
            )}
          </div>
        )
      })}
    </div>
  )
}
