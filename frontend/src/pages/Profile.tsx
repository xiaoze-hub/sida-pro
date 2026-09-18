import { fetchAPI } from '@panwatch/api'
import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import { Target, Star, Briefcase, UserRound, Upload, X, KeyRound, Check, AlertTriangle, Crown } from 'lucide-react'
import { Input } from '@panwatch/base-ui/components/ui/input'
import { Label } from '@panwatch/base-ui/components/ui/label'
import { Button } from '@panwatch/base-ui/components/ui/button'
import { useToast } from '@panwatch/base-ui/components/ui/toast'
import { fileToAvatarDataUrl } from '@/hooks/use-avatar'
import { formatDateTime } from '@/lib/utils'
import { submitChangePassword } from '@/lib/change-password'
import { useApiQuery } from '@/hooks/useApiQuery'
import NotifyChannelsSection from '@/components/profile/NotifyChannelsSection'
import SectionHeader from '@panwatch/biz-ui/components/SectionHeader'
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

/** 头像首字母圆形色块(无头像时兜底); 240 色相实底, 不用渐变。 */
function AvatarCircle({ name, avatar, size = 'lg' }: { name: string; avatar: string; size?: 'lg' | 'sm' }) {
  const cls = size === 'lg' ? 'w-16 h-16 text-[20px]' : 'w-9 h-9 text-[13px]'
  if (avatar) {
    return (
      <div className={`${cls} rounded-full overflow-hidden shrink-0 ring-1 ring-border/40 bg-background`}>
        <img src={avatar} alt="头像" className="w-full h-full object-cover" />
      </div>
    )
  }
  return (
    <div className={`${cls} rounded-full shrink-0 bg-primary flex items-center justify-center text-white font-semibold ring-1 ring-primary/40`}>
      {(name || '?').charAt(0).toUpperCase()}
    </div>
  )
}

function StatTile({ icon: Icon, label, value, sub, accent }: { icon: any; label: string; value: string; sub?: string; accent: string }) {
  return (
    <div className="rounded-md border border-border/40 bg-accent/25 p-3.5">
      <div className="flex items-center gap-2 mb-1.5">
        <Icon className={`w-3.5 h-3.5 ${accent}`} />
        <span className="text-[11px] text-muted-foreground">{label}</span>
      </div>
      <div className="text-[16px] font-semibold text-foreground">{value}</div>
      {sub && <div className="text-[10px] text-muted-foreground mt-1 leading-4">{sub}</div>}
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
  const [nicknameDraft, setNicknameDraft] = useState('')
  const [avatarDraft, setAvatarDraft] = useState('') // '' = 未设置; 由头像是否改动区分
  const [avatarChanged, setAvatarChanged] = useState(false)
  const [savingProfile, setSavingProfile] = useState(false)
  const [pasteOpen, setPasteOpen] = useState(false)
  const [pasteValue, setPasteValue] = useState('')

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

  const onPasteAvatar = () => {
    const v = (pasteValue || '').trim()
    if (!v) return
    if (!v.startsWith('data:image/')) {
      toast('请粘贴图片 data URL(data:image/... 开头)', 'error')
      return
    }
    if (v.length > 100_000) {
      toast('图片过大, 请压缩到 75KB 以内', 'error')
      return
    }
    setAvatarDraft(v)
    setAvatarChanged(true)
    setPasteValue('')
    setPasteOpen(false)
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
      {/* 页头 */}
      <div className="relative overflow-hidden border-b border-border/40 p-5 md:p-7">
        <div className="relative flex flex-col md:flex-row md:items-end md:justify-between gap-4">
          <div className="min-w-0">
            <h1 className="text-[16px] md:text-[16px] font-bold text-foreground">{t('profile.title')}</h1>
            <p className="text-[12px] text-muted-foreground mt-1">{t('profile.subtitle')}</p>
          </div>
        </div>
      </div>

      <div className="mt-6 grid grid-cols-1 lg:grid-cols-12 gap-6">
        {/* ① 个人资料 */}
        <section className="border-t border-border/40 pt-4 md:pt-5 lg:col-span-7">
          <SectionHeader title={t('profile.personalInfo')} className="mb-4" />

          <div className="flex items-start gap-4">
            {/* 头像: 默认用户名首字母色块; 可上传或粘贴 data URL */}
            <div className="flex flex-col items-center gap-2 shrink-0">
              <AvatarCircle name={displayName} avatar={avatarDraft} />
              <div className="flex flex-col items-center gap-1">
                <input ref={fileRef} type="file" accept="image/*" className="hidden" onChange={onPickAvatar} />
                <Button variant="secondary" size="sm" className="h-7 text-[11px]" onClick={() => fileRef.current?.click()}>
                  <Upload className="w-3 h-3" /> 上传
                </Button>
                <Button variant="ghost" size="sm" className="h-6 text-[11px]" onClick={() => setPasteOpen(v => !v)}>
                  粘贴 data URL
                </Button>
                {avatarChanged && avatarDraft === '' && (
                  <span className="text-[10px] text-muted-foreground">头像将清空</span>
                )}
              </div>
            </div>

            <div className="flex-1 min-w-0 space-y-4">
              {pasteOpen && (
                <div className="rounded-md border border-border/40 bg-accent/20 p-3 space-y-2">
                  <Label className="text-[11px]">粘贴图片 data URL(200KB 以内)</Label>
                  <div className="flex gap-2">
                    <Input
                      value={pasteValue}
                      onChange={e => setPasteValue(e.target.value)}
                      placeholder="data:image/png;base64,..."
                      className="h-8 text-[11px] font-mono"
                    />
                    <Button size="sm" className="h-8 shrink-0" onClick={onPasteAvatar}>
                      <Check className="w-3.5 h-3.5" />
                    </Button>
                  </div>
                </div>
              )}

              <div>
                <Label className="text-[11px]">昵称(1-32 字)</Label>
                <Input
                  value={nicknameDraft}
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
        </section>

        {/* ② 安全中心 */}
        <section className="border-t border-border/40 pt-4 md:pt-5 lg:col-span-5">
          <SectionHeader title={t('profile.security')} className="mb-4" />
          
          {/* 当前账号 */}
          <div className="mb-5 rounded-md border border-border/40 bg-accent/20 p-3.5 space-y-2">
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
                  className="rounded border border-border/60 px-2.5 py-1 text-[11px] text-primary transition-colors hover:bg-accent/30 disabled:opacity-60"
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
                  <span className="font-medium text-foreground truncate">{row.v}</span>
                </div>
              ))
            )}
          </div>

          {/* 修改密码(复用 /api/auth/change-password) */}
          <div className="space-y-3">
            <div>
              <Label className="text-[11px]">{t('profile.oldPassword')}</Label>
              <Input type="password" value={oldPwd} onChange={e => { setOldPwd(e.target.value); setPwdError(null) }} placeholder={t('accountMenu.oldPwdPlaceholder')} autoComplete="current-password" className="mt-1.5 h-9 min-h-[44px]" />
            </div>
            <div>
              <Label className="text-[11px]">{t('profile.newPassword')}</Label>
              <Input type="password" value={newPwd} onChange={e => { setNewPwd(e.target.value); setPwdError(null) }} placeholder={t('accountMenu.newPwdPlaceholder')} autoComplete="new-password" className="mt-1.5 h-9 min-h-[44px]" />
            </div>
            <div>
              <Label className="text-[11px]">{t('profile.confirmPassword')}</Label>
              <Input type="password" value={confirmPwd} onChange={e => { setConfirmPwd(e.target.value); setPwdError(null) }} placeholder={t('accountMenu.confirmPwdPlaceholder')} autoComplete="new-password" className="mt-1.5 h-9 min-h-[44px]" />
            </div>
            {pwdError && <div className="text-[12px] text-destructive">{pwdError}</div>}
            <Button className="h-8 w-full min-h-[44px]" onClick={handleChangePassword} disabled={changingPwd}>
              <KeyRound className="w-3.5 h-3.5" />
              {changingPwd ? t('accountMenu.submitting') : t('profile.changePassword')}
            </Button>
          </div>
        </section>

        {/* ③ 我的 API Key(2026-09-16 控制台) */}
        <MyApiKeyCard />

        {/* ④ 我的数据 */}
        <section className="border-t border-border/40 pt-4 md:pt-5 lg:col-span-12">
          <div className="flex items-center gap-2 mb-4">
            <Target className="w-4 h-4 text-primary" />
            <h3 className="text-[12px] md:text-[13px] font-semibold text-foreground">我的数据</h3>
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
        </section>

        {/* ⑤ Pro 升级(B.1) */}
        <section className="border-t border-border/40 pt-4 md:pt-5 lg:col-span-12">
          <div className="flex items-center gap-2 mb-4">
            <Crown className="w-4 h-4 text-amber-500" />
            <h3 className="text-[12px] md:text-[13px] font-semibold text-foreground">Pro 升级</h3>
          </div>
          {profile?.role === 'owner' || profile?.role === 'pro' ? (
            <div className="rounded-md border border-emerald-700/25 bg-emerald-500/10 p-3.5 text-[12px] text-emerald-700 dark:text-emerald-400">
              您已是 {profile.role === 'owner' ? '管理员' : 'Pro 用户'}，可使用全部功能。
            </div>
          ) : (
            <ProApplyCard />
          )}
        </section>

        {/* ⑥ 通知渠道(B.4) */}
        <NotifyChannelsSection />
      </div>
    </div>
  )
}

/** 我的 API Key 卡片(2026-09-16): 显示当前 key 前缀 + 跳转控制台。 */
function MyApiKeyCard() {
  const navigate = useNavigate()
  const { data, isLoading } = useApiQuery<{ keys: Array<{ key_prefix: string; status: string; tier: string }> }>(
    ['api-keys', 'my'],
    '/keys/my',
  )
  const keys = data?.keys ?? []
  const current = keys.find(k => k.status === 'active') || keys[0] || null

  return (
    <section className="border-t border-border/40 pt-4 md:pt-5 lg:col-span-12">
      <div className="flex items-center gap-2 mb-4">
        <KeyRound className="w-4 h-4 text-primary" />
        <h3 className="text-[12px] md:text-[13px] font-semibold text-foreground">我的 API Key</h3>
      </div>
      <div className="flex flex-col sm:flex-row sm:items-center gap-3 rounded-md border border-border/40 bg-accent/20 p-3.5">
        <div className="min-w-0 flex-1">
          {isLoading ? (
            <div className="text-[12px] text-muted-foreground">加载中…</div>
          ) : current ? (
            <>
              <div className="flex items-center gap-2 flex-wrap">
                <code className="font-mono text-[13px] text-foreground font-medium">{current.key_prefix}...</code>
                <span className="text-[10px] rounded border border-border/60 bg-background/50 px-1.5 py-0.5 text-muted-foreground">
                  {current.tier === 'pro' ? 'Pro' : current.tier === 'trial' ? '试用' : '免费'}
                </span>
                <span className="text-[10px] text-muted-foreground">
                  共 {keys.length} 个 Key
                </span>
              </div>
              <div className="text-[11px] text-muted-foreground mt-1">
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
      <div className="rounded-md border border-primary/25 bg-primary/5 p-3.5 text-[12px] text-foreground">
        申请已提交，等待管理员审核。审核通过后您的账号将升级为 Pro。
      </div>
    )
  }

  return (
    <div className="space-y-3">
      <div className="rounded-md border border-border/50 bg-accent/20 p-3.5 text-[12px] text-muted-foreground">
        Pro 账号可使用全部功能：机会页、数智决策三指标、暗盘资金、L2资金（不限试用次数）。
      </div>
      <div>
        <Label className="text-[11px]">申请理由（可选）</Label>
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
