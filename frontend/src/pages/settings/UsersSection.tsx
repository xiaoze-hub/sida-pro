import UserManagement from '@/components/UserManagement'
import { MailCheck } from 'lucide-react'
import { useSettings } from './context'

export function UsersSection() {
  const {
    currentUser,
    subscriptions,
    subLoading,
    sectionMatches,
    toggleSubscription,
  } = useSettings()
  return (
    <>
  {/* 多用户: 定时报告订阅 + 用户管理(2026-08-10 阶段5) */}
  <section id="sec-subscriptions" className="border-t border-border/40 pt-4 md:pt-5 lg:col-span-12" style={{ display: sectionMatches('sec-subscriptions') ? undefined : 'none' }}>
    <div className="mb-3 flex items-center gap-2">
      <h2 className="flex items-center gap-2 text-sm font-semibold">
        <MailCheck className="h-4 w-4 text-primary" />
        定时报告订阅
      </h2>
      <span className="text-[11px] text-muted-foreground">选择你要接收的定时推送</span>
    </div>
    {subLoading ? (
      <div className="py-3 text-[12px] text-muted-foreground">加载中…</div>
    ) : (
      <div className="grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-4">
        {subscriptions.map(s => (
          <div key={s.report_type} className="flex items-center justify-between rounded-md border border-border/40 bg-accent/20 px-3 py-2.5">
            <div>
              <div className="text-[12px] font-medium">{s.label}</div>
              <div className="text-[10px] text-muted-foreground">{s.report_type}</div>
            </div>
            <button
              type="button"
              role="switch"
              aria-checked={s.enabled}
              onClick={() => toggleSubscription(s.report_type)}
              className={`relative h-5 w-9 rounded-full transition-colors ${s.enabled ? 'bg-primary' : 'bg-muted'}`}
            >
              <span className={`absolute top-0.5 h-4 w-4 rounded-full bg-white transition-transform ${s.enabled ? 'translate-x-4' : 'translate-x-0.5'}`} />
            </button>
          </div>
        ))}
      </div>
    )}
  </section>

  {currentUser?.role === 'owner' && (
    <section id="sec-users" className="border-t border-border/40 pt-4 md:pt-5 lg:col-span-12" style={{ display: sectionMatches('sec-users') ? undefined : 'none' }}>
      <UserManagement currentUser={currentUser} />
    </section>
  )}
    </>
  )
}
