import { useCallback, useMemo, useState } from 'react'
import {
  Users,
  KeyRound,
  BarChart3,
  BadgeCheck,
  Loader2,
  Ban,
  CheckCircle2,
  RefreshCw,
  UserPlus,
  Activity,
} from 'lucide-react'
import { fetchAPI } from '@panwatch/api'
import { Button } from '@panwatch/base-ui/components/ui/button'
import { useToast } from '@panwatch/base-ui/components/ui/toast'
import { formatDateTime } from '@/lib/utils'
import { useApiQuery } from '@/hooks/useApiQuery'
import {
  DevPageLayout,
  Section,
  InfoCard,
  EmptyState,
  type SideMenuItem,
} from '@/components/dev/DevPageLayout'
import { useI18n } from '@/hooks/useI18n'

/**
 * Admin 管理后台(2026-09-16)。
 * owner 专属: 用户管理 / API Key 管理 / 用量监控 / Pro 申请审核。
 * 后端 require_owner + 前端 PermGuard(manage_users) 双重防护。
 */

// ── 类型 ──
interface AdminUser {
  id: string
  username: string
  email: string | null
  role: string
  is_active: boolean
  created_at: string | null
  last_login: string | null
}

interface AdminUserList {
  users: AdminUser[]
}

interface AdminStats {
  total_users: number
  active_users: number
  new_today: number
  new_7d: number
  new_30d: number
  by_role: Record<string, number>
  recent_registrations: { username: string; email: string | null; created_at: string | null }[]
}

interface AdminKey {
  id: number
  key_prefix: string
  owner_label: string
  username?: string
  user_id?: string | null
  tier: string
  status: string
  daily_limit: number
  used_today?: number
  frozen_reason: string
  created_at: string | null
  last_used_at: string | null
}

interface AdminKeysResp {
  keys: AdminKey[]
}

interface SkillUsageRow {
  key_prefix: string
  tier: string
  total: number
  errors: number
  top_skills: [string, number][]
}

interface SkillUsageResp {
  days: number
  report: SkillUsageRow[]
}

interface HighValueUsageResp {
  since: string
  days: number
  by_user_api: {
    user_id: string
    username: string
    api_name: string
    calls: number
    last_at: string | null
  }[]
  by_day: { day: string; api_name: string; calls: number }[]
}

interface ProApplication {
  id: number
  user_id: string
  username: string
  reason: string
  status: string
  created_at: string | null
  reviewed_at: string | null
  reviewed_by: string
  review_note: string
}

interface ProAppsResp {
  applications: ProApplication[]
  pending_count: number
}

// ── 菜单 ──
const MENU: SideMenuItem[] = [
  { id: 'users', label: '用户管理', icon: <Users className="h-3.5 w-3.5" />, anchor: 'sec-users' },
  { id: 'keys', label: 'API Key 管理', icon: <KeyRound className="h-3.5 w-3.5" />, anchor: 'sec-keys' },
  { id: 'usage', label: '用量监控', icon: <BarChart3 className="h-3.5 w-3.5" />, anchor: 'sec-usage' },
  { id: 'pro', label: 'Pro 申请审核', icon: <BadgeCheck className="h-3.5 w-3.5" />, anchor: 'sec-pro' },
]

const ROLE_LABEL: Record<string, string> = { owner: '管理员', pro: 'Pro', member: '成员', guest: '访客' }
const ROLE_BADGE: Record<string, string> = {
  owner: 'bg-amber-500/15 text-amber-500 border-amber-500/20',
  pro: 'bg-cyan-500/15 text-cyan-400 border-cyan-500/20',
  member: 'bg-sky-500/15 text-sky-400 border-sky-500/20',
  guest: 'bg-violet-500/15 text-violet-400 border-violet-500/20',
}
const TIER_LABEL: Record<string, string> = { free: '免费', trial: '试用', pro: 'Pro' }
const TIER_BADGE: Record<string, string> = {
  free: 'bg-slate-500/10 text-slate-400 border-slate-500/20',
  trial: 'bg-amber-500/10 text-amber-400 border-amber-500/20',
  pro: 'bg-cyan-500/10 text-cyan-400 border-cyan-500/20',
}
const KEY_STATUS_LABEL: Record<string, string> = { active: '正常', frozen: '已冻结', disabled: '已禁用' }
const APP_STATUS_LABEL: Record<string, string> = { pending: '待审核', approved: '已通过', rejected: '已拒绝' }
const APP_STATUS_BADGE: Record<string, string> = {
  pending: 'bg-amber-500/15 text-amber-400 border-amber-500/20',
  approved: 'bg-emerald-500/15 text-emerald-400 border-emerald-500/20',
  rejected: 'bg-red-500/15 text-red-400 border-red-500/20',
}

function TableLoading({ label = '加载中…' }: { label?: string }) {
  return (
    <div className="flex items-center justify-center gap-2 rounded-xl border border-border/50 dark:border-white/5 bg-muted/20 dark:bg-white/[0.01] py-10 text-[12px] text-muted-foreground">
      <Loader2 className="h-4 w-4 animate-spin" />
      {label}
    </div>
  )
}

function Th({ children, className = '' }: { children: React.ReactNode; className?: string }) {
  return (
    <th className={`whitespace-nowrap px-3 py-2 text-left text-[11px] font-medium text-muted-foreground ${className}`}>
      {children}
    </th>
  )
}

function Td({ children, className = '' }: { children: React.ReactNode; className?: string }) {
  return <td className={`px-3 py-2 text-[12px] text-foreground dark:text-slate-300 ${className}`}>{children}</td>
}

export default function AdminPage() {
  const { toast } = useToast()
  const { t } = useI18n()
  const [activeSection, setActiveSection] = useState('users')
  const [keyActionId, setKeyActionId] = useState<number | null>(null)
  const [userActionId, setUserActionId] = useState<string | null>(null)
  const [appActionId, setAppActionId] = useState<number | null>(null)

  const {
    data: userList,
    isLoading: usersLoading,
    refetch: refetchUsers,
  } = useApiQuery<AdminUserList>(['admin-users'], '/users/admin/list')
  const users = useMemo(() => userList?.users ?? [], [userList])

  const { data: stats, refetch: refetchStats } = useApiQuery<AdminStats>(['admin-stats'], '/users/admin/stats')

  const {
    data: keysResp,
    isLoading: keysLoading,
    refetch: refetchKeys,
  } = useApiQuery<AdminKeysResp>(['admin-keys'], '/admin/skills/keys')
  const keys = useMemo(() => keysResp?.keys ?? [], [keysResp])

  const { data: skillUsage, isLoading: skillUsageLoading } = useApiQuery<SkillUsageResp>(
    ['admin-skill-usage'],
    '/admin/skills/usage?days=7',
  )

  const { data: hvUsage, isLoading: hvUsageLoading } = useApiQuery<HighValueUsageResp>(
    ['admin-hv-usage'],
    '/users/admin/usage-report?days=7',
  )

  const {
    data: proApps,
    isLoading: proLoading,
    refetch: refetchPro,
  } = useApiQuery<ProAppsResp>(['admin-pro-apps'], '/pro/admin/applications')

  const refreshAll = useCallback(() => {
    void refetchUsers()
    void refetchStats()
    void refetchKeys()
    void refetchPro()
  }, [refetchUsers, refetchStats, refetchKeys, refetchPro])

  // ── 用户操作 ──
  const toggleActive = async (u: AdminUser) => {
    setUserActionId(u.id)
    try {
      await fetchAPI(`/users/admin/${u.id}/toggle-active`, {
        method: 'POST',
        body: JSON.stringify({ is_active: !u.is_active }),
      })
      toast(u.is_active ? `已禁用 ${u.username}` : `已启用 ${u.username}`, 'success')
      void refetchUsers()
      void refetchStats()
    } catch (e: any) {
      toast(e?.message || '操作失败', 'error')
    } finally {
      setUserActionId(null)
    }
  }

  const changeRole = async (u: AdminUser, role: string) => {
    if (role === u.role) return
    setUserActionId(u.id)
    try {
      await fetchAPI(`/users/admin/${u.id}/change-role`, {
        method: 'POST',
        body: JSON.stringify({ role }),
      })
      toast(`${u.username} 角色已改为 ${ROLE_LABEL[role] || role}`, 'success')
      void refetchUsers()
      void refetchStats()
    } catch (e: any) {
      toast(e?.message || '修改角色失败', 'error')
      void refetchUsers()
    } finally {
      setUserActionId(null)
    }
  }

  // ── Key 操作 ──
  const keyAction = async (key: AdminKey, action: 'freeze' | 'unfreeze' | 'disable') => {
    const label = action === 'freeze' ? '冻结' : action === 'unfreeze' ? '解冻' : '禁用'
    if (action === 'disable' && !confirm(`确定禁用 Key ${key.key_prefix}...？禁用后不可恢复为 active，需新建 Key。`)) return
    setKeyActionId(key.id)
    try {
      await fetchAPI('/admin/skills/keys/action', {
        method: 'POST',
        body: JSON.stringify({ key_id: key.id, action }),
      })
      toast(`Key ${key.key_prefix}... 已${label}`, 'success')
      void refetchKeys()
    } catch (e: any) {
      toast(e?.message || `${label}失败`, 'error')
    } finally {
      setKeyActionId(null)
    }
  }

  // ── Pro 审核 ──
  const reviewApp = async (app: ProApplication, action: 'approve' | 'reject') => {
    const label = action === 'approve' ? '批准' : '拒绝'
    if (!confirm(`确定${label} ${app.username} 的 Pro 申请？`)) return
    setAppActionId(app.id)
    try {
      await fetchAPI(`/pro/admin/${action}`, {
        method: 'POST',
        body: JSON.stringify({ application_id: app.id, note: '' }),
      })
      toast(`已${label} ${app.username} 的 Pro 申请`, 'success')
      void refetchPro()
      void refetchUsers()
      void refetchStats()
    } catch (e: any) {
      toast(e?.message || `${label}失败`, 'error')
    } finally {
      setAppActionId(null)
    }
  }

  // ── 派生统计 ──
  const keyStats = useMemo(() => {
    const total = keys.length
    const active = keys.filter(k => k.status === 'active').length
    const frozen = keys.filter(k => k.status === 'frozen').length
    const totalCalls = keys.reduce((s, k) => s + (k.used_today || 0), 0)
    const skillTotal = (skillUsage?.report || []).reduce((s, r) => s + r.total, 0)
    return { total, active, frozen, totalCalls: Math.max(totalCalls, skillTotal) }
  }, [keys, skillUsage])

  const pendingCount = proApps?.pending_count ?? proApps?.applications.filter(a => a.status === 'pending').length ?? 0

  return (
    <DevPageLayout
      title={t('admin.title')}
      subtitle="用户、API Key、用量与 Pro 申请统一管理（仅 owner）"
      badge={pendingCount > 0 ? `${pendingCount} 待审` : 'owner'}
      menu={MENU}
      activeId={activeSection}
      onNav={setActiveSection}
      headerExtra={
        <Button size="sm" variant="outline" className="h-8 shrink-0" onClick={refreshAll}>
          <RefreshCw className="h-3.5 w-3.5" />
          <span className="hidden sm:inline">刷新</span>
        </Button>
      }
    >
      {/* ── 用户管理 ── */}
      <Section
        id="sec-users"
        title="用户管理"
        description="查看全部账号，启用/禁用、修改角色"
        icon={<Users className="h-4 w-4" />}
      >
        {/* 统计卡片 */}
        <div className="mb-4 grid grid-cols-2 gap-3 sm:grid-cols-4">
          <InfoCard>
            <div className="text-[10px] text-slate-600">总用户</div>
            <div className="font-mono text-[20px] font-semibold text-foreground">{stats?.total_users ?? '—'}</div>
          </InfoCard>
          <InfoCard>
            <div className="text-[10px] text-slate-600">活跃用户</div>
            <div className="font-mono text-[20px] font-semibold text-cyan-400">{stats?.active_users ?? '—'}</div>
          </InfoCard>
          <InfoCard>
            <div className="text-[10px] text-slate-600">今日新增</div>
            <div className="font-mono text-[20px] font-semibold text-emerald-400">{stats?.new_today ?? '—'}</div>
          </InfoCard>
          <InfoCard>
            <div className="text-[10px] text-slate-600">7 日新增</div>
            <div className="font-mono text-[20px] font-semibold text-foreground">{stats?.new_7d ?? '—'}</div>
          </InfoCard>
        </div>

        {stats?.by_role && (
          <div className="mb-4 flex flex-wrap gap-2">
            {Object.entries(stats.by_role).map(([role, n]) => (
              <span
                key={role}
                className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[10px] ${ROLE_BADGE[role] || ROLE_BADGE.member}`}
              >
                {ROLE_LABEL[role] || role}
                <span className="font-mono">{n}</span>
              </span>
            ))}
            {stats.new_30d != null && (
              <span className="inline-flex items-center rounded-full border border-border/50 dark:border-white/10 bg-muted/40 dark:bg-white/[0.03] px-2 py-0.5 text-[10px] text-muted-foreground dark:text-slate-400">
                30 日新增 <span className="ml-1 font-mono">{stats.new_30d}</span>
              </span>
            )}
          </div>
        )}

        {usersLoading ? (
          <TableLoading label="加载用户列表…" />
        ) : users.length === 0 ? (
          <EmptyState icon={<Users className="h-8 w-8" />} title="暂无用户" />
        ) : (
          <div className="overflow-x-auto rounded-xl border border-border/50 dark:border-white/5 bg-card/60 dark:bg-white/[0.02]">
            <table className="w-full min-w-[720px]">
              <thead className="border-b border-border/50 dark:border-white/5 bg-card/60 dark:bg-white/[0.02]">
                <tr>
                  <Th>用户名</Th>
                  <Th>邮箱</Th>
                  <Th>角色</Th>
                  <Th>状态</Th>
                  <Th>注册时间</Th>
                  <Th>最后登录</Th>
                  <Th className="text-right">操作</Th>
                </tr>
              </thead>
              <tbody>
                {users.map(u => (
                  <tr key={u.id} className="border-b border-border/40 dark:border-white/5 last:border-0 hover:bg-accent/30 dark:hover:bg-white/[0.02]">
                    <Td className="font-medium text-foreground">{u.username}</Td>
                    <Td className="text-muted-foreground dark:text-slate-500">{u.email || '—'}</Td>
                    <Td>
                      <select
                        className="rounded-md border border-border/60 dark:border-white/10 bg-background dark:bg-[#0d0d18] px-2 py-1 text-[11px] text-foreground dark:text-slate-200 outline-none focus:border-primary/40 dark:focus:border-cyan-500/40"
                        value={u.role}
                        disabled={userActionId === u.id}
                        onChange={e => void changeRole(u, e.target.value)}
                      >
                        <option value="member">成员</option>
                        <option value="pro">Pro</option>
                        <option value="owner">管理员</option>
                      </select>
                    </Td>
                    <Td>
                      {u.is_active ? (
                        <span className="inline-flex items-center rounded-full border border-emerald-500/20 bg-emerald-500/10 px-2 py-0.5 text-[10px] text-emerald-400">
                          启用
                        </span>
                      ) : (
                        <span className="inline-flex items-center rounded-full border border-red-500/20 bg-red-500/10 px-2 py-0.5 text-[10px] text-red-400">
                          禁用
                        </span>
                      )}
                    </Td>
                    <Td className="whitespace-nowrap text-muted-foreground dark:text-slate-500">{formatDateTime(u.created_at) || '—'}</Td>
                    <Td className="whitespace-nowrap text-muted-foreground dark:text-slate-500">{formatDateTime(u.last_login) || '从未'}</Td>
                    <Td className="text-right">
                      <Button
                        size="sm"
                        variant="outline"
                        className={`h-7 px-2 text-[11px] ${u.is_active ? 'hover:border-red-500/30 hover:text-red-400' : 'hover:border-emerald-500/30 hover:text-emerald-400'}`}
                        disabled={userActionId === u.id}
                        onClick={() => void toggleActive(u)}
                      >
                        {userActionId === u.id ? (
                          <Loader2 className="h-3 w-3 animate-spin" />
                        ) : u.is_active ? (
                          <Ban className="h-3 w-3" />
                        ) : (
                          <CheckCircle2 className="h-3 w-3" />
                        )}
                        {u.is_active ? '禁用' : '启用'}
                      </Button>
                    </Td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {/* 最近注册 */}
        {stats && stats.recent_registrations.length > 0 && (
          <div className="mt-4">
            <div className="mb-2 flex items-center gap-2 text-[12px] font-medium text-slate-400">
              <UserPlus className="h-3.5 w-3.5" />
              最近注册
            </div>
            <div className="space-y-1.5">
              {stats.recent_registrations.map((r, i) => (
                <div
                  key={`${r.username}-${i}`}
                  className="flex items-center justify-between rounded-lg border border-border/50 dark:border-white/5 bg-card/60 dark:bg-white/[0.02] px-3 py-2"
                >
                  <div className="min-w-0">
                    <span className="text-[12px] font-medium text-foreground">{r.username}</span>
                    {r.email && <span className="ml-2 text-[11px] text-muted-foreground dark:text-slate-500">{r.email}</span>}
                  </div>
                  <span className="shrink-0 text-[11px] text-slate-600">{formatDateTime(r.created_at)}</span>
                </div>
              ))}
            </div>
          </div>
        )}
      </Section>

      {/* ── API Key 管理 ── */}
      <Section
        id="sec-keys"
        title="API Key 管理"
        description="全部 Skill Gateway Key，可冻结/解冻/禁用"
        icon={<KeyRound className="h-4 w-4" />}
      >
        <div className="mb-4 grid grid-cols-2 gap-3 sm:grid-cols-4">
          <InfoCard>
            <div className="text-[10px] text-slate-600">总 Key</div>
            <div className="font-mono text-[20px] font-semibold text-foreground">{keyStats.total}</div>
          </InfoCard>
          <InfoCard>
            <div className="text-[10px] text-slate-600">活跃</div>
            <div className="font-mono text-[20px] font-semibold text-emerald-400">{keyStats.active}</div>
          </InfoCard>
          <InfoCard>
            <div className="text-[10px] text-slate-600">已冻结</div>
            <div className="font-mono text-[20px] font-semibold text-amber-400">{keyStats.frozen}</div>
          </InfoCard>
          <InfoCard>
            <div className="text-[10px] text-slate-600">今日调用量</div>
            <div className="font-mono text-[20px] font-semibold text-cyan-400">{keyStats.totalCalls}</div>
          </InfoCard>
        </div>

        {keysLoading ? (
          <TableLoading label="加载 Key 列表…" />
        ) : keys.length === 0 ? (
          <EmptyState icon={<KeyRound className="h-8 w-8" />} title="暂无 API Key" desc="用户创建 Key 后会出现在这里" />
        ) : (
          <div className="overflow-x-auto rounded-xl border border-border/50 dark:border-white/5 bg-card/60 dark:bg-white/[0.02]">
            <table className="w-full min-w-[860px]">
              <thead className="border-b border-border/50 dark:border-white/5 bg-card/60 dark:bg-white/[0.02]">
                <tr>
                  <Th>Key 前缀</Th>
                  <Th>用户</Th>
                  <Th>Tier</Th>
                  <Th>状态</Th>
                  <Th>日限</Th>
                  <Th>今日用量</Th>
                  <Th>最后使用</Th>
                  <Th className="text-right">操作</Th>
                </tr>
              </thead>
              <tbody>
                {keys.map(k => (
                  <tr key={k.id} className="border-b border-border/40 dark:border-white/5 last:border-0 hover:bg-accent/30 dark:hover:bg-white/[0.02]">
                    <Td>
                      <code className="font-mono text-[12px] text-cyan-300">{k.key_prefix}…</code>
                    </Td>
                    <Td className="text-slate-400">{k.username || k.owner_label || '—'}</Td>
                    <Td>
                      <span
                        className={`inline-flex items-center rounded-full border px-2 py-0.5 text-[10px] ${TIER_BADGE[k.tier] || TIER_BADGE.free}`}
                      >
                        {TIER_LABEL[k.tier] || k.tier}
                      </span>
                    </Td>
                    <Td>
                      {k.status === 'active' ? (
                        <span className="inline-flex items-center rounded-full border border-emerald-500/20 bg-emerald-500/10 px-2 py-0.5 text-[10px] text-emerald-400">
                          正常
                        </span>
                      ) : (
                        <span
                          className={`inline-flex items-center rounded-full border px-2 py-0.5 text-[10px] ${
                            k.status === 'frozen'
                              ? 'border-amber-500/20 bg-amber-500/10 text-amber-400'
                              : 'border-red-500/20 bg-red-500/10 text-red-400'
                          }`}
                        >
                          {KEY_STATUS_LABEL[k.status] || k.status}
                        </span>
                      )}
                    </Td>
                    <Td className="font-mono">{k.daily_limit}</Td>
                    <Td className="font-mono">{k.used_today ?? '—'}</Td>
                    <Td className="whitespace-nowrap text-muted-foreground dark:text-slate-500">{formatDateTime(k.last_used_at) || '从未'}</Td>
                    <Td>
                      <div className="flex items-center justify-end gap-1">
                        {k.status === 'active' ? (
                          <Button
                            size="sm"
                            variant="outline"
                            className="h-7 px-2 text-[11px] hover:border-amber-500/30 hover:text-amber-400"
                            disabled={keyActionId === k.id}
                            onClick={() => void keyAction(k, 'freeze')}
                          >
                            {keyActionId === k.id ? <Loader2 className="h-3 w-3 animate-spin" /> : <Ban className="h-3 w-3" />}
                            冻结
                          </Button>
                        ) : k.status === 'frozen' ? (
                          <Button
                            size="sm"
                            variant="outline"
                            className="h-7 px-2 text-[11px] hover:border-emerald-500/30 hover:text-emerald-400"
                            disabled={keyActionId === k.id}
                            onClick={() => void keyAction(k, 'unfreeze')}
                          >
                            {keyActionId === k.id ? <Loader2 className="h-3 w-3 animate-spin" /> : <CheckCircle2 className="h-3 w-3" />}
                            解冻
                          </Button>
                        ) : null}
                        {k.status !== 'disabled' && (
                          <Button
                            size="sm"
                            variant="outline"
                            className="h-7 px-2 text-[11px] hover:border-red-500/30 hover:text-red-400"
                            disabled={keyActionId === k.id}
                            onClick={() => void keyAction(k, 'disable')}
                          >
                            禁用
                          </Button>
                        )}
                      </div>
                    </Td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Section>

      {/* ── 用量监控 ── */}
      <Section
        id="sec-usage"
        title="用量监控"
        description="按 Key / 按用户接口 / 按天 聚合近 7 天调用"
        icon={<BarChart3 className="h-4 w-4" />}
      >
        {/* 按 Key */}
        <div className="mb-2 text-[12px] font-medium text-slate-400">按 API Key</div>
        {skillUsageLoading ? (
          <TableLoading label="加载用量…" />
        ) : !skillUsage?.report?.length ? (
          <EmptyState icon={<BarChart3 className="h-8 w-8" />} title="近 7 天暂无调用" />
        ) : (
          <div className="mb-6 overflow-x-auto rounded-xl border border-border/50 dark:border-white/5 bg-card/60 dark:bg-white/[0.02]">
            <table className="w-full min-w-[640px]">
              <thead className="border-b border-border/50 dark:border-white/5 bg-card/60 dark:bg-white/[0.02]">
                <tr>
                  <Th>Key 前缀</Th>
                  <Th>Tier</Th>
                  <Th>调用次数</Th>
                  <Th>错误数</Th>
                  <Th>Top Skills</Th>
                </tr>
              </thead>
              <tbody>
                {skillUsage.report.map((r, i) => (
                  <tr key={`${r.key_prefix}-${i}`} className="border-b border-border/40 dark:border-white/5 last:border-0">
                    <Td>
                      <code className="font-mono text-cyan-300">{r.key_prefix}…</code>
                    </Td>
                    <Td>
                      <span className={`inline-flex rounded-full border px-2 py-0.5 text-[10px] ${TIER_BADGE[r.tier] || TIER_BADGE.free}`}>
                        {TIER_LABEL[r.tier] || r.tier}
                      </span>
                    </Td>
                    <Td className="font-mono text-foreground">{r.total}</Td>
                    <Td className={`font-mono ${r.errors > 0 ? 'text-red-400' : 'text-muted-foreground dark:text-slate-500'}`}>{r.errors}</Td>
                    <Td className="text-[11px] text-muted-foreground dark:text-slate-500">
                      {(r.top_skills || []).map(([name, n]) => `${name}(${n})`).join('、') || '—'}
                    </Td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {/* 按用户+接口 */}
        <div className="mb-2 text-[12px] font-medium text-slate-400">按用户 / 接口（高价值接口）</div>
        {hvUsageLoading ? (
          <TableLoading label="加载用量…" />
        ) : !hvUsage?.by_user_api?.length ? (
          <EmptyState icon={<Activity className="h-8 w-8" />} title="近 7 天暂无高价值接口调用" />
        ) : (
          <div className="mb-6 overflow-x-auto rounded-xl border border-border/50 dark:border-white/5 bg-card/60 dark:bg-white/[0.02]">
            <table className="w-full min-w-[640px]">
              <thead className="border-b border-border/50 dark:border-white/5 bg-card/60 dark:bg-white/[0.02]">
                <tr>
                  <Th>用户</Th>
                  <Th>接口</Th>
                  <Th>调用次数</Th>
                  <Th>最后调用</Th>
                </tr>
              </thead>
              <tbody>
                {hvUsage.by_user_api.map((r, i) => (
                  <tr key={`${r.user_id}-${r.api_name}-${i}`} className="border-b border-border/40 dark:border-white/5 last:border-0">
                    <Td className="text-foreground">{r.username || r.user_id}</Td>
                    <Td>
                      <code className="font-mono text-[11px] text-cyan-300">{r.api_name}</code>
                    </Td>
                    <Td className="font-mono text-foreground">{r.calls}</Td>
                    <Td className="whitespace-nowrap text-muted-foreground dark:text-slate-500">{formatDateTime(r.last_at) || '—'}</Td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {/* 按天 */}
        <div className="mb-2 text-[12px] font-medium text-slate-400">按天</div>
        {hvUsageLoading ? (
          <TableLoading label="加载用量…" />
        ) : !hvUsage?.by_day?.length ? (
          <EmptyState icon={<BarChart3 className="h-8 w-8" />} title="近 7 天暂无按天数据" />
        ) : (
          <div className="overflow-x-auto rounded-xl border border-border/50 dark:border-white/5 bg-card/60 dark:bg-white/[0.02]">
            <table className="w-full min-w-[480px]">
              <thead className="border-b border-border/50 dark:border-white/5 bg-card/60 dark:bg-white/[0.02]">
                <tr>
                  <Th>日期</Th>
                  <Th>接口</Th>
                  <Th>调用次数</Th>
                </tr>
              </thead>
              <tbody>
                {hvUsage.by_day.map((r, i) => (
                  <tr key={`${r.day}-${r.api_name}-${i}`} className="border-b border-border/40 dark:border-white/5 last:border-0">
                    <Td className="whitespace-nowrap text-slate-400">{r.day}</Td>
                    <Td>
                      <code className="font-mono text-[11px] text-cyan-300">{r.api_name}</code>
                    </Td>
                    <Td className="font-mono text-foreground">{r.calls}</Td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Section>

      {/* ── Pro 申请审核 ── */}
      <Section
        id="sec-pro"
        title="Pro 申请审核"
        description="批准后用户升为 Pro，并同步升级其 active Key"
        icon={<BadgeCheck className="h-4 w-4" />}
        action={
          pendingCount > 0 ? (
            <span className="inline-flex items-center rounded-full border border-amber-500/20 bg-amber-500/10 px-2 py-0.5 text-[10px] text-amber-400">
              {pendingCount} 待审核
            </span>
          ) : undefined
        }
      >
        {proLoading ? (
          <TableLoading label="加载申请列表…" />
        ) : !proApps?.applications?.length ? (
          <EmptyState icon={<BadgeCheck className="h-8 w-8" />} title="暂无 Pro 申请" desc="用户提交申请后会出现在这里" />
        ) : (
          <div className="overflow-x-auto rounded-xl border border-border/50 dark:border-white/5 bg-card/60 dark:bg-white/[0.02]">
            <table className="w-full min-w-[720px]">
              <thead className="border-b border-border/50 dark:border-white/5 bg-card/60 dark:bg-white/[0.02]">
                <tr>
                  <Th>用户名</Th>
                  <Th>理由</Th>
                  <Th>状态</Th>
                  <Th>申请时间</Th>
                  <Th className="text-right">操作</Th>
                </tr>
              </thead>
              <tbody>
                {proApps.applications.map(a => (
                  <tr key={a.id} className="border-b border-border/40 dark:border-white/5 last:border-0 hover:bg-accent/30 dark:hover:bg-white/[0.02]">
                    <Td className="font-medium text-foreground">{a.username}</Td>
                    <Td className="max-w-[280px]">
                      <span className="line-clamp-2 text-[11px] text-slate-400" title={a.reason}>
                        {a.reason || '—'}
                      </span>
                    </Td>
                    <Td>
                      <span
                        className={`inline-flex items-center rounded-full border px-2 py-0.5 text-[10px] ${APP_STATUS_BADGE[a.status] || APP_STATUS_BADGE.pending}`}
                      >
                        {APP_STATUS_LABEL[a.status] || a.status}
                      </span>
                    </Td>
                    <Td className="whitespace-nowrap text-muted-foreground dark:text-slate-500">{formatDateTime(a.created_at) || '—'}</Td>
                    <Td className="text-right">
                      {a.status === 'pending' ? (
                        <div className="flex items-center justify-end gap-1">
                          <Button
                            size="sm"
                            variant="outline"
                            className="h-7 px-2 text-[11px] hover:border-emerald-500/30 hover:text-emerald-400"
                            disabled={appActionId === a.id}
                            onClick={() => void reviewApp(a, 'approve')}
                          >
                            {appActionId === a.id ? (
                              <Loader2 className="h-3 w-3 animate-spin" />
                            ) : (
                              <CheckCircle2 className="h-3 w-3" />
                            )}
                            批准
                          </Button>
                          <Button
                            size="sm"
                            variant="outline"
                            className="h-7 px-2 text-[11px] hover:border-red-500/30 hover:text-red-400"
                            disabled={appActionId === a.id}
                            onClick={() => void reviewApp(a, 'reject')}
                          >
                            拒绝
                          </Button>
                        </div>
                      ) : (
                        <span className="text-[11px] text-slate-600">
                          {a.reviewed_by ? `${a.reviewed_by} · ` : ''}
                          {formatDateTime(a.reviewed_at)}
                        </span>
                      )}
                    </Td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Section>
    </DevPageLayout>
  )
}
