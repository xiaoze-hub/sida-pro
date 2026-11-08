/**
 * 认证守卫(2026-11-08 从 App.tsx 抽出并接入身份生命周期)。
 *
 * 原有职责不变: 未登录访问功能页 → 转 /login(首页 '/' 交给公开落地页); 首次登录落
 * /ai-copilot; 每次路由变化复检 `isAuthenticated()`。
 *
 * 新增的三条 fail-closed 约束(跨标签页换号的隐私残留):
 *   ① **换人瞬间先封锁**: `identity.phase === 'resolving'` 时只渲染过渡块 —— 旧用户常驻
 *      页签的面板(连同其 useState/在途回调)在这一帧整体卸载, 一个像素都不留给下一个人;
 *   ② **以账号为界重建**: 放行时 children 包在 `key={userId}` 的 Fragment 里 —— 即使某条
 *      路径没有经历过渡帧(同一次提交里换人), React 也按新 key 重建整棵子树, 不留旧实例;
 *   ③ **凭证不可解析/已登出 = 不放行**: 不看 `isAuthenticated()` 的宽松结果(它只看 token
 *      是否存在/未过期), 一律按未登录处理, 绝不回退到"先渲染旧页面再说"。
 *
 * 同一个人换发 token(撤销会话后重登/续期)时 `userId` 不变 ⇒ 不重建, 页签内状态保留。
 *
 * `identity` 由 App 持有并传入(而不是本组件自己订阅): 换号时 App 必须一并重算
 * 权限/角色/导航 —— 否则壳层会拿着上一个账号的 `myPerms` 重建。
 */
import { Fragment, useEffect, useState, type ReactNode } from 'react'
import { Navigate, useLocation } from 'react-router-dom'

import { isAuthenticated } from '@sida/api'

import type { AuthGateSnapshot } from '@/lib/identity-lifecycle'

type AuthGuardState = 'checking' | 'authenticated' | 'unauthenticated'

function FullPageSpinner() {
  return (
    <div className="min-h-screen flex items-center justify-center bg-background">
      <span className="w-6 h-6 border-2 border-primary/30 border-t-primary rounded-full animate-spin" />
    </div>
  )
}

/**
 * 身份过渡块: 只说"正在重新确认", 不携带上一个用户的任何标识/内容。
 * `role=status` + `aria-busy` 让读屏用户知道这是等待态, 而不是页面坏了。
 */
export function IdentityReloading() {
  return (
    <div
      data-testid="auth-identity-reloading"
      role="status"
      aria-live="polite"
      aria-busy="true"
      className="min-h-screen flex items-center justify-center bg-background"
    >
      <div className="flex flex-col items-center gap-3 text-muted-foreground">
        <span className="w-6 h-6 border-2 border-primary/30 border-t-primary rounded-full animate-spin" />
        <span className="text-[12px]">登录状态已变化，正在重新确认…</span>
      </div>
    </div>
  )
}

export interface RequireAuthProps {
  children: ReactNode
  /** 登录身份生命周期快照(App 持有: 换号时壳层权限/导航要一起重算) */
  identity: AuthGateSnapshot
  /** 未登录且当前在首页时要展示的公开落地页(App 传入, 认证层不直接依赖页面模块) */
  landing?: ReactNode
}

export default function RequireAuth({ children, identity, landing }: RequireAuthProps) {
  const location = useLocation()
  const [authState, setAuthState] = useState<AuthGuardState>('checking')

  // 路由变化 + **身份变化**都要复检登录态。原来只依赖 location.key: 另一个标签页登出后
  // 本页不导航, 守卫永远不会再问一次。
  useEffect(() => {
    setAuthState(isAuthenticated() ? 'authenticated' : 'unauthenticated')
  }, [location.key, identity.phase, identity.userId])

  // ① 换人确认窗口: 连"登录检查中"的 spinner 都不给 —— 那还是本标签页自己的壳层。
  if (identity.phase === 'resolving') return <IdentityReloading />

  if (authState === 'checking') return <FullPageSpinner />

  // 已经在登录页: 不再自我重定向(同路径带 state 的连续 replace 会变成渲染死循环)。
  // 生产里 App 对 /login 有 early-return, 这里是守卫自身的兜底。
  if (location.pathname === '/login') return null

  // ③ 未登录 / 凭证不可解析: 不放行任何 children。
  if (authState === 'unauthenticated' || identity.phase === 'blocked') {
    // 官网落地页(2026-09-16): 未登录访问 / 直接展示 Landing(不再跳 /login)
    if (location.pathname === '/') {
      return <>{landing ?? <Navigate to="/login" state={{ from: location }} replace />}</>
    }
    return <Navigate to="/login" state={{ from: location }} replace />
  }

  // L2-D(2026-10-24): 首次进入落 /ai-copilot(小白先看到"能问什么"), 老用户仍落首页。
  // 先写标记再跳转: /ai-copilot 有 PermGuard(view_quotes), 无权限用户会被弹回 `/`,
  // 此时标记已写 → 不会来回弹致死循环。隐私模式写不进去 → 视为老用户, 不跳。
  if (location.pathname === '/') {
    let firstRun = false
    try { firstRun = localStorage.getItem('sida_first_run_landed') === null } catch { /* 隐私模式 */ }
    if (firstRun) {
      try { localStorage.setItem('sida_first_run_landed', '1') } catch { /* 忽略 */ }
      return <Navigate to="/ai-copilot" replace />
    }
  }

  // ② 以账号为界: 换人即整棵子树重建(常驻页签的 useState 随之释放)。
  return <Fragment key={identity.userId ?? 'anonymous'}>{children}</Fragment>
}
