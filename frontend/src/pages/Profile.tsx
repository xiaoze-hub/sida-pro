import PageHeader from '@/components/PageHeader'
import { SectionWorkspace, SectionPanel, useSectionRoute } from '@/components/SectionWorkspace'
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription } from '@panwatch/base-ui/components/ui/dialog'
import { fetchAPI } from '@panwatch/api'
import { useEffect, useRef, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import { Target, Star, Briefcase, UserRound, Upload, X, KeyRound, AlertTriangle, Crown } from 'lucide-react'
import { Input } from '@panwatch/base-ui/components/ui/input'
import { Label } from '@panwatch/base-ui/components/ui/label'
import { Button } from '@panwatch/base-ui/components/ui/button'
import { useToast } from '@panwatch/base-ui/components/ui/toast'
import { fileToAvatarDataUrl } from '@/hooks/use-avatar'
import { formatDateTime } from '@/lib/utils'
import { submitChangePassword } from '@/lib/change-password'
import { useApiQuery } from '@/hooks/useApiQuery'
import NotifyChannelsSection from '@/components/profile/NotifyChannelsSection'
import SessionDevicesSection from '@/components/profile/SessionDevicesSection'
import DataPrivacySection from '@/components/profile/DataPrivacySection'
import { useI18n } from '@/hooks/useI18n'

interface ProfileInfo {
  username: string
  nickname: string
  avatar: string
  role: 'owner' | 'pro' | 'member'
  created_at: string | null
}

interface ProfileStats {
  prediction: {
    hit_count: number
    total: number
    hit_rate: number | null
    scope: string
    note: string
  }
  watchlist_count: number
  position_count: number
  has_shadow_profile: boolean
}

/** 分 Agent 命中榜(GET /profile/stats/accuracy)。字段全可选, 防后端/传输异常时崩。 */
interface AccuracyAgent {
  agent: string
  hit: number
  total: number
  hit_rate: number | null
  avg_return_pct: number | null
  qualified: boolean
}

interface AccuracyBoard {
  since: string | null
  scope: string
  note?: string
  overall?: {
    hit_count: number
    total: number
    hit_rate: number | null
    avg_return_pct: number | null
    scope: string
  }
  agents?: AccuracyAgent[]
}

/** 头像首字母圆形色块(无头像时兜底); 240 色相实底, 不用渐变。 */
function AvatarCircle({ name, avatar, size = 'lg' }: { name: string; avatar: string; size?: 'lg' | 'sm' }) {
  const cls = size === 'lg' ? 'w-16 h-16 text-[20px]' : 'w-9 h-9 text-[14px]'
  if (avatar) {
    return (
      <div className={`${cls} rounded-full overflow-hidden shrink-0 ring-1 ring-border/40 bg-background`}>
        <img src={avatar} alt="头像" className="w-full h-full object-cover" />
      </div>
    )
  }
  return (
    <div className={`${cls} rounded-full shrink-0 bg-primary flex items-center justify-center text-primary-foreground font-semibold ring-1 ring-primary/40`}>
      {(name || '?').charAt(0).toUpperCase()}
    </div>
  )
}

function StatTile({ icon: Icon, label, value, sub, accent }: { icon: any; label: string; value: string; sub?: string; accent: string }) {
  return (
    <div className="card-surface rounded-md bg-s2 p-3.5">
      <div className="flex items-center gap-2 mb-1.5">
        <Icon className={`w-3.5 h-3.5 ${accent}`} />
        <span className="text-[12px] text-muted-foreground">{label}</span>
      </div>
      <div className="text-[16px] font-semibold text-foreground">{value}</div>
      {sub && <div className="text-[12px] text-muted-foreground mt-1 leading-4">{sub}</div>}
    </div>
  )
}

/**
 * 个人中心(2026-08-15 SIDA 完整度评估 P1): 个人资料 / 安全中心 / 我的数据。
 * 240 色相卡片风格(参考 Settings), 无 emoji、无渐变。
 */
export function Profile() {
  const { toast } = useToast()
  const { t } = useI18n()
  const fileRef = useRef<HTMLInputElement | null>(null)
  const queryClient = useQueryClient()

  // ── 个人资料 ──
  // W3.7/D7: GET /profile 与 /profile/stats 交给 TanStack Query; 编辑态(nicknameDraft 等)仍是本地 state,
  // 由 data 变化同步; PUT 保存后用 setQueryData 就地更新缓存, 不整页重取。
  const { data: profile, isLoading, error: profileError, refetch: refetchProfile, isFetching: profileFetching } =
    useApiQuery<ProfileInfo>(['profile'], '/profile')
  const { data: stats, error: statsError } = useApiQuery<ProfileStats>(['profile', 'stats'], '/profile/stats')
  // 2026-10-10 AI 链路 P1: 分 Agent 命中榜(此前后端已实现、零前端消费)。独立 query, 不拖累账号区。
  const { data: board, error: boardError } = useApiQuery<AccuracyBoard>(
    ['profile', 'stats', 'accuracy'],
    '/profile/stats/accuracy',
  )
  const [editOpen, setEditOpen] = useState(false)
  const [passwordOpen, setPasswordOpen] = useState(false)
  const profileSections = [{ id: 'account', label: '资料与安全' }, { id: 'activity', label: '分析记录与数据' }, { id: 'notifications', label: '通知渠道' }, { id: 'access', label: 'API 与权益' }, { id: 'privacy', label: '数据与隐私' }]
  const [selected, select] = useSectionRoute(profileSections, 'account')
  const [nicknameDraft, setNicknameDraft] = useState('')
  const [avatarDraft, setAvatarDraft] = useState('') // '' = 未设置; 由头像是否改动区分
  const [avatarChanged, setAvatarChanged] = useState(false)
  const [savingProfile, setSavingProfile] = useState(false)

  // ── 安全中心(修改密码) ──
  const [oldPwd, setOldPwd] = useState('')
  const [newPwd, setNewPwd] = useState('')
  const [confirmPwd, setConfirmPwd] = useState('')
  const [pwdError, setPwdError] = useState<string | null>(null)
  const [changingPwd, setChangingPwd] = useState(false)

  useEffect(() => {
    if (!profile) return
    setNicknameDraft(profile.nickname || '')
    setAvatarDraft(profile.avatar || '')
    setAvatarChanged(false)
  }, [profile])

  useEffect(() => {
    if (profileError) toast(profileError instanceof Error ? profileError.message : '加载个人资料失败', 'error')
    if (statsError) toast(statsError instanceof Error ? statsError.message : '加载我的数据失败', 'error')
  }, [profileError, statsError, toast])

  const onPickAvatar = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    e.target.value = ''
    if (!file) return
    try {
      const dataUrl = await fileToAvatarDataUrl(file)
      setAvatarDraft(dataUrl)
      setAvatarChanged(true)
    } catch (err) {
      toast(err instanceof Error ? err.message : '图片处理失败', 'error')
    }
  }


  const saveProfile = async () => {
    const nickname = nicknameDraft.trim()
    if (!nickname) {
      toast('昵称不能为空', 'error')
      return
    }
    if (nickname.length > 32) {
      toast('昵称最多 32 个字', 'error')
      return
    }
    setSavingProfile(true)
    try {
      const payload: { nickname: string; avatar?: string } = { nickname }
      if (avatarChanged) payload.avatar = avatarDraft // 未改动头像则不提交, 避免无谓回写
      const updated = await fetchAPI<ProfileInfo>('/profile', { method: 'PUT', body: JSON.stringify(payload) })
      queryClient.setQueryData(['profile'], updated)
      setNicknameDraft(updated.nickname || '')
      setAvatarDraft(updated.avatar || '')
      setAvatarChanged(false)
      // 广播头像变更(2026-08-15 评审 A): 右上角 AccountMenu 头像本会话内同步刷新
      if (avatarChanged) {
        window.dispatchEvent(new CustomEvent('panwatch:avatar-changed'))
      }
      toast('资料已保存', 'success')
      setEditOpen(false)
    } catch (e) {
      toast(e instanceof Error ? e.message : '保存失败', 'error')
    } finally {
      setSavingProfile(false)
    }
  }

  const clearAvatar = () => {
    setAvatarDraft('')
    setAvatarChanged(true)
  }

  const handleChangePassword = async () => {
    // 2026-08-17: 改用公共 helper (关闭 A P1-8 双份实现)
    await submitChangePassword({
      oldPwd, newPwd, confirmPwd,
      onError: setPwdError,
      onSuccess: () => {
        toast('密码已更新', 'success')
        setPasswordOpen(false)
        setOldPwd('')
        setNewPwd('')
        setConfirmPwd('')
      },
      onLoadingChange: setChangingPwd,
    })
  }

  if (isLoading && !profile) {
    return (
      <div className="w-full h-[60vh] flex items-center justify-center">
        <span className="w-6 h-6 border-2 border-primary/30 border-t-primary rounded-full animate-spin" />
      </div>
    )
  }

  const displayName = profile?.nickname || profile?.username || ''

  return (
    <div className="sida-page-enter">
      <PageHeader title={t('profile.title')} description={t('profile.subtitle')} />
      <div className="card mb-5 flex flex-wrap items-center gap-4 p-4">
        <AvatarCircle name={displayName} avatar={profile?.avatar || ''} />
        <div className="min-w-0 flex-1 basis-[180px]"><p className="text-[20px] font-semibold">{displayName || '个人账户'}</p><p className="mt-1 text-[12px] text-muted-foreground">管理个人资料、分析记录与账户安全</p></div>
        <div className="flex flex-wrap gap-2 sm:ml-auto"><Button variant="outline" size="sm" onClick={() => setEditOpen(true)}>编辑资料</Button>
        <Button variant="ghost" size="sm" onClick={() => setPasswordOpen(true)}><KeyRound className="h-4 w-4" />修改密码</Button></div>
      </div>
      <SectionWorkspace items={profileSections} value={selected} onChange={select} label="个人中心分类">
        <SectionPanel id="account">
          <section className="analysis-section"><h2 className="mb-4 text-[16px] font-semibold">账户信息</h2>
          <div
            className="space-y-3"
            data-testid="profile-account-card"
          >
            {/*
              2026-09-14 走查 B: 账号/角色/注册时间此前无论加载失败还是真缺值都渲染 '--' —— 用户
              分不清"没拉到"和"本来就是空的"。已登录用户的 username/role/created_at 恒存在,
              这里不存在合法空态 ⇒ 拉取失败必须显式故障态 + 重试, 不得铺一排 '--'。
            */}
            {profileError && !profile ? (
              <div className="space-y-2" role="alert">
                <div className="flex items-start gap-1.5 text-[12px] text-amber-600 dark:text-amber-500">
                  <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                  {/* 后端/传输层原文透传, 不在前端编造失败原因 */}
                  <span>账号信息加载失败: {profileError instanceof Error ? profileError.message : '未知错误'}</span>
                </div>
                <button
                  type="button"
                  onClick={() => void refetchProfile()}
                  disabled={profileFetching}
                  className="rounded border border-border px-2.5 py-1 text-[12px] text-primary transition-colors hover:bg-s2 disabled:opacity-60"
                >
                  {profileFetching ? '重试中…' : '重试'}
                </button>
              </div>
            ) : (
              [
                { k: t('profile.username'), v: profile?.username || '--' },
                { k: t('profile.role'), v: t(`profile.roles.${profile?.role || 'member'}`) },
                { k: t('profile.createdAt'), v: profile?.created_at ? formatDateTime(profile.created_at) : '--' },
              ].map(row => (
                <div key={row.k} className="flex items-center justify-between gap-3 text-[12px]">
                  <span className="text-muted-foreground">{row.k}</span>
                  <span className="font-medium text-foreground truncate" title={row.v}>{row.v}</span>
                </div>
              ))
            )}
          </div>

          </section>
          <section className="analysis-section"><SessionDevicesSection /></section>
        </SectionPanel>
        <SectionPanel id="activity">        {/* ④ 我的数据 */}
        <section className="analysis-section">
          <div className="flex items-center gap-2 mb-4">
            <Target className="w-4 h-4 text-primary" />
            <h3 className="text-[12px] md:text-[14px] font-semibold text-foreground">我的数据</h3>
          </div>

          <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
            <StatTile
              icon={Target}
              label="预测命中率"
              value={stats?.prediction?.hit_rate != null ? `${stats.prediction.hit_rate}%` : '--'}
              sub={
                stats?.prediction?.total
                  ? [
                      `命中 ${stats.prediction.hit_count}/${stats.prediction.total}`,
                      stats.prediction.note ? String(stats.prediction.note) : null,
                    ]
                      .filter(Boolean)
                      .join(' · ')
                  : '暂无已评估预测'
              }
              accent="text-primary"
            />
            <StatTile
              icon={Star}
              label="自选数"
              value={stats?.watchlist_count != null ? String(stats.watchlist_count) : '--'}
              sub="自选股(含历史全局)"
              accent="text-amber-600"
            />
            <StatTile
              icon={Briefcase}
              label="持仓数"
              value={stats?.position_count != null ? String(stats.position_count) : '--'}
              sub="持仓记录"
              accent="text-primary"
            />
            <StatTile
              icon={UserRound}
              label="影子账户画像"
              value={stats?.has_shadow_profile ? '已生成' : '未生成'}
              sub={stats?.has_shadow_profile ? '交割单分析已落库' : '上传交割单后生成'}
              accent={stats?.has_shadow_profile ? 'text-emerald-600' : 'text-muted-foreground'}
            />
          </div>

          {/* 分 Agent 命中榜(2026-10-10 AI 链路 P1): 谁准谁不准, 一眼见。
              消费 GET /profile/stats/accuracy(此前后端已实现、零前端消费)。
              样本不足(qualified=false)显式标注不参评; 失败/空态显式, 不编造。 */}
          <div className="mt-5 border-t border-border pt-4" data-testid="profile-agent-board">
            <div className="mb-3 flex flex-wrap items-baseline gap-2">
              <span className="text-[12px] font-semibold text-foreground">Agent 命中榜</span>
              <span className="text-[12px] text-muted-foreground">
                分智能体预测命中率 · 近 30 天 · 样本不足不参评(按全平台统计, 预测记录无用户维度)
              </span>
            </div>
            {boardError ? (
              <div className="text-[12px] text-amber-600 dark:text-amber-500" data-testid="profile-agent-board-error">
                Agent 命中榜加载失败: {boardError instanceof Error ? boardError.message : '未知错误'}
              </div>
            ) : !board ? (
              <div className="text-[12px] text-muted-foreground">加载中…</div>
            ) : (board.agents ?? []).length === 0 ? (
              <div className="text-[12px] text-muted-foreground" data-testid="profile-agent-board-empty">
                暂无已评估的 Agent 预测(近 30 天无样本)
              </div>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-left text-[12px]">
                  <thead>
                    <tr className="border-b border-border text-muted-foreground">
                      <th className="px-2 py-1 font-medium">Agent</th>
                      <th className="px-2 py-1 text-right font-medium">命中率</th>
                      <th className="px-2 py-1 text-right font-medium">命中</th>
                      <th className="px-2 py-1 text-right font-medium">平均收益</th>
                      <th className="px-2 py-1 text-right font-medium">参评</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-border/40">
                    {(board.agents ?? []).map(a => (
                      <tr key={a.agent} data-testid={`profile-agent-row-${a.agent}`}>
                        <td className="px-2 py-1 text-foreground">{a.agent}</td>
                        <td className="px-2 py-1 text-right font-mono text-foreground">
                          {a.hit_rate != null ? `${a.hit_rate}%` : '--'}
                        </td>
                        <td className="px-2 py-1 text-right font-mono text-muted-foreground">
                          {a.hit}/{a.total}
                        </td>
                        <td
                          className={`px-2 py-1 text-right font-mono ${
                            a.avg_return_pct == null
                              ? 'text-muted-foreground'
                              : a.avg_return_pct > 0
                                ? 'text-stock-up'
                                : a.avg_return_pct < 0
                                  ? 'text-stock-down'
                                  : 'text-foreground'
                          }`}
                        >
                          {a.avg_return_pct != null ? `${a.avg_return_pct > 0 ? '+' : ''}${a.avg_return_pct}%` : '--'}
                        </td>
                        <td className="px-2 py-1 text-right">
                          {a.qualified ? (
                            <span className="text-muted-foreground">参评</span>
                          ) : (
                            <span className="text-amber-600 dark:text-amber-500">样本不足</span>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </section>

        </SectionPanel>
        <SectionPanel id="notifications"><NotifyChannelsSection /></SectionPanel>
        <SectionPanel id="access">        {/* ③ 我的 API Key(2026-09-16 控制台) */}
        <MyApiKeyCard />

        {/* ⑤ Pro 升级(B.1) */}
        <section className="analysis-section">
          <div className="flex items-center gap-2 mb-4">
            <Crown className="w-4 h-4 text-amber-500" />
            <h3 className="text-[12px] md:text-[14px] font-semibold text-foreground">Pro 升级</h3>
          </div>
          {profile?.role === 'owner' || profile?.role === 'pro' ? (
            <div className="card-surface rounded-md bg-emerald-500/10 p-3.5 text-[12px] text-emerald-700 dark:text-emerald-400">
              您已是 {profile.role === 'owner' ? '管理员' : 'Pro 用户'}，可使用全部功能。
            </div>
          ) : (
            <ProApplyCard />
          )}
        </section>

        </SectionPanel>
        <SectionPanel id="privacy">        {/* ④½ 数据与隐私(2026-10-23): 导出/注销此前有后端无前端 */}
        <section className="analysis-section">
          <DataPrivacySection username={profile?.username} />
        </section>

        </SectionPanel>
      </SectionWorkspace>
      <Dialog open={editOpen} onOpenChange={setEditOpen}><DialogContent><DialogHeader><DialogTitle>编辑个人资料</DialogTitle><DialogDescription>调整昵称和头像，保存后生效。</DialogDescription></DialogHeader>
<div className="space-y-4">


          <div className="flex items-start gap-4">
            {/* 头像: 默认用户名首字母色块; 可上传 */}
            <div className="flex flex-col items-center gap-2 shrink-0">
              <AvatarCircle name={displayName} avatar={avatarDraft} />
              <div className="flex flex-col items-center gap-1">
                <input ref={fileRef} type="file" accept="image/*" className="hidden" onChange={onPickAvatar} />
                <Button variant="secondary" size="sm" className="h-7 text-[12px]" onClick={() => fileRef.current?.click()}>
                  <Upload className="w-3 h-3" /> 上传
                </Button>
                {avatarChanged && avatarDraft === '' && (
                  <span className="text-[12px] text-muted-foreground">头像将清空</span>
                )}
              </div>
            </div>

            <div className="flex-1 min-w-0 space-y-4">

              <div>
                <Label htmlFor="profile-nickname" className="text-[12px]">昵称(1-32 字)</Label>
                <Input
                  id="profile-nickname" value={nicknameDraft}
                  onChange={e => setNicknameDraft(e.target.value)}
                  placeholder={profile?.username || '输入昵称'}
                  maxLength={32}
                  className="mt-1.5 h-9"
                />
              </div>

              <div className="flex items-center gap-2">
                <Button size="sm" className="h-8" onClick={saveProfile} disabled={savingProfile}>
                  {savingProfile ? '保存中...' : '保存资料'}
                </Button>
                {avatarChanged && avatarDraft !== '' && (
                  <Button variant="ghost" size="sm" className="h-8 text-[12px]" onClick={clearAvatar} disabled={savingProfile}>
                    <X className="w-3.5 h-3.5" /> 清除头像
                  </Button>
                )}
              </div>
            </div>
          </div>
        </div>      </DialogContent></Dialog>
      <Dialog open={passwordOpen} onOpenChange={open => { setPasswordOpen(open); if (!open) { setOldPwd(''); setNewPwd(''); setConfirmPwd(''); setPwdError(null) } }}><DialogContent><DialogHeader><DialogTitle>修改密码</DialogTitle><DialogDescription>验证当前密码后设置新密码。</DialogDescription></DialogHeader>
          {/* 修改密码(复用 /api/auth/change-password) */}
          <div className="space-y-3">
            <div>
              <Label htmlFor="profile-old-password" className="text-[12px]">{t('profile.oldPassword')}</Label>
              <Input id="profile-old-password" type="password" value={oldPwd} onChange={e => { setOldPwd(e.target.value); setPwdError(null) }} placeholder={t('accountMenu.oldPwdPlaceholder')} autoComplete="current-password" className="mt-1.5 h-9 min-h-[44px]" />
            </div>
            <div>
              <Label htmlFor="profile-new-password" className="text-[12px]">{t('profile.newPassword')}</Label>
              <Input id="profile-new-password" type="password" value={newPwd} onChange={e => { setNewPwd(e.target.value); setPwdError(null) }} placeholder={t('accountMenu.newPwdPlaceholder')} autoComplete="new-password" className="mt-1.5 h-9 min-h-[44px]" />
            </div>
            <div>
              <Label htmlFor="profile-confirm-password" className="text-[12px]">{t('profile.confirmPassword')}</Label>
              <Input id="profile-confirm-password" type="password" value={confirmPwd} onChange={e => { setConfirmPwd(e.target.value); setPwdError(null) }} placeholder={t('accountMenu.confirmPwdPlaceholder')} autoComplete="new-password" className="mt-1.5 h-9 min-h-[44px]" />
            </div>
            {pwdError && <div className="text-[12px] text-destructive">{pwdError}</div>}
            <Button className="h-8 w-full min-h-[44px]" onClick={handleChangePassword} disabled={changingPwd}>
              <KeyRound className="w-3.5 h-3.5" />
              {changingPwd ? t('accountMenu.submitting') : t('profile.changePassword')}
            </Button>
          </div>

      </DialogContent></Dialog>
    </div>
  )
}

function MyApiKeyCard() {
  const navigate = useNavigate()
  const { data, isLoading } = useApiQuery<{ keys: Array<{ key_prefix: string; status: string; tier: string }> }>(
    ['api-keys', 'my'],
    '/keys/my',
  )
  const keys = data?.keys ?? []
  const current = keys.find(k => k.status === 'active') || keys[0] || null

  return (
    <section className="analysis-section">
      <div className="flex items-center gap-2 mb-4">
        <KeyRound className="w-4 h-4 text-primary" />
        <h3 className="text-[12px] md:text-[14px] font-semibold text-foreground">我的 API Key</h3>
      </div>
      <div className="card-surface flex flex-col sm:flex-row sm:items-center gap-3 rounded-md bg-s2 p-3.5">
        <div className="min-w-0 flex-1">
          {isLoading ? (
            <div className="text-[12px] text-muted-foreground">加载中…</div>
          ) : current ? (
            <>
              <div className="flex items-center gap-2 flex-wrap">
                <code className="font-mono text-[14px] text-foreground font-medium">{current.key_prefix}...</code>
                <span className="text-[12px] rounded border border-border bg-background px-1.5 py-0.5 text-muted-foreground">
                  {current.tier === 'pro' ? 'Pro' : current.tier === 'trial' ? '试用' : '免费'}
                </span>
                <span className="text-[12px] text-muted-foreground">
                  共 {keys.length} 个 Key
                </span>
              </div>
              <div className="text-[12px] text-muted-foreground mt-1">
                用于 Skill Gateway / 智能体接入; 完整明文仅创建时可见一次
              </div>
            </>
          ) : (
            <div className="text-[12px] text-muted-foreground">
              尚未创建 API Key — 前往控制台创建后即可接入智能体
            </div>
          )}
        </div>
        <Button size="sm" className="h-8 shrink-0" onClick={() => navigate('/api-keys')}>
          管理
        </Button>
      </div>
    </section>
  )
}

function ProApplyCard() {
  const { toast } = useToast()
  const [reason, setReason] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [applied, setApplied] = useState(false)

  const submit = async () => {
    setSubmitting(true)
    try {
      const r = await fetchAPI<{ status: string; message: string }>('/profile/pro-apply', {
        method: 'POST',
        body: JSON.stringify({ reason }),
      })
      toast(r.message || '申请已提交', 'success')
      setApplied(true)
    } catch (e: any) {
      toast(e?.message || '提交失败，请稍后重试', 'error')
    } finally {
      setSubmitting(false)
    }
  }

  if (applied) {
    return (
      <div className="card-surface rounded-md bg-primary/5 p-3.5 text-[12px] text-foreground">
        申请已提交，等待管理员审核。审核通过后您的账号将升级为 Pro。
      </div>
    )
  }

  return (
    <div className="space-y-3">
      {/* P2-4(2026-09-18): 这里原来**硬编码**了一串 Pro 能力清单 —— 权限一改就变成假话。
          改成指向档位页, 由后端 `GET /api/tiers` 从权限定义实时生成(单一来源)。 */}
      <div className="card-surface rounded-md bg-s2 p-3.5 text-[12px] text-muted-foreground">
        Pro 可使用全部功能。完整对比见
        <Link to="/tiers" className="mx-1 underline underline-offset-4 hover:text-foreground">档位页</Link>
        （清单由服务端按当前权限实时生成）。
      </div>
      <div>
        <Label className="text-[12px]">申请理由（可选）</Label>
        <Input
          value={reason}
          onChange={e => setReason(e.target.value)}
          placeholder="简述您的使用场景..."
          className="mt-1.5 h-9"
          maxLength={200}
        />
      </div>
      <Button className="h-8 w-full" onClick={submit} disabled={submitting}>
        <Crown className="w-3.5 h-3.5" />
        {submitting ? '提交中...' : '申请升级 Pro'}
      </Button>
    </div>
  )
}

export default Profile
