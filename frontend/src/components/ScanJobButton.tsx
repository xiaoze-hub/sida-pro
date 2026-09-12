/** 「立即扫描」按钮(KI-053): 调既有 POST 端点起一个后台作业, 进度在「系统 → 任务」看。
 *
 * 后端已带 single-flight: 重复点击拿回同一个 job_id, 不会并发起第二个扫描。
 * 演示/访客是只读档, 不显示触发入口。
 */
import { useState } from 'react'
import { fetchAPI } from '@panwatch/api'
import { RefreshCw } from 'lucide-react'
import { useToast } from '@panwatch/base-ui/components/ui/toast'
import { Button } from '@panwatch/base-ui/components/ui/button'
import { isDemoUser, isGuestUser } from '@/lib/jwt'

interface ScanResp {
  started?: boolean
  reason?: string | null
  job_id?: string
}

interface JobState {
  status?: 'pending' | 'running' | 'succeeded' | 'failed' | 'cancelled' | 'stale'
}

const POLL_MS = 3000
const MAX_WAIT_MS = 20 * 60 * 1000   // 兜底: 超过 20 分钟不再盯, 页面数据靠自身轮询刷新

const sleep = (ms: number) => new Promise<void>((r) => window.setTimeout(r, ms))

/** 盯到任务离开运行态; 超时/异常都安静返回(不报错 —— 任务本身还在跑, 只是不追了)。 */
async function waitJobDone(jobId: string): Promise<void> {
  const deadline = Date.now() + MAX_WAIT_MS
  while (Date.now() < deadline) {
    await sleep(POLL_MS)
    try {
      const j = await fetchAPI<JobState>(`/jobs/${jobId}`)
      if (j?.status && j.status !== 'pending' && j.status !== 'running') return
    } catch {
      return
    }
  }
}

export default function ScanJobButton({
  path, label = '立即扫描', title, onDone,
}: { path: string; label?: string; title?: string; onDone?: () => void }) {
  const [busy, setBusy] = useState(false)
  const { toast } = useToast()

  if (isDemoUser() || isGuestUser()) return null

  const run = async () => {
    setBusy(true)
    try {
      const res = await fetchAPI<ScanResp>(path, { method: 'POST' })
      if (!res?.started) {
        toast(res?.reason || `${label}: 已有任务在跑, 进度看「系统 → 任务」`, 'info')
        return
      }
      toast(`${label}: 已开始, 进度看「系统 → 任务」`, 'success')
      if (onDone && res.job_id) {
        await waitJobDone(res.job_id)
        onDone()
      }
    } catch (e) {
      toast(`${label} 启动失败: ${e instanceof Error ? e.message : String(e)}`, 'error')
    } finally {
      setBusy(false)
    }
  }

  return (
    <Button
      onClick={run}
      disabled={busy}
      size="sm"
      variant="ghost"
      className="h-7 px-2 text-[11px] text-muted-foreground"
      title={title ?? `${label}(后台执行, 可在「系统 → 任务」看进度或取消)`}
    >
      <RefreshCw className={`h-3.5 w-3.5 mr-1 ${busy ? 'animate-spin' : ''}`} />
      {label}
    </Button>
  )
}
