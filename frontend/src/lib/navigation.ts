/**
 * 共享导航元数据(P0, 2026-11-07) —— 桌面侧栏 / 移动底栏+更多 / 命令面板(Ctrl+K) /
 * 工作台页签标题 / 老 URL 重定向, 全部从这一份表派生。
 *
 * 为什么要有这一份表(病根):
 *   · 同一入口在 App.tsx(navItems / desktopNavGroups / MOBILE_PRIMARY_TO / GUEST_HIDDEN_PATHS)、
 *     CommandPalette.tsx(PAGE_COMMANDS 手写中文)、tab-shell.ts(ROUTE_LABEL_KEYS) 各写一遍;
 *     加一个页面要改 4 处, 漏一处 = "菜单里有、页签是裸路径 / 命令面板搜不到 / 移动端丢了"。
 *   · 权限可见性也有三套 if(isNavHiddenForXxx) —— 桌面、移动底栏、移动"更多"各过滤一次,
 *     命令面板**完全不过滤**(guest 也能从面板点进 /admin 再吃 403)。
 *   · 老路由别名散落成 7 个 redirect 组件 + 12 条路由, 没有单一清单可核对。
 *
 * 本模块只做**纯数据 + 纯函数**(零 React / 零路由 / 零 window): 可逻辑单测。
 * 图标是 lucide 组件(纯展示), 是这里唯一的 UI 依赖。
 *
 * 不变量(见 tests/lib/navigation.test.ts):
 *   ① 每个 NAV_ITEMS 入口在 App.tsx 有真实 `<Route>`, 且可见性规则唯一;
 *   ② 桌面组 ≤6, 组内顺序 = NAV_ITEMS 顺序(与原 navItems 完全一致);
 *   ③ 移动端 5 个主槽位顺序不变, 其余全部落"更多";
 *   ④ 命令面板 / 移动端 / 页签标题用同一个 labelKey(不再各写中文);
 *   ⑤ 老 URL 全部在 LEGACY_ALIASES 里可查, 且目标页真实存在(不产生死链)。
 */
import {
  Activity,
  BarChart3,
  Bell,
  BellRing,
  ClipboardList,
  Clock,
  Code2,
  FileText,
  FlaskConical,
  Flame,
  KeyRound,
  Landmark,
  LayoutDashboard,
  LayoutGrid,
  LineChart,
  List,
  ServerCog,
  Settings,
  Shield,
  ShieldCheck,
  Sparkles,
  TrendingUp,
  User,
  type LucideIcon,
} from 'lucide-react'

/** 桌面侧栏的一级分组(设计稿 v3.0 §六: ≤6 组) */
export type NavGroupKey = 'market' | 'opportunity' | 'decision' | 'research' | 'mine' | 'system'

export interface NavGroup {
  key: NavGroupKey
  /** 组标题文案 key(与导航项共用词表) */
  labelKey: string
}

export const NAV_GROUPS: readonly NavGroup[] = [
  { key: 'market', labelKey: 'nav.stocks' },
  { key: 'opportunity', labelKey: 'nav.opportunities' },
  { key: 'decision', labelKey: 'nav.decision' },
  { key: 'research', labelKey: 'nav.reports' },
  { key: 'mine', labelKey: 'nav.profile' },
  { key: 'system', labelKey: 'nav.system' },
]

export type MobileSlot = 'primary' | 'more'

export interface NavItem {
  /** 目标地址(可带 ?tab= 直达页内页签) */
  to: string
  labelKey: string
  icon: LucideIcon
  /** 桌面侧栏分组; null = 不占桌面分组(独立入口/被收纳的别名) */
  group: NavGroupKey | null
  /** 移动端槽位: 底栏 5 个主入口 = primary, 其余 = more(头像下拉) */
  mobile: MobileSlot
  /** primary 槽位的固定顺序(0 起), 保持用户已经形成的肌肉记忆 */
  mobileOrder?: number
  /** 模块权限点: 有值且权限已加载时, 不含该点则不可见 */
  perm?: string
  /** 至少持有一个权限点时可见 */
  anyPerm?: readonly string[]
  /** owner 专属(管理后台) */
  ownerOnly?: boolean
  /** demo/guest 只读账号隐藏 */
  guestHidden?: boolean
  /** 是否占工作台页签(默认 true; 重定向型入口须为 false, 否则留僵尸页签) */
  tabbable?: boolean
  /** 桌面侧栏独立高亮卡(数智参谋), 不进分组 */
  spotlight?: boolean
  /** 命令面板额外搜索词(菜单名之外的叫法) */
  keywords?: readonly string[]
}

/**
 * 全部功能入口 —— 顺序 = 原 App.tsx `navItems` 顺序(桌面组内顺序、移动"更多"顺序都靠它)。
 * `to` 一律写**规范地址**: 老地址(/history、/paper-trading、/alerts)只在 LEGACY_ALIASES 里
 * 作为兼容入口保留, 菜单/面板/页签不再产生新的老地址链接。
 */
export const NAV_ITEMS: readonly NavItem[] = [
  { to: '/', labelKey: 'nav.home', icon: LayoutDashboard, group: 'market', mobile: 'primary', mobileOrder: 0, perm: 'view_dashboard' },
  // 行情入口是"解析器": 按当前账号持仓/关注跳到 /stocks/:symbol, 本身不该占页签
  { to: '/quotes', labelKey: 'nav.stocks', icon: LineChart, group: 'market', mobile: 'primary', mobileOrder: 3, perm: 'view_quotes', tabbable: false, keywords: ['个股', '工作台', '行情', '预测'] },
  { to: '/heatmap', labelKey: 'nav.heatmap', icon: LayoutGrid, group: 'market', mobile: 'more', perm: 'view_heatmap', keywords: ['热力图', '板块'] },
  { to: '/indexes', labelKey: 'nav.indexes', icon: BarChart3, group: 'market', mobile: 'more', perm: 'view_quotes', keywords: ['指数', '宽基'] },
  { to: '/market-mood', labelKey: 'nav.marketMood', icon: Activity, group: 'market', mobile: 'more', perm: 'view_quotes', keywords: ['题材', '情绪', '复盘', '连板'] },
  { to: '/etf-watch', labelKey: 'nav.etfWatch', icon: Landmark, group: 'market', mobile: 'more', perm: 'view_quotes', keywords: ['ETF', '国家队'] },
  { to: '/opportunities', labelKey: 'nav.opportunities', icon: Sparkles, group: 'opportunity', mobile: 'primary', mobileOrder: 2, perm: 'view_opportunities', keywords: ['扫描', '机会'] },
  { to: '/auction', labelKey: 'nav.auction', icon: Flame, group: 'opportunity', mobile: 'more', perm: 'view_opportunities', keywords: ['竞价', '集合竞价'] },
  // 数智参谋: 独立一级入口(设计稿 §9.3 方案 A) —— 不进分组, 桌面渲染独立高亮卡
  { to: '/ai-copilot', labelKey: 'nav.aiCopilot', icon: Sparkles, group: null, mobile: 'more', perm: 'view_quotes', spotlight: true, keywords: ['AI', '参谋', '助手'] },
  { to: '/funds', labelKey: 'nav.mainFunds', icon: TrendingUp, group: 'opportunity', mobile: 'more', anyPerm: ['view_dark', 'view_forecast'], keywords: ['暗盘', 'DDE', '主力', '战报'] },
  // 决策域: 决策账本 + 口径对照 + 因子有效性(证据链, 不是账户视图)
  { to: '/caliber-compare', labelKey: 'nav.caliberCompare', icon: Activity, group: 'decision', mobile: 'more', perm: 'view_forecast', keywords: ['口径', 'L2', '暗盘'] },
  { to: '/decision-ledger', labelKey: 'nav.decisionLedger', icon: ClipboardList, group: 'decision', mobile: 'more', perm: 'view_quotes', keywords: ['决策', '账本'] },
  { to: '/factor-ic', labelKey: 'nav.factorIC', icon: FlaskConical, group: 'decision', mobile: 'more', perm: 'view_quotes', keywords: ['因子', 'IC'] },
  { to: '/reports', labelKey: 'nav.reports', icon: FileText, group: 'research', mobile: 'more', perm: 'view_reports', keywords: ['报告'] },
  // 历史分析并入报告页(?tab=history); 老地址 /history 仍可用
  { to: '/reports?tab=history', labelKey: 'nav.history', icon: Clock, group: null, mobile: 'more', keywords: ['历史', '分析'] },
  { to: '/portfolio', labelKey: 'nav.portfolio', icon: List, group: 'mine', mobile: 'primary', mobileOrder: 1, perm: 'edit_portfolio', keywords: ['持仓', '自选'] },
  { to: '/shadow', labelKey: 'nav.shadow', icon: Shield, group: 'mine', mobile: 'more', perm: 'manage_shadow', guestHidden: true, keywords: ['影子', '账户'] },
  // 模拟盘并入影子账户(?tab=paper); 老地址 /paper-trading 仍可用
  { to: '/shadow?tab=paper', labelKey: 'nav.paperTrading', icon: Activity, group: null, mobile: 'more', perm: 'manage_paper_trading', guestHidden: true, keywords: ['模拟盘'] },
  { to: '/profile', labelKey: 'nav.profile', icon: User, group: 'mine', mobile: 'more', keywords: ['个人中心', '账号'] },
  { to: '/api-keys', labelKey: 'nav.apiKeys', icon: KeyRound, group: 'mine', mobile: 'more', keywords: ['API Key', '密钥'] },
  // 注意: 不要在这里加 'Agent'/'数据源' 搜索词 —— 面板里已有更精确的 /system?tab=* 直达项,
  // 同一个词命中两条会让「数据源」跳到 /system 首屏(实测)。
  { to: '/system', labelKey: 'nav.system', icon: ServerCog, group: 'system', mobile: 'more', guestHidden: true },
  { to: '/notifications', labelKey: 'nav.notifications', icon: Bell, group: 'system', mobile: 'primary', mobileOrder: 4, keywords: ['通知', '消息'] },
  // 提醒并入通知(?tab=alerts); 老地址 /alerts 仍可用. guest 隐藏口径与原 /alerts 一致
  { to: '/notifications?tab=alerts', labelKey: 'nav.alerts', icon: BellRing, group: null, mobile: 'more', guestHidden: true, keywords: ['提醒', '预警'] },
  { to: '/settings', labelKey: 'nav.settings', icon: Settings, group: 'system', mobile: 'more' },
  { to: '/developers', labelKey: 'nav.developers', icon: Code2, group: 'system', mobile: 'more', keywords: ['开发者', 'API 文档'] },
  { to: '/admin', labelKey: 'nav.admin', icon: ShieldCheck, group: 'system', mobile: 'more', ownerOnly: true, perm: 'manage_users', keywords: ['管理后台', '用户'] },
]

/**
 * 页签化但不进菜单的二级路由(从参谋工作台/状态条进入)。
 * 放进这里 = 允许占工作台页签 + 有中文标题; 不放进 App 路由的条目会被钉子测试抓住
 * (历史真 bug: '/copilot/governance' 登记了标题但真实路由是 '/copilot-governance')。
 */
export interface SecondaryRoute {
  path: string
  labelKey: string
  perm?: string
}

export const SECONDARY_ROUTES: readonly SecondaryRoute[] = [
  { path: '/theme-mood', labelKey: 'nav.themeMood', perm: 'view_quotes' },
  { path: '/replay-tree', labelKey: 'nav.replayTree', perm: 'view_quotes' },
  { path: '/dark-fund-top', labelKey: 'nav.darkFundTop', perm: 'view_dark' },
  { path: '/war-report', labelKey: 'nav.warReport', perm: 'view_forecast' },
  { path: '/system/status', labelKey: 'nav.systemStatus' },
  { path: '/copilot-governance', labelKey: 'nav.copilotGovernance', perm: 'view_quotes' },
  { path: '/copilot/runs', labelKey: 'nav.copilotRuns', perm: 'view_quotes' },
  { path: '/copilot/experiments', labelKey: 'nav.copilotExperiments', perm: 'view_quotes' },
  { path: '/copilot/agents', labelKey: 'nav.copilotAgents', perm: 'view_quotes' },
  { path: '/copilot/tools', labelKey: 'nav.copilotTools', perm: 'view_quotes' },
  { path: '/copilot/proposals', labelKey: 'nav.copilotProposals', perm: 'view_quotes' },
  { path: '/copilot/parallel', labelKey: 'nav.copilotParallel', perm: 'view_quotes' },
]

/** 命令面板独占的页内页签入口(不在侧栏/底栏单列, 但键盘可直达) */
export interface PaletteEntry {
  to: string
  labelKey: string
  groupKey: NavGroupKey | null
  perm?: string
  guestHidden?: boolean
  keywords?: readonly string[]
}

export const PALETTE_EXTRA_ENTRIES: readonly PaletteEntry[] = [
  { to: '/system?tab=agents', labelKey: 'nav.agents', groupKey: 'system', guestHidden: true, keywords: ['Agent', '智能体'] },
  { to: '/system?tab=datasources', labelKey: 'nav.dataSources', groupKey: 'system', guestHidden: true, keywords: ['数据源'] },
  { to: '/settings?tab=audit', labelKey: 'nav.audit', groupKey: 'system', keywords: ['审计', '日志'] },
  { to: '/settings?tab=help', labelKey: 'nav.help', groupKey: 'system', keywords: ['帮助', '快捷键'] },
]

/** 老路由 → 规范地址。静态别名由 App.tsx 直接按表生成 <Route>; 动态别名保留专用组件。 */
export interface LegacyAlias {
  from: string
  to: string
  labelKey: string
  kind: 'static' | 'dynamic'
}

export const LEGACY_ALIASES: readonly LegacyAlias[] = [
  { from: '/stocks', to: '/portfolio', labelKey: 'nav.portfolio', kind: 'static' },
  { from: '/history', to: '/reports?tab=history', labelKey: 'nav.history', kind: 'static' },
  { from: '/paper-trading', to: '/shadow?tab=paper', labelKey: 'nav.paperTrading', kind: 'static' },
  { from: '/alerts', to: '/notifications?tab=alerts', labelKey: 'nav.alerts', kind: 'static' },
  { from: '/agents', to: '/system?tab=agents', labelKey: 'nav.agents', kind: 'static' },
  { from: '/datasources', to: '/system?tab=datasources', labelKey: 'nav.dataSources', kind: 'static' },
  { from: '/audit', to: '/settings?tab=audit', labelKey: 'nav.audit', kind: 'static' },
  { from: '/help', to: '/settings?tab=help', labelKey: 'nav.help', kind: 'static' },
  // 个股工作台三合一(2026-09-13): 行情/盘口/指数/板块全部并入 /stocks/:symbol.
  // 这些别名带参数/多段逻辑(读 ?symbol&type), 由 App.tsx 的 Legacy*Redirect 处理。
  { from: '/forecast', to: '/stocks/000001?type=index', labelKey: 'nav.forecast', kind: 'dynamic' },
  { from: '/quote', to: '/stocks/000001?type=index', labelKey: 'nav.forecast', kind: 'dynamic' },
  { from: '/quote/:symbol', to: '/stocks/:symbol', labelKey: 'nav.forecast', kind: 'dynamic' },
  { from: '/l2', to: '/stocks/000001?type=index', labelKey: 'nav.forecast', kind: 'dynamic' },
  { from: '/index/:symbol', to: '/stocks/:symbol?type=index', labelKey: 'nav.forecast', kind: 'dynamic' },
  { from: '/boards/:blockCode', to: '/stocks/:blockCode?type=board', labelKey: 'nav.forecast', kind: 'dynamic' },
]

/**
 * 不页签化的路由(工作台页签状态机用):
 * · 公开文档面(无壳层, 整页);
 * · 老别名(跳转后不留在页签集里);
 * · /quotes 是"解析器"页(立刻 Navigate 到 /stocks/:symbol), 占页签只会留僵尸页签。
 */
export const PUBLIC_PAGE_PATHS: readonly string[] = ['/terms', '/tiers', '/login']
export const LEGACY_ALIAS_PATHS: readonly string[] = [
  '/history',
  '/paper-trading',
  '/alerts',
  '/agents',
  '/datasources',
  '/audit',
  '/help',
  '/stocks',
  '/quote',
  '/quotes',
  '/l2',
  '/forecast',
]
export const LEGACY_ALIAS_PREFIXES: readonly string[] = ['/quote/', '/index/', '/boards/']

/** 取地址的 pathname 部分(无 query 时即自身) */
export function routePathOf(to: string): string {
  const i = to.indexOf('?')
  return i < 0 ? to : to.slice(0, i)
}

/**
 * 老别名跳转目标(带 query 合并):
 *   · 保留老链接自带的 query(书签里的筛选/页签不丢);
 *   · 新地址显式给出的 ?tab= 覆盖老地址同名键(收纳后的落地页签优先);
 *   · 合并后没有 query 时**不留裸 '?'**(/stocks → /portfolio 而不是 /portfolio?)。
 */
export function legacyRedirectTarget(to: string, currentSearch = ''): string {
  const merged = new URLSearchParams(currentSearch)
  const target = new URLSearchParams(to.split('?')[1] || '')
  target.forEach((v, k) => merged.set(k, v))
  const search = merged.toString()
  const base = routePathOf(to)
  return search ? `${base}?${search}` : base
}

/** 组标题文案 key; 未知分组返回 null(不编标题) */
export function navGroupLabelKey(key: NavGroupKey | null): string | null {
  if (!key) return null
  return NAV_GROUPS.find((g) => g.key === key)?.labelKey ?? null
}

/** 当前查看者身份 —— 只用于**入口可见性**, 授权仍以后端为准 */
export interface NavViewer {
  /** effective 权限点; null = 尚未加载(按既有口径 fail-open, 不误伤) */
  perms: ReadonlySet<string> | null
  /** JWT role: owner | member | guest | null */
  role: string | null
  /** demo/guest 只读账号 */
  guest: boolean
}

/**
 * 唯一的入口可见性判定(原 App.tsx 三个 isNavHiddenForXxx + 面板零过滤的统一替换)。
 * 规则与旧行为逐条一致:
 *   · ownerOnly 且非 owner → 隐藏;
 *   · guest 且 guestHidden(或旧 /manage* 前缀) → 隐藏;
 *   · 有权限点 + 权限已加载 + 不含 → 隐藏; 权限未加载 → **不隐藏**(回退角色判断)。
 */
export function isNavItemVisible(
  item: { perm?: string; anyPerm?: readonly string[]; ownerOnly?: boolean; guestHidden?: boolean; to?: string },
  viewer: NavViewer,
): boolean {
  if (item.ownerOnly && viewer.role !== 'owner') return false
  if (viewer.guest) {
    const pathname = item.to ? routePathOf(item.to) : ''
    if (item.guestHidden) return false
    if (pathname === '/manage' || pathname.startsWith('/manage/')) return false
  }
  if (item.perm && viewer.perms && !viewer.perms.has(item.perm)) return false
  if (item.anyPerm?.length && viewer.perms && !item.anyPerm.some((perm) => viewer.perms?.has(perm))) return false
  return true
}

/** 桌面某分组的可见条目(保持 NAV_ITEMS 顺序) */
export function navItemsForGroup(key: NavGroupKey, viewer: NavViewer): NavItem[] {
  return NAV_ITEMS.filter((n) => n.group === key && isNavItemVisible(n, viewer))
}

/** 桌面全部分组(空组剔除 —— 组标题不单独飘着) */
export function visibleNavGroups(viewer: NavViewer): { key: NavGroupKey; labelKey: string; items: NavItem[] }[] {
  return NAV_GROUPS.map((g) => ({ ...g, items: navItemsForGroup(g.key, viewer) })).filter((g) => g.items.length > 0)
}

/** 桌面独立高亮入口(数智参谋); 不可见时返回 null */
export function spotlightNavItem(viewer: NavViewer): NavItem | null {
  return NAV_ITEMS.find((n) => n.spotlight && isNavItemVisible(n, viewer)) ?? null
}

/** 移动端某槽位的可见条目; primary 按 mobileOrder 固定排序 */
export function mobileNavItems(slot: MobileSlot, viewer: NavViewer): NavItem[] {
  const items = NAV_ITEMS.filter((n) => n.mobile === slot && isNavItemVisible(n, viewer))
  if (slot !== 'primary') return items
  return [...items].sort((a, b) => (a.mobileOrder ?? 99) - (b.mobileOrder ?? 99))
}

/** 命令面板的页面命令 = 可见导航项 + 页内页签直达项(按 to 去重) */
export function palettePageCommands(viewer: NavViewer): PaletteEntry[] {
  const fromNav: PaletteEntry[] = NAV_ITEMS.filter((n) => isNavItemVisible(n, viewer)).map((n) => ({
    to: n.to,
    labelKey: n.labelKey,
    groupKey: n.group,
    perm: n.perm,
    guestHidden: n.guestHidden,
    keywords: n.keywords,
  }))
  const seen = new Set(fromNav.map((e) => e.to))
  const extras = PALETTE_EXTRA_ENTRIES.filter((e) => !seen.has(e.to) && isNavItemVisible(e, viewer))
  return [...fromNav, ...extras]
}

/**
 * 工作台页签标题注册表: pathname → 文案 key。
 * 由 NAV_ITEMS(可页签的) + SECONDARY_ROUTES 派生 —— 不再手抄第二份。
 * 同一 pathname 多个入口(如 /reports 与 /reports?tab=history)以**先出现者**为准,
 * 页签标题是"功能页"粒度, 不细到页内 tab。
 */
export const ROUTE_LABEL_KEYS: Readonly<Record<string, string>> = (() => {
  const out: Record<string, string> = {}
  for (const item of NAV_ITEMS) {
    if (item.tabbable === false) continue
    const pathname = routePathOf(item.to)
    if (!out[pathname]) out[pathname] = item.labelKey
  }
  for (const route of SECONDARY_ROUTES) {
    if (!out[route.path]) out[route.path] = route.labelKey
  }
  return out
})()
