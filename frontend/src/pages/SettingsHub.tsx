import { lazy, Suspense } from 'react'
import { HelpCircle, Loader2, Settings, ShieldCheck } from 'lucide-react'

import TabbedPage, { type TabDef } from '@/components/TabbedPage'

/**
 * 设置枢纽页(设计稿 §4.3): 审计 + 帮助 收纳进「设置」。
 *
 * - 设置 Tab = 原有 SettingsPage(124KB / 11 个锚点分区, 原样嵌入不改造)
 * - 审计 Tab = 原 AuditPage, 保持 ownerOnly(非 owner 页签直接隐藏)
 * - 帮助 Tab = 原 HelpPage, 无权限限制
 *
 * 旧路由 /audit、/help 由 App.tsx 重定向到 /settings?tab=xxx, 书签不失效。
 */
const SettingsPage = lazy(() => import('@/pages/Settings'))
const AuditPage = lazy(() => import('@/pages/Audit'))
const HelpPage = lazy(() => import('@/pages/Help'))

const SETTINGS_TABS: TabDef[] = [
  {
    key: 'settings',
    label: '设置',
    icon: Settings,
    render: () => (
      <Suspense fallback={<TabLoading />}>
        <SettingsPage />
      </Suspense>
    ),
  },
  {
    key: 'audit',
    label: '审计',
    icon: ShieldCheck,
    ownerOnly: true,
    render: () => (
      <Suspense fallback={<TabLoading />}>
        <AuditPage />
      </Suspense>
    ),
  },
  {
    key: 'help',
    label: '帮助',
    icon: HelpCircle,
    render: () => (
      <Suspense fallback={<TabLoading />}>
        <HelpPage />
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

export default function SettingsHubPage({ myPerms, isOwner }: { myPerms?: Set<string> | null; isOwner?: () => boolean }) {
  return (
    <TabbedPage tabs={SETTINGS_TABS} defaultTab="settings" myPerms={myPerms} isOwner={isOwner} />
  )
}
