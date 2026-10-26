import { Switch } from '@panwatch/base-ui/components/ui/switch'
import UserManagement from '@/components/UserManagement'
import { MailCheck } from 'lucide-react'
import { useSettings } from './context'

export function UsersSection({ activeId }: { activeId?: string }) {
  const {
    currentUser,
    subscriptions,
    subLoading,
    sectionMatches,
    toggleSubscription,
  } = useSettings()
  const matches = (id: string) => (!activeId || activeId === id) && sectionMatches(id)
  return (
    <>
  {/* 多用户: 定时报告订阅 + 用户管理(2026-08-10 阶段5) */}
  <section id="sec-subscriptions" className="analysis-section" style={{ display: matches('sec-subscriptions') ? undefined : 'none' }}>
    <div className="mb-3 flex items-center gap-2">
      <h2 className="flex items-center gap-2 text-[14px] font-semibold">
        <MailCheck className="h-4 w-4 text-primary" />
        定时报告订阅
      </h2>
      <span className="text-[12px] text-muted-foreground">选择你要接收的定时推送</span>
    </div>
    {subLoading ? (
      <div className="py-3 text-[12px] text-muted-foreground">加载中…</div>
    ) : (
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        {subscriptions.map(s => (
          <div key={s.report_type} className="card-surface flex min-w-0 items-center justify-between gap-3 rounded-md bg-s2 px-3 py-2.5">
            <div>
              <div className="text-[12px] font-medium">{s.label}</div>
              <div className="text-[12px] text-muted-foreground">{s.report_type}</div>
            </div>
            <Switch checked={s.enabled} onCheckedChange={() => toggleSubscription(s.report_type)} aria-label={`订阅 ${s.label}`} />
          </div>
        ))}
      </div>
    )}
  </section>

  {currentUser?.role === 'owner' && (
    <section id="sec-users" className="analysis-section" style={{ display: matches('sec-users') ? undefined : 'none' }}>
      <UserManagement currentUser={currentUser} />
    </section>
  )}
    </>
  )
}
