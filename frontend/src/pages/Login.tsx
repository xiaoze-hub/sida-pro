import { useState, useEffect, useRef, useCallback } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { BrandMark } from '@/components/BrandMark'
import { Lock, Eye, EyeOff, User, Mail, ShieldCheck, Ticket } from 'lucide-react'
import { authApi, fetchAPI, type AuthTokenPayload } from '@panwatch/api'
import { Button } from '@panwatch/base-ui/components/ui/button'
import { Input } from '@panwatch/base-ui/components/ui/input'
import { Label } from '@panwatch/base-ui/components/ui/label'
import { useToast } from '@panwatch/base-ui/components/ui/toast'

// 邮箱注册(2026-09-16): 前端格式校验(与后端 EMAIL_RE 同口径)
const EMAIL_RE = /^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$/

type AuthMode = 'password' | 'register' | 'email-code' | 'reset'

const CODE_RE = /^\d{6}$/

export default function LoginPage() {
  const navigate = useNavigate()
  const [searchParams] = useSearchParams()
  const { toast } = useToast()
  const [email, setEmail] = useState('')
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')
  const [code, setCode] = useState('')
  const [loading, setLoading] = useState(false)
  const [sending, setSending] = useState(false)
  const [countdown, setCountdown] = useState(0)
  const [showPassword, setShowPassword] = useState(false)
  const [isSetup, setIsSetup] = useState(false)
  // 合规(2026-09-16): 注册须勾选同意用户协议/隐私政策, 否则禁用注册按钮
  const [agreed, setAgreed] = useState(false)
  // 三种模式(2026-09-16): password(默认) / register / email-code
  const [mode, setMode] = useState<AuthMode>(() =>
    searchParams.get('mode') === 'register' ? 'register' : 'password',
  )
  const [checking, setChecking] = useState(true)
  // 邮件服务可用性(来自 /api/auth/status): 默认按"可用"渲染, 只有明确 false 才提示
  // 注册模式(2026-09-19 内部使用): invite 需邀请码 / open / closed
  const [registerMode, setRegisterMode] = useState<'invite' | 'open' | 'closed'>('invite')
  const [inviteCode, setInviteCode] = useState('')
  const [emailReady, setEmailReady] = useState(true)
  // 注册自动签发的 API Key 明文(仅此一次): 后端建 key 失败时为 null, 静默不弹
  const [issuedApiKey, setIssuedApiKey] = useState<string | null>(null)
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null)

  useEffect(() => {
    // 检查认证状态
    authApi.status()
      .then(data => {
        setIsSetup(!data.initialized)
        // 未配置邮件服务时, 邮箱注册/验证码登录这条路径**发不出验证码** —— 提前告知,
        // 别让用户填完表单才撞 503(以前还会显示"已发送", 更误导)。
        setEmailReady(data.email_configured !== false)
        // 内部使用模式(2026-09-19): 后端告知注册模式, 前端据此渲染邀请码/关闭态
        if (data.register_mode) setRegisterMode(data.register_mode)
        setChecking(false)
      })
      .catch(() => setChecking(false))
  }, [])

  // 倒计时清理
  useEffect(() => {
    return () => {
      if (timerRef.current) clearInterval(timerRef.current)
    }
  }, [])

  const startCountdown = useCallback(() => {
    setCountdown(60)
    if (timerRef.current) clearInterval(timerRef.current)
    timerRef.current = setInterval(() => {
      setCountdown(prev => {
        if (prev <= 1) {
          if (timerRef.current) clearInterval(timerRef.current)
          timerRef.current = null
          return 0
        }
        return prev - 1
      })
    }, 1000)
  }, [])

  const saveToken = (data: AuthTokenPayload) => {
    localStorage.setItem('token', data.token)
    localStorage.setItem('token_expires', data.expires_at)
    if (data.user) {
      localStorage.setItem('user', JSON.stringify(data.user))
    }
  }

  const doLogin = async (uname: string, pwd: string) => {
    const data = await authApi.login({ username: uname, password: pwd })
    saveToken(data)
  }

  const handleSendCode = async () => {
    const em = email.trim()
    if (!EMAIL_RE.test(em)) {
      toast('请输入有效的邮箱地址', 'error')
      return
    }
    if (countdown > 0 || sending) return
    setSending(true)
    try {
      const purpose = mode === 'register' ? 'register' : mode === 'reset' ? 'reset' : 'login'
      // purpose 含 reset(公共包 authApi.sendCode 的类型尚未扩展, 这里走底层 fetchAPI)
      await fetchAPI<{ sent: boolean }>('/auth/send-code', {
        method: 'POST',
        body: JSON.stringify({ email: em, purpose }),
      })
      startCountdown()
      toast('验证码已发送, 请注意查收', 'success')
    } catch (e) {
      toast(e instanceof Error ? e.message : '发送失败', 'error')
    } finally {
      setSending(false)
    }
  }

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()

    if (mode === 'email-code') {
      const em = email.trim()
      if (!EMAIL_RE.test(em)) {
        toast('请输入有效的邮箱地址', 'error')
        return
      }
      if (!CODE_RE.test(code.trim())) {
        toast('请输入 6 位数字验证码', 'error')
        return
      }
      setLoading(true)
      try {
        const data = await authApi.loginByEmail(em, code.trim())
        saveToken(data)
        toast('登录成功', 'success')
        navigate('/')
      } catch (err) {
        toast(err instanceof Error ? err.message : '登录失败', 'error')
      } finally {
        setLoading(false)
      }
      return
    }

    if (mode === 'reset') {
      // 忘记密码(2026-10-23): 邮箱验证码重置。成功后不自动登录, 切回密码登录。
      const em = email.trim()
      if (!EMAIL_RE.test(em)) {
        toast('请输入有效的邮箱地址', 'error')
        return
      }
      if (!CODE_RE.test(code.trim())) {
        toast('请输入 6 位数字验证码', 'error')
        return
      }
      if (password.length < 8) {
        toast('新密码长度至少 8 位', 'error')
        return
      }
      if (password !== confirmPassword) {
        toast('两次密码不一致', 'error')
        return
      }
      setLoading(true)
      try {
        // 未登录用户没有 csrf_token Cookie(只在登录/注册后下发) → 先引导取一枚,
        // 否则双提交中间件会把本请求 403「CSRF token 缺失」。
        const { csrf_token } = await fetchAPI<{ csrf_token: string }>('/auth/csrf')
        await fetchAPI<{ message: string }>('/auth/reset-password-by-email', {
          method: 'POST',
          headers: { 'X-CSRF-Token': csrf_token },
          body: JSON.stringify({ email: em, code: code.trim(), new_password: password }),
        })
        toast('密码已重置, 请用新密码登录', 'success')
        setPassword('')
        setConfirmPassword('')
        switchMode('password')
      } catch (err) {
        toast(err instanceof Error ? err.message : '重置失败', 'error')
      } finally {
        setLoading(false)
      }
      return
    }

    if (mode === 'register') {
      if (!email || !password || !code) return
      if (!agreed) {
        toast('请先阅读并同意《用户协议》和《隐私政策》', 'error')
        return
      }
    } else if (!username || !password) {
      return
    }

    if (mode === 'register') {
      // 注册表单校验: 邮箱必填; 用户名可选(2-20 位字母数字); 密码 ≥8 位, 两次一致; 验证码 6 位
      if (!EMAIL_RE.test(email.trim())) {
        toast('请输入有效的邮箱地址', 'error')
        return
      }
      const uname = username.trim()
      if (uname && !/^[A-Za-z0-9]{2,20}$/.test(uname)) {
        toast('用户名需为 2-20 位字母或数字', 'error')
        return
      }
      if (!CODE_RE.test(code.trim())) {
        toast('请输入 6 位数字验证码', 'error')
        return
      }
      if (password !== confirmPassword) {
        toast('两次密码不一致', 'error')
        return
      }
      if (password.length < 8) {
        toast('密码长度至少 8 位', 'error')
        return
      }
    } else if (isSetup) {
      if (password !== confirmPassword) {
        toast('两次密码不一致', 'error')
        return
      }
      if (password.length < 8) {
        toast('密码长度至少 8 位', 'error')
        return
      }
    }

    // 邀请制(2026-09-19): 没填邀请码就别发请求 —— 后端会 400, 但前端先说更省事
    if (mode === 'register' && registerMode === 'invite' && !inviteCode.trim()) {
      toast('请填写邀请码（内测邀请制）', 'error')
      return
    }

    setLoading(true)
    try {
      if (mode === 'register') {
        // 注册(member 账号); 失败时 fetchAPI 抛出后端 message
        // username 可选: 不传则后端用邮箱前缀生成
        const uname = username.trim()
        const reg = await fetchAPI<{
          message?: string
          api_key?: string | null
          username?: string
          token?: string
          expires_at?: string
          user?: AuthTokenPayload['user']
        }>('/auth/register', {
          method: 'POST',
          body: JSON.stringify({
            email: email.trim(),
            password,
            code: code.trim(),
            // 邀请码(2026-09-19): invite 模式后端强制校验, 不传会被 400 拒
            ...(inviteCode.trim() ? { invite_code: inviteCode.trim() } : {}),
            ...(uname ? { username: uname } : {}),
          }),
        })
        // 2026-10-23: 注册响应本身就带 token/expires_at(后端已签发并下发 Cookie),
        // 旧代码丢弃它再调 doLogin → 第二次 scrypt 校验(~120ms) + 第二条 login 审计。
        // 直接用注册返回的凭证落本地, 不再重复登录。
        if (!reg.token || !reg.expires_at) {
          throw new Error('注册成功但未返回登录凭证, 请用密码登录')
        }
        saveToken({ token: reg.token, expires_at: reg.expires_at, user: reg.user })

        if (reg.api_key) {
          // 自动签发的 API Key 明文只回这一次 —— 弹一次性提示层, 用户确认后再跳转
          setIssuedApiKey(reg.api_key)
          toast('注册成功', 'success')
          return
        }
        // 后端建 key 失败时 api_key 为 null: 静默, 不阻断注册
        toast('注册成功, 已自动登录', 'success')
        navigate('/')
        return
      }

      await doLogin(username, password)
      toast(isSetup ? '密码设置成功' : '登录成功', 'success')
      navigate('/')
    } catch (e) {
      toast(e instanceof Error ? e.message : '操作失败', 'error')
    } finally {
      setLoading(false)
    }
  }

  const copyIssuedKey = async () => {
    if (!issuedApiKey) return
    try {
      await navigator.clipboard.writeText(issuedApiKey)
      toast('已复制到剪贴板', 'success')
    } catch {
      toast('复制失败, 请手动选中复制', 'error')
    }
  }

  const dismissIssuedKey = () => {
    setIssuedApiKey(null)
    navigate('/')
  }

  const switchMode = (next: AuthMode) => {
    setMode(next)
    setCode('')
    if (timerRef.current) {
      clearInterval(timerRef.current)
      timerRef.current = null
    }
    setCountdown(0)
  }

  if (checking) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-background">
        <span className="w-6 h-6 border-2 border-primary/30 border-t-primary rounded-full animate-spin" />
      </div>
    )
  }

  const isRegister = mode === 'register'
  const isEmailCode = mode === 'email-code'
  const isReset = mode === 'reset'
  // 注册关闭(closed)时不给"注册"入口 —— 点了也只会被 403, 不如不显示
  const showModeTabs = !isSetup
  const modeTabs = [
    { key: 'password' as AuthMode, label: '密码登录' },
    ...(registerMode === 'closed' ? [] : [{ key: 'register' as AuthMode, label: '注册' }]),
    { key: 'email-code' as AuthMode, label: '验证码登录' },
    { key: 'reset' as AuthMode, label: '忘记密码' },
  ]

  return (
    <div className="min-h-screen flex items-center justify-center bg-background px-4">
      <div className="w-full max-w-sm">
        {/* Logo */}
        <div className="flex flex-col items-center mb-8">
          <div className="w-16 h-16 rounded-xl bg-primary flex items-center justify-center mb-4">
            <BrandMark className="w-9 h-9 text-white" />
          </div>
          <h1 className="text-[20px] font-bold text-foreground">数智分析 SIDA</h1>
          <p className="text-[13px] text-muted-foreground mt-1">行情 · 资金 · 情报 · 决策，一个工作台看完</p>
        </div>

        {/* Form(P2 轻量化: card 底 → hairline, 零逻辑改动) */}
        <div className="border-t border-border/40 pt-6">
          <div className="flex items-center gap-2 mb-4">
            <Lock className="w-5 h-5 text-primary" />
            <h2 className="text-[16px] font-semibold">
              {isRegister ? '注册账号' : isEmailCode ? '验证码登录' : isReset ? '重置密码' : isSetup ? '设置访问密码' : '登录'}
            </h2>
          </div>

          {/* 三种模式切换 Tab */}
          {showModeTabs && (
            <div className="flex gap-1 mb-6 p-1 rounded-lg bg-muted/50">
              {modeTabs.map(tab => (
                <button
                  key={tab.key}
                  type="button"
                  onClick={() => switchMode(tab.key)}
                  className={
                    'flex-1 py-1.5 text-[13px] rounded-md transition-colors ' +
                    (mode === tab.key
                      ? 'bg-background text-foreground font-medium shadow-sm'
                      : 'text-muted-foreground hover:text-foreground')
                  }
                >
                  {tab.label}
                </button>
              ))}
            </div>
          )}

          {isSetup && !isRegister && !isEmailCode && (
            <p className="text-[13px] text-muted-foreground mb-4">
              首次使用，请设置访问密码以保护您的数据
            </p>
          )}

          <form onSubmit={handleSubmit} className="space-y-4">
            {/* 邀请制(2026-09-19 内部使用): 注册需管理员发放的邀请码; closed 模式直接不给填 */}
            {isRegister && registerMode === 'invite' && (
              <div>
                <Label>邀请码</Label>
                <div className="relative">
                  <Ticket className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground" />
                  <Input
                    type="text"
                    value={inviteCode}
                    onChange={e => setInviteCode(e.target.value.toUpperCase())}
                    placeholder="请输入管理员发放的邀请码"
                    className="pl-10 tracking-widest"
                    autoFocus
                    autoComplete="off"
                  />
                </div>
                <p className="mt-1 text-[12px] text-muted-foreground">
                  本平台为内测邀请制，邀请码由管理员发放。
                </p>
              </div>
            )}
            {(isRegister || isEmailCode || isReset) && (
              <div>
                <Label>邮箱</Label>
                <div className="relative">
                  <Mail className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground" />
                  <Input
                    type="email"
                    value={email}
                    onChange={e => setEmail(e.target.value)}
                    placeholder="请输入邮箱地址"
                    className="pl-10"
                    autoFocus
                    autoComplete="email"
                  />
                </div>
              </div>
            )}

            {/* 邮件服务未配置: 这条路径发不出验证码 —— 直说, 别让用户白填一遍 */}
            {!emailReady && (isRegister || isEmailCode || isReset) && (
              <div className="rounded-lg border border-amber-500/40 bg-amber-500/5 px-3 py-2 text-[12px] leading-relaxed">
                邮件服务暂未开通，验证码发不出去。请把邮箱发给管理员手工开通账号，
                或改用「密码登录」。
              </div>
            )}

            {(isRegister || isEmailCode || isReset) && (
              <div>
                <Label>验证码</Label>
                <div className="flex gap-2">
                  <div className="relative flex-1">
                    <ShieldCheck className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground" />
                    <Input
                      type="text"
                      inputMode="numeric"
                      maxLength={6}
                      value={code}
                      onChange={e => setCode(e.target.value.replace(/\D/g, ''))}
                      placeholder="6 位数字验证码"
                      className="pl-10 tracking-widest"
                      autoComplete="one-time-code"
                    />
                  </div>
                  <Button
                    type="button"
                    variant="outline"
                    className="shrink-0 w-28"
                    disabled={countdown > 0 || sending || !emailReady}
                    title={emailReady ? '' : '邮件服务未配置, 验证码发不出去'}
                    onClick={handleSendCode}
                  >
                    {sending
                      ? '发送中…'
                      : countdown > 0
                        ? `${countdown}s`
                        : '发送验证码'}
                  </Button>
                </div>
              </div>
            )}

            {!isEmailCode && !isReset && (
              <div>
                <Label>
                  {isRegister ? '用户名(可选)' : '用户名'}
                </Label>
                <div className="relative">
                  <User className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground" />
                  <Input
                    type="text"
                    value={username}
                    onChange={e => setUsername(e.target.value)}
                    placeholder={isRegister ? '可选, 不填则用邮箱前缀' : '请输入用户名'}
                    className="pl-10"
                    autoFocus={!isRegister}
                    autoComplete="username"
                  />
                </div>
              </div>
            )}

            {!isEmailCode && (
              <div>
                <Label>{isReset ? '新密码' : '密码'}</Label>
                <div className="relative">
                  <Lock className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground" />
                  <Input
                    type={showPassword ? 'text' : 'password'}
                    value={password}
                    onChange={e => setPassword(e.target.value)}
                    placeholder={isRegister || isSetup || isReset ? '至少 8 位' : '请输入密码'}
                    className="pl-10 pr-10"
                    autoComplete={isRegister || isSetup || isReset ? 'new-password' : 'current-password'}
                  />
                  <Button
                    type="button"
                    variant="ghost"
                    size="icon"
                    className="absolute right-1 top-1/2 -translate-y-1/2 h-8 w-8"
                    onClick={() => setShowPassword(!showPassword)}
                  >
                    {showPassword ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
                  </Button>
                </div>
              </div>
            )}

            {/* 忘记密码入口(2026-10-23): 密码登录表单下方一行文字链接 */}
            {!isRegister && !isEmailCode && !isReset && !isSetup && (
              <div className="-mt-1 text-right">
                <button
                  type="button"
                  className="text-[11px] text-primary transition-colors hover:underline"
                  onClick={() => switchMode('reset')}
                >
                  忘记密码？
                </button>
              </div>
            )}

            {(isRegister || isSetup || isReset) && !isEmailCode && (
              <div>
                <Label>确认密码</Label>
                <Input
                  type={showPassword ? 'text' : 'password'}
                  value={confirmPassword}
                  onChange={e => setConfirmPassword(e.target.value)}
                  placeholder="再次输入密码"
                  autoComplete="new-password"
                />
              </div>
            )}

            {/* 合规(2026-09-16): 注册必勾用户协议 + 隐私政策 */}
            {isRegister && (
              <label className="flex cursor-pointer items-start gap-2 select-none">
                <input
                  type="checkbox"
                  className="mt-0.5 h-4 w-4 shrink-0 rounded border-border accent-[hsl(var(--primary))]"
                  checked={agreed}
                  onChange={e => setAgreed(e.target.checked)}
                />
                <span className="text-[12px] leading-relaxed text-muted-foreground">
                  我已阅读并同意
                  <Link
                    to="/terms?tab=agreement"
                    className="mx-0.5 text-primary hover:underline"
                    target="_blank"
                    rel="noopener noreferrer"
                  >
                    《用户协议》
                  </Link>
                  和
                  <Link
                    to="/terms?tab=privacy"
                    className="mx-0.5 text-primary hover:underline"
                    target="_blank"
                    rel="noopener noreferrer"
                  >
                    《隐私政策》
                  </Link>
                </span>
              </label>
            )}

            <Button type="submit" className="w-full" disabled={loading || (isRegister && !agreed)}>
              {loading ? (
                <span className="w-4 h-4 border-2 border-white/30 border-t-white rounded-full animate-spin" />
              ) : isRegister ? (
                '注册'
              ) : isReset ? (
                '重置密码'
              ) : isEmailCode ? (
                '验证码登录'
              ) : isSetup ? (
                '设置密码并进入'
              ) : (
                '登录'
              )}
            </Button>
          </form>

          {/* 模式切换链接(Tab 备用, 保持轻量入口) */}
          {showModeTabs && (
            <div className="mt-4 text-center text-[13px]">
              {isRegister ? (
                <span className="text-muted-foreground">
                  已有账号？{' '}
                  <button
                    type="button"
                    className="text-primary hover:underline font-medium"
                    onClick={() => switchMode('password')}
                  >
                    去登录
                  </button>
                </span>
              ) : isReset ? (
                <span className="text-muted-foreground">
                  想起密码了？{' '}
                  <button
                    type="button"
                    className="text-primary hover:underline font-medium"
                    onClick={() => switchMode('password')}
                  >
                    返回密码登录
                  </button>
                </span>
              ) : isEmailCode ? (
                <span className="text-muted-foreground">
                  想用密码登录？{' '}
                  <button
                    type="button"
                    className="text-primary hover:underline font-medium"
                    onClick={() => switchMode('password')}
                  >
                    密码登录
                  </button>
                </span>
              ) : (
                <span className="text-muted-foreground">
                  还没有账号？{' '}
                  <button
                    type="button"
                    className="text-primary hover:underline font-medium"
                    onClick={() => switchMode('register')}
                  >
                    注册账号
                  </button>
                </span>
              )}
            </div>
          )}
        </div>

        <p className="text-center text-xs text-muted-foreground mt-6">
          AI 驱动的股票数据分析助手 · 分析结果仅供参考，不构成投资建议
        </p>

        {/* 注册自动签发的 API Key: 明文仅此一次, 关掉就再也看不到 */}
        {issuedApiKey && (
          <div
            className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 px-4"
            role="dialog"
            aria-modal="true"
            aria-labelledby="issued-key-title"
          >
            <div className="w-full max-w-md rounded-lg border border-border/60 bg-background p-5 shadow-float">
              <h2 id="issued-key-title" className="text-[16px] font-semibold text-foreground">
                你的 API Key，仅显示这一次
              </h2>
              <p className="mt-2 text-[12px] leading-relaxed text-muted-foreground">
                关闭后无法再次查看明文。请立即复制并妥善保存。
              </p>
              <pre className="mt-3 max-h-32 overflow-auto rounded-md border border-border/40 bg-muted/40 p-3 font-mono text-[12px] text-foreground whitespace-pre-wrap break-all">
                {issuedApiKey}
              </pre>
              <div className="mt-3 flex gap-2">
                <Button
                  type="button"
                  variant="outline"
                  className="h-8 flex-1 text-[12px]"
                  onClick={() => void copyIssuedKey()}
                >
                  复制
                </Button>
                <Button type="button" className="h-8 flex-1 text-[12px]" onClick={dismissIssuedKey}>
                  我已保存
                </Button>
              </div>
              <p className="mt-2.5 text-[11px] leading-relaxed text-muted-foreground">
                之后可在
                <Link to="/api-keys" className="mx-0.5 text-primary hover:underline">
                  我的 API Key
                </Link>
                页面查看前缀或重新生成。
              </p>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
