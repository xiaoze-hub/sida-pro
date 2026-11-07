import PageHeader from '@/components/PageHeader'
import { Suspense } from 'react'
import { Link } from 'react-router-dom'
import { Database, HelpCircle, Loader2, Settings, ShieldCheck } from 'lucide-react'

import TabbedPage, { type TabDef } from '@/components/TabbedPage'
import { lazyWithRetry } from '@/lib/lazy-with-retry'

/**
 * 设置枢纽页(设计稿 §4.3): 审计 + 帮助 收纳进「设置」。
 *
 * - 设置 Tab = 原有 SettingsPage(124KB / 11 个锚点分区, 原样嵌入不改造)
 * - 审计 Tab = 原 AuditPage, 保持 ownerOnly(非 owner 页签直接隐藏)
 * - 帮助 Tab = 原 HelpPage, 无权限限制
 *
 * 旧路由 /audit、/help 由 App.tsx 重定向到 /settings?tab=xxx, 书签不失效。
 */
const SettingsPage = lazyWithRetry(() => import('@/pages/Settings'), 'settings-page')
const AuditPage = lazyWithRetry(() => import('@/pages/Audit'), 'audit-page')
const HelpPage = lazyWithRetry(() => import('@/pages/Help'), 'help-page')

export const SETTINGS_TABS: TabDef[] = [
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

/**
 * 相关入口(2026-11-07): 设置页只负责账号/偏好, 数据源连接在「系统」、账号总控在「管理后台」,
 * 而本页没有任何跨页入口 —— 最典型的误入是找「数据源」时落到「数据源凭证」(那只是接口 Key)。
 * 这里给两条直达链接, 与 navigation.ts 同口径过滤: manage_datasources / owner + manage_users。
 * 只加链接, 不改任何 Tab 的权限、默认落点与旧地址。
 */
export function SettingsRelatedEntries({ myPerms, isOwner }: { myPerms?: Set<string> | null; isOwner?: () => boolean }) {
  const canDataSources = !myPerms || myPerms.has('manage_datasources')
  const canAdmin = (!isOwner || isOwner()) && (!myPerms || myPerms.has('manage_users'))
  if (!canDataSources && !canAdmin) return null
  const link = 'inline-flex min-w-0 items-center gap-1.5 text-[12px] font-medium text-primary hover:underline'
  return (
    <nav aria-label="相关入口" className="flex flex-wrap items-center gap-x-4 gap-y-1.5">
      <span className="text-[12px] text-muted-foreground">相关入口</span>
      {canDataSources && (
        <Link to="/system?tab=datasources" className={link}>
          <Database className="h-3.5 w-3.5 shrink-0" />
          数据源管理
          <span className="font-normal text-muted-foreground">连接与采集(数据源凭证只是接口 Key)</span>
        </Link>
      )}
      {canAdmin && (
        <Link to="/admin" className={link}>
          <ShieldCheck className="h-3.5 w-3.5 shrink-0" />
          管理后台
          <span className="font-normal text-muted-foreground">邀请码 / API Key / 用量 / Pro 审核(用户数据与本页「用户管理」同一份)</span>
        </Link>
      )}
    </nav>
  )
}

function TabLoading() {
  return (
    <div className="flex h-[40vh] items-center justify-center text-[12px] text-muted-foreground">
      <Loader2 className="mr-2 h-4 w-4 animate-spin" /> 加载中…
    </div>
  )
}

export default function SettingsHubPage({ myPerms, isOwner }: { myPerms?: Set<string> | null; isOwner?: () => boolean }) {
  return (
    <div className="page-container">
      <PageHeader
        title="设置"
        description="管理外观、账户偏好与系统设置"
        metadata={<SettingsRelatedEntries myPerms={myPerms} isOwner={isOwner} />}
      />
    <TabbedPage tabs={SETTINGS_TABS} defaultTab="settings" myPerms={myPerms} isOwner={isOwner} />
    </div>
  )
}
