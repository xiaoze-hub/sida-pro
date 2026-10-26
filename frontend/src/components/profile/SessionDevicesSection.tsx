/**
 * 在线设备(2026-10-23 P0 设备限制失效 修复的 UI 面)。
 *
 * 后端 `/api/auth/sessions` 用 JWT jti 精确标识会话; 旧实现用 `token[:32]`,
 * 而 JWT 头部恒定 → 全平台同一 session_id, 设备上限与「下线」全部形同虚设。
 * 本组件让用户可以看见并管理真实在线的设备。
 */
import { useCallback, useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { fetchAPI } from '@panwatch/api'
import { Button } from '@panwatch/base-ui/components/ui/button'
import { useToast } from '@panwatch/base-ui/components/ui/toast'
import { AlertTriangle, Laptop, LogOut, MonitorSmartphone, ShieldCheck } from 'lucide-react'
import { formatDateTime } from '@/lib/utils'

interface SessionRow {
  id: string
  id_prefix: string
  device: string
  ip: string
  last_seen: string
  expires_at: string
  created_at: string
  is_current: boolean
}

interface SessionsResponse {
  sessions: SessionRow[]
  limit: number
}

export default function SessionDevicesSection() {
  const { toast } = useToast()
  const navigate = useNavigate()
  const [data, setData] = useState<SessionsResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<Error | null>(null)
  const [busyId, setBusyId] = useState<string | null>(null)
  const [kickingAll, setKickingAll] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const res = await fetchAPI<SessionsResponse>('/auth/sessions')
      setData(res)
      setError(null)
    } catch (e) {
      // 失败态显式给错误原文 + 重试, 不铺 '--' 假装有数据
      setError(e instanceof Error ? e : new Error('加载失败'))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const kickOne = async (row: SessionRow) => {
    setBusyId(row.id)
    try {
      const res = await fetchAPI<{ message: string; current: boolean }>(
        `/auth/sessions/${encodeURIComponent(row.id)}`,
        { method: 'DELETE' },
      )
      if (res.current) {
        // 用户把当前这台也下线了: 清本地凭证并回登录页
        localStorage.removeItem('token')
        localStorage.removeItem('token_expires')
        localStorage.removeItem('user')
        toast('当前设备已下线, 请重新登录', 'success')
        navigate('/login')
        return
      }
      toast('该设备已下线', 'success')
      await load()
    } catch (e) {
      toast(e instanceof Error ? e.message : '下线失败', 'error')
    } finally {
      setBusyId(null)
    }
  }

  const kickOthers = async () => {
    setKickingAll(true)
    try {
      const res = await fetchAPI<{ kicked: number; message: string }>(
        '/auth/sessions/kick-others',
        { method: 'POST' },
      )
      toast(res.kicked > 0 ? `已下线 ${res.kicked} 台设备` : '没有其它设备在线', 'success')
      await load()
    } catch (e) {
      toast(e instanceof Error ? e.message : '操作失败', 'error')
    } finally {
      setKickingAll(false)
    }
  }

  const sessions = data?.sessions ?? []
  const others = sessions.filter(s => !s.is_current)

  return (
    <div className="mt-5">
      <div className="flex items-center justify-between gap-2 mb-3">
        <div className="flex items-center gap-1.5">
          <MonitorSmartphone className="w-3.5 h-3.5 text-muted-foreground" />
          <span className="text-[12px] font-medium text-foreground">在线设备</span>
          {data && (
            <span className="text-[12px] text-muted-foreground tabular-nums">
              上限 {data.limit} 台
            </span>
          )}
        </div>
        <Button
          variant="outline"
          size="sm"
          className="h-7 px-2 text-[12px]"
          disabled={kickingAll || loading || !!error || others.length === 0}
          onClick={() => void kickOthers()}
        >
          {kickingAll ? '处理中…' : '下线其它设备'}
        </Button>
      </div>

      {loading && (
        <div className="py-3 text-[12px] text-muted-foreground">正在加载设备列表…</div>
      )}

      {!loading && error && (
        <div className="space-y-2" role="alert">
          <div className="flex items-start gap-1.5 text-[12px] text-amber-600 dark:text-amber-500">
            <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
            <span>设备列表加载失败: {error.message}</span>
          </div>
          <button
            type="button"
            onClick={() => void load()}
            className="rounded border border-border px-2.5 py-1 text-[12px] text-primary transition-colors hover:bg-s2"
          >
            重新加载设备
          </button>
        </div>
      )}

      {!loading && !error && sessions.length === 0 && (
        <div className="card-surface rounded-md bg-s1 px-3 py-2.5 text-[12px] leading-relaxed text-muted-foreground">
          暂未记录到在线设备。若刚刚登录, 会话记录可能尚未写入, 可点上方「重新加载设备」;
          这里为空不影响正常使用。
        </div>
      )}

      {!loading && !error && sessions.length > 0 && (
        <div className="card-surface bg-card rounded-md ">
          {sessions.map(s => (
            <div
              key={s.id}
              className="flex items-center gap-3 px-3 py-2.5 border-t border-border first:border-t-0"
            >
              <Laptop className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-1.5 flex-wrap">
                  <span className="text-[12px] text-foreground truncate">{s.device}</span>
                  {s.is_current && (
                    <span className="inline-flex items-center gap-0.5 rounded border border-emerald-700/30 bg-emerald-500/10 px-1 py-0.5 text-[12px] text-emerald-700 dark:text-emerald-400">
                      <ShieldCheck className="h-2.5 w-2.5" />
                      当前设备
                    </span>
                  )}
                </div>
                <div className="mt-0.5 flex items-center gap-2 text-[12px] text-muted-foreground tabular-nums">
                  <span>{s.id_prefix}</span>
                  {s.ip && <span>{s.ip}</span>}
                  <span>最近活跃 {formatDateTime(s.last_seen)}</span>
                </div>
              </div>
              <Button
                variant="ghost"
                size="sm"
                className="h-7 shrink-0 px-2 text-[12px] text-muted-foreground hover:text-destructive"
                disabled={busyId === s.id}
                onClick={() => void kickOne(s)}
              >
                <LogOut className="h-3 w-3" />
                {busyId === s.id ? '下线中…' : s.is_current ? '下线本机' : '下线'}
              </Button>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
