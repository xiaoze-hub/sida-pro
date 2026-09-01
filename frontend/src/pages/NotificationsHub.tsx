import { lazy, Suspense } from 'react'
import { Bell, BellRing, Loader2 } from 'lucide-react'

import TabbedPage, { type TabDef } from '@/components/TabbedPage'

/**
 * 系统域枢纽页(设计稿 v2.0 §4.3「提醒并入通知」): 通知 + 提醒 两 Tab。
 *
 * ⚠️ 2026-09-01 补做: 该合并项属第 1 块(§4.3 四项 Tab 合并), 其产物曾被
 * 同步事故冲掉; 重建时派活清单只列了三项, 此项被遗漏 —— 本次对照设计稿补齐。
 *
 * 设计稿理由: "提醒是通知一种"。旧路由 /alerts 由 App.tsx 重定向到
 * /notifications?tab=alerts, 书签不失效; 移动端底栏的 /alerts 入口
 * 同样经重定向直达提醒 Tab, 交互不变。
 */
const NotificationsPage = lazy(() => import('@/pages/Notifications'))
const PriceAlertsPage = lazy(() => import('@/pages/PriceAlerts'))

const NOTIFICATIONS_TABS: TabDef[] = [
  {
    key: 'notifications',
    label: '通知',
    icon: Bell,
    render: () => (
      <Suspense fallback={<TabLoading />}>
        <NotificationsPage />
      </Suspense>
    ),
  },
  {
    key: 'alerts',
    label: '提醒',
    icon: BellRing,
    render: () => (
      <Suspense fallback={<TabLoading />}>
        <PriceAlertsPage />
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

export default function NotificationsHubPage({ myPerms, isOwner }: { myPerms?: Set<string> | null; isOwner?: () => boolean }) {
  return (
    <TabbedPage tabs={NOTIFICATIONS_TABS} defaultTab="notifications" myPerms={myPerms} isOwner={isOwner} />
  )
}
