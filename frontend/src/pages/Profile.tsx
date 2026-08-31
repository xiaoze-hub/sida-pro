import { fetchAPI } from '@panwatch/api'
import { useEffect, useRef, useState } from 'react'
import { UserCog, Target, Star, Briefcase, UserRound, Upload, X, KeyRound, Check, ShieldCheck } from 'lucide-react'
import { Input } from '@panwatch/base-ui/components/ui/input'
import { Label } from '@panwatch/base-ui/components/ui/label'
import { Button } from '@panwatch/base-ui/components/ui/button'
import { useToast } from '@panwatch/base-ui/components/ui/toast'
import { fileToAvatarDataUrl } from '@/hooks/use-avatar'
import { formatDateTime } from '@/lib/utils'
import { submitChangePassword } from '@/lib/change-password'

interface ProfileInfo {
  username: string
  nickname: string
  avatar: string
  role: 'owner' | 'member'
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

const ROLE_LABEL: Record<string, string> = { owner: '管理员', member: '普通成员' }

/** 头像首字母圆形色块(无头像时兜底); 240 色相实底, 不用渐变。 */
function AvatarCircle({ name, avatar, size = 'lg' }: { name: string; avatar: string; size?: 'lg' | 'sm' }) {
  const cls = size === 'lg' ? 'w-16 h-16 text-[22px]' : 'w-9 h-9 text-[13px]'
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
    <div className="rounded-xl border border-border/40 bg-accent/25 p-3.5">
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
  const fileRef = useRef<HTMLInputElement | null>(null)

  // ── 个人资料 ──
  const [profile, setProfile] = useState<ProfileInfo | null>(null)
  const [loading, setLoading] = useState(true)
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

  // ── 我的数据 ──
  const [stats, setStats] = useState<ProfileStats | null>(null)

  const loadProfile = async () => {
    try {
      const p = await fetchAPI<ProfileInfo>('/profile', { cacheMode: 'reload' })
      setProfile(p)
      setNicknameDraft(p.nickname || '')
      setAvatarDraft(p.avatar || '')
      setAvatarChanged(false)
    } catch (e) {
      toast(e instanceof Error ? e.message : '加载个人资料失败', 'error')
    } finally {
      setLoading(false)
    }
  }

  const loadStats = async () => {
    try {
      const s = await fetchAPI<ProfileStats>('/profile/stats', { cacheMode: 'reload' })
      setStats(s)
    } catch (e) {
      toast(e instanceof Error ? e.message : '加载我的数据失败', 'error')
    }
  }

  useEffect(() => {
    loadProfile()
    loadStats()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

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
      setProfile(updated)
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

  if (loading && !profile) {
    return (
      <div className="w-full h-[60vh] flex items-center justify-center">
        <span className="w-6 h-6 border-2 border-primary/30 border-t-primary rounded-full animate-spin" />
      </div>
    )
  }

  const displayName = profile?.nickname || profile?.username || ''

  return (
    <div>
      {/* 页头 */}
      <div className="card relative overflow-hidden p-5 md:p-7">
        <div className="relative flex flex-col md:flex-row md:items-end md:justify-between gap-4">
          <div className="min-w-0">
            <h1 className="text-[16px] md:text-[18px] font-bold text-foreground">个人中心</h1>
            <p className="text-[12px] text-muted-foreground mt-1">管理个人资料、账号安全与我的数据</p>
          </div>
        </div>
      </div>

      <div className="mt-6 grid grid-cols-1 lg:grid-cols-12 gap-6">
        {/* ① 个人资料 */}
        <section className="card p-4 md:p-6 lg:col-span-7">
          <div className="flex items-center gap-2 mb-4">
            <UserCog className="w-4 h-4 text-primary" />
            <h3 className="text-[12px] md:text-[13px] font-semibold text-foreground">个人资料</h3>
          </div>

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
                <div className="rounded-xl border border-border/40 bg-accent/20 p-3 space-y-2">
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
        <section className="card p-4 md:p-6 lg:col-span-5">
          <div className="flex items-center gap-2 mb-4">
            <ShieldCheck className="w-4 h-4 text-primary" />
            <h3 className="text-[12px] md:text-[13px] font-semibold text-foreground">安全中心</h3>
          </div>

          {/* 当前账号 */}
          <div className="mb-5 rounded-xl border border-border/40 bg-accent/20 p-3.5 space-y-2">
            {[
              { k: '账号', v: profile?.username || '--' },
              { k: '角色', v: ROLE_LABEL[profile?.role || ''] || profile?.role || '--' },
              { k: '注册时间', v: profile?.created_at ? formatDateTime(profile.created_at) : '--' },
            ].map(row => (
              <div key={row.k} className="flex items-center justify-between gap-3 text-[12px]">
                <span className="text-muted-foreground">{row.k}</span>
                <span className="font-medium text-foreground truncate">{row.v}</span>
              </div>
            ))}
          </div>

          {/* 修改密码(复用 /api/auth/change-password) */}
          <div className="space-y-3">
            <div>
              <Label className="text-[11px]">旧密码</Label>
              <Input type="password" value={oldPwd} onChange={e => { setOldPwd(e.target.value); setPwdError(null) }} placeholder="当前使用的密码" autoComplete="current-password" className="mt-1.5 h-9" />
            </div>
            <div>
              <Label className="text-[11px]">新密码(至少 8 位)</Label>
              <Input type="password" value={newPwd} onChange={e => { setNewPwd(e.target.value); setPwdError(null) }} placeholder="设置新密码" autoComplete="new-password" className="mt-1.5 h-9" />
            </div>
            <div>
              <Label className="text-[11px]">确认新密码</Label>
              <Input type="password" value={confirmPwd} onChange={e => { setConfirmPwd(e.target.value); setPwdError(null) }} placeholder="再次输入新密码" autoComplete="new-password" className="mt-1.5 h-9" />
            </div>
            {pwdError && <div className="text-[12px] text-destructive">{pwdError}</div>}
            <Button className="h-8 w-full" onClick={handleChangePassword} disabled={changingPwd}>
              <KeyRound className="w-3.5 h-3.5" />
              {changingPwd ? '提交中...' : '修改密码'}
            </Button>
          </div>
        </section>

        {/* ③ 我的数据 */}
        <section className="card p-4 md:p-6 lg:col-span-12">
          <div className="flex items-center gap-2 mb-4">
            <Target className="w-4 h-4 text-primary" />
            <h3 className="text-[12px] md:text-[13px] font-semibold text-foreground">我的数据</h3>
          </div>

          <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
            <StatTile
              icon={Target}
              label="预测命中率"
              value={stats?.prediction?.hit_rate != null ? `${stats.prediction.hit_rate}%` : '--'}
              sub={stats?.prediction?.total ? `命中 ${stats.prediction.hit_count}/${stats.prediction.total} · ${stats.prediction.note}` : '暂无已评估预测'}
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
      </div>
    </div>
  )
}

export default Profile
