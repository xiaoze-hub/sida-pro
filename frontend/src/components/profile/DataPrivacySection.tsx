/**
 * 数据与隐私(2026-10-23): GDPR 导出 / 注销的前端入口。
 *
 * 后端早已实现(user_data.py, 本次不改):
 *   GET  /api/user/data/export          导出全部个人数据 JSON
 *   POST /api/user/data/delete          {password, confirm:true} 软删, 30 天宽限
 *   GET  /api/user/data/delete/status   查询剩余天数
 * 此前无任何 UI 入口, 用户根本用不到 —— 本组件补齐。
 *
 * 文案必须诚实: 宽限期内 admin 可恢复, 到期由 purge_expired_deletions() 物理清除。
 */
import { useCallback, useEffect, useState } from 'react'
import { fetchAPI, API_BASE } from '@panwatch/api'
import { Button } from '@panwatch/base-ui/components/ui/button'
import { Input } from '@panwatch/base-ui/components/ui/input'
import { Label } from '@panwatch/base-ui/components/ui/label'
import { useToast } from '@panwatch/base-ui/components/ui/toast'
import { AlertTriangle, Download, Trash2 } from 'lucide-react'
import { formatDateTime } from '@/lib/utils'

interface DeleteStatus {
  status: 'none' | 'pending' | 'expired'
  is_deleted: boolean
  deleted_at?: string | null
  purge_after?: string | null
  days_left?: number | null
  message?: string
}

function _stamp(): string {
  return new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19)
}

export default function DataPrivacySection({ username }: { username?: string }) {
  const { toast } = useToast()
  const [status, setStatus] = useState<DeleteStatus | null>(null)
  const [statusError, setStatusError] = useState<Error | null>(null)
  const [statusLoading, setStatusLoading] = useState(true)
  const [exporting, setExporting] = useState(false)

  const [panelOpen, setPanelOpen] = useState(false)
  const [password, setPassword] = useState('')
  const [ack, setAck] = useState(false)
  const [deleting, setDeleting] = useState(false)

  const loadStatus = useCallback(async () => {
    setStatusLoading(true)
    try {
      const res = await fetchAPI<DeleteStatus>('/user/data/delete/status')
      setStatus(res)
      setStatusError(null)
    } catch (e) {
      setStatusError(e instanceof Error ? e : new Error('加载失败'))
    } finally {
      setStatusLoading(false)
    }
  }, [])

  useEffect(() => {
    void loadStatus()
  }, [loadStatus])

  const exportData = async () => {
    setExporting(true)
    try {
      // 导出是二进制/附件下载, fetchAPI 会按 JSON 解析 —— 这里直接取流。
      // Cookie 鉴权(credentials: include), 与全站一致。
      const res = await fetch(`${API_BASE}/user/data/export`, { credentials: 'include' })
      if (!res.ok) {
        let msg = `导出失败(HTTP ${res.status})`
        try {
          const body = await res.json()
          msg = body?.message || body?.detail || msg
        } catch {
          /* 非 JSON 错误体: 保留状态码文案 */
        }
        throw new Error(msg)
      }
      const blob = await res.blob()
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `sida-user-data-${username || 'user'}-${_stamp()}.json`
      document.body.appendChild(a)
      a.click()
      a.remove()
      URL.revokeObjectURL(url)
      toast('数据已导出', 'success')
    } catch (e) {
      toast(e instanceof Error ? e.message : '导出失败', 'error')
    } finally {
      setExporting(false)
    }
  }

  const submitDelete = async () => {
    if (!password || !ack) return
    setDeleting(true)
    try {
      const res = await fetchAPI<{ status: string; message: string; days_left?: number }>(
        '/user/data/delete',
        {
          method: 'POST',
          body: JSON.stringify({ password, confirm: true }),
        },
      )
      setPanelOpen(false)
      setPassword('')
      setAck(false)
      toast(res.message || '删除申请已受理', 'success')
      // 软删后 token_version 已递增 → 当前会话失效, 回登录页更诚实
      localStorage.removeItem('token')
      localStorage.removeItem('token_expires')
      localStorage.removeItem('user')
      window.setTimeout(() => {
        window.location.href = '/login'
      }, 1200)
    } catch (e) {
      toast(e instanceof Error ? e.message : '申请失败', 'error')
    } finally {
      setDeleting(false)
    }
  }

  const pending = status?.is_deleted === true

  return (
    <div className="mt-5">
      <div className="flex items-center justify-between gap-2 mb-3">
        <div className="flex items-center gap-1.5">
          <Download className="w-3.5 h-3.5 text-muted-foreground" />
          <span className="text-[12px] font-medium text-foreground">数据与隐私</span>
        </div>
        <Button
          variant="outline"
          size="sm"
          className="h-7 px-2 text-[11px]"
          disabled={exporting}
          onClick={() => void exportData()}
        >
          <Download className="h-3 w-3" />
          {exporting ? '导出中…' : '导出我的数据'}
        </Button>
      </div>

      {statusLoading && (
        <div className="py-2 text-[12px] text-muted-foreground">正在查询账号状态…</div>
      )}

      {!statusLoading && statusError && (
        <div className="space-y-2" role="alert">
          <div className="flex items-start gap-1.5 text-[12px] text-amber-600 dark:text-amber-500">
            <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
            <span>账号状态查询失败: {statusError.message}</span>
          </div>
          <button
            type="button"
            onClick={() => void loadStatus()}
            className="rounded border border-border/60 px-2.5 py-1 text-[11px] text-primary transition-colors hover:bg-s2"
          >
            重新查询状态
          </button>
        </div>
      )}

      {!statusLoading && !statusError && pending && (
        <div className="rounded-md border border-amber-500/40 bg-amber-500/5 px-3 py-2.5 text-[12px] leading-relaxed">
          <div className="font-medium text-amber-700 dark:text-amber-400">注销申请处理中</div>
          <div className="mt-1 text-muted-foreground tabular-nums">
            申请时间 {status?.deleted_at ? formatDateTime(status.deleted_at) : '未知'}
            {typeof status?.days_left === 'number' ? ` · 约 ${status.days_left} 天后物理清除` : ''}
          </div>
          <div className="mt-1 text-muted-foreground">
            宽限期内可由管理员恢复账号; 到期后由清理任务物理删除, 不可恢复。
          </div>
        </div>
      )}

      {!statusLoading && !statusError && !pending && (
        <div className="rounded-md border border-border/40">
          <div className="px-3 py-2.5">
            <div className="text-[12px] text-foreground">导出个人数据</div>
            <div className="mt-0.5 text-[11px] leading-relaxed text-muted-foreground">
              下载你在本平台的账号信息、自选、持仓与预测等数据的 JSON 快照。
            </div>
          </div>
          <div className="border-t border-border/40 px-3 py-2.5">
            <div className="flex items-center justify-between gap-2">
              <div className="min-w-0">
                <div className="text-[12px] text-foreground">注销账号</div>
                <div className="mt-0.5 text-[11px] leading-relaxed text-muted-foreground">
                  申请后账号立即停用、所有设备下线; 30 天宽限期内可由管理员恢复,
                  到期后数据被物理清除。
                </div>
              </div>
              <Button
                variant="outline"
                size="sm"
                className="h-7 shrink-0 px-2 text-[11px] text-destructive"
                onClick={() => setPanelOpen(v => !v)}
              >
                <Trash2 className="h-3 w-3" />
                {panelOpen ? '收起' : '申请注销'}
              </Button>
            </div>

            {panelOpen && (
              <div className="mt-3 space-y-2.5 border-t border-border/40 pt-3">
                <div>
                  <Label className="text-[11px]">当前密码(确认身份)</Label>
                  <Input
                    type="password"
                    value={password}
                    onChange={e => setPassword(e.target.value)}
                    placeholder="请输入当前登录密码"
                    autoComplete="current-password"
                    className="mt-1.5 h-9"
                  />
                </div>
                <label className="flex cursor-pointer items-start gap-2 select-none">
                  <input
                    type="checkbox"
                    className="mt-0.5 h-4 w-4 shrink-0 rounded border-border accent-[hsl(var(--primary))]"
                    checked={ack}
                    onChange={e => setAck(e.target.checked)}
                  />
                  <span className="text-[11px] leading-relaxed text-muted-foreground">
                    我了解: 提交后账号立即无法登录, 30 天宽限期内可由管理员恢复,
                    宽限期满后数据被物理删除且不可恢复。
                  </span>
                </label>
                <Button
                  variant="destructive"
                  size="sm"
                  className="h-8 w-full text-[12px]"
                  disabled={deleting || !password || !ack}
                  onClick={() => void submitDelete()}
                >
                  {deleting ? '提交中…' : '确认申请注销'}
                </Button>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  )
}
