/**
 * 「本轮同图快照」纯逻辑契约（2026-11-17）。
 *
 * 这一层决定的是**哪一份缠论快照配跟着这一轮提问进请求体**，所以它守的不是渲染，而是：
 *   ① 形状可证：摘要必须 64 位小写 hex、天数为真实正整数、周期只认 `1d`、
 *      类型只认 `stock|index`（不按代码猜）—— 任一不成立就整条不要，不做默认值补齐；
 *   ② 账号隔离：仓库按 `账号|市场|代码|类型` 分键，读侧再比一次 owner；
 *   ③ 不猜标的：`000001` 既是上证指数又是平安银行 —— 同号多张一起命中的时候返回歧义，不选一个；
 *   ④ 会过期：超过 `CHAN_SNAPSHOT_TTL_MS` 没有新的图表广播 = 不再是"现在这张图"，整轮不带；
 *   ⑤ 用户清除后不复活：清除按「账号|市场|代码」记账，之后工作台再广播同一只票也被拒绝；
 *   ⑥ 发出去的那块**只有协议里的 7 个字段**：owner / capturedAt 永不出现在请求体里。
 */
import { beforeEach, describe, expect, it } from 'vitest'

import {
  CHAN_SNAPSHOT_SCHEMA,
  CHAN_SNAPSHOT_TTL_MS,
  buildChanSnapshotRecord,
  chanSnapshotBlock,
  chanSnapshotLabel,
  chanSnapshotStoreSize,
  clearChanSnapshotStore,
  dismissChanSnapshot,
  isChanSnapshotDismissed,
  publishChanSnapshot,
  readChanSnapshotDetailed,
  resolveChanSnapshotForTurn,
  subscribeChanSnapshot,
  unpublishChanSnapshot,
} from '../../src/lib/copilot/chart-context'

const OWNER = 'test-user'
const OTHER = 'another-user'
const DIGEST_A = 'a'.repeat(64)
const DIGEST_B = 'b'.repeat(64)

function input(overrides: Record<string, unknown> = {}) {
  return {
    owner: OWNER,
    symbol: '600111',
    market: 'CN',
    instrument: 'stock',
    interval: '1d',
    days: 250,
    digest: DIGEST_A,
    ...overrides,
  }
}

beforeEach(() => {
  clearChanSnapshotStore()
})

describe('形状可证才收（不补默认值）', () => {
  it('字段齐全 → 收下，并且只有协议字段', () => {
    const rec = buildChanSnapshotRecord(input())
    expect(rec).toEqual({
      owner: OWNER,
      symbol: '600111',
      market: 'CN',
      instrument: 'stock',
      interval: '1d',
      days: 250,
      digest: DIGEST_A,
      capturedAt: expect.any(Number),
    })
    expect(chanSnapshotBlock(rec!)).toEqual({
      schema: CHAN_SNAPSHOT_SCHEMA,
      symbol: '600111',
      market: 'CN',
      instrument: 'stock',
      interval: '1d',
      days: 250,
      digest: DIGEST_A,
    })
    // owner 与 capturedAt 是仓库内部字段，绝不进请求体
    expect(Object.keys(chanSnapshotBlock(rec!)).sort()).toEqual(
      ['days', 'digest', 'instrument', 'interval', 'market', 'schema', 'symbol'].sort(),
    )
  })

  it.each([
    ['摘要是大写 hex', { digest: 'A'.repeat(64) }],
    ['摘要只有 63 位', { digest: 'a'.repeat(63) }],
    ['摘要含非 hex 字符', { digest: 'z'.repeat(64) }],
    ['摘要为空', { digest: '' }],
    ['天数为 0', { days: 0 }],
    ['天数为小数', { days: 120.5 }],
    ['天数是字符串', { days: '250' }],
    ['天数缺失', { days: undefined }],
    ['周期不是日线', { interval: '1w' }],
    ['周期缺失（不猜日线）', { interval: undefined }],
    ['类型是板块', { instrument: 'board' }],
    ['类型缺失（不按代码猜）', { instrument: undefined }],
    ['市场不合法', { market: 'XX' }],
    ['代码不合法', { symbol: '6001 11' }],
    ['账号为空', { owner: '   ' }],
  ])('%s → 整条拒收', (_label, override) => {
    expect(buildChanSnapshotRecord(input(override))).toBeNull()
    expect(publishChanSnapshot(input(override))).toBeNull()
    expect(chanSnapshotStoreSize()).toBe(0)
  })
})

describe('读侧：账号隔离 / 歧义 / 过期', () => {
  it('同一账号同一标的 → ok，块与记录一致', () => {
    publishChanSnapshot(input())
    const read = readChanSnapshotDetailed('600111', 'CN', OWNER)
    expect(read.reason).toBe('ok')
    expect(read.expired).toBe(false)
    expect(read.block?.digest).toBe(DIGEST_A)
    expect(read.record?.symbol).toBe('600111')
  })

  it('别的账号读不到（不串号）', () => {
    publishChanSnapshot(input())
    const read = readChanSnapshotDetailed('600111', 'CN', OTHER)
    expect(read.block).toBeNull()
    expect(read.reason).toBe('missing')
  })

  it('000001 个股 + 指数两张都发布 → 歧义，不猜是哪一张', () => {
    publishChanSnapshot(input({ symbol: '000001', digest: DIGEST_A }))
    publishChanSnapshot(input({ symbol: '000001', instrument: 'index', digest: DIGEST_B }))
    const read = readChanSnapshotDetailed('000001', 'CN', OWNER)
    expect(read.block).toBeNull()
    expect(read.reason).toBe('ambiguous')
    // 带类型也不是本模块的入口：读取只按代码+市场，歧义就得说歧义
    expect(chanSnapshotStoreSize()).toBe(2)
  })

  it('超过 TTL 没有新广播 → 过期：记录可显示，但这一轮不带', () => {
    const now = Date.now()
    publishChanSnapshot(input({ now: now - CHAN_SNAPSHOT_TTL_MS - 1 }))
    const read = readChanSnapshotDetailed('600111', 'CN', OWNER, now)
    expect(read.block).toBeNull()
    expect(read.reason).toBe('expired')
    expect(read.expired).toBe(true)
    expect(read.record?.digest).toBe(DIGEST_A)
  })

  it('未来时间戳按不可信处理（时钟异常不得让快照永不过期）', () => {
    const now = Date.now()
    publishChanSnapshot(input({ now: now + 60_000 }))
    expect(readChanSnapshotDetailed('600111', 'CN', OWNER, now).block).toBeNull()
  })

  it('撤下（换标的/卸载/页面隐藏）后读不到', () => {
    const key = publishChanSnapshot(input())
    expect(readChanSnapshotDetailed('600111', 'CN', OWNER).block).not.toBeNull()
    unpublishChanSnapshot(key)
    expect(readChanSnapshotDetailed('600111', 'CN', OWNER).reason).toBe('missing')
  })
})

describe('用户清除', () => {
  it('清除后读不到，并且工作台再次广播同一只票被拒绝（不复活）', () => {
    publishChanSnapshot(input())
    const rec = readChanSnapshotDetailed('600111', 'CN', OWNER).record!
    dismissChanSnapshot(rec)
    expect(isChanSnapshotDismissed(rec)).toBe(true)
    expect(readChanSnapshotDetailed('600111', 'CN', OWNER).reason).toBe('dismissed')
    // 同一张图、甚至换了一份新摘要，都不许复活
    expect(publishChanSnapshot(input({ digest: DIGEST_B }))).toBeNull()
    expect(chanSnapshotStoreSize()).toBe(0)
    // 别的标的不受影响
    expect(publishChanSnapshot(input({ symbol: '600519' }))).not.toBeNull()
  })
})

describe('发送前复核（resolveChanSnapshotForTurn）', () => {
  it('账号/标的都对 → 给出可发送的块', () => {
    publishChanSnapshot(input())
    const read = resolveChanSnapshotForTurn({ symbol: '600111', market: 'CN', owner: OWNER })
    expect(read.reason).toBe('ok')
    expect(read.block?.schema).toBe(CHAN_SNAPSHOT_SCHEMA)
  })

  it('本轮标的与仓库里的图不同 → 不带（绝不把 A 的图塞给 B）', () => {
    publishChanSnapshot(input())
    const read = resolveChanSnapshotForTurn({ symbol: '600519', market: 'CN', owner: OWNER })
    expect(read.block).toBeNull()
    expect(read.reason).toBe('missing')
  })

  it('本轮声明的天数与图上的不一致 → 不带（days 也要复核）', () => {
    publishChanSnapshot(input({ days: 250 }))
    const read = resolveChanSnapshotForTurn({ symbol: '600111', market: 'CN', owner: OWNER, days: 120 })
    expect(read.block).toBeNull()
    expect(read.reason).toBe('days')
    // 天数一致才发
    expect(resolveChanSnapshotForTurn({ symbol: '600111', market: 'CN', owner: OWNER, days: 250 }).block).not.toBeNull()
  })

  it('拿不到账号身份 → 一律不带', () => {
    publishChanSnapshot(input())
    const read = resolveChanSnapshotForTurn({ symbol: '600111', market: 'CN', owner: '' })
    expect(read.block).toBeNull()
    expect(read.reason).toBe('missing')
  })

  it('本轮标的为空（用户已清掉锚点）→ 不带', () => {
    publishChanSnapshot(input())
    expect(resolveChanSnapshotForTurn({ symbol: '', owner: OWNER }).block).toBeNull()
  })
})

describe('界面文案与订阅', () => {
  it('展示项覆盖可读内容，且不展示完整摘要', () => {
    publishChanSnapshot(input())
    const rec = readChanSnapshotDetailed('600111', 'CN', OWNER).record!
    const text = chanSnapshotLabel(rec)
    expect(text).toContain('600111')
    expect(text).toContain('个股')
    expect(text).toContain('CN')
    expect(text).toContain('日线')
    expect(text).toContain('近 250 天')
    expect(text).toContain('摘要 aaaaaaaa…')
    expect(text).not.toContain(DIGEST_A) // 完整摘要不上界面
  })

  it('发布/撤下/清除都会通知订阅者（参谋页据此刷新"这一轮带不带"）', () => {
    const seen: number[] = []
    const off = subscribeChanSnapshot(() => seen.push(1))
    const key = publishChanSnapshot(input())
    expect(seen).toHaveLength(1)
    // 同一张图、同一份摘要重复广播不重复通知
    publishChanSnapshot(input())
    expect(seen).toHaveLength(1)
    // 摘要变了 = 图变了
    publishChanSnapshot(input({ digest: DIGEST_B }))
    expect(seen).toHaveLength(2)
    unpublishChanSnapshot(key)
    expect(seen).toHaveLength(3)
    off()
    publishChanSnapshot(input({ symbol: '600519' }))
    expect(seen).toHaveLength(3)
  })
})
