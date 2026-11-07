// @vitest-environment jsdom
//
// P1 报告枢纽「按数据源正名」回归钉。
//
// 两个页签是**两种不同数据源**, 之前叫「报告中心 / 底稿归档」——既和侧栏、页内标题撞名,
// 也说不清哪个是"我的分析"。本文件守:
//   ① 页签按真实数据源命名: 分析记录(账号内 /history, 按用户隔离) vs 定时任务归档
//      (cron 写盘的 markdown 文件, 只读); 旧名不再出现;
//   ② 每个页签带来源说明(note 的 data-source 跟着激活页签变), 且 Tab 的 title 提示写出来源;
//   ③ 书签/深链兼容: ?tab=history、?tab=reports 仍各自落到原页签(key 没改);
//   ④ 权限不变: 无 view_reports 时归档页签隐藏, 直链 ?tab=reports 规范化回分析记录;
//      myPerms=null(未加载)不过滤(不闪隐);
//   ⑤ 状态不丢: 「立即分析」排队/报错态跨页签切换保持(轮询在 Hub 层, 不随 Tab 卸载)。
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const P = vi.hoisted(() => ({
  runCalls: 0,
  runImpl: null as null | (() => Promise<unknown>),
  locationSearch: '',
}))

vi.mock('@sida/api', () => ({
  getToken: () => 'test-token',
  // 分析记录: /history → 空列表(空态文案落在页内); 报告归档: /reports/list → 空
  fetchAPI: () => Promise.resolve([]),
  reportsApi: {
    list: () => Promise.resolve({ items: [], jobs: [] }),
    content: () => Promise.resolve({ content: '' }),
  },
  premarketApi: {
    getToday: () => Promise.resolve({ date: '2026-11-07', empty: true }),
    run: () => {
      P.runCalls++
      const impl = P.runImpl ?? (() => Promise.resolve({ queued: true }))
      return impl()
    },
    getEvidence: () => Promise.resolve({ available: false, blocks: [] }),
  },
}))

vi.mock('@/components/SafeMarkdown', () => ({ default: () => null }))
vi.mock('@/components/Skeleton', () => ({ Skeleton: () => <div /> }))
vi.mock('@/components/ErrorBanner', () => ({
  default: ({ errors }: { errors: Array<{ source: string }> }) => (
    <div data-testid="error-banner">{errors.map((e) => e.source).join(',')}</div>
  ),
}))
vi.mock('@/hooks/useI18n', () => {
  const t = (key: string) => key
  return { useI18n: () => ({ locale: 'zh-CN', setLocale: () => {}, t, dict: {} }) }
})

import ReportsHubPage from '../../src/pages/ReportsHub'

function LocationProbe() {
  const location = useLocation()
  P.locationSearch = location.search
  return <div data-testid="location-probe" />
}

function renderHub({ path = '/reports', myPerms }: { path?: string; myPerms?: Set<string> | null } = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route
            path="/reports"
            element={
              <>
                <ReportsHubPage myPerms={myPerms} />
                <LocationProbe />
              </>
            }
          />
          <Route path="/ai-copilot" element={<div data-testid="copilot-probe" />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

const tab = (name: string) => screen.getByRole('tab', { name })
const note = () => screen.getByTestId('reports-hub-source-note')

beforeEach(() => {
  localStorage.clear()
  P.runCalls = 0
  P.runImpl = null
  P.locationSearch = ''
})
afterEach(() => cleanup())

describe('ReportsHub 页签按数据源正名', () => {
  it('两个页签叫「分析记录」「定时任务归档」, 默认落分析记录, 旧名不再出现', async () => {
    renderHub()
    await screen.findByText('暂无分析记录')

    expect(tab('分析记录').getAttribute('aria-selected')).toBe('true')
    expect(tab('定时任务归档').getAttribute('aria-selected')).toBe('false')
    expect(screen.queryByRole('tab', { name: '报告中心' })).toBeNull()
    expect(screen.queryByRole('tab', { name: '底稿归档' })).toBeNull()
    expect(screen.queryByRole('tab', { name: '分析' })).toBeNull()

    // Tab 顺序: 分析记录在前(默认页), 定时任务归档在后
    const names = screen.getAllByRole('tab').map((el) => el.textContent)
    expect(names).toEqual(['分析记录', '定时任务归档'])
  })

  it('页签 title 提示写清来源: 分析记录=分析历史表(按用户隔离), 归档=cron 输出目录', async () => {
    renderHub()
    await screen.findByText('暂无分析记录')

    const historyHint = tab('分析记录').getAttribute('title') || ''
    expect(historyHint).toContain('分析历史')
    expect(historyHint).toContain('归属未知')

    const archiveHint = tab('定时任务归档').getAttribute('title') || ''
    expect(archiveHint).toContain('cron')
    expect(archiveHint).toContain('cron/output')
    expect(archiveHint).toContain('只读')
  })

  it('来源说明跟着激活页签走: 分析记录=账号内可重试; 定时任务归档=文件只读', async () => {
    renderHub()
    await screen.findByText('暂无分析记录')

    expect(note().getAttribute('data-source')).toBe('history')
    expect(note().textContent).toContain('分析记录')
    expect(note().textContent).toContain('管理员可查看归属未知的历史记录')
    expect(note().textContent).toContain('可重试')

    fireEvent.click(tab('定时任务归档'))

    await waitFor(() => expect(note().getAttribute('data-source')).toBe('reports'))
    expect(note().textContent).toContain('定时任务归档')
    expect(note().textContent).toContain('cron 输出目录')
    expect(note().textContent).toContain('只读')
    // 归档页签真的换了内容(Reports 页 e2e key 落地), 不是只换了标签
    await screen.findByText('reports.empty')
    // 分析记录页的空态已卸载
    expect(screen.queryByText('暂无分析记录')).toBeNull()
    // URL 同步写 tab key(可分享 / 可后退)
    expect(P.locationSearch).toContain('tab=reports')
  })
})

describe('ReportsHub 书签与深链兼容(改名不动 key)', () => {
  it('?tab=history 仍落分析记录', async () => {
    renderHub({ path: '/reports?tab=history' })
    await screen.findByText('暂无分析记录')
    expect(tab('分析记录').getAttribute('aria-selected')).toBe('true')
    expect(note().getAttribute('data-source')).toBe('history')
  })

  it('?tab=reports 仍落定时任务归档(旧书签/推送链接不失效)', async () => {
    renderHub({ path: '/reports?tab=reports' })
    await waitFor(() => expect(note().getAttribute('data-source')).toBe('reports'))
    expect(tab('定时任务归档').getAttribute('aria-selected')).toBe('true')
    await screen.findByText('reports.empty')
  })
})

describe('ReportsHub 权限过滤不因改名放宽', () => {
  it('无 view_reports: 只出分析记录, 直链 ?tab=reports 规范化回分析记录', async () => {
    renderHub({ path: '/reports?tab=reports', myPerms: new Set<string>() })
    await screen.findByText('暂无分析记录')

    expect(screen.queryByRole('tab', { name: '定时任务归档' })).toBeNull()
    expect(tab('分析记录').getAttribute('aria-selected')).toBe('true')
    expect(note().getAttribute('data-source')).toBe('history')
    // 归一化写入 URL, 不留一个指向不可见页签的 tab
    await waitFor(() => expect(P.locationSearch).toContain('tab=history'))
  })

  it('有 view_reports: 两个页签都在', async () => {
    renderHub({ path: '/reports', myPerms: new Set(['view_reports']) })
    await screen.findByText('暂无分析记录')
    expect(tab('分析记录')).toBeTruthy()
    expect(tab('定时任务归档')).toBeTruthy()
  })

  it('myPerms=null(未加载)不做权限过滤, 两个页签先都在(不闪隐)', async () => {
    renderHub({ path: '/reports', myPerms: null })
    await screen.findByText('暂无分析记录')
    expect(tab('分析记录')).toBeTruthy()
    expect(tab('定时任务归档')).toBeTruthy()
  })
})

describe('ReportsHub 状态跨页签保持', () => {
  it('「立即分析」排队态切到归档再切回, 仍是「分析生成中…」且不发第二次请求', async () => {
    renderHub()
    const btn = await screen.findByTestId('premarket-run-btn')
    fireEvent.click(btn)
    await waitFor(() => expect(P.runCalls).toBe(1))
    await waitFor(() => expect(btn.textContent).toContain('分析生成中…'))

    fireEvent.click(tab('定时任务归档'))
    await waitFor(() => expect(note().getAttribute('data-source')).toBe('reports'))
    expect(btn.textContent).toContain('分析生成中…')
    expect((btn as HTMLButtonElement).disabled).toBe(true)

    fireEvent.click(tab('分析记录'))
    await screen.findByText('暂无分析记录')
    expect(btn.textContent).toContain('分析生成中…')
    expect((btn as HTMLButtonElement).disabled).toBe(true)
    expect(P.runCalls).toBe(1)
  })

  it('run 失败的内联错误跨页签保持(不因换页签静默消失)', async () => {
    P.runImpl = () => {
      const e = new Error('Too many requests')
      Object.assign(e, { status: 429 })
      return Promise.reject(e)
    }
    renderHub()
    fireEvent.click(await screen.findByTestId('premarket-run-btn'))
    const err = await screen.findByTestId('premarket-run-error')
    expect(err.textContent).toContain('429')

    fireEvent.click(tab('定时任务归档'))
    await waitFor(() => expect(note().getAttribute('data-source')).toBe('reports'))

    const errAfter = screen.getByTestId('premarket-run-error')
    expect(errAfter.textContent).toContain('429')
    expect(errAfter.textContent).toContain('Too many requests')
  })

  it('「立即分析」按钮标明产出落「分析记录」页签', async () => {
    renderHub()
    const btn = await screen.findByTestId('premarket-run-btn')
    expect(btn.getAttribute('title')).toContain('分析记录')
  })
})

// 兜底: 未登录/接口异常等不该让页签崩掉 —— 归档页签的取数失败只落在页内 ErrorBanner
describe('ReportsHub 归档取数失败不炸页签', () => {
  it('归档页渲染出来(空列表), 页签仍可切回分析记录', async () => {
    renderHub({ path: '/reports?tab=reports' })
    await waitFor(() => expect(note().getAttribute('data-source')).toBe('reports'))
    await act(async () => {
      await Promise.resolve()
    })
    fireEvent.click(tab('分析记录'))
    await screen.findByText('暂无分析记录')
  })
})
