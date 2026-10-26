// @vitest-environment jsdom
/**
 * 真 keep-alive 钉子 —— 2026-10-15。
 *
 * 用户新裁决(原话): 「需要支持同时存在多个页签，点击哪个页签就进入里面的内容」。
 * 本文件守的就是这句话的可感后果(渲染**真**组件, 只把路由内容换成带状态的桩页):
 *   ① 已开页签的页面组件**同时存在**(隐藏不卸) —— 隐藏面板仍在 DOM 里;
 *   ② 切走再切回**状态不丢**(输入框的值还在), 且**不重新挂载**(实例复用, 不是重建);
 *   ③ 隐藏页签里的组件读到 `useTabActive() === false`, 活动页签读到 `true`(数据层据此停表);
 *   ④ 地址栏 = **活动页签**的地址: 活动页签内导航同步地址栏; 隐藏页签内导航不动地址栏;
 *   ⑤ 跨页签导航交给外层路由(开/切对应页签), **不会**把别的功能页塞进当前页签;
 *   ⑥ 页签上限: 挤掉最久未访问的非首页页签并**明确提示**(不静默丢页)。
 */
import { useEffect, useState } from 'react'
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import {
  MemoryRouter,
  Route,
  Routes,
  useLocation,
  useNavigate,
  useParams,
  type NavigateFunction,
} from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { ToastProvider } from '@panwatch/base-ui/components/ui/toast'
import { useTabActive } from '@panwatch/base-ui'

import WorkspaceTabShell from '@/components/WorkspaceTabShell'
import { MAX_TABS, TABS_STORAGE_KEY } from '@/lib/tab-shell'

/** 每个桩页的**挂载次数**: "切走再切回不重建"靠它钉住 */
const mounts: Record<string, number> = {}
/** 外层路由的导航句柄(模拟左侧菜单/命令面板的站内跳转) */
let outerNavigate: NavigateFunction | null = null

function OuterProbe() {
  const navigate = useNavigate()
  const location = useLocation()
  outerNavigate = navigate
  return <div data-testid="outer-url">{location.pathname + location.search}</div>
}

/** 带状态的桩页: 输入框(状态) + 内部导航(同页签) + 跨页签跳转 */
function StatePage({ name, inner, cross }: { name: string; inner?: string; cross?: string }) {
  const [val, setVal] = useState('')
  const navigate = useNavigate()
  const active = useTabActive()
  useEffect(() => {
    mounts[name] = (mounts[name] ?? 0) + 1
  }, [name])
  return (
    <div data-testid="page">
      <span>{name}内容</span>
      <span data-testid={`active-${name}`}>{active ? 'on' : 'off'}</span>
      <input
        data-testid={`input-${name}`}
        value={val}
        onChange={(e) => setVal(e.target.value)}
      />
      {inner && (
        <button type="button" data-testid={`inner-${name}`} onClick={() => navigate(inner)}>
          inner
        </button>
      )}
      {cross && (
        <button type="button" data-testid={`cross-${name}`} onClick={() => navigate(cross)}>
          cross
        </button>
      )}
    </div>
  )
}

function StockPage() {
  const { symbol } = useParams()
  return <StatePage name={`stock-${symbol}`} inner="?tab=l2" />
}

function renderShell(initialPath: string) {
  return render(
    <MemoryRouter initialEntries={[initialPath]}>
      <ToastProvider>
        <OuterProbe />
        <WorkspaceTabShell>
          <Routes>
            <Route path="/" element={<StatePage name="首页" />} />
            <Route path="/heatmap" element={<StatePage name="热力" inner="?focus=1" cross="/stocks/600111" />} />
            <Route path="/opportunities" element={<StatePage name="机会" />} />
            <Route path="/reports" element={<StatePage name="报告" inner="?tab=history" />} />
            <Route path="/stocks/:symbol" element={<StockPage />} />
            <Route path="*" element={<div data-testid="page">通配内容</div>} />
          </Routes>
        </WorkspaceTabShell>
      </ToastProvider>
    </MemoryRouter>,
  )
}

function go(to: string) {
  act(() => {
    if (!outerNavigate) throw new Error('外层路由句柄还没就绪')
    outerNavigate(to)
  })
}

const tabLabels = () => screen.getAllByTestId('workspace-tab').map((el) => el.textContent ?? '')
function paneOf(key: string): HTMLElement {
  const el = document.querySelector<HTMLElement>(
    `[data-testid="workspace-tab-pane"][data-tab-key="${key}"]`,
  )
  if (!el) throw new Error(`没有页签面板: ${key}；现有 ${screen.getAllByTestId('workspace-tab-pane').map((p) => p.getAttribute('data-tab-key')).join(' | ')}`)
  return el
}
function activeKey(): string | null {
  return (
    screen
      .getAllByTestId('workspace-tab-pane')
      .find((p) => p.getAttribute('data-active') === 'true')
      ?.getAttribute('data-tab-key') ?? null
  )
}
const outerUrl = () => screen.getByTestId('outer-url').textContent ?? ''
function clickTab(label: string) {
  const btn = screen.getAllByTestId('workspace-tab').find((el) => el.textContent === label)
  if (!btn) throw new Error(`页签栏里没有「${label}」: ${tabLabels().join(' | ')}`)
  fireEvent.click(btn)
}

beforeEach(() => {
  for (const k of Object.keys(mounts)) delete mounts[k]
  try {
    localStorage.clear()
  } catch {
    /* ignore */
  }
})
afterEach(() => {
  cleanup()
  try {
    localStorage.clear()
  } catch {
    /* ignore */
  }
})

describe('① 多页签同时存在(隐藏不卸)', () => {
  it('打开两个功能页 → 两个面板都在 DOM 里, 非活动的那个隐藏但不卸载', () => {
    renderShell('/heatmap')
    go('/opportunities')
    expect(tabLabels()).toEqual(['首页', '板块热力', '市场扫描'])
    expect(screen.getAllByTestId('workspace-tab-pane').length).toBe(3)
    const heat = paneOf('/heatmap')
    expect(heat.getAttribute('data-active')).toBe('false')
    expect(heat.hidden).toBe(true)
    // 关键: 隐藏面板的**内容还在**(不是卸载)
    expect(within(heat).getByTestId('page').textContent).toContain('热力内容')
    expect(activeKey()).toBe('/opportunities')
  })
})

describe('② 切走再切回: 状态与实例都保住', () => {
  it('输入框的值不丢, 组件实例不重建(挂载次数恒为 1)', () => {
    renderShell('/heatmap')
    const heat = paneOf('/heatmap')
    fireEvent.change(within(heat).getByTestId('input-热力'), { target: { value: 'abc' } })
    expect(mounts['热力']).toBe(1)

    go('/opportunities') // 切走(隐藏)
    expect(mounts['热力']).toBe(1) // 隐藏 ≠ 卸载

    clickTab('板块热力') // 切回
    expect(activeKey()).toBe('/heatmap')
    expect((within(paneOf('/heatmap')).getByTestId('input-热力') as HTMLInputElement).value).toBe('abc')
    expect(mounts['热力']).toBe(1) // 切回也不重建
  })

  it('两个个股页签: 各自状态互不串味', () => {
    renderShell('/stocks/600111')
    fireEvent.change(within(paneOf('/stocks/600111?type=stock')).getByTestId('input-stock-600111'), {
      target: { value: 'AAA' },
    })
    go('/stocks/000001')
    fireEvent.change(within(paneOf('/stocks/000001?type=stock')).getByTestId('input-stock-000001'), {
      target: { value: 'BBB' },
    })
    clickTab('600111 个股')
    expect(activeKey()).toBe('/stocks/600111?type=stock')
    expect(
      (within(paneOf('/stocks/600111?type=stock')).getByTestId('input-stock-600111') as HTMLInputElement).value,
    ).toBe('AAA')
    expect(
      (within(paneOf('/stocks/000001?type=stock')).getByTestId('input-stock-000001') as HTMLInputElement).value,
    ).toBe('BBB')
  })
})

describe('③ 隐藏页签的组件知道自己"不可见"(数据层据此停表)', () => {
  it('活动页签 useTabActive()=true, 隐藏页签 =false', () => {
    renderShell('/heatmap')
    go('/opportunities')
    expect(within(paneOf('/opportunities')).getByTestId('active-机会').textContent).toBe('on')
    expect(within(paneOf('/heatmap')).getByTestId('active-热力').textContent).toBe('off')
    // 切回后翻转
    clickTab('板块热力')
    expect(within(paneOf('/heatmap')).getByTestId('active-热力').textContent).toBe('on')
    expect(within(paneOf('/opportunities')).getByTestId('active-机会').textContent).toBe('off')
  })
})

describe('④ 地址栏 = 活动页签的地址', () => {
  it('活动页签内导航 → 地址栏跟着走, 不新开页签', () => {
    renderShell('/heatmap')
    expect(outerUrl()).toBe('/heatmap')
    fireEvent.click(within(paneOf('/heatmap')).getByTestId('inner-热力'))
    expect(outerUrl()).toBe('/heatmap?focus=1')
    expect(tabLabels()).toEqual(['首页', '板块热力'])
    expect(activeKey()).toBe('/heatmap')
  })

  it('隐藏页签内导航不动地址栏、不新开页签(内容只在它自己页签里变)', () => {
    renderShell('/heatmap')
    go('/reports')
    const before = tabLabels()
    fireEvent.click(within(paneOf('/heatmap')).getByTestId('inner-热力'))
    expect(outerUrl()).toBe('/reports') // 地址栏仍是活动页签的
    expect(tabLabels()).toEqual(before)
    expect(activeKey()).toBe('/reports')
  })

  it('切到已开页签 → 地址栏回到该页签上次的地址', () => {
    renderShell('/heatmap')
    fireEvent.click(within(paneOf('/heatmap')).getByTestId('inner-热力')) // /heatmap?focus=1
    go('/opportunities')
    clickTab('板块热力')
    expect(outerUrl()).toBe('/heatmap?focus=1')
  })
})

describe('⑤ 跨页签导航交给外层(不把别的功能页塞进当前页签)', () => {
  it('热力页签里点"去个股" → 开个股页签并切过去, 热力页签内容不被串味', () => {
    renderShell('/heatmap')
    fireEvent.click(within(paneOf('/heatmap')).getByTestId('cross-热力'))
    expect(tabLabels()).toEqual(['首页', '板块热力', '600111 个股'])
    expect(activeKey()).toBe('/stocks/600111?type=stock')
    expect(outerUrl()).toBe('/stocks/600111')
    // 热力页签里仍是热力内容(没被个股工作台顶掉)
    expect(within(paneOf('/heatmap')).getByTestId('page').textContent).toContain('热力内容')
    expect(within(paneOf('/heatmap')).queryByText(/600111 内容/)).toBeNull()
  })
})

describe('⑥ 页签上限: 挤掉最久未访问的非首页页签并提示(L2-E: 12→8 且 FIFO→LRU)', () => {
  it('开满上限后继续开 → 最久未访问的非首页页签被挤掉, 并弹出提示', () => {
    renderShell('/')
    const extras = [
      '/heatmap',
      '/opportunities',
      '/reports',
      '/theme-mood',
      '/dark-fund-top',
      '/war-report',
      '/caliber-compare',
      '/decision-ledger',
      '/factor-ic',
      '/portfolio',
      '/shadow',
      '/notifications',
      '/profile',
    ]
    for (const r of extras) go(r)
    expect(tabLabels().length).toBe(MAX_TABS)
    expect(tabLabels()[0]).toBe('首页')
    expect(tabLabels()).not.toContain('板块热力') // 最早的非首页页签被挤掉
    expect(tabLabels()).toContain('个人中心') // 刚开的还在
    const toasts = screen.getAllByText(new RegExp(`页签最多 ${MAX_TABS} 个`))
    expect(toasts.length).toBeGreaterThan(0)
    expect(toasts[0].textContent).toContain('板块热力')
    // 被挤掉的页签面板也不在 DOM 里了(它的状态随之释放)
    expect(
      document.querySelector('[data-testid="workspace-tab-pane"][data-tab-key="/heatmap"]'),
    ).toBeNull()
  })
})

describe('⑦ 非功能页(用户协议类)不占页签, 走页签之外的整页槽', () => {
  it('跳到非页签路由 → 页签栏不新增, 内容在整页槽里渲染', () => {
    renderShell('/heatmap')
    go('/terms')
    expect(tabLabels()).toEqual(['首页', '板块热力'])
    expect(activeKey()).toBeNull()
    expect(within(screen.getByTestId('workspace-ephemeral')).getByTestId('page').textContent).toContain('通配内容')
    // 已开页签仍在(隐藏但不卸)
    expect(paneOf('/heatmap').hidden).toBe(true)
  })
})

describe('持久化: 恢复的页签也各就各位', () => {
  it('mounted 前先有存储 → 恢复的页签都建面板, 只有当前路由那个是活动的', () => {
    localStorage.setItem(
      TABS_STORAGE_KEY,
      JSON.stringify({ v: 1, tabs: [{ key: '/heatmap', path: '/heatmap' }, { key: '/opportunities', path: '/opportunities' }] }),
    )
    renderShell('/opportunities')
    expect(tabLabels()).toEqual(['首页', '板块热力', '市场扫描'])
    expect(activeKey()).toBe('/opportunities')
    expect(paneOf('/heatmap').hidden).toBe(true)
    expect(within(paneOf('/heatmap')).getByTestId('page').textContent).toContain('热力内容')
  })
})
