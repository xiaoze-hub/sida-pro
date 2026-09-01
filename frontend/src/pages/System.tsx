import { lazy, Suspense } from 'react'
import { Bot, Database, Loader2 } from 'lucide-react'

import TabbedPage, { type TabDef } from '@/components/TabbedPage'

/**
 * 系统二级页(设计稿 §4.3): Agent + 数据源 收纳进「系统」。
 *
 * 两个页签各自沿用原有页面组件(Agents / DataSources, 均自包含无 props),
 * 权限点原样保留(manage_agents / manage_datasources), 未授权则页签隐藏。
 * 旧路由 /agents、/datasources 由 App.tsx 重定向到 /system?tab=xxx, 书签不失效。
 */
const AgentsPage = lazy(() => import('@/pages/Agents'))
const DataSourcesPage = lazy(() => import('@/pages/DataSources'))

const SYSTEM_TABS: TabDef[] = [
  {
    key: 'agents',
    label: 'Agent',
    icon: Bot,
    perm: 'manage_agents',
    render: () => (
      <Suspense fallback={<TabLoading />}>
        <AgentsPage />
      </Suspense>
    ),
  },
  {
    key: 'datasources',
    label: '数据源',
    icon: Database,
    perm: 'manage_datasources',
    render: () => (
      <Suspense fallback={<TabLoading />}>
        <DataSourcesPage />
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

export default function SystemPage({ myPerms, isOwner }: { myPerms?: Set<string> | null; isOwner?: () => boolean }) {
  return (
    <TabbedPage tabs={SYSTEM_TABS} defaultTab="agents" myPerms={myPerms} isOwner={isOwner} />
  )
}
