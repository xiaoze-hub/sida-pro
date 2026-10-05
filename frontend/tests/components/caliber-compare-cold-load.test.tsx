// @vitest-environment jsdom
//
// perf(2026-10-05) 回归钉: 口径对照页 `/caliber-compare` 首屏加载链 ——
//   先出壳后补数 / 对照与漂移并发 / 慢接口显式降级占位 / 竞态守卫 / 渲染缓存。
//
// 背景(B1 生产走查 2026-10-01): 冷态 settle 22.4s 主内容才稳定, 暖态 3-5s; 内容最终完整、无报错。
// 走查产物文件已被缓存清理删除 → 本测试全部结论来自**代码定位 + 本机 mock 延迟探针**(非生产实测,
// 不代表线上绝对值):
//   - 后端 `build_caliber_compare` 三源为**串行**网络取数且单响应返回(前端改不了其绝对耗时);
//   - 前端原本把「对照 + 漂移」两个请求与慢响应**串成一条链**: 漂移区块要等 `data` 回来才 mount
//     → 总耗时 ≈ 对照 + 漂移; 且对照在途时整片空白(只有按钮转圈);
//   - 对照 `load` 无序号: 手动连查/回车与初始请求重叠时, 旧响应(含错误)可能覆盖新数据;
//   - 三列/差异归因/漂移区块未 memo: 输入框逐字(无关状态)变更整棵重渲染。
//
// 本测试全 mock(禁真实网络), 钉住六条契约:
//   1. 先出壳: 对照在途时即挂外壳, 且外壳**不显示任何占位数字**(无 '--' / 0);
//   2. 并发: 漂移请求在对照**未返回时**就已发起(两个请求同时在途), 不再串行;
//   3. 慢接口显式降级: 对照在途超过窗口 → 显式「取数较慢…不会用 0 顶上」+ 重试入口;
//   4. 竞态守卫: 被更新一轮取代后, 旧响应(含错误)**不落地**;
//   5. 失败显式空态不假值: 对照失败 → 显式错误, 不渲染任何源列, 不填 0;
//   6. 渲染缓存: 无关状态变更(输入框逐字)不重渲染三列/归因/漂移重区块。
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import CaliberComparePage from '@/pages/CaliberCompare'

// ── 延迟可编排的 mock 队列(全 mock, 无真实网络) ────────────────────────────
const H = vi.hoisted(() => {
  type Deferred = { resolve: (v: unknown) => void; reject: (e: unknown) => void; promise: Promise<unknown> }
  const counters: Record<string, number> = {}
  const queues: Record<string, Deferred[]> = {}
  const args: Record<string, unknown[][]> = { compare: [], drift: [] }
  return {
    counters,
    queues,
    args,
    inc(k: string) {
      counters[k] = (counters[k] || 0) + 1
    },
    reset() {
      for (const k of Object.keys(counters)) delete counters[k]
      for (const k of Object.keys(queues)) delete queues[k]
      args.compare.length = 0
      args.drift.length = 0
    },
    defer(key: string) {
      let resolve!: (v: unknown) => void
      let reject!: (e: unknown) => void
      const promise = new Promise((res, rej) => {
        resolve = res
        reject = rej
      })
      ;(queues[key] ||= []).push({ resolve, reject, promise })
      return promise
    },
    pending(key: string) {
      return (queues[key] || []).length
    },
    resolveAt(key: string, i: number, v: unknown) {
      queues[key]?.[i]?.resolve(v)
    },
    rejectAt(key: string, i: number, e: unknown) {
      queues[key]?.[i]?.reject(e)
    },
  }
})

vi.mock('@panwatch/api', () => ({
  caliberCompareApi: {
    get: vi.fn((symbol: string) => {
      H.args.compare.push([symbol])
      return H.defer('compare')
    }),
  },
  caliberDriftApi: {
    get: vi.fn((symbol: string, days: number) => {
      H.args.drift.push([symbol, days])
      return H.defer('drift')
    }),
  },
}))

// 计数渲染次数: SourceGrid(memo) 内每列都渲染 CaliberBadge → 徽章重渲即三列重渲。
vi.mock('@panwatch/biz-ui/components/CaliberBadge', () => ({
  default: ({ caliber }: { caliber?: string }) => {
    H.inc('badge')
    return <span data-testid="caliber-badge" data-caliber={caliber} />
  },
  caliberOf: (x: unknown) => x ?? 'unknown',
}))

// WhyDifferent 与 DriftSection 各渲染一个 lucide `Info` → 覆盖它即可观测这两块是否重渲。
vi.mock('lucide-react', async (importOriginal) => {
  const actual = await importOriginal<typeof import('lucide-react')>()
  const Info = () => {
    H.inc('info')
    return <span data-testid="icon-info" />
  }
  return { ...actual, Info }
})

// ── 造数(全本地, 无网络) ───────────────────────────────────────────────────
const mkResp = (symbol: string, over: Record<string, unknown> = {}) => ({
  symbol,
  market: 'CN',
  as_of: '2026-09-18T16:30:00+08:00',
  available_count: 1,
  sources: [
    {
      key: 'thsdk_l2',
      name: '明盘 L2 主力净流入（TQ / 同花顺口径）',
      caliber: '按单笔成交金额分档汇总的特大单+大单净额。',
      unit: '元',
      available: true,
      // 口径字段缺失 → 必须渲染 '--', 绝不填 0
      fields: [{ label: '主力净流入', value: null }],
      note: '',
      caliber_type: 'ths',
    },
    {
      key: 'tencent_dark',
      name: '暗盘资金（腾讯逐笔 v6）',
      caliber: '腾讯逐笔主动成交，含拆单识别。',
      unit: '元',
      available: true,
      fields: [{ label: '主力净额（≥20万）', value: -12500000 }],
      note: '',
      caliber_type: 'tick',
      direction_semantics: '逐笔主动买卖',
      directional_allowed: true,
    },
    {
      key: 'eastmoney_flow',
      name: '东财四档资金流',
      caliber: '公开 Level-1 衍生，按单金额四档归类。',
      unit: '元',
      available: false,
      fields: [],
      note: '东财未返回资金流数据',
      caliber_type: 'eastmoney4',
    },
  ],
  differences: [{ topic: '「主力」定义不同', detail: 'TQ 按单笔金额分档、暗盘含拆单识别。' }],
  pair_diffs: [
    {
      a: 'thsdk_l2',
      b: 'eastmoney_flow',
      label_a: '明盘 L2',
      label_b: '东财四档',
      value_a: null,
      value_b: null,
      abs_diff: null,
      rel_diff: null,
      ratio: null,
      level: 'unknown',
      expected: false,
      sign_conflict: false,
      prefer: '',
      note: '一端缺数据, 不比较',
    },
  ],
  diff_conclusion: { level: 'unknown', hint: '数据不足(有的源未取到) —— 不比较, 也不用 0 顶上。' },
  direction_reconcile: null,
  ...over,
})

const DRIFT_EMPTY = {
  symbol: '002361',
  days: 30,
  field_by_source: { thsdk_l2: '主力净流入', tencent_dark: '主力净额（≥20万）', eastmoney_flow: '主力净流入' },
  series: [],
  comparisons: [],
  archived_days: 0,
  note: '',
}

beforeEach(() => {
  H.reset()
})

afterEach(() => {
  cleanup()
  vi.useRealTimers()
})

describe('口径对照页 · 首屏冷加载链(perf 回归)', () => {
  it('先出壳: 对照在途即挂外壳, 且外壳不显示任何占位数字', async () => {
    render(<CaliberComparePage />)
    // 对照请求已发出且仍在途
    await waitFor(() => expect(H.pending('compare')).toBe(1))

    // 外壳立即在, 静态头/口径词典也在(不被慢接口阻塞)
    expect(screen.getByTestId('caliber-compare-shell')).toBeTruthy()
    expect(screen.getAllByTestId('caliber-compare-shell-col').length).toBe(3)
    expect(screen.getByTestId('caliber-glossary')).toBeTruthy()
    expect(screen.getByText('口径对照')).toBeTruthy()
    // 外壳明说"不显示任何占位值", 且真的没有 '--' 或数字顶上(诚实口径)
    expect(screen.getByText(/不显示任何占位值/)).toBeTruthy()
    expect(screen.queryAllByText('--')).toHaveLength(0)
    expect(screen.queryByText('0')).toBeNull()
    // 数据未回 → 不同时挂三列/漂移表
    expect(screen.queryByText('明盘 L2 主力净流入（TQ / 同花顺口径）')).toBeNull()
  })

  it('并发: 漂移请求在对照返回前就已发起(两个请求同时在途)', async () => {
    render(<CaliberComparePage />)
    await waitFor(() => expect(H.pending('compare')).toBe(1))

    // 关键: 对照尚未 resolve, 漂移请求已发出(旧实现要等 data 回来才 mount → 串行)
    await waitFor(() => expect(H.pending('drift')).toBeGreaterThan(0))
    expect(H.args.drift[0]?.[0]).toBe('002361')
    // 两个请求此刻都在途
    expect(H.pending('compare')).toBeGreaterThan(0)
    expect(H.pending('drift')).toBeGreaterThan(0)
  })

  it('慢接口显式降级: 对照在途超窗口 → 显式「较慢」提示 + 重试, 不填占位值', async () => {
    vi.useFakeTimers()
    try {
      render(<CaliberComparePage />)
      // 对照在途(不 resolve)
      await act(async () => {
        await Promise.resolve()
      })
      expect(H.pending('compare')).toBe(1)
      // 还没到窗口: 无「较慢」提示
      expect(screen.queryByTestId('caliber-compare-slow')).toBeNull()

      act(() => {
        vi.advanceTimersByTime(8001)
      })

      const slow = screen.getByTestId('caliber-compare-slow')
      expect(slow.textContent).toContain('取数较慢')
      expect(slow.textContent).toContain('不会用 0 或估算值顶上')
      // 降级态仍不显示占位数字
      expect(screen.queryAllByText('--')).toHaveLength(0)
      // 有重试入口
      expect(screen.getByText('重试')).toBeTruthy()
    } finally {
      vi.useRealTimers()
    }
  })

  it('竞态守卫: 被更新一轮取代后, 旧的对照响应不落地', async () => {
    render(<CaliberComparePage />)
    await waitFor(() => expect(H.pending('compare')).toBe(1)) // #0 = 002361

    // 新一轮: 输入 600519 + 回车(在途时按钮 disabled, 回车不拦 → 两次请求重叠)
    fireEvent.change(screen.getByLabelText('股票代码'), { target: { value: '600519' } })
    fireEvent.keyDown(screen.getByLabelText('股票代码'), { key: 'Enter' })
    await waitFor(() => expect(H.pending('compare')).toBe(2)) // #1 = 600519

    // 后发的(600519)先回
    await act(async () => {
      H.resolveAt('compare', 1, mkResp('600519'))
    })
    await screen.findByText('600519（CN）')

    // 旧的(002361)晚回 → 必须被丢弃, 不得覆盖
    await act(async () => {
      H.resolveAt('compare', 0, mkResp('002361'))
    })
    expect(screen.queryByText('002361（CN）')).toBeNull()
    expect(screen.getByText('600519（CN）')).toBeTruthy()
  })

  it('竞态守卫: 旧响应带错误晚回, 不得把错误横幅落到当前结果上', async () => {
    render(<CaliberComparePage />)
    await waitFor(() => expect(H.pending('compare')).toBe(1))

    fireEvent.change(screen.getByLabelText('股票代码'), { target: { value: '600519' } })
    fireEvent.keyDown(screen.getByLabelText('股票代码'), { key: 'Enter' })
    await waitFor(() => expect(H.pending('compare')).toBe(2))

    // 新一轮成功
    await act(async () => {
      H.resolveAt('compare', 1, mkResp('600519'))
    })
    await screen.findByText('600519（CN）')

    // 旧一轮失败晚回 → 丢弃
    await act(async () => {
      H.rejectAt('compare', 0, new Error('取数失败：网关超时'))
    })
    expect(screen.queryByText(/网关超时/)).toBeNull()
    expect(screen.getByText('600519（CN）')).toBeTruthy()
  })

  it('失败显式空态不假值: 对照失败 → 显式错误, 不渲染源列, 不填 0', async () => {
    render(<CaliberComparePage />)
    await waitFor(() => expect(H.pending('compare')).toBe(1))

    await act(async () => {
      H.rejectAt('compare', 0, new Error('取数失败：三源均不可用'))
    })

    expect(await screen.findByText(/取数失败：三源均不可用/)).toBeTruthy()
    // 不渲染任何源列 / 差异块 / 漂移区(整体失败态)
    expect(screen.queryByText('明盘 L2 主力净流入（TQ / 同花顺口径）')).toBeNull()
    expect(screen.queryByText('为什么三个数字不一样')).toBeNull()
    expect(screen.queryByText('口径漂移（逐日留痕）')).toBeNull()
    expect(screen.queryAllByText('--')).toHaveLength(0)
  })

  it('口径字段缺失不填 0/默认方向: null → --, 缺 directional_allowed → 禁用于方向判定', async () => {
    render(<CaliberComparePage />)
    await waitFor(() => expect(H.pending('compare')).toBe(1))
    await act(async () => {
      H.resolveAt('compare', 0, mkResp('002361'))
    })
    await screen.findByText('明盘 L2 主力净流入（TQ / 同花顺口径）')

    // 字段值为 null → '--'(字段行 + 归因表 null 列), 不是 0 / +0.00
    expect(screen.getAllByText('--').length).toBeGreaterThan(0)
    expect(screen.queryByText('+0.00')).toBeNull()
    expect(screen.queryByText(/^0(\.00)?$/)).toBeNull()
    expect(screen.queryByText('+0.00万')).toBeNull()
    // 缺失 directional_allowed 的两源 → 一律「禁用于主力意图判定」(不默认方向可用)
    expect(screen.getAllByText('仅资金面参考 · 禁用于主力意图判定').length).toBe(2)
    // 仅显式 directional_allowed 的逐笔源标「可用于方向判定」
    expect(screen.getAllByText('可用于方向判定').length).toBe(1)
    // 无数据源显式写原因, 不画空表
    expect(screen.getByText(/无数据 —— 东财未返回资金流数据/)).toBeTruthy()
  })

  it('渲染缓存: 无关状态变更(输入框逐字)不重渲染三列/归因/漂移重区块', async () => {
    render(<CaliberComparePage />)
    await waitFor(() => expect(H.pending('compare')).toBe(1))
    await act(async () => {
      H.resolveAt('compare', 0, mkResp('002361'))
      H.resolveAt('drift', 0, DRIFT_EMPTY)
    })
    await screen.findByText('明盘 L2 主力净流入（TQ / 同花顺口径）')
    await screen.findByText('口径漂移（逐日留痕）')

    const badge = H.counters.badge || 0
    const info = H.counters.info || 0
    expect(badge).toBeGreaterThan(0)
    expect(info).toBeGreaterThan(0)

    // 输入框逐字变更 = 无关状态变更(既不改 data 也不改 querySymbol)
    fireEvent.change(screen.getByLabelText('股票代码'), { target: { value: '60051' } })
    await act(async () => {
      await Promise.resolve()
    })

    // memo 命中 → 三列(徽章)与两块(Info)均未重渲
    expect(H.counters.badge || 0).toBe(badge)
    expect(H.counters.info || 0).toBe(info)
  })
})
