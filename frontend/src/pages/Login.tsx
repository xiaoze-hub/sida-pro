import { useState, useEffect } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { BrandMark } from '@/components/BrandMark'
import { Lock, Eye, EyeOff, User } from 'lucide-react'
import { authApi, fetchAPI } from '@panwatch/api'
import { Button } from '@panwatch/base-ui/components/ui/button'
import { Input } from '@panwatch/base-ui/components/ui/input'
import { Label } from '@panwatch/base-ui/components/ui/label'
import { useToast } from '@panwatch/base-ui/components/ui/toast'

export default function LoginPage() {
  const navigate = useNavigate()
  const [searchParams] = useSearchParams()
  const { toast } = useToast()
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')
  const [loading, setLoading] = useState(false)
  const [showPassword, setShowPassword] = useState(false)
  const [isSetup, setIsSetup] = useState(false)
  // 开放注册(2026-09-16): 注册模式可用, 支持 ?mode=register 直达
  const [registerMode, setRegisterMode] = useState(() => searchParams.get('mode') === 'register')
  const [checking, setChecking] = useState(true)

  useEffect(() => {
    // 检查认证状态
    authApi.status()
      .then(data => {
        setIsSetup(!data.initialized)
        setChecking(false)
      })
      .catch(() => setChecking(false))
  }, [])

  const doLogin = async (uname: string, pwd: string) => {
    const data = await authApi.login({ username: uname, password: pwd })
    // 保存 token
    localStorage.setItem('token', data.token)
    localStorage.setItem('token_expires', data.expires_at)
    // 保存用户信息(多用户)
    if (data.user) {
      localStorage.setItem('user', JSON.stringify(data.user))
    }
  }

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!username || !password) return

    if (registerMode) {
      // 注册表单校验: 用户名 2-20 位字母数字, 密码 ≥8 位, 两次一致
      if (!/^[A-Za-z0-9]{2,20}$/.test(username)) {
        toast('用户名需为 2-20 位字母或数字', 'error')
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
      if (registerMode) {
        // 注册(member 账号); 失败时 fetchAPI 抛出后端 message
        await fetchAPI<{ message?: string; api_key?: string | null }>('/auth/register', {
          method: 'POST',
          body: JSON.stringify({ username, password }),
        })
        // 注册成功后自动登录并跳转首页
        await doLogin(username, password)
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

  if (checking) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-background">
        <span className="w-6 h-6 border-2 border-primary/30 border-t-primary rounded-full animate-spin" />
      </div>
    )
  }

  return (
    <div className="min-h-screen flex items-center justify-center bg-background px-4">
      <div className="w-full max-w-sm">
        {/* Logo */}
        <div className="flex flex-col items-center mb-8">
          <div className="w-16 h-16 rounded-2xl bg-primary flex items-center justify-center mb-4">
            <BrandMark className="w-9 h-9 text-white" />
          </div>
          <h1 className="text-2xl font-bold text-foreground">数智分析 SIDA</h1>
          <p className="text-sm text-muted-foreground mt-1">A股智能分析 · AI 全链路打通</p>
        </div>

        {/* Form(P2 轻量化: card 底 → hairline, 零逻辑改动) */}
        <div className="border-t border-border/40 pt-6">
          <div className="flex items-center gap-2 mb-6">
            <Lock className="w-5 h-5 text-primary" />
            <h2 className="text-lg font-semibold">
              {registerMode ? '注册账号' : isSetup ? '设置访问密码' : '登录'}
            </h2>
          </div>

          {isSetup && !registerMode && (
            <p className="text-sm text-muted-foreground mb-4">
              首次使用，请设置访问密码以保护您的数据
            </p>
          )}

          <form onSubmit={handleSubmit} className="space-y-4">
            <div>
              <Label>{registerMode ? '用户名(2-20 位字母数字)' : '用户名'}</Label>
              <div className="relative">
                <User className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground" />
                <Input
                  type="text"
                  value={username}
                  onChange={e => setUsername(e.target.value)}
                  placeholder="请输入用户名"
                  className="pl-10"
                  autoFocus
                />
              </div>
            </div>

            <div>
              <Label>密码</Label>
              <div className="relative">
                <Lock className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground" />
                <Input
                  type={showPassword ? 'text' : 'password'}
                  value={password}
                  onChange={e => setPassword(e.target.value)}
                  placeholder={registerMode || isSetup ? '至少 8 位' : '请输入密码'}
                  className="pl-10 pr-10"
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

            {(registerMode || isSetup) && (
              <div>
                <Label>确认密码</Label>
                <Input
                  type={showPassword ? 'text' : 'password'}
                  value={confirmPassword}
                  onChange={e => setConfirmPassword(e.target.value)}
                  placeholder="再次输入密码"
                />
              </div>
            )}

            <Button type="submit" className="w-full" disabled={loading}>
              {loading ? (
                <span className="w-4 h-4 border-2 border-white/30 border-t-white rounded-full animate-spin" />
              ) : registerMode ? (
                '注册'
              ) : isSetup ? (
                '设置密码并进入'
              ) : (
                '登录'
              )}
            </Button>
          </form>

          {/* 登录/注册切换 */}
          {!isSetup && (
            <div className="mt-4 text-center text-sm">
              {registerMode ? (
                <span className="text-muted-foreground">
                  已有账号？{' '}
                  <button
                    type="button"
                    className="text-primary hover:underline font-medium"
                    onClick={() => setRegisterMode(false)}
                  >
                    去登录
                  </button>
                </span>
              ) : (
                <span className="text-muted-foreground">
                  还没有账号？{' '}
                  <button
                    type="button"
                    className="text-primary hover:underline font-medium"
                    onClick={() => setRegisterMode(true)}
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
