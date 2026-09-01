import { useState, useEffect, useRef, lazy, Suspense } from 'react'
import { Routes, Route, NavLink, useLocation, useNavigate, Navigate } from 'react-router-dom'
import { TrendingUp, ScrollText, Settings, List, Clock, LayoutDashboard, Github, BellRing, Sparkles, Activity, LineChart, FileText, Shield, User, Bell, PanelLeftClose, PanelLeftOpen, ServerCog } from 'lucide-react'
import { useTheme } from '@/hooks/use-theme'
import { useHotkeys } from '@/hooks/use-hotkeys'
import { appApi, fetchAPI, getMyPermissions, isAuthenticated } from '@panwatch/api'
// 2026-08-12 性能优化: 路由懒加载 — 17 个页面原本静态 import 打进单 bundle 1.2MB,
// 点任意路由都要下载/解析整个应用。改为 React.lazy 按需加载, 首屏只下载登录页+当前页。
const DashboardPage = lazy(() => import('@/pages/Dashboard'))
const OpportunitiesPage = lazy(() => import('@/pages/Opportunities'))
const StocksPage = lazy(() => import('@/pages/Stocks'))
// §4.3: Settings/Agents/DataSources/Help/Audit/Forecast 已不再由 App 直接挂载 —
// 分别由 SettingsHub / System / Quote 三个枢纽页内部懒加载, 避免首屏多拉 6 个 chunk。
// §4.3 补齐(2026-09-01): History/Reports/PaperTrading/PriceAlerts/Notifications/
// ShadowAccount 已由各枢纽页(ReportsHub/ShadowHub/NotificationsHub)内部懒加载,
// App.tsx 不再直接挂载, 避免首屏多拉 6 个 chunk。
const AnalysisDetailPage = lazy(() => import('@/pages/AnalysisDetail'))
const LoginPage = lazy(() => import('@/pages/Login'))
const IndexDetailPage = lazy(() => import('@/pages/IndexDetail'))
const BoardDetailPage = lazy(() => import('@/pages/BoardDetail'))
const ProfilePage = lazy(() => import('@/pages/Profile'))
// 设计稿 v2.0 §4.3 (2026-09-01): 行情三合一页 + 两个收纳枢纽页
const QuotePage = lazy(() => import('@/pages/Quote'))
const SystemPage = lazy(() => import('@/pages/System'))
const SettingsHubPage = lazy(() => import('@/pages/SettingsHub'))
// §4.3 补齐(2026-09-01 下午): 历史并入报告 / 模拟盘并入影子 / 提醒并入通知。
// 这三项属第 1 块, 产物被同步事故冲掉后重建时派活清单漏列, 对照设计稿补做。
const ReportsHubPage = lazy(() => import('@/pages/ReportsHub'))
const ShadowHubPage = lazy(() => import('@/pages/ShadowHub'))
const NotificationsHubPage = lazy(() => import('@/pages/NotificationsHub'))
import LogsModal from '@panwatch/biz-ui/components/logs-modal'
import AmbientBackground from '@panwatch/biz-ui/components/AmbientBackground'
import NotificationBell from '@panwatch/biz-ui/components/notification-bell'
import ChatWidget from '@/components/ChatWidget'
import BrowserNotificationBridge from '@/components/BrowserNotificationBridge'
import AccountMenu from '@/components/AccountMenu'
import SelfCheckModal from '@/components/SelfCheckModal'
import CommandPalette from '@/components/CommandPalette'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@panwatch/base-ui/components/ui/dialog'
import { Button } from '@panwatch/base-ui/components/ui/button'
import AppErrorBoundary from '@/components/ErrorBoundary'

const navItems = [
  { to: '/', icon: LayoutDashboard, label: '首页', perm: 'view_dashboard' },
  // §4.3 行情三合一: /forecast 由「预测」升为「行情」入口
  { to: '/forecast', icon: LineChart, label: '行情', perm: 'view_forecast' },
  { to: '/opportunities', icon: Sparkles, label: '机会', perm: 'view_opportunities' },
  { to: '/reports', icon: FileText, label: '报告', perm: 'view_reports' },
  { to: '/history', icon: Clock, label: '历史' },
  { to: '/portfolio', icon: List, label: '持仓', perm: 'edit_portfolio' },
  { to: '/shadow', icon: Shield, label: '影子账户', perm: 'manage_shadow' },
  { to: '/paper-trading', icon: Activity, label: '模拟盘', perm: 'manage_paper_trading' },
  { to: '/profile', icon: User, label: '个人中心' },
  // §4.3: Agent + 数据源 收纳进「系统」二级页(/agents /datasources 保留为重定向)
  { to: '/system', icon: ServerCog, label: '系统' },
  { to: '/notifications', icon: Bell, label: '通知' },
  { to: '/alerts', icon: BellRing, label: '提醒' },
  // §4.3: 审计 + 帮助 收纳进「设置」页签(/audit /help 保留为重定向)
  { to: '/settings', icon: Settings, label: '设置' },
]
// 设计稿 v2.0 §4.2/§4.3: 6 项主导航(驾驶舱/行情/机会/投研/我的/系统), 取代原 21 项扁平三组。
// 合并优化: 预测并入行情 / 历史并入投研 / 模拟盘并入我的 / 提醒并入系统(通知)。个股/指数/板块为详情页(行情域), 经搜索进入。
const desktopNavGroups = [
  { key: 'cockpit', label: '驾驶舱', items: navItems.filter(n => n.to === '/') },
  { key: 'market', label: '行情', items: navItems.filter(n => ['/forecast'].includes(n.to)) },
  { key: 'opportunity', label: '机会', items: navItems.filter(n => ['/opportunities'].includes(n.to)) },
  // §4.3 补齐(2026-09-01): 历史并入报告 / 模拟盘并入影子 / 提醒并入通知 后,
  // 投研 2→1 项、我的 4→3 项、系统 4→3 项(全部经 ?tab= 直达, 快捷键兜底不变)
  { key: 'research', label: '投研', items: navItems.filter(n => ['/reports'].includes(n.to)) },
  { key: 'mine', label: '我的', items: navItems.filter(n => ['/portfolio', '/shadow', '/profile'].includes(n.to)) },
  // §4.3: 系统域从 7 项瘦身到 3 项(Agent/数据源→系统页, 审计/帮助→设置页签, 提醒→通知页签)
  { key: 'system', label: '系统', items: navItems.filter(n => ['/system', '/notifications', '/settings'].includes(n.to)) },
]
// 移动端底部 5 槽位按 to 路径挑选: 首页/持仓/机会/预测/通知(2026-09-01 §4.3 补齐:
// 提醒并入通知后底栏由 /alerts 改指 /notifications; 提醒 Tab 在通知页内直达,
// 老书签 /alerts 仍经 LegacyTabRedirect 跳到 /notifications?tab=alerts)。
// navItems 数组顺序保持不动, 桌面端平铺分组完全不变。
const MOBILE_PRIMARY_TO = ['/', '/portfolio', '/opportunities', '/forecast', '/notifications']

// ═══ demo 账号只读模式(2026-08-15): 从 JWT payload 解出 username/role, 按角色控制导航 ═══
// 修复(M-2, 2026-08-23): atob 解 JWT payload 必须容错:
// - token 不是合法 base64(空 token / 第三方篡改) → JSON.parse 抛 → catch 返回 null
// - token 仅有两段(去除签名/头为空) → split('.')[1] 拿到 undefined → atob 抛
// - payload 是数组/其它非对象类型(JSON.parse 合法但结构异常) → null 安全返回
// 仅用于 UI 展示用途的 claims 解析, 不参与授权判断(权限最终看后端).
function _safeJwtPayload(token: string): Record<string, unknown> | null {
  try {
    if (!token) return null
    const parts = token.split('.')
    if (parts.length < 2) return null
    // base64url → base64: '-' '_' 替换成 '+' '/', 补 '='
    let s = parts[1].replace(/-/g, '+').replace(/_/g, '/')
    while (s.length % 4) s += '='
    const decoded = atob(s)
    const obj = JSON.parse(decoded)
    return obj && typeof obj === 'object' && !Array.isArray(obj) ? (obj as Record<string, unknown>) : null
  } catch {
    return null
  }
}
const getJwtUsername = (): string | null => {
  const t = localStorage.getItem('token') || ''
  const p = _safeJwtPayload(t)
  const u = p?.username
  return typeof u === 'string' ? u : null
}
// 2026-08-15: 从 JWT payload 取 role(owner|member|guest); demo 账号兼容按 username==demo 判定
const getJwtRole = (): string | null => {
  const t = localStorage.getItem('token') || ''
  const p = _safeJwtPayload(t)
  const r = p?.role
  return typeof r === 'string' ? r : null
}
const isDemoUser = (): boolean => getJwtUsername() === 'demo'
// 是否 guest 角色: role==guest 或 demo 账号(后端口径: username=="demo" || role=="guest")
const isGuestUser = (): boolean => getJwtRole() === 'guest' || isDemoUser()
// 角色化隐藏导航: owner/member 全部显示(现状不变); guest(demo) 隐藏管理/个人页面
// (数据源/AI配置/Agent/策略等核心内容; 设置/持仓/自选可浏览但只读)
const GUEST_HIDDEN_PATHS = ['/paper-trading', '/alerts', '/shadow', '/system']
const isNavHiddenForGuest = (to: string): boolean =>
  GUEST_HIDDEN_PATHS.includes(to) || to.startsWith('/manage')
// owner 专属导航(审计页): 非 owner 一律隐藏(2026-08-15)
const isNavHiddenForRole = (n: { to: string; ownerOnly?: boolean }): boolean =>
  !!n.ownerOnly && getJwtRole() !== 'owner'
// 模块权限过滤(2026-08-16): 导航项带 perm 权限点 → 当前用户 effective 权限
// 不含该点则隐藏(未授权模块前端也看不到, 不再"能点进去但数据403")。
// 注意: 拉取失败(myPerms==null)或权限点未映射的项 → 不隐藏(回退角色判断, 不误伤)。
const isNavHiddenForPerm = (n: { perm?: string }, myPerms: Set<string> | null): boolean => {
  if (!n.perm) return false
  if (!myPerms) return false
  return !myPerms.has(n.perm)
}
const mobilePrimaryNavItems = navItems.filter(n => MOBILE_PRIMARY_TO.includes(n.to))
const mobileMoreNavItems = navItems.filter(n => !MOBILE_PRIMARY_TO.includes(n.to))

function LegacyStocksRedirect() {
  const location = useLocation()
  return <Navigate to={`/portfolio${location.search}`} replace />
}

/**
 * §4.3 旧路由重定向: 页面被合并进 Tab 后, 老路由统一跳到新地址的对应页签。
 * 保留旧路径 = 不破坏既有书签 / 推送链接 / 用户习惯(设计稿要求"别回退")。
 */
function LegacyTabRedirect({ to }: { to: string }) {
  const location = useLocation()
  // 老链接可能自带 query, 合并进去(新地址的 ?tab= 由调用方给出, 优先级更高)
  const merged = new URLSearchParams(location.search)
  const target = new URLSearchParams(to.split('?')[1] || '')
  target.forEach((v, k) => merged.set(k, v))
  return <Navigate to={`${to.split('?')[0]}?${merged.toString()}`} replace />
}

// 认证守卫组件
function RequireAuth({ children }: { children: React.ReactNode }) {
  const [authState, setAuthState] = useState<'checking' | 'authenticated' | 'unauthenticated'>('checking')
  const location = useLocation()

  useEffect(() => {
    // 检查本地 token
    if (isAuthenticated()) {
      setAuthState('authenticated')
      return
    }

    // 没有 token，需要去登录页（设置密码或登录）
    setAuthState('unauthenticated')
  }, [])

  if (authState === 'checking') {
    return (
      <div className="min-h-screen flex items-center justify-center bg-background">
        <span className="w-6 h-6 border-2 border-primary/30 border-t-primary rounded-full animate-spin" />
      </div>
    )
  }

  if (authState === 'unauthenticated') {
    return <Navigate to="/login" state={{ from: location }} replace />
  }

  return <>{children}</>
}

// 模块权限路由守卫(2026-08-16): 未授权模块直接跳首页。
// 配合后端 403 + 导航过滤, 三层防"未授权模块可见"。
// myPerms 尚未加载(null)时不拦截(避免闪跳), 加载完成后才生效。
function PermGuard({ perm, myPerms, children }: { perm?: string; myPerms: Set<string> | null; children: React.ReactNode }) {
  if (!perm || !myPerms) return <>{children}</>
  if (!myPerms.has(perm)) return <Navigate to="/" replace />
  return <>{children}</>
}

/** 懒加载路由的轻量占位(2026-08-12): 纯静态骨架, 不依赖任何懒加载模块 */
function PageFallback() {
  return (
    <div className="w-full h-[60vh] flex items-center justify-center">
      <div className="flex flex-col items-center gap-3 text-muted-foreground">
        <div className="w-6 h-6 border-2 border-primary/30 border-t-primary rounded-full animate-spin" />
        <span className="text-[12px]">加载中…</span>
      </div>
    </div>
  )
}

function App() {
  const { mode, setMode } = useTheme()
  const location = useLocation()
  const [version, setVersion] = useState('')
  const [logsOpen, setLogsOpen] = useState(false)
  const [searchOpen, setSearchOpen] = useState(false)
  // 设计稿 v2.0 §4.2 可折叠侧边栏: 折叠态持久化到 localStorage
  const [sidebarCollapsed, setSidebarCollapsed] = useState(() => {
    try { return localStorage.getItem('sida_sidebar_collapsed') === '1' } catch { return false }
  })
  const toggleSidebar = () => setSidebarCollapsed((c) => {
    try { localStorage.setItem('sida_sidebar_collapsed', c ? '0' : '1') } catch {}
    return !c
  })
  const [selfCheckOpen, setSelfCheckOpen] = useState(false)
  const [upgradeOpen, setUpgradeOpen] = useState(false)
  const [upgradeInfo, setUpgradeInfo] = useState<{ latest: string; url: string } | null>(null)
  const checkedUpdateRef = useRef(false)
  // 当前用户模块权限(导航过滤: 未授权模块隐藏入口; 拉取失败回退角色判断)
  const [myPerms, setMyPerms] = useState<Set<string> | null>(null)
  const repoUrl = 'https://github.com/xiaoze-hub/Stock-Intelligent-Data-Analytics'

  useEffect(() => {
    if (!isAuthenticated()) return
    getMyPermissions()
      .then(p => p && setMyPerms(new Set(p.effective)))
      .catch(() => {})
  }, [])

  useEffect(() => {
    appApi.version()
      .then(data => setVersion(data?.version || ''))
      .catch(() => {})
  }, [])

  useEffect(() => {
    if (checkedUpdateRef.current) return
    if (!isAuthenticated()) return
    const current = String(version || '').trim()
    if (!current || current === 'dev') return
    checkedUpdateRef.current = true

    fetchAPI<any>('/settings/update-check')
      .then((res) => {
        const latest = String(res?.latest_version || '').trim()
        const shouldOpen = !!res?.update_available && !!latest
        if (!shouldOpen) return
        const dismissed = localStorage.getItem('panwatch_upgrade_dismissed_version') || ''
        if (dismissed === latest) return
        setUpgradeInfo({ latest, url: String(res?.release_url || 'https://github.com/xiaoze-hub/Stock-Intelligent-Data-Analytics/releases') })
        setUpgradeOpen(true)
      })
      .catch(() => {})
  }, [version])

  // ===== PC 快捷键(2026-08-12,增量功能,不影响现有交互) =====
  // 仅桌面端(>=768px)生效,移动端自动禁用;登录页不响应
  const navigate = useNavigate()
  const [hotkeysOpen, setHotkeysOpen] = useState(false)
  // 登录页守卫:登录态外不响应快捷键
  const runOnDesktop = (fn: () => void) => () => {
    if (location.pathname === '/login') return
    fn()
  }

  useHotkeys([
    {
      combo: 'mod+k',
      handler: runOnDesktop(() => setSearchOpen(true)),
    },
    { combo: 'mod+,', handler: runOnDesktop(() => navigate('/settings')) },
    // v2.0 §4.4 快捷键导航: g + {key} 序列, 6 主导航全覆盖.
    { sequence: ['g', 'd'], sequenceTimeout: 1500, handler: runOnDesktop(() => navigate('/')) },           // 驾驶舱 Dashboard
    { sequence: ['g', 'p'], sequenceTimeout: 1500, handler: runOnDesktop(() => navigate('/portfolio')) }, // 我的 Portfolio
    { sequence: ['g', 'm'], sequenceTimeout: 1500, handler: runOnDesktop(() => navigate('/forecast')) },  // 行情 Market (合并预测)
    { sequence: ['g', 'o'], sequenceTimeout: 1500, handler: runOnDesktop(() => navigate('/opportunities')) }, // 机会 Opportunities
    { sequence: ['g', 'r'], sequenceTimeout: 1500, handler: runOnDesktop(() => navigate('/reports')) },   // 投研 Reports (合并历史)
    { sequence: ['g', 'u'], sequenceTimeout: 1500, handler: runOnDesktop(() => navigate('/settings')) },  // 系统 User settings
    { sequence: ['g', 'n'], sequenceTimeout: 1500, handler: runOnDesktop(() => navigate('/notifications')) }, // 通知 Notifications
    { sequence: ['g', 's'], sequenceTimeout: 1500, handler: runOnDesktop(() => navigate('/shadow')) },    // 我的 shadow account
    { combo: '?', preventDefault: true, handler: runOnDesktop(() => setHotkeysOpen(true)) },
  ])

  // 登录页面不显示导航
  if (location.pathname === '/login') {
    return (
      <Suspense fallback={<PageFallback />}>
        <Routes>
          <Route path="/login" element={<LoginPage />} />
        </Routes>
      </Suspense>
    )
  }

  return (
    <RequireAuth>
    <div className="min-h-screen pb-16 md:pb-0 relative overflow-x-clip bg-background">
      <BrowserNotificationBridge />
      <AmbientBackground />
      {/* Desktop Sidebar (设计稿 v2.0 §4.2: 6 项主导航可折叠侧边栏, 交易线顶/研究线中/系统沉底) */}
      <aside className={`fixed inset-y-0 left-0 z-50 hidden md:flex flex-col border-r border-border bg-card/80 backdrop-blur transition-[width] duration-200 ${sidebarCollapsed ? 'w-16' : 'w-60'}`}>
        {/* Logo + 折叠按钮 */}
        <div className="flex items-center gap-2 h-14 px-3 border-b border-border shrink-0">
          <NavLink to="/" className="flex items-center gap-2.5 min-w-0 flex-1">
            <div className="w-8 h-8 rounded-2xl bg-gradient-to-br from-primary to-primary/70 flex items-center justify-center shadow-sm shrink-0">
              <TrendingUp className="w-4 h-4 text-white" />
            </div>
            {!sidebarCollapsed && (
              <>
                <span className="text-[15px] font-bold text-foreground truncate">数智分析</span>
                {version && <span className="text-[11px] text-muted-foreground/60 font-normal shrink-0">v{version}</span>}
                {isDemoUser() && (
                  <span className="shrink-0 rounded-md bg-amber-500/15 px-1.5 py-0.5 text-[10px] font-medium text-amber-600" title="演示账号为只读浏览模式">演示</span>
                )}
              </>
            )}
          </NavLink>
          <button
            onClick={toggleSidebar}
            className="w-7 h-7 rounded-lg flex items-center justify-center text-muted-foreground hover:text-foreground hover:bg-accent transition-colors shrink-0"
            title={sidebarCollapsed ? '展开侧边栏' : '折叠侧边栏'}
          >
            {sidebarCollapsed ? <PanelLeftOpen className="w-4 h-4" /> : <PanelLeftClose className="w-4 h-4" />}
          </button>
        </div>

        {/* 6 项主导航 */}
        <nav className="flex-1 overflow-y-auto px-2 py-2">
          {desktopNavGroups.map((group) => {
            // guest(demo): 隐藏管理/个人页面导航; 模块权限: 未授权模块隐藏入口
            const items = group.items.filter(n => (!isGuestUser() || !isNavHiddenForGuest(n.to)) && !isNavHiddenForRole(n) && !isNavHiddenForPerm(n, myPerms))
            if (items.length === 0) return null
            return (
              <div key={group.key} className="mb-3 last:mb-0">
                {!sidebarCollapsed && <div className="px-2 pb-1 text-[10px] font-medium text-muted-foreground/50">{group.label}</div>}
                <div className="space-y-0.5">
                  {items.map(({ to, icon: Icon, label }) => {
                    const isActive = to === '/' ? location.pathname === '/' : location.pathname.startsWith(to)
                    return (
                      <NavLink
                        key={to}
                        to={to}
                        title={sidebarCollapsed ? label : undefined}
                        className={`flex items-center gap-2.5 rounded-xl text-[13px] font-medium transition-colors ${
                          sidebarCollapsed ? 'justify-center px-0 py-2' : 'px-2.5 py-2'
                        } ${
                          isActive
                            ? 'bg-[linear-gradient(135deg,hsl(var(--primary)/0.14),hsl(var(--primary)/0.04))] ring-1 ring-primary/20 text-foreground'
                            : 'text-muted-foreground hover:text-foreground hover:bg-accent'
                        }`}
                      >
                        <Icon className={`w-4 h-4 shrink-0 ${isActive ? 'text-primary' : ''}`} />
                        {!sidebarCollapsed && <span className="truncate">{label}</span>}
                      </NavLink>
                    )
                  })}
                </div>
              </div>
            )
          })}
        </nav>

        {/* 底部: GitHub + 日志 + 通知 + 头像 */}
        <div className={`border-t border-border p-2 shrink-0 flex ${sidebarCollapsed ? 'flex-col items-center gap-1' : 'flex-row items-center justify-between'}`}>
          <div className="flex items-center gap-0.5">
            <button
              onClick={() => window.open(repoUrl, '_blank', 'noopener,noreferrer')}
              className="w-8 h-8 rounded-lg flex items-center justify-center text-muted-foreground hover:text-foreground hover:bg-accent transition-colors"
              title="GitHub 项目"
            >
              <Github className="w-4 h-4" />
            </button>
            <button
              onClick={() => setLogsOpen(true)}
              className="w-8 h-8 rounded-lg flex items-center justify-center text-muted-foreground hover:text-foreground hover:bg-accent transition-colors"
              title="查看日志"
            >
              <ScrollText className="w-4 h-4" />
            </button>
            <NotificationBell size="sm" />
          </div>
          <AccountMenu
            navItems={[]}
            mode={mode}
            onSetMode={setMode}
            onOpenSelfCheck={() => setSelfCheckOpen(true)}
          />
        </div>
      </aside>

      {/* Mobile Top Bar */}
      <div className="sticky top-0 z-50 px-4 pt-[max(0.75rem,env(safe-area-inset-top))] pb-2 md:hidden">
        <header className="card px-4">
          <div className="h-12 flex items-center justify-between">
            <NavLink to="/" className="flex items-center gap-2 group">
              <div className="w-7 h-7 rounded-xl bg-gradient-to-br from-primary to-primary/70 flex items-center justify-center shadow-sm">
                <TrendingUp className="w-3.5 h-3.5 text-white" />
              </div>
              <span className="text-[14px] font-bold text-foreground">数智分析</span>
              {version && <span className="text-[10px] text-muted-foreground/60 font-normal">v{version}</span>}
            </NavLink>
            <div className="flex items-center gap-1.5 px-1.5 py-1 rounded-2xl bg-accent/20 border border-border/40">
              <button
                onClick={() => window.open(repoUrl, '_blank', 'noopener,noreferrer')}
                className="w-8 h-8 rounded-xl flex items-center justify-center text-muted-foreground hover:text-foreground hover:bg-background/70 transition-colors"
                title="GitHub 项目"
              >
                <Github className="w-4 h-4" />
              </button>
              <button
                onClick={() => setLogsOpen(true)}
                className="w-8 h-8 rounded-xl flex items-center justify-center text-muted-foreground hover:text-foreground hover:bg-background/70 transition-colors"
                title="查看日志"
              >
                <ScrollText className="w-4 h-4" />
              </button>
              <NotificationBell size="sm" />
              <AccountMenu
                size="sm"
                navItems={isGuestUser() ? mobileMoreNavItems.filter(n => !isNavHiddenForGuest(n.to) && !isNavHiddenForRole(n) && !isNavHiddenForPerm(n, myPerms)) : mobileMoreNavItems.filter(n => !isNavHiddenForRole(n) && !isNavHiddenForPerm(n, myPerms))}
                mode={mode}
                onSetMode={setMode}
                onOpenSelfCheck={() => setSelfCheckOpen(true)}
              />
            </div>
          </div>
        </header>
      </div>

      {/* Mobile Bottom Nav */}
      <nav className="fixed bottom-0 left-0 right-0 z-50 md:hidden bg-card border-t border-border px-2 pb-[env(safe-area-inset-bottom)]">
        <div className="flex items-center justify-around h-14">
          {mobilePrimaryNavItems.filter(n => (!isGuestUser() || !isNavHiddenForGuest(n.to)) && !isNavHiddenForRole(n) && !isNavHiddenForPerm(n, myPerms)).map(({ to, icon: Icon, label }) => {
            const isActive = to === '/' ? location.pathname === '/' : location.pathname.startsWith(to)
            return (
              <NavLink
                key={to}
                to={to}
                className={`flex flex-col items-center justify-center gap-0.5 px-2 py-1.5 rounded-xl transition-[background-color,color,box-shadow] min-w-[56px] ${
                  isActive
                    ? 'text-primary bg-primary/8 ring-1 ring-primary/15'
                    : 'text-muted-foreground hover:bg-accent/30'
                }`}
              >
                <Icon className="w-5 h-5" />
                <span className="text-[10px] font-medium">{label}</span>
              </NavLink>
            )
          })}
        </div>
      </nav>

      {/* Content */}
      <main className={`px-4 md:px-6 py-4 md:py-6 w-full ${sidebarCollapsed ? 'md:pl-20' : 'md:pl-64'}`}>
        <Suspense fallback={<PageFallback />}>
          <AppErrorBoundary>
            <Routes>

              <Route path="/" element={<DashboardPage />} />
              <Route path="/opportunities" element={<PermGuard perm="view_opportunities" myPerms={myPerms}><OpportunitiesPage /></PermGuard>} />
              {/* §4.3 行情三合一: /forecast 作为行情入口(内部分时日K/预测/资金/事件 四 Tab) */}
              <Route path="/forecast" element={<PermGuard perm="view_forecast" myPerms={myPerms}><QuotePage /></PermGuard>} />
              <Route path="/index/:symbol" element={<IndexDetailPage />} />
              <Route path="/boards/:blockCode" element={<BoardDetailPage />} />
              <Route path="/portfolio" element={<PermGuard perm="edit_portfolio" myPerms={myPerms}><StocksPage /></PermGuard>} />
              <Route path="/stocks" element={<LegacyStocksRedirect />} />
              {/* §4.3: Agent + 数据源 → /system 二级页 */}
              <Route path="/system" element={<SystemPage myPerms={myPerms} isOwner={() => getJwtRole() === 'owner'} />} />
              <Route path="/agents" element={<LegacyTabRedirect to="/system?tab=agents" />} />
              {/* §4.3 补齐: 历史并入报告(投研内"历史Tab"), 都是研究产出。
                  权限粒度对齐原路由: reports Tab=view_reports(Tab级过滤), history 原本无守卫。
                  路由级不再套 PermGuard, 避免只有 history 权限的用户被整页拦住。 */}
              <Route path="/reports" element={<ReportsHubPage myPerms={myPerms} isOwner={() => getJwtRole() === 'owner'} />} />
              <Route path="/history" element={<LegacyTabRedirect to="/reports?tab=history" />} />
              {/* §4.3 补齐: 模拟盘并入影子(我的内"模拟Tab"), 都是模拟资金。
                  权限粒度对齐原路由: shadow=manage_shadow / paper=manage_paper_trading,
                  均为 Tab 级过滤 —— 只有模拟盘权限的用户仍能从 /paper-trading 进入。 */}
              <Route path="/shadow" element={<ShadowHubPage myPerms={myPerms} isOwner={() => getJwtRole() === 'owner'} />} />
              <Route path="/paper-trading" element={<LegacyTabRedirect to="/shadow?tab=paper" />} />
              {/* §4.3 补齐: 提醒并入通知(通知内"提醒Tab"), 提醒是通知一种 */}
              <Route path="/notifications" element={<NotificationsHubPage myPerms={myPerms} isOwner={() => getJwtRole() === 'owner'} />} />
              <Route path="/alerts" element={<LegacyTabRedirect to="/notifications?tab=alerts" />} />
              <Route path="/profile" element={<ProfilePage />} />
              {/* §4.3: 审计 + 帮助 → /settings 页签 */}
              <Route path="/settings" element={<SettingsHubPage myPerms={myPerms} isOwner={() => getJwtRole() === 'owner'} />} />
              <Route path="/audit" element={<LegacyTabRedirect to="/settings?tab=audit" />} />
              <Route path="/help" element={<LegacyTabRedirect to="/settings?tab=help" />} />
              <Route path="/datasources" element={<LegacyTabRedirect to="/system?tab=datasources" />} />
              <Route path="/analysis/:symbol/:date" element={<AnalysisDetailPage />} />
              <Route path="*" element={<Navigate to="/" replace />} />
            </Routes>
          </AppErrorBoundary>

        </Suspense>
      </main>
      <ChatWidget />
      <CommandPalette open={searchOpen} onClose={() => setSearchOpen(false)} />
      <LogsModal open={logsOpen} onOpenChange={setLogsOpen} />
      <SelfCheckModal open={selfCheckOpen} onClose={() => setSelfCheckOpen(false)} />
      <Dialog open={upgradeOpen} onOpenChange={setUpgradeOpen}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>发现新版本</DialogTitle>
            <DialogDescription>
              当前版本 v{version}，可升级到 v{upgradeInfo?.latest}。
            </DialogDescription>
          </DialogHeader>
          <div className="text-[12px] text-muted-foreground">
            建议升级以获取最新功能和修复。
          </div>
          <div className="flex items-center justify-end gap-2">
            <Button
              variant="secondary"
              onClick={() => {
                if (upgradeInfo?.latest) localStorage.setItem('panwatch_upgrade_dismissed_version', upgradeInfo.latest)
                setUpgradeOpen(false)
              }}
            >
              稍后提醒
            </Button>
            <Button
              onClick={() => {
                const url = upgradeInfo?.url || 'https://github.com/xiaoze-hub/Stock-Intelligent-Data-Analytics/releases'
                window.open(url, '_blank', 'noopener,noreferrer')
              }}
            >
              去升级
            </Button>
          </div>
        </DialogContent>
      </Dialog>
      {/* 快捷键帮助(2026-08-12):按 ? 打开 */}
      <Dialog open={hotkeysOpen} onOpenChange={setHotkeysOpen}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>键盘快捷键</DialogTitle>
            <DialogDescription>仅桌面端生效,移动端自动禁用。</DialogDescription>
          </DialogHeader>
          <div className="space-y-2.5 text-[13px]">
            {[
              { desc: '打开日志(搜索占位)', keys: ['⌘/Ctrl', 'K'] },
              { desc: '跳转设置', keys: ['⌘/Ctrl', ','] },
              { desc: '跳转首页', keys: ['G', 'D'] },
              { desc: '跳转持仓', keys: ['G', 'P'] },
              { desc: '显示本帮助', keys: ['?'] },
            ].map(row => (
              <div key={row.desc} className="flex items-center justify-between gap-4">
                <span className="text-muted-foreground">{row.desc}</span>
                <span className="flex items-center gap-1 shrink-0">
                  {row.keys.map(k => (
                    <kbd
                      key={k}
                      className="px-1.5 py-0.5 rounded-md bg-accent border border-border text-[11px] font-medium text-foreground"
                    >
                      {k}
                    </kbd>
                  ))}
                </span>
              </div>
            ))}
          </div>
        </DialogContent>
      </Dialog>
    </div>
    </RequireAuth>
  )
}

export default App
