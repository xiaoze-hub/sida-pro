/**
 * 工作台页签(Tab Shell)纯逻辑 — 2026-10-15
 *
 * 用户诉求原话: 「还需要做功能菜单的页签，这样每次关闭页签，而不是把整个功能全关掉」。
 * 病根: 左侧菜单点开一个功能页, 再点另一个就整页切换(前页被卸载), 且**没有页签/关闭入口** ——
 * 用户想"收掉某个功能页"时没有对象可收, 只能整页切走。
 *
 * 本模块只放**状态机**(零 React / 零路由依赖, 可纯函数单测), UI 在
 * `components/WorkspaceTabShell.tsx` + `components/WorkspaceTabs.tsx`。
 *
 * 三条不变量(验收标准):
 *   ① 同一路由不重复开: key 相同 → 切到已有页签(不新开)。
 *      `/stocks/:symbol` 以 **symbol + type** 为粒度: 两只股票=两个页签;
 *      同一票换 `?tab=l2`/`?tab=news` 仍是**一个**页签(页内状态, 不是新功能页)。
 *   ② 首页('/')是**常驻页签**(钉住, 无 ✕)。所以"关最后一个页签"的落点是首页,
 *      永远不会出现"把整个功能全关掉 / 空白工作台"。
 *   ③ 关掉当前页签 → 落**相邻**页签(优先右邻, 没有则左邻; 兜底首页), 关非当前页签只移除它自己。
 */

import { normalizeType, type WorkbenchType } from './workbench-tabs'

/** 首页页签 key —— 常驻页签, 不可关闭(关掉最后一个功能页签的落点) */
export const HOME_TAB_KEY = '/'
/** 持久化 key(带版本号: 结构变更时换新 key, 旧数据自然失效, 不做迁移猜测) */
export const TABS_STORAGE_KEY = 'sida_workspace_tabs_v1'
/** 页签上限: 8 个(L2-E)。超出按 **LRU** 淘汰最久未访问的非首页页签 —— 见 pickEvictionTarget。
 *  为什么不是 FIFO: 用户会在两三个页签之间反复切, 早打开但一直在用的页签被挤掉会让人莫名其妙。 */
export const MAX_TABS = 8
/**
 * 整页路由(不页签化)。
 * (a) 公开文档面: 用户协议 / 档位对比 / 登录 —— 不在功能菜单里, 打开即整页;
 * (b) 老路由别名: App.tsx 里一律 `LegacyXRedirect`/`LegacyTabRedirect` 到新地址。
 *     若给别名开页签, 跳转后页签集里会**留一个再也回不去的僵尸页签** —— 故别名不参与页签。
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
  '/l2',
  '/forecast',
]
export const LEGACY_ALIAS_PREFIXES: readonly string[] = ['/quote/', '/index/', '/boards/']

export interface WorkspaceTab {
  /** 页签身份(去重键, 见 tabKeyOf) */
  key: string
  /** 该页签最近一次的地址(pathname + search); 点页签回到这里 */
  path: string
  /** 能否关闭: 首页钉住 = false */
  closable: boolean
}

/** 路由 → 页签的最小描述(还差 closable, 由 normalizeTabs/upsertTab 统一派生) */
export interface RouteTabSpec {
  key: string
  path: string
}

/** 页签标题的 i18n key 注册表(与 App.tsx 的 navItems 对齐, 有钉子测试兜底) */
export const ROUTE_LABEL_KEYS: Record<string, string> = {
  '/': 'nav.home',
  '/heatmap': 'nav.heatmap',
  '/theme-mood': 'nav.themeMood',
  '/opportunities': 'nav.opportunities',
  '/auction': 'nav.auction',
  '/ai-copilot': 'nav.aiCopilot',
  // 2026-10-25: 参谋任务列表. 只加 Route 不加这里 -> 被判"不可开页签"并回落首屏(实测)
  '/copilot/runs': 'nav.copilotRuns',
  // 2026-10-25: 经验库（只加 Route 不加这里 -> 被判"不可开页签"并回落首屏）
  '/copilot/experiments': 'nav.copilotExperiments',
  '/copilot/agents': 'nav.copilotAgents',
  // 2026-10-25: 工具目录（插件面板）/ 能力提案审核台
  '/copilot/tools': 'nav.copilotTools',
  '/copilot/proposals': 'nav.copilotProposals',
  '/copilot/parallel': 'nav.copilotParallel',
  '/copilot/governance': 'nav.copilotGovernance',
  '/dark-fund-top': 'nav.darkFundTop',
  '/war-report': 'nav.warReport',
  '/replay-tree': 'nav.replayTree',
  '/caliber-compare': 'nav.caliberCompare',
  '/decision-ledger': 'nav.decisionLedger',
  '/factor-ic': 'nav.factorIC',
  '/reports': 'nav.reports',
  '/portfolio': 'nav.portfolio',
  '/shadow': 'nav.shadow',
  '/notifications': 'nav.notifications',
  '/profile': 'nav.profile',
  '/api-keys': 'nav.apiKeys',
  '/system': 'nav.system',
  '/system/status': 'nav.systemStatus',
  '/settings': 'nav.settings',
  '/developers': 'nav.developers',
  '/admin': 'nav.admin',
}

/** `/stocks/:symbol` 的品种后缀(个股/指数/板块), 标题形如 "600111 个股" */
export function stockSuffixKey(type: WorkbenchType): string {
  if (type === 'index') return 'tabs.index'
  if (type === 'board') return 'tabs.board'
  return 'tabs.stock'
}

/** 拆 path → pathname + search(含 '?'; 无 query 时空串) */
export function parseTabPath(path: string): { pathname: string; search: string } {
  const i = path.indexOf('?')
  if (i < 0) return { pathname: path || HOME_TAB_KEY, search: '' }
  return { pathname: path.slice(0, i) || HOME_TAB_KEY, search: path.slice(i) }
}

/** 非页签路由(公开文档面 + 老路由别名) */
export function isNonTabbable(pathname: string): boolean {
  if (PUBLIC_PAGE_PATHS.includes(pathname)) return true
  if (LEGACY_ALIAS_PATHS.includes(pathname)) return true
  return LEGACY_ALIAS_PREFIXES.some((p) => pathname.startsWith(p))
}

/**
 * 是不是"功能页"(值得占一个页签)。
 * 只认**登记过的**路由(标题注册表 ∪ 首页 ∪ 个股/深度分析前缀) —— 通配/未知(routes 的 `*`
 * 与老别名)不页签化, 否则会留下指向 404 的僵尸页签。新功能页忘登记 = 页签栏剥落,
 * 由 `tests/lib/tab-shell.test.ts` 的"navItems 全覆盖"钉子拦住。
 */
export function isTabbable(pathname: string): boolean {
  if (isNonTabbable(pathname)) return false
  if (pathname === HOME_TAB_KEY) return true
  if (pathname.startsWith('/stocks/')) return true
  if (pathname.startsWith('/analysis/')) return true
  // 2026-10-25: 参谋证据详情是动态路由, 精确查表查不到, 必须按前缀放行
  if (pathname.startsWith('/copilot/runs/')) return true
  return !!ROUTE_LABEL_KEYS[pathname]
}

/**
 * 页签身份 key。
 * · `/stocks/:symbol` → `/stocks/<symbol>?type=<stock|index|board>`(symbol+品种粒度;
 *   `?tab=`/其它页内参数**不进** key —— 否则页内切标签就长出一个新页签)。
 * · 其余路由 → pathname(`/reports?tab=history` 与 `/reports?tab=alerts` 是同一功能页)。
 */
export function tabKeyOf(pathname: string, search = ''): string {
  if (pathname.startsWith('/stocks/')) {
    const symbol = decodeURIComponent(pathname.slice('/stocks/'.length))
    const type = normalizeType(new URLSearchParams(search).get('type'))
    return `/stocks/${symbol}?type=${type}`
  }
  return pathname
}

/** 当前路由对应的页签描述; 非页签路由(公开文档面/老别名/未知)返回 null */
export function routeTab(pathname: string, search = ''): RouteTabSpec | null {
  if (!isTabbable(pathname)) return null
  return { key: tabKeyOf(pathname, search), path: `${pathname}${search}` }
}

/**
 * 页签标题。个股=代码+品种("600111 个股"), 深度分析=品种+代码, 其余走注册表;
 * 注册表里没有的(未登记的新路由)→ 诚实回落到裸 pathname(不编一个漂亮名字)。
 */
export function tabLabel(path: string, t: (key: string) => string): string {
  const { pathname, search } = parseTabPath(path)
  if (pathname.startsWith('/stocks/')) {
    const symbol = decodeURIComponent(pathname.slice('/stocks/'.length))
    const type = normalizeType(new URLSearchParams(search).get('type'))
    return `${symbol} ${t(stockSuffixKey(type))}`
  }
  if (pathname.startsWith('/copilot/runs/')) {
    const rid = decodeURIComponent(pathname.slice('/copilot/runs/'.length))
    return rid ? `${t('nav.copilotRuns')} ${rid.slice(0, 8)}` : t('nav.copilotRuns')
  }
  if (pathname.startsWith('/analysis/')) {
    const symbol = decodeURIComponent(pathname.slice('/analysis/'.length).split('/')[0] ?? '')
    return symbol ? `${t('tabs.analysis')} ${symbol}` : t('tabs.analysis')
  }
  const key = ROUTE_LABEL_KEYS[pathname]
  return key ? t(key) : pathname
}

/** 首页常驻页签 */
export function homeTab(): WorkspaceTab {
  return { key: HOME_TAB_KEY, path: HOME_TAB_KEY, closable: false }
}

/**
 * 规整一组页签(持久化恢复 / 外部输入都走这里):
 * 首页恒在且唯一居首; 非法条目丢弃(非字符串、相对路径、公开文档面、重复 key); 截断到上限。
 * 存进来的 closable 字段**一律重算** —— 首页不可关是代码定义的, 不由存储决定。
 */
export function normalizeTabs(tabs: readonly WorkspaceTab[], max = MAX_TABS): WorkspaceTab[] {
  const out: WorkspaceTab[] = [homeTab()]
  const seen = new Set<string>([HOME_TAB_KEY])
  for (const tab of tabs) {
    if (!tab || typeof tab.key !== 'string' || typeof tab.path !== 'string') continue
    if (!tab.path.startsWith('/') || !tab.key.startsWith('/')) continue
    if (seen.has(tab.key)) continue
    if (isNonTabbable(parseTabPath(tab.path).pathname)) continue
    if (out.length >= max) break
    seen.add(tab.key)
    out.push({ key: tab.key, path: tab.path, closable: tab.key !== HOME_TAB_KEY })
  }
  return out
}

/**
 * 打开/更新一个页签:
 * · 已开过(同 key) → 只更新它的 path(该页签内的最新地址), **不重排、不新开** —— 这是"同一路由不重复开"。
 * · 未开过 → 追加到末尾(首页永远在第一位)。
 * · 超过上限 → 丢最早的**非首页**页签(正在开的这个不丢)。
 * · 内容无变化 → **返回原引用**(调用方据此跳过持久化与重渲染)。
 */
/**
 * LRU 淘汰目标: 最久未「touch」的非首页页签。
 *
 * `touchOrder` 是壳层维护的访问顺序(最旧在前), **不参与 tabs 数组顺序** —— 页签栏的视觉顺序
 * 必须保持「打开顺序」, 否则关闭后的相邻落点和成片顺序断言全乱(见 tab-shell.test.ts)。
 * 找不到可用候选时退回 tabs 顺序里第一个非首页非 incoming, 保证不会因为 LRU 记录缺失而不淘汰。
 */
export function pickEvictionTarget(
  tabs: readonly WorkspaceTab[],
  touchOrder: readonly string[],
  incomingKey: string,
): string | undefined {
  const alive = new Set(tabs.map((tab) => tab.key))
  for (const key of touchOrder) {
    if (key === HOME_TAB_KEY || key === incomingKey) continue
    if (alive.has(key)) return key
  }
  for (const tab of tabs) {
    if (tab.key === HOME_TAB_KEY || tab.key === incomingKey) continue
    return tab.key
  }
  return undefined
}

export function upsertTab(
  tabs: WorkspaceTab[],
  spec: RouteTabSpec,
  max = MAX_TABS,
  /** 超限时要淘汰的 key(壳层按 LRU 算好传入); 不传则退回「最早的」非首页页签 */
  evictKey?: string,
): WorkspaceTab[] {
  const base = normalizeTabs(tabs, max)
  const idx = base.findIndex((tab) => tab.key === spec.key)
  if (idx >= 0) {
    if (base[idx].path === spec.path) return sameShape(tabs, base) ? tabs : base
    const next = base.slice()
    next[idx] = { ...next[idx], path: spec.path }
    return next
  }
  const next: WorkspaceTab[] = [
    ...base,
    { key: spec.key, path: spec.path, closable: spec.key !== HOME_TAB_KEY },
  ]
  while (next.length > max) {
    const lru = evictKey ? next.findIndex((tab) => tab.key === evictKey) : -1
    const victim = lru > 0 ? lru : next.findIndex((tab, i) => i > 0 && tab.key !== spec.key)
    if (victim < 0) break
    next.splice(victim, 1)
  }
  return next
}

/** 两个页签集是否同形(长度/顺序/key/path/closable 全等) */
function sameShape(a: readonly WorkspaceTab[], b: readonly WorkspaceTab[]): boolean {
  if (a.length !== b.length) return false
  return a.every(
    (tab, i) =>
      tab.key === b[i].key && tab.path === b[i].path && tab.closable === b[i].closable,
  )
}

export interface CloseResult {
  tabs: WorkspaceTab[]
  /** 需要切过去的页签 key; null = 不用导航(关的不是当前页签 / 关闭被拒) */
  nextActiveKey: string | null
}

/**
 * 关闭一个页签。**只关这一个** —— 首页常驻, 所以不存在"全关掉"的结果。
 * · key 不存在 或 不可关闭(首页) → 原样返回, nextActiveKey=null(调用方不导航)。
 * · 关的不是当前页签 → 只移除它, 当前页签不动。
 * · 关的是当前页签 → 切**相邻**页签: 优先右邻(移除后同下标), 无右邻取左邻, 兜底首页。
 */
export function closeTab(
  tabs: readonly WorkspaceTab[],
  key: string,
  activeKey: string,
): CloseResult {
  const base = normalizeTabs(tabs)
  const idx = base.findIndex((tab) => tab.key === key)
  if (idx < 0 || !base[idx].closable) return { tabs: base, nextActiveKey: null }
  const next = base.filter((tab) => tab.key !== key)
  if (key !== activeKey) return { tabs: next, nextActiveKey: null }
  const neighbor = next[idx] ?? next[next.length - 1] ?? homeTab()
  return { tabs: next, nextActiveKey: neighbor.key }
}

/** 最小存储接口(便于单测注入假 storage) */
export type TabStore = Pick<Storage, 'getItem' | 'setItem'>

function defaultStore(): TabStore | null {
  try {
    return typeof localStorage === 'undefined' ? null : localStorage
  } catch {
    return null // 隐私模式/沙箱: 不持久化, 但工作台照常可用
  }
}

/** 序列化(只存身份与地址; 标题由路由现算, 避免存储里的旧文案与 i18n 漂移) */
export function serializeTabs(tabs: readonly WorkspaceTab[]): string {
  const normalized = normalizeTabs(tabs)
  return JSON.stringify({
    v: 1,
    tabs: normalized.map((tab) => ({ key: tab.key, path: tab.path })),
  })
}

/** 反序列化: 任何形式的损坏(非 JSON/缺字段/错类型)一律降级为空(只剩首页), 不抛错 */
export function parsePersisted(raw: unknown): WorkspaceTab[] {
  if (typeof raw !== 'string' || !raw) return normalizeTabs([])
  let data: unknown
  try {
    data = JSON.parse(raw)
  } catch {
    return normalizeTabs([])
  }
  const list = (data as { tabs?: unknown } | null)?.tabs
  if (!Array.isArray(list)) return normalizeTabs([])
  return normalizeTabs(
    list.map((item) => {
      const it = item as { key?: unknown; path?: unknown } | null
      return {
        key: typeof it?.key === 'string' ? it.key : '',
        path: typeof it?.path === 'string' ? it.path : '',
        closable: true,
      }
    }),
  )
}

/** 从存储恢复(失败/损坏 → 只剩首页) */
export function loadTabs(store: TabStore | null = defaultStore()): WorkspaceTab[] {
  if (!store) return normalizeTabs([])
  try {
    return parsePersisted(store.getItem(TABS_STORAGE_KEY))
  } catch {
    return normalizeTabs([])
  }
}

/** 写回存储(失败只吞掉持久化, 不影响当前会话的页签行为) */
export function saveTabs(tabs: readonly WorkspaceTab[], store: TabStore | null = defaultStore()): void {
  if (!store) return
  try {
    store.setItem(TABS_STORAGE_KEY, serializeTabs(tabs))
  } catch {
    /* 配额满/隐私模式: 忽略 */
  }
}

/** 关闭其他: 只留首页 + 锚点页签(首页常驻不可关) */
export function closeOtherTabs(tabs: readonly WorkspaceTab[], keepKey: string): WorkspaceTab[] {
  const base = normalizeTabs(tabs)
  if (!base.some((tab) => tab.key === keepKey)) return base
  return base.filter((tab) => tab.key === HOME_TAB_KEY || tab.key === keepKey)
}

/** 关闭右侧: 保留锚点及其左侧全部页签(锚点不存在时原样返回) */
export function closeTabsToRight(tabs: readonly WorkspaceTab[], anchorKey: string): WorkspaceTab[] {
  const base = normalizeTabs(tabs)
  const idx = base.findIndex((tab) => tab.key === anchorKey)
  if (idx < 0) return base
  return base.slice(0, idx + 1)
}
