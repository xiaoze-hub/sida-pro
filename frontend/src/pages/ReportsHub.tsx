import { lazy, Suspense } from 'react'
import { Clock, FileText, Loader2 } from 'lucide-react'

import TabbedPage, { type TabDef } from '@/components/TabbedPage'

/**
 * 投研枢纽页(设计稿 v2.0 §4.3「历史并入报告」): 报告 + 历史 两 Tab。
 *
 * ⚠️ 2026-09-01 补做: 该合并项属第 1 块(§4.3 四项 Tab 合并), 其产物曾被
 * 同步事故冲掉; 重建时派活清单只列了三项, 此项被遗漏 —— 本次对照设计稿补齐。
 *
 * 旧路由 /history 由 App.tsx 重定向到 /reports?tab=history, 书签不失效。
 */
const ReportsPage = lazy(() => import('@/pages/Reports'))
const HistoryPage = lazy(() => import('@/pages/History'))

const REPORTS_TABS: TabDef[] = [
  {
    key: 'reports',
    label: '报告',
    icon: FileText,
    perm: 'view_reports',
    render: () => (
      <Suspense fallback={<TabLoading />}>
        <ReportsPage />
      </Suspense>
    ),
  },
  {
    key: 'history',
    label: '历史',
    icon: Clock,
    render: () => (
      <Suspense fallback={<TabLoading />}>
        <HistoryPage />
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

export default function ReportsHubPage({ myPerms, isOwner }: { myPerms?: Set<string> | null; isOwner?: () => boolean }) {
  return (
    <TabbedPage tabs={REPORTS_TABS} defaultTab="reports" myPerms={myPerms} isOwner={isOwner} />
  )
}
