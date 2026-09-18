import { useState, useEffect, useRef, useCallback } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { BrandMark } from '@/components/BrandMark'
import { Lock, Eye, EyeOff, User, Mail, ShieldCheck } from 'lucide-react'
import { authApi, fetchAPI, type AuthTokenPayload } from '@panwatch/api'
import { Button } from '@panwatch/base-ui/components/ui/button'
import { Input } from '@panwatch/base-ui/components/ui/input'
import { Label } from '@panwatch/base-ui/components/ui/label'
import { useToast } from '@panwatch/base-ui/components/ui/toast'

// 邮箱注册(2026-09-16): 前端格式校验(与后端 EMAIL_RE 同口径)
const EMAIL_RE = /^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$/

type AuthMode = 'password' | 'register' | 'email-code'

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
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null)

  useEffect(() => {
    // 检查认证状态
    authApi.status()
      .then(data => {
        setIsSetup(!data.initialized)
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
      const purpose = mode === 'register' ? 'register' : 'login'
      await authApi.sendCode(em, purpose)
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

    setLoading(true)
    try {
      if (mode === 'register') {
        // 注册(member 账号); 失败时 fetchAPI 抛出后端 message
        // username 可选: 不传则后端用邮箱前缀生成
        const uname = username.trim()
        const reg = await fetchAPI<{ message?: string; api_key?: string | null; username?: string }>('/auth/register', {
          method: 'POST',
          body: JSON.stringify({
            email: email.trim(),
            password,
            code: code.trim(),
            ...(uname ? { username: uname } : {}),
          }),
        })
        // 注册成功后自动登录并跳转首页(用后端回传的实际用户名, 兼容自动生成场景)
        const loginName = reg.username || uname || email.trim()
        await doLogin(loginName, password)
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
  const showModeTabs = !isSetup

  return (
    <div className="min-h-screen flex items-center justify-center bg-background px-4">
      <div className="w-full max-w-sm">
        {/* Logo */}
        <div className="flex flex-col items-center mb-8">
          <div className="w-16 h-16 rounded-2xl bg-primary flex items-center justify-center mb-4">
            <BrandMark className="w-9 h-9 text-white" />
          </div>
          <h1 className="text-[20px] font-bold text-foreground">数智分析 SIDA</h1>
          <p className="text-[13px] text-muted-foreground mt-1">A股智能分析 · AI 全链路打通</p>
        </div>

        {/* Form(P2 轻量化: card 底 → hairline, 零逻辑改动) */}
        <div className="border-t border-border/40 pt-6">
          <div className="flex items-center gap-2 mb-4">
            <Lock className="w-5 h-5 text-primary" />
            <h2 className="text-[16px] font-semibold">
              {isRegister ? '注册账号' : isEmailCode ? '验证码登录' : isSetup ? '设置访问密码' : '登录'}
            </h2>
          </div>

          {/* 三种模式切换 Tab */}
          {showModeTabs && (
            <div className="flex gap-1 mb-6 p-1 rounded-lg bg-muted/50">
              {([
                { key: 'password' as AuthMode, label: '密码登录' },
                { key: 'register' as AuthMode, label: '注册' },
                { key: 'email-code' as AuthMode, label: '验证码登录' },
              ]).map(tab => (
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
            {(isRegister || isEmailCode) && (
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

            {(isRegister || isEmailCode) && (
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
                    disabled={countdown > 0 || sending}
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

            {!isEmailCode && (
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
                <Label>密码</Label>
                <div className="relative">
                  <Lock className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground" />
                  <Input
                    type={showPassword ? 'text' : 'password'}
                    value={password}
                    onChange={e => setPassword(e.target.value)}
                    placeholder={isRegister || isSetup ? '至少 8 位' : '请输入密码'}
                    className="pl-10 pr-10"
                    autoComplete={isRegister || isSetup ? 'new-password' : 'current-password'}
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

            {(isRegister || isSetup) && !isEmailCode && (
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
      </div>
    </div>
  )
}
