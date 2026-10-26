import { useEffect, useState } from 'react'
import { createPortal } from 'react-dom'
import { X, Flame } from 'lucide-react'
import type { LucideIcon } from 'lucide-react'
import {
  Activity,
  Bell,
  ClipboardList,
  Code2,
  FileText,
  FlaskConical,
  GitBranch,
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
 *   · **hairline 分隔取代边框** —— 整条栏只有一条 1px 下分隔线(`border-b border-border`),
 *     页签之间不画竖线、不做卡片化包裹。
 *   · 层级只靠 Surface 阶梯(`bg-s2` 选中 / `hover:bg-s2` 悬停 / `bg-s3` 关闭键悬停),
 *     不手调透明度(R12 棘轮)、不用阴影(R13)。
 *   · 选中态以实色背景与字重区分，不使用彩色描边或选择指示条,
 *     与侧栏「当前项」同一套语言。
 *   · 暗色优先: 全部走 token(--s0..s4 / --border / --primary), 亮暗两套自动成立。
 */
const TAB_ICONS: Record<string, LucideIcon> = {
  [HOME_TAB_KEY]: LayoutDashboard,
  '/heatmap': LayoutGrid,
  '/theme-mood': Activity,
  '/opportunities': Sparkles,
  '/auction': Flame,
  '/dark-fund-top': TrendingUp,
  '/war-report': TrendingUp,
  '/replay-tree': GitBranch,
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
  onCloseOthers,
  onCloseRight,
}: {
  tabs: WorkspaceTab[]
  /** 当前页签 key(= 当前路由的页签身份); null 或不在 tabs 里时视为无选中 */
  activeKey: string | null
  onActivate: (tab: WorkspaceTab) => void
  onClose: (tab: WorkspaceTab) => void
  /** 右键菜单: 关闭其他(留首页 + 该页签) */
  onCloseOthers?: (tab: WorkspaceTab) => void
  /** 右键菜单: 关闭右侧(该页签右边全部) */
  onCloseRight?: (tab: WorkspaceTab) => void
}) {
  const { t } = useI18n()
  const [menu, setMenu] = useState<{ key: string; x: number; y: number } | null>(null)

  // 菜单开着时的收尾: 点空白处 / Esc / 滚动 / 改窗口大小 一律关掉。
  // 页签栏本身是 overflow-x-auto, 滚动后 fixed 坐标会与页签错位 —— 必须跟滚动一起关。
  useEffect(() => {
    if (!menu) return
    const close = () => setMenu(null)
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') close()
    }
    window.addEventListener('pointerdown', close)
    window.addEventListener('keydown', onKey)
    window.addEventListener('scroll', close, true)
    window.addEventListener('resize', close)
    return () => {
      window.removeEventListener('pointerdown', close)
      window.removeEventListener('keydown', onKey)
      window.removeEventListener('scroll', close, true)
      window.removeEventListener('resize', close)
    }
  }, [menu])

  if (tabs.length === 0) return null

  const menuTab = menu ? tabs.find((tab) => tab.key === menu.key) : undefined
  const menuIndex = menuTab ? tabs.findIndex((tab) => tab.key === menuTab.key) : -1
  // 「还有其他页签可关」才算可用: 只剩首页+锚点时关闭其他无意义
  const canCloseOthers = Boolean(menuTab) && tabs.length > (menuTab?.key === HOME_TAB_KEY ? 1 : 2)
  const canCloseRight = Boolean(menuTab) && menuIndex >= 0 && menuIndex < tabs.length - 1

  return (
    <>
      <div
        data-testid="workspace-tabs"
        role="tablist"
        aria-label={t('tabs.bar')}
        onKeyDown={(event) => {
          const index = tabs.findIndex((tab) => tab.key === activeKey)
          let next = index
          if (event.key === 'ArrowRight') next = (index + 1) % tabs.length
          else if (event.key === 'ArrowLeft') next = (index - 1 + tabs.length) % tabs.length
          else if (event.key === 'Home') next = 0
          else if (event.key === 'End') next = tabs.length - 1
          else return
          if (event.target instanceof HTMLElement && event.target.getAttribute('role') !== 'tab') return
          event.preventDefault()
          if (tabs[next]) onActivate(tabs[next])
          event.currentTarget.querySelectorAll<HTMLButtonElement>('[role="tab"]')[next]?.focus()
        }}
        // 桌面端吸附在内容区顶部: 页签是"我在哪儿"的常驻坐标; 移动端顶部已有 sticky header, 不叠一层
        className="mb-4 flex items-stretch gap-1 overflow-x-auto rounded-xl border border-border bg-s2 p-1 md:sticky md:top-0 md:z-30"
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
              onContextMenu={(e) => {
                e.preventDefault()
                setMenu({ key: tab.key, x: e.clientX, y: e.clientY })
              }}
              className={`group flex shrink-0 items-stretch rounded-lg border-b-2 transition-colors ${
                active ? 'border-transparent bg-card shadow-sm' : 'border-transparent hover:bg-s3'
              }`}
            >
              <button
                type="button"
                role="tab"
                data-testid="workspace-tab"
                aria-selected={active}
                tabIndex={active ? 0 : -1}
                title={label}
                onClick={() => onActivate(tab)}
                className={`flex min-h-10 min-w-0 items-center gap-2 py-2 pl-3 text-[14px] font-medium transition-colors ${
                  tab.closable ? 'pr-1' : 'pr-3'
                } ${active ? 'font-semibold text-foreground' : 'text-muted-foreground group-hover:text-foreground'}`}
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
                  className="my-1 mr-1 flex min-w-8 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-s3 hover:text-foreground"
                >
                  <X className="h-3 w-3" />
                </button>
              )}
            </div>
          )
        })}
      </div>
      {/* 右键菜单挂到 body: 既不受页签栏 overflow 裁切, 也不会把菜单文案混进 tablist 的文本内容 */}
      {menuTab
        ? createPortal(
            <div
              data-testid="workspace-tab-menu"
              role="menu"
              aria-label={t('tabs.menu')}
              style={{ left: menu?.x ?? 0, top: menu?.y ?? 0 }}
              className="fixed z-50 min-w-[150px] rounded-lg border border-border-strong bg-popover p-1 shadow-float"
            >
              <button
                type="button"
                role="menuitem"
                data-testid="tab-menu-close"
                disabled={!menuTab.closable}
                onClick={() => {
                  setMenu(null)
                  onClose(menuTab)
                }}
                className="flex w-full items-center rounded px-2 py-1.5 text-left text-[12px] text-foreground transition-colors hover:bg-s3 disabled:cursor-not-allowed disabled:opacity-40"
              >
                {t('tabs.closeSelf')}
              </button>
              <button
                type="button"
                role="menuitem"
                data-testid="tab-menu-close-others"
                disabled={!canCloseOthers || !onCloseOthers}
                onClick={() => {
                  setMenu(null)
                  onCloseOthers?.(menuTab)
                }}
                className="flex w-full items-center rounded px-2 py-1.5 text-left text-[12px] text-foreground transition-colors hover:bg-s3 disabled:cursor-not-allowed disabled:opacity-40"
              >
                {t('tabs.closeOthers')}
              </button>
              <button
                type="button"
                role="menuitem"
                data-testid="tab-menu-close-right"
                disabled={!canCloseRight || !onCloseRight}
                onClick={() => {
                  setMenu(null)
                  onCloseRight?.(menuTab)
                }}
                className="flex w-full items-center rounded px-2 py-1.5 text-left text-[12px] text-foreground transition-colors hover:bg-s3 disabled:cursor-not-allowed disabled:opacity-40"
              >
                {t('tabs.closeRight')}
              </button>
            </div>,
            document.body,
          )
        : null}
    </>
  )
}
