import { useEffect, useState, useCallback } from 'react'
import { ScrollText, RefreshCw } from 'lucide-react'
import { fetchAPI } from '@panwatch/api'
import { Button } from '@panwatch/base-ui/components/ui/button'
import { formatFullDateTime } from '@/lib/utils'
import ErrorBanner from '@/components/ErrorBanner'

interface AuditEntry {
  id: number
  user_id: string | null
  username: string
  action: string
  detail: string
  ip: string
  created_at: string | null
}

interface AuditResponse {
  logs: AuditEntry[]
  total?: number
  users?: string[]
}

/** 操作类型 → 中文标签(与后端 AuditLog.action 约定一致) */
const ACTION_LABELS: Record<string, { label: string; tone: string }> = {
  login: { label: '登录', tone: 'text-sky-600 dark:text-sky-400' },
  register: { label: '注册', tone: 'text-emerald-600 dark:text-emerald-700' },
  logout: { label: '登出', tone: 'text-muted-foreground' },
  update_profile: { label: '修改资料', tone: 'text-amber-600 dark:text-amber-600' },
  update_password: { label: '改密', tone: 'text-amber-600 dark:text-amber-600' },
  manage_user: { label: '用户管理', tone: 'text-violet-600 dark:text-violet-400' },
  update_settings: { label: '配置修改', tone: 'text-rose-600 dark:text-rose-400' },
  update_datasource: { label: '数据源修改', tone: 'text-rose-600 dark:text-rose-400' },
  export: { label: '导出', tone: 'text-indigo-600 dark:text-indigo-400' },
}

export default function AuditPage() {
  const [logs, setLogs] = useState<AuditEntry[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [filterUser, setFilterUser] = useState('')
  const [users, setUsers] = useState<string[]>([])

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const q = filterUser ? `&user=${encodeURIComponent(filterUser)}` : ''
      const data = await fetchAPI<AuditResponse>(`/audit?limit=200${q}`, { cacheMode: 'reload' })
      setLogs(data?.logs || [])
      if (data?.users) setUsers(data.users)
    } catch (e) {
      setError(e instanceof Error ? e.message : '加载失败')
    } finally {
      setLoading(false)
    }
  }, [filterUser])

  useEffect(() => {
    load()
  }, [load])

  const actionMeta = (action: string) => ACTION_LABELS[action] || { label: action || '--', tone: 'text-muted-foreground' }

  return (
    <div className="w-full space-y-4 md:space-y-6">
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
        <div className="flex items-center gap-2 md:gap-3">
          <div className="w-9 h-9 md:w-10 md:h-10 rounded-xl bg-accent flex items-center justify-center shadow-sm border border-border/60">
            <ScrollText className="w-4 h-4 md:w-5 md:h-5 text-foreground" />
          </div>
          <div>
            <h1 className="text-lg md:text-xl font-bold">操作审计</h1>
            <p className="text-[12px] md:text-[13px] text-muted-foreground">关键写操作记录(最近 200 条, 倒序)</p>
          </div>
          <div className="hidden md:flex px-2.5 py-1 rounded-full bg-background/70 border border-border/50 text-[11px] text-muted-foreground">
            共 <span className="font-mono text-foreground/90">{logs.length}</span> 条
          </div>
        </div>
        <div className="flex items-center gap-2">
          <select
            value={filterUser}
            onChange={e => setFilterUser(e.target.value)}
            className="h-8 rounded-md border border-border/50 bg-background px-2 text-[12px] text-foreground focus:outline-none"
            title="按用户筛选"
          >
            <option value="">全部用户</option>
            {users.map(u => (
              <option key={u} value={u}>{u}</option>
            ))}
          </select>
          <Button variant="outline" size="sm" onClick={load} disabled={loading} className="w-fit">
            <RefreshCw className={`w-3.5 h-3.5 mr-1.5 ${loading ? 'animate-spin' : ''}`} />
            刷新
          </Button>
        </div>
      </div>

      <div className="card overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full text-left text-[13px]">
            <thead>
              <tr className="border-b border-border/60 bg-accent/20 text-[12px] text-muted-foreground">
                <th className="px-4 py-2.5 font-medium whitespace-nowrap">时间</th>
                <th className="px-4 py-2.5 font-medium whitespace-nowrap">用户</th>
                <th className="px-4 py-2.5 font-medium whitespace-nowrap">操作</th>
                <th className="px-4 py-2.5 font-medium">详情</th>
                <th className="px-4 py-2.5 font-medium whitespace-nowrap">IP</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border/40">
              {!loading && logs.map(row => {
                const meta = actionMeta(row.action)
                return (
                  <tr key={row.id} className="hover:bg-accent/20 transition-colors">
                    <td className="px-4 py-2.5 whitespace-nowrap font-mono text-[12px] text-muted-foreground">
                      {formatFullDateTime(row.created_at) || '--'}
                    </td>
                    <td className="px-4 py-2.5 whitespace-nowrap">
                      <span className="font-medium text-foreground">{row.username || '--'}</span>
                    </td>
                    <td className="px-4 py-2.5 whitespace-nowrap">
                      <span className={`text-[12px] font-medium ${meta.tone}`}>{meta.label}</span>
                    </td>
                    <td className="px-4 py-2.5 text-muted-foreground max-w-[320px] truncate" title={row.detail || ''}>
                      {row.detail || '--'}
                    </td>
                    <td className="px-4 py-2.5 whitespace-nowrap font-mono text-[12px] text-muted-foreground">
                      {row.ip || '--'}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>

        {loading && (
          <div className="p-10 text-center">
            <span className="inline-block w-6 h-6 border-2 border-primary/30 border-t-primary rounded-full animate-spin" />
          </div>
        )}
        {!loading && !error && logs.length === 0 && (
          <div className="p-10 text-center text-[13px] text-muted-foreground">暂无审计记录</div>
        )}
        <ErrorBanner errors={!loading && error ? [{ source: '审计日志', message: error, retry: () => void load() }] : []} onDismiss={() => setError('')} />
      </div>
    </div>
  )
}
