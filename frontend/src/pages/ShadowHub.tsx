import { lazy, Suspense } from 'react'
import { Activity, Loader2, Shield } from 'lucide-react'

import TabbedPage, { type TabDef } from '@/components/TabbedPage'

/**
 * 我的域枢纽页(设计稿 v2.0 §4.3「模拟盘并入影子」): 影子账户 + 模拟盘 两 Tab。
 *
 * ⚠️ 2026-09-01 补做: 该合并项属第 1 块(§4.3 四项 Tab 合并), 其产物曾被
 * 同步事故冲掉; 重建时派活清单只列了三项, 此项被遗漏 —— 本次对照设计稿补齐。
 *
 * 旧路由 /paper-trading 由 App.tsx 重定向到 /shadow?tab=paper, 书签不失效。
 */
const ShadowAccountPage = lazy(() => import('@/pages/ShadowAccount'))
const PaperTradingPage = lazy(() => import('@/pages/PaperTrading'))

const SHADOW_TABS: TabDef[] = [
  {
    key: 'shadow',
    label: '影子账户',
    icon: Shield,
    perm: 'manage_shadow',
    render: () => (
      <Suspense fallback={<TabLoading />}>
        <ShadowAccountPage />
      </Suspense>
    ),
  },
  {
    key: 'paper',
    label: '模拟盘',
    icon: Activity,
    perm: 'manage_paper_trading',
    render: () => (
      <Suspense fallback={<TabLoading />}>
        <PaperTradingPage />
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

export default function ShadowHubPage({ myPerms, isOwner }: { myPerms?: Set<string> | null; isOwner?: () => boolean }) {
  return (
    <TabbedPage tabs={SHADOW_TABS} defaultTab="shadow" myPerms={myPerms} isOwner={isOwner} />
  )
}
