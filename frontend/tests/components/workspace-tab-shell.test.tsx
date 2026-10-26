// @vitest-environment jsdom
/**
 * 页签式工作台(Tab Shell)交互钉子 —— 2026-10-15。
 *
 * 渲染**真**组件(WorkspaceTabShell + WorkspaceTabs + 真 MemoryRouter), 只把路由内容换成
 * 极简桩页面 —— 断言打的是用户看到的东西: 页签栏里有哪些页签、哪个高亮、内容区是哪一页、
 * 关掉之后是不是"只关那一个"。
 *
 * 2026-10-15 升级真 keep-alive: 每个已开页签一个常驻面板(同时挂载, 隐藏不卸)。
 * 所以"内容区是哪一页"要**限定在活动面板里**看(隐藏面板的桩页仍在 DOM 里, 这是 keep-alive 的代价)。
 * 断言语义一字未改。
 *
 * 用户原话是验收标准: 「每次关闭页签，而不是把整个功能全关掉」。
 */
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useNavigate } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import WorkspaceTabShell from '@/components/WorkspaceTabShell'
import { TABS_STORAGE_KEY } from '@/lib/tab-shell'

/** 模拟左侧菜单/任何站内跳转(真跳转都走这一条路径: 路由变 → 页签集跟着变) */
function NavProbe() {
  const navigate = useNavigate()
  const go = (to: string) => () => navigate(to)
  return (
    <div>
      <button data-testid="go-home" onClick={go('/')}>
        menu-home
      </button>
      <button data-testid="go-heatmap" onClick={go('/heatmap')}>
        menu-heatmap
      </button>
      <button data-testid="go-opps" onClick={go('/opportunities')}>
        menu-opportunities
      </button>
      <button data-testid="go-600111" onClick={go('/stocks/600111')}>
        menu-600111
      </button>
      <button data-testid="go-000001" onClick={go('/stocks/000001')}>
        menu-000001
      </button>
      <button data-testid="go-terms" onClick={go('/terms')}>
        menu-terms
      </button>
    </div>
  )
}

function renderShell(initialPath: string) {
  return render(
    <MemoryRouter initialEntries={[initialPath]}>
      <NavProbe />
      <WorkspaceTabShell>
        <Routes>
          <Route path="/" element={<div data-testid="page">首页内容</div>} />
          <Route path="/heatmap" element={<div data-testid="page">热力图内容</div>} />
          <Route path="/opportunities" element={<div data-testid="page">机会内容</div>} />
          <Route path="/stocks/:symbol" element={<div data-testid="page">工作台内容</div>} />
          <Route path="/terms" element={<div data-testid="page">协议内容</div>} />
          <Route path="*" element={<div data-testid="page">通配内容</div>} />
        </Routes>
      </WorkspaceTabShell>
    </MemoryRouter>,
  )
}

const tabButtons = () => screen.getAllByTestId('workspace-tab')
const labels = () => tabButtons().map((el) => el.textContent ?? '')
/** 内容区根(**活动面板**; 无活动面板时=非功能页的整页槽)。隐藏面板仍在 DOM, 故必须限定范围。 */
function visibleRoot(): HTMLElement {
  const pane = screen
    .getAllByTestId('workspace-tab-pane')
    .find((el) => el.getAttribute('data-active') === 'true')
  return pane ?? screen.getByTestId('workspace-ephemeral')
}
const page = () => within(visibleRoot()).getByTestId('page').textContent ?? ''
function activeLabel(): string | null {
  const item = screen
    .getAllByTestId('workspace-tab-item')
    .find((el) => el.getAttribute('data-active') === 'true')
  return item?.querySelector('[data-testid="workspace-tab"]')?.textContent ?? null
}
function clickTab(label: string) {
  const btn = tabButtons().find((el) => el.textContent === label)
  if (!btn) throw new Error(`页签栏里没有「${label}」: ${labels().join(' | ')}`)
  fireEvent.click(btn)
}
function closeTab(label: string) {
  const item = screen
    .getAllByTestId('workspace-tab-item')
    .find((el) => el.querySelector('[data-testid="workspace-tab"]')?.textContent === label)
  const btn = item?.querySelector('[data-testid="workspace-tab-close"]')
  if (!btn) throw new Error(`「${label}」没有关闭键(或页签不存在)`)
  fireEvent.click(btn)
}
function storedTabs(): { key: string; path: string }[] {
  const raw = localStorage.getItem(TABS_STORAGE_KEY)
  return raw ? (JSON.parse(raw).tabs as { key: string; path: string }[]) : []
}

beforeEach(() => {
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

describe('页签栏: 打开功能页', () => {
  it('进功能页 → 出现页签并高亮当前页; 首页常驻在第一位', () => {
    renderShell('/heatmap')
    expect(labels()).toEqual(['首页', '板块热力'])
    expect(activeLabel()).toBe('板块热力')
    expect(page()).toBe('热力图内容')
  })

  it('首页也是页签(不是空白工作台)', () => {
    renderShell('/')
    expect(labels()).toEqual(['首页'])
    expect(activeLabel()).toBe('首页')
    expect(page()).toBe('首页内容')
  })
})

describe('点左侧菜单: 未开过→新开, 已开过→切过去(不重复开)', () => {
  it('再点另一个功能页 → 新开页签, 前一个功能页签**还在**(不是整页切走)', () => {
    renderShell('/heatmap')
    fireEvent.click(screen.getByTestId('go-opps'))
    expect(labels()).toEqual(['首页', '板块热力', '市场扫描'])
    expect(activeLabel()).toBe('市场扫描')
    expect(page()).toBe('机会内容')
  })

  it('点回已开过的功能页 → 切过去, 页签数不变(同一路由不重复开)', () => {
    renderShell('/heatmap')
    fireEvent.click(screen.getByTestId('go-opps'))
    fireEvent.click(screen.getByTestId('go-heatmap'))
    expect(labels()).toEqual(['首页', '板块热力', '市场扫描'])
    expect(activeLabel()).toBe('板块热力')
    expect(page()).toBe('热力图内容')
  })

  it('点页签本体也能切(不只菜单)', () => {
    renderShell('/heatmap')
    fireEvent.click(screen.getByTestId('go-opps'))
    clickTab('首页')
    expect(activeLabel()).toBe('首页')
    expect(page()).toBe('首页内容')
    expect(labels()).toEqual(['首页', '板块热力', '市场扫描'])
  })

  it('个股按 symbol 分页签: 两个股票 = 两个页签; 同票回切不新开', () => {
    renderShell('/stocks/600111')
    expect(labels()).toEqual(['首页', '600111 个股'])
    fireEvent.click(screen.getByTestId('go-000001'))
    expect(labels()).toEqual(['首页', '600111 个股', '000001 个股'])
    expect(activeLabel()).toBe('000001 个股')
    fireEvent.click(screen.getByTestId('go-600111'))
    expect(labels()).toEqual(['首页', '600111 个股', '000001 个股'])
    expect(activeLabel()).toBe('600111 个股')
  })
})

describe('✕ 关闭: 只关那一个功能页', () => {
  it('首页页签没有 ✕(常驻); 功能页签每个都有 ✕', () => {
    renderShell('/heatmap')
    fireEvent.click(screen.getByTestId('go-opps'))
    expect(document.querySelector('[data-close-for="/"]')).toBeNull()
    const closes = screen.getAllByTestId('workspace-tab-close')
    expect(closes.length).toBe(2) // 板块热力 + 机会
    expect(closes.map((el) => el.getAttribute('data-close-for'))).toEqual([
      '/heatmap',
      '/opportunities',
    ])
    expect(screen.getByLabelText('关闭 板块热力 页签')).toBeTruthy()
  })

  it('关当前页签 → 落相邻页签(右邻优先), 其余页签一个不少', () => {
    renderShell('/heatmap')
    fireEvent.click(screen.getByTestId('go-opps'))
    closeTab('板块热力')
    expect(labels()).toEqual(['首页', '市场扫描'])
    expect(activeLabel()).toBe('市场扫描')
    expect(page()).toBe('机会内容')
  })

  it('关掉最后一个功能页签 → 回首页(只关那一个, 不"全关掉")', () => {
    renderShell('/heatmap')
    closeTab('板块热力')
    expect(labels()).toEqual(['首页'])
    expect(activeLabel()).toBe('首页')
    expect(page()).toBe('首页内容')
  })

  it('关非当前页签 → 只移除它自己, 当前页内容不动', () => {
    renderShell('/heatmap')
    fireEvent.click(screen.getByTestId('go-opps'))
    closeTab('板块热力')
    // 现在只剩 首页 + 机会; 再开一个, 然后关掉非当前的那个
    fireEvent.click(screen.getByTestId('go-heatmap'))
    expect(labels()).toEqual(['首页', '市场扫描', '板块热力'])
    expect(activeLabel()).toBe('板块热力')
    closeTab('市场扫描')
    expect(labels()).toEqual(['首页', '板块热力'])
    expect(activeLabel()).toBe('板块热力')
    expect(page()).toBe('热力图内容')
  })

  it('连关多个: 每次只少一个, 永远不会"什么都没了"', () => {
    renderShell('/heatmap')
    fireEvent.click(screen.getByTestId('go-opps'))
    fireEvent.click(screen.getByTestId('go-600111'))
    closeTab('600111 个股')
    expect(labels()).toEqual(['首页', '板块热力', '市场扫描'])
    closeTab('市场扫描')
    expect(labels()).toEqual(['首页', '板块热力'])
    closeTab('板块热力')
    expect(labels()).toEqual(['首页'])
    expect(page()).toBe('首页内容')
  })
})

describe('持久化: 刷新后恢复', () => {
  it('每次开/切/关都写 localStorage, 关掉的不会再被记住', () => {
    renderShell('/heatmap')
    fireEvent.click(screen.getByTestId('go-opps'))
    expect(storedTabs().map((x) => x.key)).toEqual(['/', '/heatmap', '/opportunities'])
    closeTab('市场扫描')
    expect(storedTabs().map((x) => x.key)).toEqual(['/', '/heatmap'])
  })

  it('重新挂载(≈刷新)后页签集恢复, 当前路由的页签高亮', () => {
    renderShell('/heatmap')
    fireEvent.click(screen.getByTestId('go-opps'))
    fireEvent.click(screen.getByTestId('go-600111'))
    cleanup()
    renderShell('/opportunities')
    expect(labels()).toEqual(['首页', '板块热力', '市场扫描', '600111 个股'])
    expect(activeLabel()).toBe('市场扫描')
    expect(page()).toBe('机会内容')
  })

  it('刷新落在未开过的功能页 → 恢复旧页签 + 新开当前页(不覆盖别人的页签)', () => {
    renderShell('/heatmap')
    cleanup()
    renderShell('/opportunities')
    expect(labels()).toEqual(['首页', '板块热力', '市场扫描'])
    expect(activeLabel()).toBe('市场扫描')
  })
})

describe('非功能页(用户协议)不页签化', () => {
  it('不新开页签、不高亮任何页签, 内容仍整页渲染', () => {
    renderShell('/heatmap')
    fireEvent.click(screen.getByTestId('go-terms'))
    expect(labels()).toEqual(['首页', '板块热力'])
    expect(activeLabel()).toBeNull()
    expect(page()).toBe('协议内容')
    expect(storedTabs().map((x) => x.key)).toEqual(['/', '/heatmap'])
  })
})
