/** 后台任务面板(2026-09-12, 借鉴 tick-stock-panel 的作业面板)。
 *
 * 之前扫描/AI 批量/回填点了"运行"之后什么都看不见 —— 这里给: 活跃任务进度条 +
 * 取消入口 + 近期任务结果(成功/失败/卡死, 含错误摘要)。数据来自 /api/jobs。
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { fetchAPI } from '@panwatch/api'
import { ListChecks, X } from 'lucide-react'
import { useToast } from '@panwatch/base-ui/components/ui/toast'

interface Job {
  id: string
  kind: string
  label: string
  status: 'pending' | 'running' | 'succeeded' | 'failed' | 'cancelled'
  progress: number
  stage: string
  message: string
  error: string
  created_at: string
  updated_at: string
  finished_at: string | null
}

const STATUS_STYLE: Record<Job['status'], string> = {
  pending: 'text-muted-foreground',
  running: 'text-primary',
  succeeded: 'text-emerald-600 dark:text-emerald-400',
  failed: 'text-red-600 dark:text-red-400',
  cancelled: 'text-amber-600 dark:text-amber-400',
}

const STATUS_LABEL: Record<Job['status'], string> = {
  pending: '排队中', running: '进行中', succeeded: '成功', failed: '失败', cancelled: '已取消',
}

function elapsed(job: Job): string {
  const start = Date.parse(job.created_at.replace(' ', 'T'))
  const end = job.finished_at ? Date.parse(job.finished_at.replace(' ', 'T')) : Date.now()
  const s = Math.max(0, Math.round((end - start) / 1000))
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m${s % 60}s`
}

export function JobPanel() {
  const [active, setActive] = useState<Job[]>([])
  const [recent, setRecent] = useState<Job[]>([])
  const [busy, setBusy] = useState('')
  const { toast } = useToast()
  const timer = useRef<number | null>(null)

  const load = useCallback(async () => {
    try {
      const res = await fetchAPI<{ active: Job[]; recent: Job[] }>('/jobs')
      setActive(res.active ?? [])
      setRecent(res.recent ?? [])
    } catch {
      /* 保留旧数据, 下一轮再试 */
    }
  }, [])

  useEffect(() => {
    void load()
    // 有活跃任务时快轮(3s), 空闲时慢轮(20s) —— 不给系统页留常驻高频轮询
    const tick = () => {
      void load()
      timer.current = window.setTimeout(tick, active.length > 0 ? 3000 : 20000)
    }
    timer.current = window.setTimeout(tick, 20000)
    return () => {
      if (timer.current) window.clearTimeout(timer.current)
    }
  }, [load, active.length > 0])        // eslint-disable-line react-hooks/exhaustive-deps

  const doCancel = async (job: Job) => {
    setBusy(job.id)
    try {
      await fetchAPI(`/jobs/${job.id}/cancel`, { method: 'POST' })
      toast('已请求取消：任务将在下一个检查点退出', 'success')
      await load()
    } catch (e) {
      toast(`取消失败：${e instanceof Error ? e.message : String(e)}`, 'error')
    } finally {
      setBusy('')
    }
  }

  return (
    <div className="space-y-3">
      {active.length > 0 && (
        <div className="card p-3">
          <div className="mb-2 flex items-center gap-2">
            <ListChecks className="h-4 w-4 text-primary" strokeWidth={1.8} />
            <span className="text-[13px] font-semibold">进行中</span>
            <span className="ml-auto text-[11px] text-muted-foreground">重复点击会复用同一任务, 不会并发起第二个</span>
          </div>
          <div className="space-y-2">
            {active.map((j) => (
              <div key={j.id} className="flex items-center gap-3 text-[12px]">
                <span className="w-[150px] shrink-0 truncate">{j.label}</span>
                <div className="h-1.5 w-[160px] shrink-0 overflow-hidden rounded-full bg-accent/40">
                  <div className="h-full rounded-full bg-primary transition-[width]"
                       style={{ width: `${Math.max(3, j.progress)}%` }} />
                </div>
                <span className="w-[40px] shrink-0 font-mono text-[11px] text-muted-foreground">{j.progress}%</span>
                <span className="min-w-0 flex-1 truncate text-muted-foreground">{j.message || j.stage}</span>
                <button
                  type="button"
                  disabled={busy === j.id}
                  onClick={() => void doCancel(j)}
                  title="取消(协作式: 任务在下一个分块边界退出)"
                  className="shrink-0 rounded p-1 text-muted-foreground hover:bg-accent hover:text-foreground disabled:opacity-50"
                >
                  <X className="h-3.5 w-3.5" />
                </button>
              </div>
            ))}
          </div>
        </div>
      )}

      <div className="card p-3">
        <div className="mb-2 flex items-center gap-2">
          <ListChecks className="h-4 w-4 text-muted-foreground" strokeWidth={1.8} />
          <span className="text-[13px] font-semibold">近期任务</span>
          <span className="ml-auto text-[11px] text-muted-foreground">
            超过 15 分钟无进度更新会被判卡死(重启遗留的任务也会自愈)
          </span>
        </div>
        {recent.length === 0 ? (
          <div className="py-6 text-center text-[12px] text-muted-foreground">
            还没有后台任务记录 —— 在「题材情绪」或「决策先锋」页点「立即扫描」后回到这里看进度
          </div>
        ) : (
          <div className="space-y-1">
            {recent.slice(0, 20).map((j) => (
              <div key={j.id} className="flex items-center gap-3 text-[12px]">
                <span className={`w-[52px] shrink-0 ${STATUS_STYLE[j.status]}`}>{STATUS_LABEL[j.status]}</span>
                <span className="w-[150px] shrink-0 truncate">{j.label}</span>
                <span className="w-[40px] shrink-0 font-mono text-[11px] text-muted-foreground">{j.progress}%</span>
                <span className="w-[46px] shrink-0 font-mono text-[11px] text-muted-foreground">{elapsed(j)}</span>
                <span className="min-w-0 flex-1 truncate text-muted-foreground" title={j.error || j.message}>
                  {j.error || j.message}
                </span>
                <span className="shrink-0 font-mono text-[11px] text-muted-foreground">{j.created_at.slice(5, 16)}</span>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
