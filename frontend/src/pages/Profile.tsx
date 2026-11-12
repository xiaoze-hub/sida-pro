import PageHeader from '@/components/PageHeader'
import { SectionWorkspace, SectionPanel, useSectionRoute } from '@/components/SectionWorkspace'
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription } from '@sida/base-ui/components/ui/dialog'
import { fetchAPI } from '@sida/api'
import { useEffect, useRef, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import { Target, Star, Briefcase, UserRound, Upload, X, KeyRound, AlertTriangle, Crown } from 'lucide-react'
import { Input } from '@sida/base-ui/components/ui/input'
import { Label } from '@sida/base-ui/components/ui/label'
import { Button } from '@sida/base-ui/components/ui/button'
import { useToast } from '@sida/base-ui/components/ui/toast'
import { fileToAvatarDataUrl } from '@/hooks/use-avatar'
import { formatDateTime } from '@/lib/utils'
import { submitChangePassword } from '@/lib/change-password'
import { currentIdentity, scopedKey, useApiQuery, useIdentityScope } from '@/hooks/useApiQuery'
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
  /** 归属不可证明的历史行(加列前存量), 不纳入成绩; >0 时界面必须说明 */
  legacy_unscoped_rows?: number
  legacy_untrusted?: boolean
  /** system 桶(无会话的系统调度运行)的独立统计条数; 绝不并入本账号 */
  system_scoped_rows?: number
  overall?: {
    hit_count: number
    total: number
    hit_rate: number | null
    avg_return_pct: number | null
    scope: string
    legacy_unscoped_rows?: number
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
  //
  // 2026-11-07 账号隔离: 个人页全是"属于某个登录身份"的数据。key 必须带身份后缀,
  // 否则同标签页换号(登录页 navigate('/'), 不刷新)后会先命中上一个账号的缓存 ——
  // B 登录进个人中心看到的是 A 的昵称/头像/统计。旧身份迟到的响应只会落进旧身份的分桶,
  // 碰不到当前页面。
  const identity = useIdentityScope()
  // 无法确认身份(无 localStorage 凭证 / token 解不开) ⇒ 个人页**不展示、不写入**:
  // 这种情况下 query key 只能是 'anon' 这类共享分桶, 展示出来就是把别人的资料当成自己的。
  const trustedIdentity = identity !== ''
  const { data: profile, isLoading, error: profileError, refetch: refetchProfile, isFetching: profileFetching } =
    useApiQuery<ProfileInfo>(scopedKey(['profile'], identity), '/profile', {
      cancelOnQueryAbort: true,
      enabled: trustedIdentity,
    })
  const { data: stats, error: statsError } = useApiQuery<ProfileStats>(
    scopedKey(['profile', 'stats'], identity),
    '/profile/stats',
    { enabled: trustedIdentity },
  )
  // 2026-10-10 AI 链路 P1: 分 Agent 命中榜(此前后端已实现、零前端消费)。独立 query, 不拖累账号区。
  const { data: board, error: boardError } = useApiQuery<AccuracyBoard>(
    scopedKey(['profile', 'stats', 'accuracy'], identity),
    '/profile/stats/accuracy',
    { enabled: trustedIdentity },
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

  // 换人即清编辑态(渲染期同步重置, 不留一帧旧草稿): 上一个账号没保存的昵称/头像草稿
  // 绝不能出现在新账号的编辑框里 —— 否则用户一按保存就把 A 的草稿写成 B 的资料。
  const [draftOwner, setDraftOwner] = useState(identity)
  if (draftOwner !== identity) {
    setDraftOwner(identity)
    setEditOpen(false)
    setPasswordOpen(false)
    setNicknameDraft('')
    setAvatarDraft('')
    setAvatarChanged(false)
    setOldPwd('')
    setNewPwd('')
    setConfirmPwd('')
    setPwdError(null)
  }

  /** 保存序号: 同一账号并发保存时只认最后一次, 先发后到的旧结果不得覆盖新头像/新昵称。 */
  const saveSeqRef = useRef(0)

  /**
   * 提交前身份核对(2026-11-07)。
   *
   * `identity` 是"页面上这份草稿属于谁"(渲染时的身份), `currentIdentity()` 是"这一发请求会
   * 写到谁"(token 里的身份)。同页换号后两者会有一段不一致的窗口: 登录已经把 token 换成 B,
   * 页面还挂在 A 的渲染上(A 的昵称草稿、A 的开着的编辑框)。这时候点保存 =
   * 用 B 的凭证提交 A 的资料 —— 会把 A 的草稿写进 B 的账号。
   * 一致性是写入的前提, 不是写入之后再纠正: 宁可不写, 也不能写错账号。
   */
  const identityChangedSinceRender = (): boolean => currentIdentity() !== identity

  const refuseOnIdentityChange = (action: string): boolean => {
    if (!identityChangedSinceRender()) return false
    toast(`账号已切换, ${action}已取消`, 'error')
    return true
  }

  useEffect(() => {
    if (!profile) return
    setNicknameDraft(profile.nickname || '')
    setAvatarDraft(profile.avatar || '')
    setAvatarChanged(false)
  }, [profile, identity])

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
    // 没有可信身份就没有"写给谁"这回事: 拒绝写入, 不拿匿名/共享分桶当归属。
    if (!trustedIdentity) {
      toast('无法确认当前登录身份, 已拒绝保存, 请重新登录', 'error')
      return
    }
    const nickname = nicknameDraft.trim()
    if (!nickname) {
      toast('昵称不能为空', 'error')
      return
    }
    if (nickname.length > 32) {
      toast('昵称最多 32 个字', 'error')
      return
    }
    // 提交前身份核对: 页面挂在 A 上而 token 已换成 B ⇒ 这一发会把 A 的草稿写进 B 的账号。
    if (refuseOnIdentityChange('保存')) return
    const seq = ++saveSeqRef.current
    // 记下发起时的身份: 保存期间换号, 结果只能落回原账号的分桶, 不许改写当前页面。
    const saveIdentity = identity
    setSavingProfile(true)
    try {
      // 先掐掉在途的旧读(否则它在 PUT 之后落地, 会用旧头像盖掉刚保存的新值)。
      await queryClient.cancelQueries({ queryKey: scopedKey(['profile'], saveIdentity) })
      // 等取消的这点时间里也可能换了号 —— 发请求前再核对一次。
      if (refuseOnIdentityChange('保存')) return
      const payload: { nickname: string; avatar?: string } = { nickname }
      if (avatarChanged) payload.avatar = avatarDraft // 未改动头像则不提交, 避免无谓回写
      const updated = await fetchAPI<ProfileInfo>('/profile', { method: 'PUT', body: JSON.stringify(payload) })
      // 并发保存乱序: 被后一次保存取代的旧结果直接丢弃(既不写缓存也不改草稿)。
      if (seq !== saveSeqRef.current) return
      // 只把**服务端返回的**结果写回缓存 —— 页头/顶栏头像必须用后端落库后的真实值,
      // 不能沿用本地草稿(data URL 会被后端重新编码/存放, 与服务端读取结果可能不同;
      // 用草稿"装作已保存"在失败/并发时会显示一张服务器上并不存在的头像)。
      queryClient.setQueryData(scopedKey(['profile'], saveIdentity), updated)
      if (currentIdentity() !== saveIdentity) return
      setNicknameDraft(updated.nickname || '')
      setAvatarDraft(updated.avatar || '')
      setAvatarChanged(false)
      // 广播头像变更(2026-08-15 评审 A): 右上角 AccountMenu / 顶栏头像本会话内同步刷新。
      // 2026-11-07: 带**服务端确认值**(而不是无值广播) —— 带值的事件让头像缓存直接采纳真值并
      // 作废在途的旧读; 服务端契约没给 avatar 时退化成无值事件, 让它自己去后端重取(不猜)。
      if (avatarChanged) {
        if (typeof updated.avatar === 'string') {
          window.dispatchEvent(new CustomEvent<string>('sida:avatar-changed', { detail: updated.avatar }))
        } else {
          window.dispatchEvent(new CustomEvent('sida:avatar-changed'))
        }
      }
      toast('资料已保存', 'success')
      setEditOpen(false)
    } catch (e) {
      if (seq !== saveSeqRef.current) return
      toast(e instanceof Error ? e.message : '保存失败', 'error')
    } finally {
      if (seq === saveSeqRef.current) setSavingProfile(false)
    }
  }

  const clearAvatar = () => {
    setAvatarDraft('')
    setAvatarChanged(true)
  }

  const handleChangePassword = async () => {
    // 改密也是"属于某个账号"的写操作: 换了号就不许拿 A 的旧密码去改 B 的密码。
    if (refuseOnIdentityChange('修改密码')) return
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

  /*
   * 无法确认登录身份 ⇒ fail-closed(2026-11-07)。
   * 以前这里用 `identity || 'anon'` 继续取数并渲染: 同一浏览器里没有可读凭证(Cookie-only)
   * 或 token 残缺时, 页面会用共享分桶的响应充当"我的资料" —— 既可能展示别人的昵称/头像,
   * 也可能让保存写到不属于任何人的分桶。宁可停在这里, 也不猜归属。
   */
  if (!trustedIdentity) {
    return (
      <div className="sida-page-enter">
        <PageHeader title={t('profile.title')} description={t('profile.subtitle')} />
        <div className="card flex flex-col gap-2 p-4" role="alert" data-testid="profile-identity-unknown">
          <div className="flex items-start gap-1.5 text-[12px] text-amber-600 dark:text-amber-500">
            <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
            <span>无法确认当前登录身份(本地凭证缺失或无法解析), 已暂停展示与写入个人资料。</span>
          </div>
          <Link to="/login" className="text-[12px] text-primary underline underline-offset-4">
            重新登录
          </Link>
        </div>
      </div>
    )
  }

  return (
    <div className="sida-page-enter">
      <PageHeader title={t('profile.title')} description={t('profile.subtitle')} />
      <div className="card mb-5 flex flex-wrap items-center gap-4 p-4" data-testid="profile-header">
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
          {/* 子区块自身也是按账号取数: 换人必须重挂, 否则在线设备列表还挂着上一个账号的会话 */}
          <section className="analysis-section"><SessionDevicesSection key={identity} /></section>
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
                分智能体预测命中率 · 近 30 天 · 样本不足不参评(仅统计本账号)
              </span>
            </div>
            {(board?.legacy_unscoped_rows ?? 0) > 0 && (
              <div
                className="mb-2 text-[12px] text-amber-600 dark:text-amber-500"
                data-testid="profile-agent-board-legacy"
              >
                另有 {board?.legacy_unscoped_rows} 条历史预测无用户归属(加列前的存量), 归属不可证明,
                不纳入成绩; 历史混算/重复不可视为已修复
              </div>
            )}
            {(board?.system_scoped_rows ?? 0) > 0 && (
              <div
                className="mb-2 text-[12px] text-muted-foreground"
                data-testid="profile-agent-board-system"
              >
                另有 {board?.system_scoped_rows} 条 system 桶记录(无会话的系统调度运行)独立统计,
                不并入本账号
              </div>
            )}
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
        <SectionPanel id="notifications"><NotifyChannelsSection key={identity} /></SectionPanel>
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
          <DataPrivacySection key={identity} username={profile?.username} />
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
  const identity = useIdentityScope()
  const { data, isLoading } = useApiQuery<{ keys: Array<{ key_prefix: string; status: string; tier: string }> }>(
    scopedKey(['api-keys', 'my'], identity),
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
