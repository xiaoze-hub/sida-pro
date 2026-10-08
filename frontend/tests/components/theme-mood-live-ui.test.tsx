// @vitest-environment jsdom
/**
 * 题材情绪 /theme-mood「实时数据更新 + 收盘定型」(2026-10-08)。
 *
 * 契约(`GET /api/theme-mood/board` 顶层新增, 后端另一路同步实现, 字段可能先到/后到):
 *   phase: 'pre' | 'live' | 'closed_pending' | 'final'
 *   as_of / settled_at(ISO8601 +08:00) / trading_day / note
 *
 * 本文件钉住四件事(改坏了就是违约):
 *  ① 五态徽标各自渲染与文案, 且**缺字段/枚举外 → 显式「未知状态」, 不假装成 live/final**;
 *  ② phase=live 每 60s 轮询一次 board; final 之后**停止**轮询; 页面隐藏暂停、回前台立即补拉;
 *  ③ 轮询失败**静默**(单条可见提示, 不弹错误风暴), 旧数据保留;
 *  ④ 竞态守卫: 已被更新请求取代的旧响应**不落地**; as_of/基准日显式标注, 缺数据显示「无数据」。
 *
 * 铁律: 布局/密度/分层零改动 → 本文件不做结构性断言(那是 theme-mood-layering/density 钉子);
 * 测试禁真实网络(全程 mock `@panwatch/api` 的 fetchAPI)。
 */
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({ fetchAPI: vi.fn() }))
vi.mock('@panwatch/api', () => ({ fetchAPI: (...a: unknown[]) => mocks.fetchAPI(...a) }))

import ThemeMoodPage from '@/pages/ThemeMood'
import ThemeMoodStatus, { parseBoardPhase } from '@/components/ThemeMoodStatus'

/** 一个可手动 resolve 的挂起请求(竞态用例用)。 */
function deferred<T = unknown>() {
  let resolve!: (v: T) => void
  let reject!: (e: unknown) => void
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}

function item(name: string, score: number) {
  return {
    block_code: '881101.SH', block_name: name, block_type: 'industry', score, delta: 4.1,
    confidence: 86, core: true, s1: 80, s2: 75, s3: 82, s4: 70, s5: 60, limit_up_cnt: 9, max_boards: 3,
    core_stocks: [], cells: [{ date: '20261008', score, limit_up_cnt: 9 }],
  }
}

function board(over: Record<string, unknown> = {}, name = '甲题材', score = 71.7) {
  return {
    trade_date: '20261008', window: 20, count: 1,
    dates: ['20261007', '20261008'],
    market: [{ date: '20261007', score: 50 }, { date: '20261008', score: score - 1 }],
    items: [item(name, score)],
    ...over,
  }
}

const LADDER = { window: 20, days: [], live_day: false, stale: false, note_closing: null }

/** board 请求次数(只看 board, ladder 有独立节奏)。 */
const boardCalls = () =>
  mocks.fetchAPI.mock.calls.map((c) => String(c[0])).filter((u) => u.includes('/theme-mood/board')).length

/** 冲掉已 resolve 的取数微任务。 */
async function flush() {
  await act(async () => { await Promise.resolve() })
}

/** 推进假时钟并冲掉期间产生的取数微任务。 */
async function tick(ms: number) {
  await act(async () => { await vi.advanceTimersByTimeAsync(ms) })
}

let hidden = false

beforeEach(() => {
  vi.useFakeTimers()
  hidden = false
  Object.defineProperty(document, 'hidden', { configurable: true, get: () => hidden })
  mocks.fetchAPI.mockReset()
  mocks.fetchAPI.mockImplementation((url: string) => {
    const u = String(url)
    if (u.includes('/theme-mood/board')) return Promise.resolve(board())
    if (u.includes('/theme-mood/ladder')) return Promise.resolve(LADDER)
    return Promise.resolve({ ok: false })
  })
})

afterEach(() => {
  cleanup()
  vi.useRealTimers()
})

// ── ① 五态徽标 + 容错解析 ────────────────────────────────────────────────────

describe('ThemeMoodStatus 状态徽标(五态, 含未知态)', () => {
  it('五个状态各自渲染自己的文案, 一态不落', () => {
    const cases: Array<[string, string]> = [
      ['pre', '盘前'],
      ['live', '实时'],
      ['closed_pending', '收盘待定型'],
      ['final', '已定型'],
      ['unknown', '未知状态'],
    ]
    for (const [phase, text] of cases) {
      const { unmount } = render(
        <ThemeMoodStatus phase={phase as never} as_of="2026-10-08T10:30:00+08:00" settled_at="2026-10-08T15:05:00+08:00" />,
      )
      expect(screen.getByText(text)).toBeTruthy()
      expect(screen.getByTestId('thememood-phase-badge').getAttribute('data-phase')).toBe(phase)
      unmount()
    }
  })

  it('live 显示 as_of 快照时间, final 显示 settled_at 定型时间', () => {
    const { unmount } = render(<ThemeMoodStatus phase="live" as_of="2026-10-08T10:30:00+08:00" />)
    expect(screen.getByText('快照 2026-10-08T10:30:00+08:00')).toBeTruthy()
    unmount()

    render(<ThemeMoodStatus phase="final" as_of="2026-10-08T15:05:00+08:00" settled_at="2026-10-08T15:05:00+08:00" />)
    expect(screen.getByText('定型 2026-10-08T15:05:00+08:00')).toBeTruthy()
  })

  it('时间字段缺失 → 显式「无数据」, 不填 0 也不留空(幻觉敏感红线)', () => {
    const { unmount } = render(<ThemeMoodStatus phase="live" as_of={null} />)
    expect(screen.getByText('快照 无数据')).toBeTruthy()
    unmount()

    render(<ThemeMoodStatus phase="final" settled_at={undefined} />)
    expect(screen.getByText('定型 无数据')).toBeTruthy()
  })

  it('盘前/未知态不硬凑时间文案(缺什么不编什么)', () => {
    const { unmount } = render(<ThemeMoodStatus phase="pre" as_of="2026-10-08T09:00:00+08:00" settled_at={null} />)
    expect(screen.queryByText(/快照/)).toBeNull()
    expect(screen.queryByText(/定型/)).toBeNull()
    unmount()

    render(<ThemeMoodStatus phase="unknown" as_of={null} settled_at={null} />)
    expect(screen.queryByText(/快照/)).toBeNull()
    expect(screen.queryByText(/定型/)).toBeNull()
  })

  it('非交易日显式标注', () => {
    render(<ThemeMoodStatus phase="pre" trading_day={false} />)
    expect(screen.getByText('非交易日')).toBeTruthy()
  })

  it('parseBoardPhase 只认四个契约值; 缺字段/枚举外/类型不对 → unknown(不是默认值)', () => {
    for (const raw of [undefined, null, {}, { phase: null }, { phase: '' }, { phase: 'LIVE' }, { phase: 'closed' }, { phase: 3 as never }]) {
      expect(parseBoardPhase(raw as never)).toBe('unknown')
    }
    for (const p of ['pre', 'live', 'closed_pending', 'final'] as const) {
      expect(parseBoardPhase({ phase: p })).toBe(p)
    }
  })

  it('未知态不冒充实时/已定型', () => {
    render(<ThemeMoodStatus phase={parseBoardPhase({})} />)
    expect(screen.queryByText('实时')).toBeNull()
    expect(screen.queryByText('已定型')).toBeNull()
    expect(screen.getByText('未知状态')).toBeTruthy()
  })
})

// ── ② 轮询节奏 ────────────────────────────────────────────────────────────────

describe('/theme-mood 盘中实时轮询', () => {
  it('phase=live: 每 60s 再拉一次 board', async () => {
    mocks.fetchAPI.mockImplementation((url: string) => {
      const u = String(url)
      if (u.includes('/theme-mood/board')) return Promise.resolve(board({ phase: 'live', as_of: '2026-10-08T10:30:00+08:00', settled_at: null, trading_day: true, note: '盘中' }))
      if (u.includes('/theme-mood/ladder')) return Promise.resolve(LADDER)
      return Promise.resolve({ ok: false })
    })
    render(<ThemeMoodPage />)
    await flush()
    expect(boardCalls()).toBe(1)

    await tick(60_000)
    expect(boardCalls()).toBe(2)
    await tick(60_000)
    expect(boardCalls()).toBe(3)
  })

  it('phase=final: 收盘定型后停止轮询', async () => {
    mocks.fetchAPI.mockImplementation((url: string) => {
      const u = String(url)
      if (u.includes('/theme-mood/board')) return Promise.resolve(board({ phase: 'final', as_of: '2026-10-08T15:05:00+08:00', settled_at: '2026-10-08T15:05:00+08:00', trading_day: true }))
      if (u.includes('/theme-mood/ladder')) return Promise.resolve(LADDER)
      return Promise.resolve({ ok: false })
    })
    render(<ThemeMoodPage />)
    await flush()
    expect(boardCalls()).toBe(1)

    await tick(600_000)   // 10 分钟
    expect(boardCalls()).toBe(1)
  })

  it('未定型(盘前/未知)沿用 120s 兜底节奏', async () => {
    mocks.fetchAPI.mockImplementation((url: string) => {
      const u = String(url)
      if (u.includes('/theme-mood/board')) return Promise.resolve(board({ phase: 'pre', as_of: '2026-10-08T09:00:00+08:00', trading_day: true }))
      if (u.includes('/theme-mood/ladder')) return Promise.resolve(LADDER)
      return Promise.resolve({ ok: false })
    })
    render(<ThemeMoodPage />)
    await flush()

    await tick(60_000)
    expect(boardCalls()).toBe(1)      // live 才 60s; pre 不跟这个节奏
    await tick(60_000)
    expect(boardCalls()).toBe(2)      // 120s 到点
  })

  it('页面隐藏时暂停轮询; 回到前台立即补拉一次', async () => {
    mocks.fetchAPI.mockImplementation((url: string) => {
      const u = String(url)
      if (u.includes('/theme-mood/board')) return Promise.resolve(board({ phase: 'live', as_of: '2026-10-08T10:30:00+08:00' }))
      if (u.includes('/theme-mood/ladder')) return Promise.resolve(LADDER)
      return Promise.resolve({ ok: false })
    })
    render(<ThemeMoodPage />)
    await flush()
    expect(boardCalls()).toBe(1)

    hidden = true
    await tick(300_000)               // 后台 5 分钟: 一次都不拉
    expect(boardCalls()).toBe(1)

    hidden = false
    await act(async () => { document.dispatchEvent(new Event('visibilitychange')) })
    await flush()
    expect(boardCalls()).toBe(2)      // 回前台立刻补一次, 不等下一个周期
  })
})

// ── ③ 失败静默 + 旧数据保留 ───────────────────────────────────────────────────

describe('/theme-mood 轮询失败: 静默重试', () => {
  it('失败时只出现一条可见提示, 旧数据保留, 不抛异常风暴', async () => {
    mocks.fetchAPI.mockImplementation((url: string) => {
      const u = String(url)
      if (u.includes('/theme-mood/board')) return Promise.resolve(board({ phase: 'live', as_of: '2026-10-08T10:30:00+08:00' }))
      if (u.includes('/theme-mood/ladder')) return Promise.resolve(LADDER)
      return Promise.resolve({ ok: false })
    })
    render(<ThemeMoodPage />)
    await flush()
    expect(screen.getAllByText('甲题材').length).toBeGreaterThan(0)

    // 之后每次 board 都失败
    mocks.fetchAPI.mockImplementation((url: string) => {
      const u = String(url)
      if (u.includes('/theme-mood/board')) return Promise.reject(new Error('network down'))
      if (u.includes('/theme-mood/ladder')) return Promise.resolve(LADDER)
      return Promise.resolve({ ok: false })
    })
    await tick(60_000)
    await tick(60_000)

    // 一次可见提示(不是每个周期一条), 且失败带 title(悬停可看原因)
    const notes = screen.getAllByText('轮询失败, 自动重试中')
    expect(notes.length).toBe(1)
    expect(notes[0].getAttribute('title')).toContain('network down')
    // 旧数据没被清掉(缺数据不显示 0 / 不显示空)
    expect(screen.getAllByText('甲题材').length).toBeGreaterThan(0)
    // 失败后仍在重试(不是死了)
    expect(boardCalls()).toBeGreaterThanOrEqual(2)
  })
})

// ── ④ 竞态守卫 + as_of/基准日 显式标注 ────────────────────────────────────────

describe('/theme-mood 竞态守卫与时间标注', () => {
  it('被新请求取代的旧响应不落地(慢响应不覆盖新数据)', async () => {
    const first = deferred()
    const second = deferred()
    let n = 0
    mocks.fetchAPI.mockImplementation((url: string) => {
      const u = String(url)
      if (u.includes('/theme-mood/board')) return (++n === 1 ? first.promise : second.promise)
      if (u.includes('/theme-mood/ladder')) return Promise.resolve(LADDER)
      return Promise.resolve({ ok: false })
    })
    render(<ThemeMoodPage />)
    await flush()

    // 切窗口 → 发出第 2 个 board 请求(window=10)
    fireEvent.click(screen.getByText('10日'))
    await flush()
    expect(boardCalls()).toBe(2)

    // 新请求先回 → 落「乙题材」
    await act(async () => { second.resolve(board({ window: 10 }, '乙题材', 11.1)) })
    await flush()
    expect(screen.getAllByText('乙题材').length).toBeGreaterThan(0)

    // 旧的 window=20 响应随后才回 → 必须被丢弃
    await act(async () => { first.resolve(board({}, '甲题材', 71.7)) })
    await flush()
    expect(screen.queryByText('甲题材')).toBeNull()
    expect(screen.getAllByText('乙题材').length).toBeGreaterThan(0)
  })

  it('基准日与 as_of 显式可见; 缺数据显示「无数据」', async () => {
    mocks.fetchAPI.mockImplementation((url: string) => {
      const u = String(url)
      if (u.includes('/theme-mood/board')) return Promise.resolve(board({ phase: 'live', as_of: '2026-10-08T10:30:00+08:00' }))
      if (u.includes('/theme-mood/ladder')) return Promise.resolve(LADDER)
      return Promise.resolve({ ok: false })
    })
    const { unmount } = render(<ThemeMoodPage />)
    await flush()
    expect(screen.getByText('基准日 20261008')).toBeTruthy()
    expect(screen.getByText('快照 2026-10-08T10:30:00+08:00')).toBeTruthy()
    unmount()

    // 后端旧版本: 无 phase / 无 trade_date → 未知状态 + 基准日「无数据」, 不假装
    mocks.fetchAPI.mockImplementation((url: string) => {
      const u = String(url)
      if (u.includes('/theme-mood/board')) return Promise.resolve({ ...board(), trade_date: undefined })
      if (u.includes('/theme-mood/ladder')) return Promise.resolve(LADDER)
      return Promise.resolve({ ok: false })
    })
    render(<ThemeMoodPage />)
    await flush()
    expect(screen.getByText('未知状态')).toBeTruthy()
    expect(screen.getByText('基准日 无数据')).toBeTruthy()
  })

  it('手动刷新: 期间展示 loading 态并立即重拉 board', async () => {
    render(<ThemeMoodPage />)
    await flush()
    expect(boardCalls()).toBe(1)

    fireEvent.click(screen.getByText('刷新'))
    expect(screen.getByText(/刷新中/)).toBeTruthy()
    await flush()
    expect(screen.getByText('刷新')).toBeTruthy()
    expect(boardCalls()).toBe(2)
  })
})
