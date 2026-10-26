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
  Ticket,
  Plus,
  RotateCcw,
} from 'lucide-react'
import { fetchAPI } from '@panwatch/api'
import Pager from '@/components/Pager'
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
import { InviteCodesSection } from '@/components/admin/InviteCodesSection'

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
  /** 软删除(注销宽限期内): 可恢复 */
  is_deleted?: boolean
  deleted_at?: string | null
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

interface UsageLatencyRow {
  calls: number
  errors: number
  /** **null = 没有可统计的样本**(全未记录耗时), 不是 0ms —— 页面显示 `--` */
  p50_ms: number | null
  p95_ms: number | null
  /** 参与分位数的样本数(已记录耗时的调用数) */
  latency_samples: number
  day?: string
  skill?: string
  users?: number
}

interface UsageLatencyResp {
  days: number
  as_of: string
  totals: { calls: number; errors: number; p50_ms: number | null; p95_ms: number | null }
  by_day: UsageLatencyRow[]
  by_skill: UsageLatencyRow[]
  /** 后端给的口径说明(未记录耗时多少条等), 页面原样展示 */
  latency_note: string
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
  { id: 'invite', label: '邀请码管理', icon: <Ticket className="h-3.5 w-3.5" />, anchor: 'sec-invite' },
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
  free: 'bg-slate-500/10 text-muted-foreground border-slate-500/20',
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

/** 注销宽限期截止日(注销时间 + 30 天, 与后端 purge 口径一致), 返回 YYYY-MM-DD。 */
function graceDeadline(deletedAt: string): string {
  const t = new Date(deletedAt).getTime()
  if (!Number.isFinite(t)) return '—'
  const d = new Date(t + 30 * 24 * 60 * 60 * 1000)
  const mm = String(d.getMonth() + 1).padStart(2, '0')
  const dd = String(d.getDate()).padStart(2, '0')
  return `${d.getFullYear()}-${mm}-${dd}`
}

function TableLoading({ label = '加载中…' }: { label?: string }) {
  return (
    <div className="flex items-center justify-center gap-2 rounded-xl border border-border dark:border-white/5 bg-s2 dark:bg-white/[0.01] py-10 text-[12px] text-muted-foreground">
      <Loader2 className="h-4 w-4 animate-spin" />
      {label}
    </div>
  )
}

function Th({ children, className = '' }: { children: React.ReactNode; className?: string }) {
  return (
    <th className={`whitespace-nowrap px-3 py-2 text-left text-[12px] font-medium text-muted-foreground ${className}`}>
      {children}
    </th>
  )
}

function Td({ children, className = '', title }: { children: React.ReactNode; className?: string; title?: string }) {
  // title 用于把"为什么是 --"(例如没有已记录耗时的样本)挂在单元格上, 不靠猜
  return (
    <td className={`px-3 py-2 text-[12px] text-foreground ${className}`} title={title}>
      {children}
    </td>
  )
}

export default function AdminPage() {
  const { toast } = useToast()
  const { t } = useI18n()
  const [activeSection, setActiveSection] = useState('users')
  const [keyActionId, setKeyActionId] = useState<number | null>(null)
  const [userActionId, setUserActionId] = useState<string | null>(null)
  const [appActionId, setAppActionId] = useState<number | null>(null)
  // 新建用户(2026-10-23: 后台补齐建号能力, 与设置页 UserManagement 对齐)
  const [showCreate, setShowCreate] = useState(false)
  const [newUser, setNewUser] = useState({ username: '', password: '', role: 'member', email: '' })
  const [creating, setCreating] = useState(false)
  // 重置密码 / Pro 审核备注
  const [resetTarget, setResetTarget] = useState<AdminUser | null>(null)
  const [resetPwd, setResetPwd] = useState('')
  const [reviewNotes, setReviewNotes] = useState<Record<number, string>>({})

  const {
    data: userList,
    isLoading: usersLoading,
    refetch: refetchUsers,
  } = useApiQuery<AdminUserList>(['admin-users'], '/users/admin/list')
  const users = useMemo(() => userList?.users ?? [], [userList])

  const { data: stats, refetch: refetchStats } = useApiQuery<AdminStats>(['admin-stats'], '/users/admin/stats')

  // 后台三类长列表分页(2026-10-24): /admin 页高 20598px、2958 个文本节点, 一屏根本读不完。
  // 全量数据仍一次取回(后端已支持 limit/offset, 数据量涨了再切服务端分页), 这里按 20 条/页切。
  const PAGE_SIZE = 20
  const [usersPage, setUsersPage] = useState(1)
  const [keysPage, setKeysPage] = useState(1)
  const [proPage, setProPage] = useState(1)
  const paged = <T,>(rows: T[], page: number): T[] =>
    rows.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE)

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

  // B7(2026-09-19): 耗时视图 —— 回答"慢不慢"(调用次数已在上面那张表里)
  const { data: usageLatency, isLoading: latencyLoading } = useApiQuery<UsageLatencyResp>(
    ['admin-usage-latency'],
    '/admin/skills/usage/latency?days=7',
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

  // ── 新建用户 / 重置密码 / 恢复账号(2026-10-23 补齐后台能力) ──
  const createUser = async () => {
    const username = newUser.username.trim()
    if (username.length < 2) return toast('用户名至少 2 位', 'error')
    if (newUser.password.length < 8) return toast('密码至少 8 位', 'error')
    setCreating(true)
    try {
      await fetchAPI('/users/admin/create', {
        method: 'POST',
        body: JSON.stringify({
          username,
          password: newUser.password,
          role: newUser.role,
          email: newUser.email.trim() || null,
        }),
      })
      toast(`已创建用户 ${username}`, 'success')
      setShowCreate(false)
      setNewUser({ username: '', password: '', role: 'member', email: '' })
      void refetchUsers()
      void refetchStats()
    } catch (e: any) {
      toast(e?.message || '创建失败', 'error')
    } finally {
      setCreating(false)
    }
  }

  const submitResetPassword = async () => {
    if (!resetTarget) return
    if (resetPwd.length < 8) return toast('密码至少 8 位', 'error')
    if (!confirm(`确定重置 ${resetTarget.username} 的密码？该用户当前的登录会话会立即失效。`)) return
    setUserActionId(resetTarget.id)
    try {
      await fetchAPI(`/users/admin/${resetTarget.id}/reset-password`, {
        method: 'POST',
        body: JSON.stringify({ new_password: resetPwd }),
      })
      toast(`${resetTarget.username} 密码已重置`, 'success')
      setResetTarget(null)
      setResetPwd('')
    } catch (e: any) {
      toast(e?.message || '重置失败', 'error')
    } finally {
      setUserActionId(null)
    }
  }

  const restoreUser = async (u: AdminUser) => {
    const until = u.deleted_at ? formatDateTime(u.deleted_at) : ''
    if (!confirm(`确定恢复已注销账号 ${u.username}？${until ? `（注销于 ${until}，仍在 30 天宽限期）` : ''}`)) return
    setUserActionId(u.id)
    try {
      await fetchAPI(`/users/admin/${u.id}/restore`, { method: 'POST' })
      toast(`已恢复 ${u.username}`, 'success')
      void refetchUsers()
      void refetchStats()
    } catch (e: any) {
      toast(e?.message || '恢复失败', 'error')
    } finally {
      setUserActionId(null)
    }
  }

  // ── Key 操作 ──
  const keyAction = async (
    key: AdminKey,
    action: 'freeze' | 'unfreeze' | 'disable' | 'set_tier' | 'set_limit',
    extra: { tier?: string; daily_limit?: number } = {},
  ) => {
    const label =
      action === 'freeze' ? '冻结'
        : action === 'unfreeze' ? '解冻'
          : action === 'disable' ? '禁用'
            : action === 'set_tier' ? '调档' : '改日限'
    if (action === 'disable' && !confirm(`确定禁用 Key ${key.key_prefix}...？禁用后不可恢复为 active，需新建 Key。`)) return
    if (action === 'set_tier' && !confirm(`确定把 Key ${key.key_prefix}... 调到 ${TIER_LABEL[extra.tier || ''] || extra.tier} 档？日限会同步改为该档默认值。`)) return
    if (action === 'set_limit' && !confirm(`确定把 Key ${key.key_prefix}... 的日限改为 ${extra.daily_limit} 次/天？`)) return
    setKeyActionId(key.id)
    try {
      await fetchAPI('/admin/skills/keys/action', {
        method: 'POST',
        body: JSON.stringify({ key_id: key.id, action, ...extra }),
      })
      toast(`Key ${key.key_prefix}... 已${label}`, 'success')
      void refetchKeys()
    } catch (e: any) {
      toast(e?.message || `${label}失败`, 'error')
    } finally {
      setKeyActionId(null)
    }
  }

  const promptKeyLimit = (k: AdminKey) => {
    const raw = prompt(`Key ${k.key_prefix}... 的新日限（1-100000）`, String(k.daily_limit))
    if (raw === null) return
    const n = Number(raw.trim())
    if (!Number.isInteger(n) || n < 1 || n > 100000) return toast('日限需为 1-100000 的整数', 'error')
    void keyAction(k, 'set_limit', { daily_limit: n })
  }

  // ── Pro 审核 ──
  const reviewApp = async (app: ProApplication, action: 'approve' | 'reject') => {
    const label = action === 'approve' ? '批准' : '拒绝'
    if (!confirm(`确定${label} ${app.username} 的 Pro 申请？`)) return
    setAppActionId(app.id)
    try {
      await fetchAPI(`/pro/admin/${action}`, {
        method: 'POST',
        body: JSON.stringify({ application_id: app.id, note: (reviewNotes[app.id] || '').trim() }),
      })
      toast(`已${label} ${app.username} 的 Pro 申请`, 'success')
      setReviewNotes(prev => {
        const next = { ...prev }
        delete next[app.id]
        return next
      })
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
      {/* ── 邀请码管理(2026-09-19 内部使用模式) ── */}
      <InviteCodesSection />

      {/* ── 用户管理 ── */}
      <Section
        id="sec-users"
        title="用户管理"
        description="查看全部账号，启用/禁用、改角色、重置密码、新建用户；已注销账号可在 30 天宽限期内恢复"
        icon={<Users className="h-4 w-4" />}
        action={
          <Button size="sm" variant="outline" className="h-8" onClick={() => setShowCreate(v => !v)}>
            <Plus className="h-3.5 w-3.5" />
            <span className="hidden sm:inline">新建用户</span>
          </Button>
        }
      >
        {showCreate && (
          <div className="mb-4 space-y-2 rounded-xl border border-border dark:border-white/10 bg-s1 p-3">
            <div className="grid gap-2 sm:grid-cols-4">
              <input
                className="h-8 rounded-md border border-border dark:border-white/10 bg-background dark:bg-[#0d0d18] px-2 text-[12px] outline-none focus:border-primary/40"
                placeholder="用户名（2-20 位字母/数字）"
                value={newUser.username}
                onChange={e => setNewUser(v => ({ ...v, username: e.target.value }))}
              />
              <input
                className="h-8 rounded-md border border-border dark:border-white/10 bg-background dark:bg-[#0d0d18] px-2 text-[12px] outline-none focus:border-primary/40"
                placeholder="密码（至少 8 位）"
                type="password"
                value={newUser.password}
                onChange={e => setNewUser(v => ({ ...v, password: e.target.value }))}
              />
              <input
                className="h-8 rounded-md border border-border dark:border-white/10 bg-background dark:bg-[#0d0d18] px-2 text-[12px] outline-none focus:border-primary/40"
                placeholder="邮箱（可空）"
                value={newUser.email}
                onChange={e => setNewUser(v => ({ ...v, email: e.target.value }))}
              />
              <select
                className="h-8 rounded-md border border-border dark:border-white/10 bg-background dark:bg-[#0d0d18] px-2 text-[12px] outline-none focus:border-primary/40"
                value={newUser.role}
                onChange={e => setNewUser(v => ({ ...v, role: e.target.value }))}
              >
                <option value="guest">访客</option>
                <option value="member">成员</option>
                <option value="pro">Pro</option>
                <option value="owner">管理员</option>
              </select>
            </div>
            <div className="flex items-center justify-end gap-2">
              <Button
                size="sm"
                variant="ghost"
                className="h-7 text-[12px]"
                onClick={() => {
                  setShowCreate(false)
                  setNewUser({ username: '', password: '', role: 'member', email: '' })
                }}
              >
                取消
              </Button>
              <Button size="sm" className="h-7 text-[12px]" disabled={creating} onClick={() => void createUser()}>
                {creating ? <Loader2 className="h-3 w-3 animate-spin" /> : <Plus className="h-3 w-3" />}
                创建
              </Button>
            </div>
          </div>
        )}

        {/* 统计卡片 */}
        <div className="mb-4 grid grid-cols-2 gap-3 sm:grid-cols-4">
          <InfoCard>
            <div className="text-[12px] text-muted-foreground">总用户</div>
            <div className="font-mono text-[20px] font-semibold text-foreground">{stats?.total_users ?? '—'}</div>
          </InfoCard>
          <InfoCard>
            <div className="text-[12px] text-muted-foreground">活跃用户</div>
            <div className="font-mono text-[20px] font-semibold text-cyan-400">{stats?.active_users ?? '—'}</div>
          </InfoCard>
          <InfoCard>
            <div className="text-[12px] text-muted-foreground">今日新增</div>
            <div className="font-mono text-[20px] font-semibold text-emerald-400">{stats?.new_today ?? '—'}</div>
          </InfoCard>
          <InfoCard>
            <div className="text-[12px] text-muted-foreground">7 日新增</div>
            <div className="font-mono text-[20px] font-semibold text-foreground">{stats?.new_7d ?? '—'}</div>
          </InfoCard>
        </div>

        {stats?.by_role && (
          <div className="mb-4 flex flex-wrap gap-2">
            {Object.entries(stats.by_role).map(([role, n]) => (
              <span
                key={role}
                className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[12px] ${ROLE_BADGE[role] || ROLE_BADGE.member}`}
              >
                {ROLE_LABEL[role] || role}
                <span className="font-mono">{n}</span>
              </span>
            ))}
            {stats.new_30d != null && (
              <span className="inline-flex items-center rounded-full border border-border dark:border-white/10 bg-s2 dark:bg-white/[0.03] px-2 py-0.5 text-[12px] text-muted-foreground">
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
          <div className="max-h-[70vh] overflow-auto rounded-xl border border-border dark:border-white/5 bg-card dark:bg-white/[0.02] [content-visibility:auto] [contain-intrinsic-size:auto_520px]">
            <table className="w-full min-w-[720px]">
              <thead className="sticky top-0 z-10 border-b border-border dark:border-white/5 bg-card">
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
                {paged(users, usersPage).map(u => (
                  <tr key={u.id} className="border-b border-border dark:border-white/5 last:border-0 hover:bg-s2 dark:hover:bg-white/[0.02]">
                    <Td className="font-medium text-foreground">{u.username}</Td>
                    <Td className="text-muted-foreground">{u.email || '—'}</Td>
                    <Td>
                      <select
                        className="rounded-md border border-border dark:border-white/10 bg-background dark:bg-[#0d0d18] px-2 py-1 text-[12px] text-foreground outline-none focus:border-primary/40 dark:focus:border-cyan-500/40"
                        value={u.role}
                        disabled={userActionId === u.id}
                        onChange={e => void changeRole(u, e.target.value)}
                      >
                        <option value="guest">访客</option>
                        <option value="member">成员</option>
                        <option value="pro">Pro</option>
                        <option value="owner">管理员</option>
                      </select>
                    </Td>
                    <Td>
                      {u.is_deleted ? (
                        <span className="inline-flex flex-col gap-0.5">
                          <span className="inline-flex w-fit items-center rounded-full border border-red-500/25 bg-red-500/10 px-2 py-0.5 text-[12px] text-red-400">
                            已注销
                          </span>
                          {u.deleted_at && (
                            <span className="text-[12px] text-muted-foreground">宽限期至 {graceDeadline(u.deleted_at)}</span>
                          )}
                        </span>
                      ) : u.is_active ? (
                        <span className="inline-flex items-center rounded-full border border-emerald-500/20 bg-emerald-500/10 px-2 py-0.5 text-[12px] text-emerald-400">
                          启用
                        </span>
                      ) : (
                        <span className="inline-flex items-center rounded-full border border-red-500/20 bg-red-500/10 px-2 py-0.5 text-[12px] text-red-400">
                          禁用
                        </span>
                      )}
                    </Td>
                    <Td className="whitespace-nowrap text-muted-foreground">{formatDateTime(u.created_at) || '—'}</Td>
                    <Td className="whitespace-nowrap text-muted-foreground">{formatDateTime(u.last_login) || '从未'}</Td>
                    <Td className="text-right">
                      <div className="flex items-center justify-end gap-1">
                        {u.is_deleted ? (
                          <Button
                            size="sm"
                            variant="outline"
                            className="h-7 px-2 text-[12px] hover:border-emerald-500/30 hover:text-emerald-400"
                            disabled={userActionId === u.id}
                            onClick={() => void restoreUser(u)}
                          >
                            {userActionId === u.id ? <Loader2 className="h-3 w-3 animate-spin" /> : <RotateCcw className="h-3 w-3" />}
                            恢复
                          </Button>
                        ) : (
                          <>
                            <Button
                              size="sm"
                              variant="outline"
                              className="h-7 px-2 text-[12px]"
                              disabled={userActionId === u.id}
                              onClick={() => {
                                setResetTarget(u)
                                setResetPwd('')
                              }}
                            >
                              <KeyRound className="h-3 w-3" />
                              重置密码
                            </Button>
                            <Button
                              size="sm"
                              variant="outline"
                              className={`h-7 px-2 text-[12px] ${u.is_active ? 'hover:border-red-500/30 hover:text-red-400' : 'hover:border-emerald-500/30 hover:text-emerald-400'}`}
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
                          </>
                        )}
                      </div>
                    </Td>
                  </tr>
                ))}
              </tbody>
            </table>
            <Pager total={users.length} page={usersPage} pageSize={PAGE_SIZE} onPage={setUsersPage} unit="人" />
          </div>
        )}

        {resetTarget && (
          <div className="mt-3 space-y-2 rounded-xl border border-border dark:border-white/10 bg-s1 p-3">
            <div className="text-[12px] font-medium text-foreground">重置 {resetTarget.username} 的密码</div>
            <div className="flex flex-wrap items-center gap-2">
              <input
                className="h-8 min-w-[220px] flex-1 rounded-md border border-border dark:border-white/10 bg-background dark:bg-[#0d0d18] px-2 text-[12px] outline-none focus:border-primary/40"
                placeholder="新密码（至少 8 位）"
                type="password"
                value={resetPwd}
                onChange={e => setResetPwd(e.target.value)}
              />
              <Button size="sm" className="h-8 text-[12px]" disabled={userActionId === resetTarget.id} onClick={() => void submitResetPassword()}>
                确定重置
              </Button>
              <Button size="sm" variant="ghost" className="h-8 text-[12px]" onClick={() => { setResetTarget(null); setResetPwd('') }}>
                取消
              </Button>
            </div>
            <div className="text-[12px] text-muted-foreground">重置后该用户旧登录会话立即失效，需用新密码重新登录。</div>
          </div>
        )}

        {/* 最近注册 */}
        {stats && stats.recent_registrations.length > 0 && (
          <div className="mt-4">
            <div className="mb-2 flex items-center gap-2 text-[12px] font-medium text-muted-foreground">
              <UserPlus className="h-3.5 w-3.5" />
              最近注册
            </div>
            <div className="space-y-1.5">
              {stats.recent_registrations.map((r, i) => (
                <div
                  key={`${r.username}-${i}`}
                  className="flex items-center justify-between rounded-lg border border-border dark:border-white/5 bg-card dark:bg-white/[0.02] px-3 py-2"
                >
                  <div className="min-w-0">
                    <span className="text-[12px] font-medium text-foreground">{r.username}</span>
                    {r.email && <span className="ml-2 text-[12px] text-muted-foreground">{r.email}</span>}
                  </div>
                  <span className="shrink-0 text-[12px] text-muted-foreground">{formatDateTime(r.created_at)}</span>
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
            <div className="text-[12px] text-muted-foreground">总 Key</div>
            <div className="font-mono text-[20px] font-semibold text-foreground">{keyStats.total}</div>
          </InfoCard>
          <InfoCard>
            <div className="text-[12px] text-muted-foreground">活跃</div>
            <div className="font-mono text-[20px] font-semibold text-emerald-400">{keyStats.active}</div>
          </InfoCard>
          <InfoCard>
            <div className="text-[12px] text-muted-foreground">已冻结</div>
            <div className="font-mono text-[20px] font-semibold text-amber-400">{keyStats.frozen}</div>
          </InfoCard>
          <InfoCard>
            <div className="text-[12px] text-muted-foreground">今日调用量</div>
            <div className="font-mono text-[20px] font-semibold text-cyan-400">{keyStats.totalCalls}</div>
          </InfoCard>
        </div>

        {keysLoading ? (
          <TableLoading label="加载 Key 列表…" />
        ) : keys.length === 0 ? (
          <EmptyState icon={<KeyRound className="h-8 w-8" />} title="暂无 API Key" desc="用户创建 Key 后会出现在这里" />
        ) : (
          <div className="overflow-x-auto rounded-xl border border-border dark:border-white/5 bg-card dark:bg-white/[0.02] [content-visibility:auto] [contain-intrinsic-size:auto_520px]">
            <table className="w-full min-w-[860px]">
              <thead className="sticky top-0 z-10 border-b border-border dark:border-white/5 bg-card">
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
                {paged(keys, keysPage).map(k => (
                  <tr key={k.id} className="border-b border-border dark:border-white/5 last:border-0 hover:bg-s2 dark:hover:bg-white/[0.02]">
                    <Td>
                      <code className="font-mono text-[12px] text-cyan-300">{k.key_prefix}…</code>
                    </Td>
                    <Td className="text-muted-foreground">{k.username || k.owner_label || '—'}</Td>
                    <Td>
                      <span
                        className={`inline-flex items-center rounded-full border px-2 py-0.5 text-[12px] ${TIER_BADGE[k.tier] || TIER_BADGE.free}`}
                      >
                        {TIER_LABEL[k.tier] || k.tier}
                      </span>
                    </Td>
                    <Td>
                      {k.status === 'active' ? (
                        <span className="inline-flex items-center rounded-full border border-emerald-500/20 bg-emerald-500/10 px-2 py-0.5 text-[12px] text-emerald-400">
                          正常
                        </span>
                      ) : (
                        <span
                          className={`inline-flex items-center rounded-full border px-2 py-0.5 text-[12px] ${
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
                    <Td className="whitespace-nowrap text-muted-foreground">{formatDateTime(k.last_used_at) || '从未'}</Td>
                    <Td>
                      <div className="flex items-center justify-end gap-1">
                        <select
                          className="h-7 rounded-md border border-border dark:border-white/10 bg-background dark:bg-[#0d0d18] px-1.5 text-[12px] text-foreground outline-none focus:border-primary/40 dark:focus:border-cyan-500/40"
                          value={k.tier}
                          title="调整档位（日限同步为该档默认值）"
                          disabled={keyActionId === k.id}
                          onChange={e => void keyAction(k, 'set_tier', { tier: e.target.value })}
                        >
                          <option value="free">免费</option>
                          <option value="trial">试用</option>
                          <option value="pro">Pro</option>
                        </select>
                        <Button
                          size="sm"
                          variant="outline"
                          className="h-7 px-2 text-[12px]"
                          disabled={keyActionId === k.id}
                          onClick={() => promptKeyLimit(k)}
                        >
                          改日限
                        </Button>
                        {k.status === 'active' ? (
                          <Button
                            size="sm"
                            variant="outline"
                            className="h-7 px-2 text-[12px] hover:border-amber-500/30 hover:text-amber-400"
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
                            className="h-7 px-2 text-[12px] hover:border-emerald-500/30 hover:text-emerald-400"
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
                            className="h-7 px-2 text-[12px] hover:border-red-500/30 hover:text-red-400"
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
            <Pager total={keys.length} page={keysPage} pageSize={PAGE_SIZE} onPage={setKeysPage} unit="个" />
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
        <div className="mb-2 text-[12px] font-medium text-muted-foreground">按 API Key</div>
        {skillUsageLoading ? (
          <TableLoading label="加载用量…" />
        ) : !skillUsage?.report?.length ? (
          <EmptyState icon={<BarChart3 className="h-8 w-8" />} title="近 7 天暂无调用" />
        ) : (
          <div className="mb-6 overflow-x-auto rounded-xl border border-border dark:border-white/5 bg-card dark:bg-white/[0.02] [content-visibility:auto] [contain-intrinsic-size:auto_520px]">
            <table className="w-full min-w-[640px]">
              <thead className="sticky top-0 z-10 border-b border-border dark:border-white/5 bg-card">
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
                  <tr key={`${r.key_prefix}-${i}`} className="border-b border-border dark:border-white/5 last:border-0">
                    <Td>
                      <code className="font-mono text-cyan-300">{r.key_prefix}…</code>
                    </Td>
                    <Td>
                      <span className={`inline-flex rounded-full border px-2 py-0.5 text-[12px] ${TIER_BADGE[r.tier] || TIER_BADGE.free}`}>
                        {TIER_LABEL[r.tier] || r.tier}
                      </span>
                    </Td>
                    <Td className="font-mono text-foreground">{r.total}</Td>
                    <Td className={`font-mono ${r.errors > 0 ? 'text-red-400' : 'text-muted-foreground'}`}>{r.errors}</Td>
                    <Td className="text-[12px] text-muted-foreground">
                      {(r.top_skills || []).map(([name, n]) => `${name}(${n})`).join('、') || '—'}
                    </Td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {/* 按 skill / 按天 · 耗时(B7 2026-09-19) —— 与上面两张表的分工: 那两张数次数, 这张回答"慢不慢" */}
        <div className="mb-2 mt-6 text-[12px] font-medium text-muted-foreground">按 Skill / 按天 · 耗时</div>
        {latencyLoading ? (
          <TableLoading label="加载耗时…" />
        ) : !usageLatency?.by_skill?.length ? (
          <EmptyState icon={<BarChart3 className="h-8 w-8" />} title="近 7 天暂无调用" />
        ) : (
          <div className="mb-6 space-y-4">
            <div className="overflow-x-auto rounded-xl border border-border dark:border-white/5 bg-card dark:bg-white/[0.02] [content-visibility:auto] [contain-intrinsic-size:auto_520px]">
              <table className="w-full min-w-[560px]">
                <thead className="sticky top-0 z-10 border-b border-border dark:border-white/5 bg-card">
                  <tr>
                    <Th>Skill</Th>
                    <Th>调用</Th>
                    <Th>错误</Th>
                    <Th>P50</Th>
                    <Th>P95</Th>
                    <Th>样本</Th>
                  </tr>
                </thead>
                <tbody>
                  {usageLatency.by_skill.map((r) => (
                    <tr key={r.skill} className="border-b border-border dark:border-white/5 last:border-0">
                      <Td><code className="font-mono text-cyan-300">{r.skill}</code></Td>
                      <Td className="font-mono text-foreground">{r.calls}{typeof r.users === 'number' ? ` · ${r.users}人` : ''}</Td>
                      <Td className={`font-mono ${r.errors > 0 ? 'text-red-400' : 'text-muted-foreground'}`}>{r.errors}</Td>
                      {/* null = 没样本可算(不是 0ms): 显示 -- 并把原因放进 title */}
                      <Td className="font-mono text-muted-foreground" title={r.p50_ms === null ? '没有已记录耗时的调用, 无法计算' : ''}>
                        {r.p50_ms === null ? '--' : `${r.p50_ms} ms`}
                      </Td>
                      <Td className="font-mono text-muted-foreground" title={r.p95_ms === null ? '没有已记录耗时的调用, 无法计算' : ''}>
                        {r.p95_ms === null ? '--' : `${r.p95_ms} ms`}
                      </Td>
                      <Td className="font-mono text-[12px] text-muted-foreground">{r.latency_samples}</Td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="overflow-x-auto rounded-xl border border-border dark:border-white/5 bg-card dark:bg-white/[0.02] [content-visibility:auto] [contain-intrinsic-size:auto_520px]">
              <table className="w-full min-w-[420px]">
                <thead className="sticky top-0 z-10 border-b border-border dark:border-white/5 bg-card">
                  <tr>
                    <Th>日期</Th>
                    <Th>调用</Th>
                    <Th>错误</Th>
                    <Th>P50</Th>
                    <Th>P95</Th>
                  </tr>
                </thead>
                <tbody>
                  {usageLatency.by_day.map((r) => (
                    <tr key={r.day} className="border-b border-border dark:border-white/5 last:border-0">
                      <Td className="font-mono text-foreground">{r.day}</Td>
                      <Td className="font-mono text-foreground">{r.calls}</Td>
                      <Td className={`font-mono ${r.errors > 0 ? 'text-red-400' : 'text-muted-foreground'}`}>{r.errors}</Td>
                      <Td className="font-mono text-muted-foreground">{r.p50_ms === null ? '--' : `${r.p50_ms} ms`}</Td>
                      <Td className="font-mono text-muted-foreground">{r.p95_ms === null ? '--' : `${r.p95_ms} ms`}</Td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {/* 口径说明由后端给("未记录 ≠ 0ms"), 页面不自己措辞 */}
            <div className="text-[12px] text-muted-foreground">{usageLatency.latency_note}</div>
          </div>
        )}

        {/* 按用户+接口 */}
        <div className="mb-2 text-[12px] font-medium text-muted-foreground">按用户 / 接口（高价值接口）</div>
        {hvUsageLoading ? (
          <TableLoading label="加载用量…" />
        ) : !hvUsage?.by_user_api?.length ? (
          <EmptyState icon={<Activity className="h-8 w-8" />} title="近 7 天暂无高价值接口调用" />
        ) : (
          <div className="mb-6 overflow-x-auto rounded-xl border border-border dark:border-white/5 bg-card dark:bg-white/[0.02] [content-visibility:auto] [contain-intrinsic-size:auto_520px]">
            <table className="w-full min-w-[640px]">
              <thead className="sticky top-0 z-10 border-b border-border dark:border-white/5 bg-card">
                <tr>
                  <Th>用户</Th>
                  <Th>接口</Th>
                  <Th>调用次数</Th>
                  <Th>最后调用</Th>
                </tr>
              </thead>
              <tbody>
                {hvUsage.by_user_api.map((r, i) => (
                  <tr key={`${r.user_id}-${r.api_name}-${i}`} className="border-b border-border dark:border-white/5 last:border-0">
                    <Td className="text-foreground">{r.username || r.user_id}</Td>
                    <Td>
                      <code className="font-mono text-[12px] text-cyan-300">{r.api_name}</code>
                    </Td>
                    <Td className="font-mono text-foreground">{r.calls}</Td>
                    <Td className="whitespace-nowrap text-muted-foreground">{formatDateTime(r.last_at) || '—'}</Td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {/* 按天 */}
        <div className="mb-2 text-[12px] font-medium text-muted-foreground">按天</div>
        {hvUsageLoading ? (
          <TableLoading label="加载用量…" />
        ) : !hvUsage?.by_day?.length ? (
          <EmptyState icon={<BarChart3 className="h-8 w-8" />} title="近 7 天暂无按天数据" />
        ) : (
          <div className="overflow-x-auto rounded-xl border border-border dark:border-white/5 bg-card dark:bg-white/[0.02] [content-visibility:auto] [contain-intrinsic-size:auto_520px]">
            <table className="w-full min-w-[480px]">
              <thead className="sticky top-0 z-10 border-b border-border dark:border-white/5 bg-card">
                <tr>
                  <Th>日期</Th>
                  <Th>接口</Th>
                  <Th>调用次数</Th>
                </tr>
              </thead>
              <tbody>
                {hvUsage.by_day.map((r, i) => (
                  <tr key={`${r.day}-${r.api_name}-${i}`} className="border-b border-border dark:border-white/5 last:border-0">
                    <Td className="whitespace-nowrap text-muted-foreground">{r.day}</Td>
                    <Td>
                      <code className="font-mono text-[12px] text-cyan-300">{r.api_name}</code>
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
            <span className="inline-flex items-center rounded-full border border-amber-500/20 bg-amber-500/10 px-2 py-0.5 text-[12px] text-amber-400">
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
          <div className="overflow-x-auto rounded-xl border border-border dark:border-white/5 bg-card dark:bg-white/[0.02] [content-visibility:auto] [contain-intrinsic-size:auto_520px]">
            <table className="w-full min-w-[720px]">
              <thead className="sticky top-0 z-10 border-b border-border dark:border-white/5 bg-card">
                <tr>
                  <Th>用户名</Th>
                  <Th>理由</Th>
                  <Th>状态</Th>
                  <Th>申请时间</Th>
                  <Th className="text-right">操作</Th>
                </tr>
              </thead>
              <tbody>
                {paged(proApps.applications, proPage).map(a => (
                  <tr key={a.id} className="border-b border-border dark:border-white/5 last:border-0 hover:bg-s2 dark:hover:bg-white/[0.02]">
                    <Td className="font-medium text-foreground">{a.username}</Td>
                    <Td className="max-w-[280px]">
                      <span className="line-clamp-2 text-[12px] text-muted-foreground" title={a.reason}>
                        {a.reason || '—'}
                      </span>
                    </Td>
                    <Td>
                      <span
                        className={`inline-flex items-center rounded-full border px-2 py-0.5 text-[12px] ${APP_STATUS_BADGE[a.status] || APP_STATUS_BADGE.pending}`}
                      >
                        {APP_STATUS_LABEL[a.status] || a.status}
                      </span>
                    </Td>
                    <Td className="whitespace-nowrap text-muted-foreground">{formatDateTime(a.created_at) || '—'}</Td>
                    <Td className="text-right">
                      {a.status === 'pending' ? (
                        <div className="flex flex-col items-end gap-1">
                          <input
                            className="h-7 w-[160px] rounded-md border border-border dark:border-white/10 bg-background dark:bg-[#0d0d18] px-2 text-[12px] text-foreground outline-none focus:border-primary/40 dark:focus:border-cyan-500/40"
                            placeholder="审核备注（可选）"
                            maxLength={255}
                            value={reviewNotes[a.id] || ''}
                            onChange={e => setReviewNotes(prev => ({ ...prev, [a.id]: e.target.value }))}
                          />
                          <div className="flex items-center justify-end gap-1">
                          <Button
                            size="sm"
                            variant="outline"
                            className="h-7 px-2 text-[12px] hover:border-emerald-500/30 hover:text-emerald-400"
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
                            className="h-7 px-2 text-[12px] hover:border-red-500/30 hover:text-red-400"
                            disabled={appActionId === a.id}
                            onClick={() => void reviewApp(a, 'reject')}
                          >
                            拒绝
                          </Button>
                          </div>
                        </div>
                      ) : (
                        <span className="text-[12px] text-muted-foreground">
                          {a.reviewed_by ? `${a.reviewed_by} · ` : ''}
                          {formatDateTime(a.reviewed_at)}
                        </span>
                      )}
                    </Td>
                  </tr>
                ))}
              </tbody>
            </table>
            <Pager total={proApps.applications.length} page={proPage} pageSize={PAGE_SIZE} onPage={setProPage} unit="条" />
          </div>
        )}
      </Section>
    </DevPageLayout>
  )
}
