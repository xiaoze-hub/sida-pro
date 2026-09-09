import { BarChart3 } from 'lucide-react'
import { Button } from '@panwatch/base-ui/components/ui/button'
import { useSettings } from './context'

export function FeedbackSection() {
  const {
    sectionMatches,
    fbStats,
    fbLoading,
    loadFeedbackStats,
  } = useSettings()
  return (
    <>
  {/* Feedback Stats */}
  <section id="sec-feedback" className="border-t border-border/40 pt-4 md:pt-5 lg:col-span-5" style={{ display: sectionMatches('sec-feedback') ? undefined : 'none' }}>
    <div className="flex items-center justify-between mb-4">
      <div>
        <h3 className="text-[12px] md:text-[13px] font-semibold text-foreground">建议反馈</h3>
        <p className="text-[11px] text-muted-foreground mt-1">用于评估推送质量与策略迭代</p>
      </div>
      <Button variant="secondary" size="sm" className="h-8" onClick={loadFeedbackStats} disabled={fbLoading}>
        <BarChart3 className="w-3.5 h-3.5" />
        <span className="hidden sm:inline">刷新</span>
      </Button>
    </div>

    {fbStats ? (
      <div className="space-y-3">
        <div className="flex flex-wrap items-center gap-2 text-[12px] text-muted-foreground">
          <span>近 {fbStats.range_days} 天</span>
          <span className="opacity-50">|</span>
          <span>反馈: <span className="font-mono text-foreground/90">{fbStats.total}</span></span>
          <span className="opacity-50">|</span>
          <span>有用: <span className="font-mono text-emerald-600">{fbStats.useful}</span></span>
          <span className="opacity-50">|</span>
          <span>没用: <span className="font-mono text-rose-600">{fbStats.useless}</span></span>
          <span className="opacity-50">|</span>
          <span>有用率: <span className="font-mono text-foreground/90">{Math.round(fbStats.useful_rate * 100)}%</span></span>
        </div>

        {fbStats.by_agent?.length ? (
          <div className="rounded-md border border-border/40 bg-accent/20 p-3">
            <div className="text-[12px] font-semibold text-foreground">按 Agent</div>
            <div className="mt-2 space-y-1">
              {fbStats.by_agent.slice(0, 6).map(a => (
                <div key={a.agent_name} className="flex items-center justify-between text-[11px]">
                  <span className="font-mono text-muted-foreground">{a.agent_name}</span>
                  <span className="font-mono text-muted-foreground">
                    {a.useful}/{a.total} ({Math.round(a.useful_rate * 100)}%)
                  </span>
                </div>
              ))}
            </div>
          </div>
        ) : (
          <div className="text-[12px] text-muted-foreground">暂无反馈数据</div>
        )}
      </div>
    ) : (
      <div className="text-[12px] text-muted-foreground">暂无反馈数据</div>
    )}
  </section>

    </>
  )
}
