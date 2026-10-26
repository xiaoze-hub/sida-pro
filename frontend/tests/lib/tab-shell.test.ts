/**
 * 工作台页签(Tab Shell)状态机钉子 —— 2026-10-15。
 *
 * 用户诉求原话:「还需要做功能菜单的页签，这样每次关闭页签，而不是把整个功能全关掉」。
 * 所以这里的断言不是"函数返回了个数组", 而是把**用户可感的六条**逐条钉住:
 *   ① 同一路由不重复开(页内 `?tab=` 不新开; 个股按 symbol+品种粒度);
 *   ② 关页签只关那一个功能页, 首页常驻 ⇒ **永不"全关掉"**; 关当前页签落相邻页签;
 *   ③ 持久化往返(存了能读回; 存储损坏不炸、不编数据);
 *   ④ 老路由别名/公开文档面不页签化(否则留僵尸页签);
 *   ⑤ 左侧菜单每个入口都有页签标题(不留裸路径页签)—— 走 App.tsx 源码级覆盖, 同 nav-ia 的做法。
 */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

import { describe, expect, it } from 'vitest'

import zhCN from '@/locales/zh-CN'
import {
  HOME_TAB_KEY,
  LEGACY_ALIAS_PATHS,
  MAX_TABS,
  PUBLIC_PAGE_PATHS,
  ROUTE_LABEL_KEYS,
  TABS_STORAGE_KEY,
  closeTab,
  homeTab,
  isTabbable,
  loadTabs,
  normalizeTabs,
  parsePersisted,
  routeTab,
  saveTabs,
  serializeTabs,
  tabKeyOf,
  tabLabel,
  upsertTab,
  type TabStore,
  type WorkspaceTab,
} from '@/lib/tab-shell'

/** 走真词典的翻译函数: 文案 key 打错会露出 key 本身(断言会抓到) */
function t(key: string): string {
  const v = key
    .split('.')
    .reduce<unknown>((acc, k) => (acc && typeof acc === 'object' ? (acc as Record<string, unknown>)[k] : undefined), zhCN)
  return typeof v === 'string' ? v : key
}

/** 内存版 storage(不依赖 jsdom; 顺带记录写入次数) */
function memStore(initial: Record<string, string> = {}) {
  const map: Record<string, string> = { ...initial }
  let writes = 0
  const store: TabStore & { dump: () => Record<string, string>; writes: () => number } = {
    getItem: (k: string) => (k in map ? map[k] : null),
    setItem: (k: string, v: string) => {
      writes += 1
      map[k] = v
    },
    dump: () => ({ ...map }),
    writes: () => writes,
  }
  return store
}

const tab = (key: string, path = key): WorkspaceTab => ({ key, path, closable: key !== HOME_TAB_KEY })

describe('① 页签身份: 同一路由不重复开, 个股按 symbol', () => {
  it('普通功能页: 页内 ?tab= 不同, 仍是同一页签(否则页内切标签就长出页签)', () => {
    expect(tabKeyOf('/reports', '?tab=history')).toBe('/reports')
    expect(tabKeyOf('/reports', '?tab=alerts')).toBe('/reports')
    expect(tabKeyOf('/settings', '?tab=audit')).toBe(tabKeyOf('/settings', ''))
  })

  it('个股: 两只股票 = 两个页签(以 symbol 为粒度)', () => {
    expect(tabKeyOf('/stocks/600111')).toBe('/stocks/600111?type=stock')
    expect(tabKeyOf('/stocks/000001')).not.toBe(tabKeyOf('/stocks/600111'))
  })

  it('同票换工作台内标签(盘口/消息/预测)不新开页签', () => {
    expect(tabKeyOf('/stocks/600111', '?tab=l2')).toBe(tabKeyOf('/stocks/600111', '?tab=news'))
    expect(tabKeyOf('/stocks/600111', '?tab=news')).toBe('/stocks/600111?type=stock')
  })

  it('同代码不同品种(指数/板块)是两个页签', () => {
    expect(tabKeyOf('/stocks/000001', '?type=index')).toBe('/stocks/000001?type=index')
    expect(tabKeyOf('/stocks/000001', '?type=index')).not.toBe(tabKeyOf('/stocks/000001'))
  })

  it('routeTab 保留本页签内的最新地址(含 query), 点页签能回到原处', () => {
    expect(routeTab('/stocks/600111', '?tab=l2')).toEqual({
      key: '/stocks/600111?type=stock',
      path: '/stocks/600111?tab=l2',
    })
  })
})

describe('④ 非功能页不页签化(公开文档面 + 老别名 + 未知路由)', () => {
  it.each([...PUBLIC_PAGE_PATHS, ...LEGACY_ALIAS_PATHS])('%s 不页签化', (p) => {
    expect(isTabbable(p)).toBe(false)
    expect(routeTab(p)).toBeNull()
  })

  it('老别名的子路径也拦住(/index/:symbol、/boards/:code、/quote/:symbol)', () => {
    for (const p of ['/index/000001', '/boards/BK0475', '/quote/600111']) {
      expect(isTabbable(p)).toBe(false)
    }
  })

  it('未知路由(通配 catch-all)不页签化 —— 否则留一个再也回不去的僵尸页签', () => {
    expect(routeTab('/definitely-not-a-route')).toBeNull()
  })

  it('功能页照常页签化(含个股/深度分析)', () => {
    for (const p of ['/', '/heatmap', '/stocks/600111', '/analysis/002636/2026-09-12']) {
      expect(isTabbable(p)).toBe(true)
    }
  })
})

describe('页签标题', () => {
  it('首页/功能页走菜单文案, 个股=代码+品种', () => {
    expect(tabLabel('/', t)).toBe('首页')
    expect(tabLabel('/heatmap?tab=x', t)).toBe('板块热力')
    expect(tabLabel('/stocks/600111', t)).toBe('600111 个股')
    expect(tabLabel('/stocks/000001?type=index', t)).toBe('000001 指数')
    expect(tabLabel('/stocks/BK0475?type=board', t)).toBe('BK0475 板块')
    expect(tabLabel('/analysis/002636/2026-09-12', t)).toBe('分析 002636')
  })

  it('未登记路由诚实回落裸路径(不编一个漂亮名字)', () => {
    expect(tabLabel('/whatever', t)).toBe('/whatever')
  })

  it('注册表里的每个 key 在词典里都真存在(打错 key 会渲染出 key 本身)', () => {
    const broken = Object.entries(ROUTE_LABEL_KEYS).filter(([, key]) => t(key) === key)
    expect(broken).toEqual([])
    for (const k of ['tabs.stock', 'tabs.index', 'tabs.board', 'tabs.analysis', 'tabs.bar']) {
      expect(t(k), `${k} 缺中文文案`).not.toBe(k)
    }
    expect(t('tabs.close')).toContain('{name}') // 关闭键 aria-label 必须带名字占位
  })
})

describe('⑤ 左侧菜单全覆盖: 每个入口都有页签标题', () => {
  const appSrc = readFileSync(resolve(__dirname, '../..', 'src/App.tsx'), 'utf-8')
  const navBlock = appSrc.slice(appSrc.indexOf('const navItems'), appSrc.indexOf('const desktopNavGroups'))
  const navPaths = [...navBlock.matchAll(/to:\s*'([^']+)'/g)].map((m) => m[1])

  it('navItems 解析到了(否则下面的断言是空跑)', () => {
    expect(navPaths.length).toBeGreaterThan(15)
  })

  it('每个菜单入口要么有标题、要么被明确列为非页签路由', () => {
    const orphans = navPaths.filter((to) => {
      const pathname = to.split('?')[0]
      if (pathname.startsWith('/stocks/')) return false // 个股标题现算(代码+品种)
      if (!isTabbable(pathname)) return false // 已明确排除(老别名)
      return !ROUTE_LABEL_KEYS[pathname]
    })
    expect(orphans, `这些菜单入口会以裸路径当页签标题: ${orphans.join(', ')}`).toEqual([])
  })
})

describe('打开/切换: upsertTab', () => {
  it('新页签追加在末尾, 首页永远第一位', () => {
    const next = upsertTab(upsertTab(normalizeTabs([]), { key: '/heatmap', path: '/heatmap' }), {
      key: '/opportunities',
      path: '/opportunities',
    })
    expect(next.map((x) => x.key)).toEqual(['/', '/heatmap', '/opportunities'])
    expect(next[0].closable).toBe(false)
    expect(next.slice(1).every((x) => x.closable)).toBe(true)
  })

  it('同 key 再开 = 切到已有(不新开、不重排), 只更新页内地址', () => {
    let tabs = upsertTab(normalizeTabs([]), { key: '/heatmap', path: '/heatmap' })
    tabs = upsertTab(tabs, { key: '/opportunities', path: '/opportunities' })
    const before = tabs.slice()
    const after = upsertTab(tabs, { key: '/heatmap', path: '/heatmap?focus=1' })
    expect(after.map((x) => x.key)).toEqual(['/', '/heatmap', '/opportunities'])
    expect(after[1].path).toBe('/heatmap?focus=1')
    expect(after.length).toBe(before.length)
  })

  it('无变化时返回原引用(不触发多余持久化/重渲染)', () => {
    const tabs = upsertTab(normalizeTabs([]), { key: '/heatmap', path: '/heatmap' })
    expect(upsertTab(tabs, { key: '/heatmap', path: '/heatmap' })).toBe(tabs)
  })

  it('超过上限丢最早的**非首页**页签, 首页与新开的不丢', () => {
    let tabs = normalizeTabs([])
    for (let i = 0; i < MAX_TABS + 3; i++) {
      tabs = upsertTab(tabs, { key: `/page-${i}`, path: `/page-${i}` })
    }
    expect(tabs.length).toBe(MAX_TABS)
    expect(tabs[0].key).toBe(HOME_TAB_KEY)
    expect(tabs.some((x) => x.key === `/page-${MAX_TABS + 2}`)).toBe(true)
    expect(tabs.some((x) => x.key === '/page-0')).toBe(false)
  })
})

describe('② 关闭: 只关那一个功能页, 永不"全关掉"', () => {
  const three = () => normalizeTabs([tab('/heatmap'), tab('/opportunities'), tab('/reports')])

  it('关非当前页签: 只移除它自己, 当前页签不动、不导航', () => {
    const { tabs, nextActiveKey } = closeTab(three(), '/heatmap', '/reports')
    expect(tabs.map((x) => x.key)).toEqual(['/', '/opportunities', '/reports'])
    expect(nextActiveKey).toBeNull()
  })

  it('关当前页签: 落**右邻**(同下标), 其余页签一个不少', () => {
    const { tabs, nextActiveKey } = closeTab(three(), '/opportunities', '/opportunities')
    expect(nextActiveKey).toBe('/reports')
    expect(tabs.map((x) => x.key)).toEqual(['/', '/heatmap', '/reports'])
  })

  it('关当前页签且它是最后一个功能页签: 落左邻', () => {
    const { tabs, nextActiveKey } = closeTab(three(), '/reports', '/reports')
    expect(nextActiveKey).toBe('/opportunities')
    expect(tabs.map((x) => x.key)).toEqual(['/', '/heatmap', '/opportunities'])
  })

  it('关掉最后一个功能页签 → 回首页; 页签集只剩首页(不是空工作台)', () => {
    const { tabs, nextActiveKey } = closeTab(normalizeTabs([tab('/heatmap')]), '/heatmap', '/heatmap')
    expect(tabs.map((x) => x.key)).toEqual([HOME_TAB_KEY])
    expect(nextActiveKey).toBe(HOME_TAB_KEY)
  })

  it('首页不可关(关闭被拒: 页签集不变, 不导航)', () => {
    const before = three()
    const { tabs, nextActiveKey } = closeTab(before, HOME_TAB_KEY, HOME_TAB_KEY)
    expect(tabs.map((x) => x.key)).toEqual(before.map((x) => x.key))
    expect(nextActiveKey).toBeNull()
  })

  it('关不存在的 key 是无操作(不误删、不导航)', () => {
    const { tabs, nextActiveKey } = closeTab(three(), '/nope', '/heatmap')
    expect(tabs.length).toBe(4)
    expect(nextActiveKey).toBeNull()
  })

  it('无论怎么关, 页签集永远非空(start 首页)', () => {
    let tabs = three()
    for (const key of ['/heatmap', '/opportunities', '/reports', '/heatmap']) {
      tabs = closeTab(tabs, key, key).tabs
      expect(tabs.length).toBeGreaterThan(0)
      expect(tabs[0].key).toBe(HOME_TAB_KEY)
    }
  })
})

describe('③ 持久化往返', () => {
  it('存了能原样读回(顺序保留), 首页不可关是代码定义不由存储决定', () => {
    const store = memStore()
    const tabs = normalizeTabs([{ key: '/heatmap', path: '/heatmap', closable: true }, tab('/stocks/600111?type=stock', '/stocks/600111?tab=l2')])
    saveTabs(tabs, store)
    expect(store.writes()).toBe(1)
    expect(store.dump()[TABS_STORAGE_KEY]).toContain('"v":1')
    const back = loadTabs(store)
    expect(back).toEqual([
      homeTab(),
      { key: '/heatmap', path: '/heatmap', closable: true },
      { key: '/stocks/600111?type=stock', path: '/stocks/600111?tab=l2', closable: true },
    ])
  })

  it('空存储 → 只剩首页(不是报错/不是空数组)', () => {
    expect(loadTabs(memStore())).toEqual([homeTab()])
  })

  it('损坏数据一律降级为只剩首页(不抛错、不编页签)', () => {
    for (const bad of [
      null,
      '',
      'not json',
      '{}',
      '{"tabs":"nope"}',
      '{"tabs":[null,1,{}]}',
      '{"tabs":[{"key":"/heatmap"}]}', // 缺 path
      '{"tabs":[{"key":"relative","path":"heatmap"}]}', // 相对路径
      '{"tabs":[{"key":"/terms","path":"/terms"}]}', // 非页签路由
      '{"tabs":[{"key":"/","path":"/"},{"key":"/heatmap","path":"/heatmap"}]}', // 首页存在也要规整
    ]) {
      const back = parsePersisted(bad)
      expect(Array.isArray(back)).toBe(true)
      expect(back[0].key).toBe(HOME_TAB_KEY)
      expect(back.filter((x) => x.key === 'relative').length).toBe(0)
      expect(back.filter((x) => x.key === '/terms').length).toBe(0)
    }
  })

  it('重复 key 去重、超上限截断(损坏的存储不放大成巨长页签栏)', () => {
    const dup = normalizeTabs([tab('/heatmap'), tab('/heatmap'), tab('/opportunities')])
    expect(dup.map((x) => x.key)).toEqual(['/', '/heatmap', '/opportunities'])

    const many = normalizeTabs(Array.from({ length: MAX_TABS + 5 }, (_, i) => tab(`/p${i}`)))
    expect(many.length).toBe(MAX_TABS)
  })

  it('序列化后再规整是幂等的(反复刷新不会漂移)', () => {
    const tabs = normalizeTabs([tab('/heatmap'), tab('/stocks/600111?type=stock', '/stocks/600111?tab=news')])
    const round1 = parsePersisted(serializeTabs(tabs))
    const round2 = parsePersisted(serializeTabs(round1))
    expect(round1).toEqual(round2)
    expect(round1).toEqual(tabs)
  })

  it('storage 抛错(配额满/隐私模式)时静默降级, 不影响本次会话', () => {
    const boom: TabStore = {
      getItem: () => {
        throw new Error('blocked')
      },
      setItem: () => {
        throw new Error('quota')
      },
    }
    expect(loadTabs(boom)).toEqual([homeTab()])
    expect(() => saveTabs([homeTab()], boom)).not.toThrow()
    expect(loadTabs(null)).toEqual([homeTab()])
  })
})
