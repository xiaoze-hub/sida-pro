// @vitest-environment jsdom
/**
 * L2-E: 页签右键菜单（关闭 / 关闭其他 / 关闭右侧）—— 2026-10-24。
 *
 * 交接文档第 4 项。两个必须钉住的点：
 *   ① 菜单**不能**渲染在 tablist 内部 —— 否则菜单文案混进 `textContent`，
 *      `workspace-tab-shell.test.tsx` 里成片的 `expect(labels()).toEqual([...])` 会整片红。
 *      实现用 `createPortal(document.body)`，这里用 `within(tablist).queryByRole('menu')` 反向钉住。
 *   ② 「关闭其他」「关闭右侧」关掉当前页签时要有合理落点（不能停在已关闭的页签上）。
 *
 * 选择器一律走 `data-tab-key`，不依赖 i18n 文案（文案会变，key 是身份）。
 */
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useNavigate } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import WorkspaceTabShell from '@/components/WorkspaceTabShell'
import { TABS_STORAGE_KEY } from '@/lib/tab-shell'

const PATHS = ['/heatmap', '/opportunities', '/reports', '/portfolio'] as const

function NavProbe() {
  const navigate = useNavigate()
  return (
    <div>
      {PATHS.map((path) => (
        <button
          key={path}
          data-testid={`go${path}`}
          onClick={() => navigate(path)}
          type="button"
        >
          {path}
        </button>
      ))}
    </div>
  )
}

function renderShell(initialPath = '/') {
  return render(
    <MemoryRouter initialEntries={[initialPath]}>
      <NavProbe />
      <WorkspaceTabShell>
        <Routes>
          <Route path="/" element={<div data-testid="page">首页</div>} />
          {PATHS.map((path) => (
            <Route key={path} path={path} element={<div data-testid="page">{path}</div>} />
          ))}
          <Route path="*" element={<div data-testid="page">其他</div>} />
        </Routes>
      </WorkspaceTabShell>
    </MemoryRouter>,
  )
}

const tabKeys = () =>
  screen.getAllByTestId('workspace-tab-item').map((el) => el.getAttribute('data-tab-key') ?? '')

function openTabs(paths: readonly string[]) {
  for (const path of paths) fireEvent.click(screen.getByTestId(`go${path}`))
}

function itemOf(key: string): HTMLElement {
  const el = document.querySelector<HTMLElement>(
    `[data-testid="workspace-tab-item"][data-tab-key="${key}"]`,
  )
  if (!el) throw new Error(`页签栏里没有 ${key}：${tabKeys().join(' | ')}`)
  return el
}

function rightClick(key: string) {
  fireEvent.contextMenu(itemOf(key), { clientX: 120, clientY: 40 })
}

const menu = () => screen.queryByTestId('workspace-tab-menu')

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

describe('L2-E: 页签右键菜单', () => {
  it('右键页签出菜单，且菜单不在 tablist 内（不污染文本断言）', () => {
    renderShell('/')
    openTabs(['/heatmap', '/opportunities'])
    expect(menu()).toBeNull()

    rightClick('/heatmap')
    expect(menu()).not.toBeNull()
    // 关键反向断言：菜单是 body 的 portal，不在 tablist 子树里
    expect(within(screen.getByTestId('workspace-tabs')).queryByTestId('workspace-tab-menu')).toBeNull()
    // 三个动作都在
    expect(within(menu()!).getByTestId('tab-menu-close')).toBeTruthy()
    expect(within(menu()!).getByTestId('tab-menu-close-others')).toBeTruthy()
    expect(within(menu()!).getByTestId('tab-menu-close-right')).toBeTruthy()
  })

  it('点空白处与 Esc 都能关掉菜单', () => {
    renderShell('/')
    openTabs(['/heatmap'])
    rightClick('/heatmap')
    expect(menu()).not.toBeNull()
    fireEvent.pointerDown(document.body)
    expect(menu()).toBeNull()

    rightClick('/heatmap')
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(menu()).toBeNull()
  })

  it('关闭右侧：锚点左边全留，右边全关', () => {
    renderShell('/')
    openTabs(['/heatmap', '/opportunities', '/reports'])
    expect(tabKeys()).toHaveLength(4) // home + 3

    rightClick('/opportunities')
    fireEvent.click(within(menu()!).getByTestId('tab-menu-close-right'))

    expect(tabKeys()).toEqual(['/', '/heatmap', '/opportunities'])
    expect(menu()).toBeNull()
  })

  it('关闭其他：只留首页 + 锚点，并把当前视图落到锚点', () => {
    renderShell('/')
    openTabs(['/heatmap', '/opportunities', '/reports'])
    // 当前停在 /reports
    rightClick('/heatmap')
    fireEvent.click(within(menu()!).getByTestId('tab-menu-close-others'))

    expect(tabKeys()).toEqual(['/', '/heatmap'])
    // 当前页签(/reports)被关掉 → 应落到锚点 /heatmap，而不是停在已关闭的页签
    const active = screen
      .getAllByTestId('workspace-tab-item')
      .find((el) => el.getAttribute('data-active') === 'true')
    expect(active?.getAttribute('data-tab-key')).toBe('/heatmap')
  })

  it('首页右键：关闭不可用（首页常驻），关闭其他/关闭右侧按可关数量启用', () => {
    renderShell('/')
    openTabs(['/heatmap'])
    rightClick('/')
    const closeSelf = within(menu()!).getByTestId('tab-menu-close') as HTMLButtonElement
    expect(closeSelf.disabled).toBe(true)
    const closeRight = within(menu()!).getByTestId('tab-menu-close-right') as HTMLButtonElement
    expect(closeRight.disabled).toBe(false)
  })

  it('关闭右侧在最后一个页签上是禁用的（没有可关的）', () => {
    renderShell('/')
    openTabs(['/heatmap', '/opportunities'])
    rightClick('/opportunities')
    const closeRight = within(menu()!).getByTestId('tab-menu-close-right') as HTMLButtonElement
    expect(closeRight.disabled).toBe(true)
  })

  it('关闭动作会写回 localStorage（刷新后不复活）', () => {
    renderShell('/')
    openTabs(['/heatmap', '/opportunities', '/reports'])
    rightClick('/opportunities')
    fireEvent.click(within(menu()!).getByTestId('tab-menu-close-others'))
    const raw = localStorage.getItem(TABS_STORAGE_KEY)
    const stored = raw ? (JSON.parse(raw).tabs as { key: string }[]) : []
    expect(stored.map((t) => t.key)).toEqual(['/', '/opportunities'])
  })
})
