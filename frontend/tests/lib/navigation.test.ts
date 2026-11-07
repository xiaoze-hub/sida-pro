/**
 * P0 共享导航元数据钉子(2026-11-07)。
 *
 * 这组断言对应四个"用户可感"的失败模式, 不是为了覆盖率:
 *   ① 入口消失/死链 —— NAV_ITEMS 里的地址在 App.tsx 没有真实 <Route>;
 *   ② 同一入口三种叫法 —— 侧栏 / 移动端 / 命令面板 / 工作台页签文案漂移;
 *   ③ 权限绕过 —— 菜单藏了, 命令面板还能点进去(此前面板完全不过滤);
 *   ④ 老 URL 断开 —— 书签/推送链接 /history /alerts /forecast 打不开或目标页已删。
 *
 * 路由核对是**源码级**的(读 App.tsx 的 path 字面量): 路由是 JSX 声明, 没有运行时清单可读,
 * 但表与路由必须同时对得上 —— 这正是过去"加了 navItems 忘了加 Route"的漏洞所在。
 */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

import { describe, expect, it } from 'vitest'

import enUS from '@/locales/en-US'
import zhCN from '@/locales/zh-CN'
import {
  LEGACY_ALIASES,
  LEGACY_ALIAS_PATHS,
  LEGACY_ALIAS_PREFIXES,
  NAV_GROUPS,
  NAV_ITEMS,
  PALETTE_EXTRA_ENTRIES,
  ROUTE_LABEL_KEYS,
  SECONDARY_ROUTES,
  isNavItemVisible,
  legacyRedirectTarget,
  mobileNavItems,
  navGroupLabelKey,
  navItemsForGroup,
  palettePageCommands,
  routePathOf,
  spotlightNavItem,
  visibleNavGroups,
  type NavViewer,
} from '@/lib/navigation'

const APP_SRC = readFileSync(resolve(__dirname, '../..', 'src/App.tsx'), 'utf-8')
const ROUTE_PATHS = [...APP_SRC.matchAll(/<Route\s+path="([^"]+)"/g)].map((m) => m[1])

/** 全权限 owner(所有入口可见) */
const ALL_PERMS = [
  'view_dashboard',
  'view_quotes',
  'view_heatmap',
  'view_opportunities',
  'view_dark',
  'view_forecast',
  'view_reports',
  'edit_portfolio',
  'manage_shadow',
  'manage_paper_trading',
  'manage_users',
]
const OWNER: NavViewer = { perms: new Set(ALL_PERMS), role: 'owner', guest: false }

function lookup(dict: unknown, key: string): unknown {
  return key.split('.').reduce<unknown>((acc, k) => (acc && typeof acc === 'object' ? (acc as Record<string, unknown>)[k] : undefined), dict)
}

/** path 模式是否覆盖某条具体路径('/stocks/:symbol' 覆盖 '/stocks/600111') */
function matchesRoute(pattern: string, pathname: string): boolean {
  if (pattern === '*') return true
  const p = pattern.split('/').filter(Boolean)
  const q = pathname.split('/').filter(Boolean)
  if (p.length !== q.length) return false
  return p.every((seg, i) => seg.startsWith(':') || seg === q[i])
}

const hasRoute = (pathname: string): boolean => ROUTE_PATHS.some((p) => matchesRoute(p, pathname))

describe('① 每个入口都有真实路由(不死链)', () => {
  it('路由清单解析到了(否则下面的断言是空跑)', () => {
    expect(ROUTE_PATHS.length).toBeGreaterThan(20)
  })

  it('NAV_ITEMS 的目标页都在 App.tsx 声明了 <Route>', () => {
    const missing = NAV_ITEMS.filter((n) => !hasRoute(routePathOf(n.to))).map((n) => n.to)
    expect(missing, `这些导航入口点了会 404/回落首页: ${missing.join(', ')}`).toEqual([])
  })

  it('二级路由(参谋任务/工具目录等)也都有真实路由', () => {
    const missing = SECONDARY_ROUTES.filter((r) => !hasRoute(r.path)).map((r) => r.path)
    expect(missing).toEqual([])
  })

  it('页签标题注册表里没有"死 key"(每条都有真实路由)', () => {
    // 真 bug 先例: '/copilot/governance' 登记了标题, 真实路由却是 '/copilot-governance'
    const dead = Object.keys(ROUTE_LABEL_KEYS).filter((p) => !hasRoute(p))
    expect(dead, `这些页签标题永远匹配不到路由: ${dead.join(', ')}`).toEqual([])
  })

  it('入口地址唯一(不允许两条菜单指向同一地址)', () => {
    const seen = new Set<string>()
    const dup = NAV_ITEMS.filter((n) => (seen.has(n.to) ? true : (seen.add(n.to), false))).map((n) => n.to)
    expect(dup).toEqual([])
  })
})

describe('② 文案收敛: 侧栏/移动端/命令面板/页签用同一套 labelKey', () => {
  it('所有文案 key 在 zh-CN / en-US 词典里都存在', () => {
    const keys = new Set<string>([
      ...NAV_GROUPS.map((g) => g.labelKey),
      ...NAV_ITEMS.map((n) => n.labelKey),
      ...SECONDARY_ROUTES.map((r) => r.labelKey),
      ...PALETTE_EXTRA_ENTRIES.map((e) => e.labelKey),
      ...LEGACY_ALIASES.map((a) => a.labelKey),
    ])
    const broken: string[] = []
    for (const key of keys) {
      if (typeof lookup(zhCN, key) !== 'string') broken.push(`zh-CN:${key}`)
      if (typeof lookup(enUS, key) !== 'string') broken.push(`en-US:${key}`)
    }
    expect(broken).toEqual([])
  })

  it('每个可见入口在命令面板里用同一个文案与分组', () => {
    const palette = palettePageCommands(OWNER)
    const byTo = new Map(palette.map((e) => [e.to, e]))
    const mismatched = NAV_ITEMS.filter((n) => byTo.get(n.to)?.labelKey !== n.labelKey).map((n) => n.to)
    expect(mismatched).toEqual([])
    // 分组标题也来自同一张分组表(不再出现面板"投研" vs 侧栏"报告中心"这种漂移)
    for (const entry of palette) {
      if (entry.groupKey) expect(navGroupLabelKey(entry.groupKey)).toBeTruthy()
    }
  })

  it('桌面分组 / 移动槽位 / 页签标题都能找到同一入口', () => {
    for (const item of NAV_ITEMS) {
      if (item.group) {
        const inGroup = navItemsForGroup(item.group, OWNER).some((n) => n.to === item.to)
        expect(inGroup, `${item.to} 没有出现在分组 ${item.group}`).toBe(true)
      }
      const inMobile =
        mobileNavItems('primary', OWNER).some((n) => n.to === item.to) ||
        mobileNavItems('more', OWNER).some((n) => n.to === item.to)
      expect(inMobile, `${item.to} 在移动端两个槽位都找不到`).toBe(true)
      if (item.tabbable !== false) {
        const pathname = routePathOf(item.to)
        expect(ROUTE_LABEL_KEYS[pathname], `${item.to} 没有页签标题`).toBeTruthy()
        // 页签标题是"功能页"粒度: /reports?tab=history 与 /reports 共用「报告中心」;
        // 不带 ?tab= 的入口必须与页码文案完全一致。
        if (!item.to.includes('?')) {
          expect(ROUTE_LABEL_KEYS[pathname], `${item.to} 的页签标题与菜单不一致`).toBe(item.labelKey)
        }
      }
    }
  })

  it('可见分组 ≤6 且没有空组', () => {
    const groups = visibleNavGroups(OWNER)
    expect(groups.length).toBeLessThanOrEqual(6)
    expect(groups.every((g) => g.items.length > 0)).toBe(true)
    expect(groups.map((g) => g.key)).toEqual(NAV_GROUPS.map((g) => g.key))
  })
})

describe('③ 权限感知可见性(菜单与命令面板同一判定)', () => {
  it('缺少模块权限点 → 隐藏(会员能看到行情, 看不到暗盘/管理后台)', () => {
    const member: NavViewer = { perms: new Set(['view_dashboard', 'view_quotes', 'edit_portfolio']), role: 'member', guest: false }
    const visible = new Set(palettePageCommands(member).map((e) => e.to))
    expect(visible.has('/')).toBe(true)
    expect(visible.has('/quotes')).toBe(true)
    expect(visible.has('/dark-fund-top')).toBe(false) // view_dark
    expect(visible.has('/war-report')).toBe(false) // view_forecast
    expect(visible.has('/admin')).toBe(false) // ownerOnly + manage_users
    expect(visible.has('/heatmap')).toBe(false) // view_heatmap
    expect(visible.has('/portfolio')).toBe(true) // edit_portfolio
  })

  it('大盘观察入口保留无行情权限账号进入原无守卫ETF视图的能力', () => {
    const market = NAV_ITEMS.find((item) => item.to === '/market-watch')!
    expect(isNavItemVisible(market, { perms: new Set(), role: 'member', guest: false })).toBe(true)
  })

  it('主力资金入口只在拥有至少一个资金视图权限时可见', () => {
    const funds = NAV_ITEMS.find((item) => item.to === '/funds')!
    for (const perm of ['view_dark', 'view_forecast']) {
      expect(isNavItemVisible(funds, { perms: new Set([perm]), role: 'member', guest: false })).toBe(true)
    }
    expect(isNavItemVisible(funds, { perms: new Set(['view_quotes']), role: 'member', guest: false })).toBe(false)
  })

  it('ownerOnly: 非 owner 即便有 manage_users 也隐藏管理后台(Role 优先)', () => {
    const memberWithPerm: NavViewer = { perms: new Set(['manage_users']), role: 'member', guest: false }
    const admin = NAV_ITEMS.find((n) => n.to === '/admin')!
    expect(isNavItemVisible(admin, memberWithPerm)).toBe(false)
    expect(isNavItemVisible(admin, OWNER)).toBe(true)
  })

  it('owner 能看到全部入口(全权限)', () => {
    const visible = new Set(palettePageCommands(OWNER).map((e) => e.to))
    expect(NAV_ITEMS.every((n) => visible.has(n.to))).toBe(true)
    expect(spotlightNavItem(OWNER)?.to).toBe('/ai-copilot')
  })

  it('权限未加载(null) → 回退角色判断, 不误伤(与旧口径一致)', () => {
    const loading: NavViewer = { perms: null, role: 'member', guest: false }
    const visible = new Set(palettePageCommands(loading).map((e) => e.to))
    expect(visible.has('/funds')).toBe(true)
    expect(visible.has('/heatmap')).toBe(true)
    expect(visible.has('/admin')).toBe(false) // ownerOnly 仍按角色挡
  })

  it('guest(演示账号) 隐藏账户/系统管理页, 但保留行情与机会', () => {
    const guest: NavViewer = { perms: new Set(ALL_PERMS), role: 'guest', guest: true }
    const visible = new Set(palettePageCommands(guest).map((e) => e.to))
    expect(visible.has('/shadow')).toBe(false)
    expect(visible.has('/shadow?tab=paper')).toBe(false)
    expect(visible.has('/system')).toBe(false)
    expect(visible.has('/notifications?tab=alerts')).toBe(false)
    expect(visible.has('/system?tab=agents')).toBe(false)
    expect(visible.has('/quotes')).toBe(true)
    expect(visible.has('/opportunities')).toBe(true)
    expect(spotlightNavItem(guest)?.to).toBe('/ai-copilot')
  })

  it('隐藏的分组整体消失(不会留一个空组标题)', () => {
    const noDecision: NavViewer = { perms: new Set(['view_dashboard']), role: 'member', guest: false }
    const keys = visibleNavGroups(noDecision).map((g) => g.key)
    expect(keys).not.toContain('decision')
  })
})

describe('④ 移动端 5 槽位固定, 其余全在"更多"', () => {
  it('主槽位顺序不变(首页/持仓/机会/行情/通知)', () => {
    expect(mobileNavItems('primary', OWNER).map((n) => n.to)).toEqual([
      '/',
      '/portfolio',
      '/opportunities',
      '/quotes',
      '/notifications',
    ])
  })

  it('每个入口恰好落一个槽位(没有既不在底栏也不在更多的入口)', () => {
    const primary = new Set(mobileNavItems('primary', OWNER).map((n) => n.to))
    const more = new Set(mobileNavItems('more', OWNER).map((n) => n.to))
    const overlap = [...primary].filter((to) => more.has(to))
    expect(overlap).toEqual([])
    const missing = NAV_ITEMS.filter((n) => !primary.has(n.to) && !more.has(n.to)).map((n) => n.to)
    expect(missing).toEqual([])
  })

  it('权限只减少槽位项, 不改变顺序(不会把"更多"的项挪到主槽位)', () => {
    const none: NavViewer = { perms: new Set<string>(), role: 'member', guest: false }
    const full = mobileNavItems('primary', OWNER).map((n) => n.to)
    const reduced = mobileNavItems('primary', none).map((n) => n.to)
    const positions = reduced.map((to) => full.indexOf(to))
    expect(positions.every((i) => i >= 0)).toBe(true)
    expect(positions).toEqual([...positions].sort((a, b) => a - b))
    // 无任何权限点时只剩不需要模块权限的入口(通知), 且它仍在第 5 位
    expect(reduced).toEqual(full.filter((to) => to === '/notifications'))
  })
})

describe('⑤ 老 URL 兼容(书签/推送链接不断)', () => {
  it('每个别名都在不页签化清单里(跳转后不留僵尸页签)', () => {
    const uncovered = LEGACY_ALIASES.filter(
      (a) => !LEGACY_ALIAS_PATHS.includes(a.from) && !LEGACY_ALIAS_PREFIXES.some((p) => a.from.startsWith(p)),
    ).map((a) => a.from)
    expect(uncovered).toEqual([])
  })

  it('别名目标页真实存在(重定向不会二次 404)', () => {
    const broken = LEGACY_ALIASES.filter((a) => !hasRoute(routePathOf(a.to))).map((a) => `${a.from} → ${a.to}`)
    expect(broken).toEqual([])
  })

  it('动态别名(/forecast /quote/:symbol /l2 /index /boards)的路径都在 App.tsx 声明了', () => {
    const dynamic = LEGACY_ALIASES.filter((a) => a.kind === 'dynamic')
    expect(dynamic.length).toBeGreaterThan(0)
    const missing = dynamic.filter((a) => !APP_SRC.includes(`path="${a.from}"`)).map((a) => a.from)
    expect(missing).toEqual([])
  })

  it('静态别名由同一张表生成(不再手写 8 条 Route)', () => {
    expect(APP_SRC).toContain('LEGACY_ALIASES.filter')
    const staticAliases = LEGACY_ALIASES.filter((a) => a.kind === 'static')
    expect(staticAliases.map((a) => a.from)).toEqual(
      expect.arrayContaining(['/stocks', '/history', '/paper-trading', '/alerts', '/agents', '/datasources', '/audit', '/help']),
    )
  })

  it('别名的文案 key 与目标入口一致(菜单/面板/页签叫同一个名字)', () => {
    const target = new Map(NAV_ITEMS.map((n) => [n.to, n.labelKey]))
    const wrong: string[] = []
    for (const alias of LEGACY_ALIASES) {
      const expected = target.get(alias.to)
      if (expected && expected !== alias.labelKey) wrong.push(`${alias.from}: ${alias.labelKey} ≠ ${expected}`)
    }
    expect(wrong).toEqual([])
  })

  it('跳转时保留老链接 query, 新地址 ?tab= 覆盖同名键(书签筛选不丢)', () => {
    expect(legacyRedirectTarget('/reports?tab=history', '?days=30')).toBe('/reports?days=30&tab=history')
    expect(legacyRedirectTarget('/notifications?tab=alerts', '?tab=all&page=2')).toBe(
      '/notifications?tab=alerts&page=2',
    )
  })

  it('无 query 的老地址不产生裸 "?"(/stocks → /portfolio)', () => {
    expect(legacyRedirectTarget('/portfolio')).toBe('/portfolio')
    expect(legacyRedirectTarget('/portfolio', '')).toBe('/portfolio')
    expect(legacyRedirectTarget('/portfolio', '?view=positions')).toBe('/portfolio?view=positions')
  })
})
