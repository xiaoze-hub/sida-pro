// @vitest-environment jsdom
/**
 * 请求体契约：`chart_snapshot` 只随**这一轮**走（2026-11-17）。
 *
 * 钉住四件事，全程走**真** `chatApi.sendMessageStream`（只 stub `globalThis.fetch`）：
 *  ① 传了快照 → 请求体里有 `chart_snapshot`，且**只有**协议那 7 个字段
 *     （owner / capturedAt / 时间戳一概不出客户端）；
 *  ② 不传 / 传 null → 请求体里**没有**这个字段（老调用方零影响，不给后端塞 `undefined`）；
 *  ③ 摘要不被拼进 `content`（正文是用户的话，历史/落库文本里不该混进指纹）；
 *  ④ 每次调用独立成块 —— 连发两轮、第二轮换摘要，第一轮的块不会被"记住"。
 */
import { afterEach, describe, expect, it, vi } from 'vitest'

import { chatApi, type ChartSnapshotPayload } from '@sida/api'

const DIGEST_A = 'a'.repeat(64)
const DIGEST_B = 'b'.repeat(64)

function sse(event: string, data: unknown): string {
  return `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`
}

function okStream(): Response {
  const enc = new TextEncoder()
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(
        enc.encode(
          sse('delta', { content: '好。' }) +
            sse('done', { id: 1, role: 'assistant', content: '好。', created_at: '' }),
        ),
      )
      controller.close()
    },
  })
  return { ok: true, status: 200, body } as unknown as Response
}

/** 记录每一次请求（url + 解析后的 body）。 */
function captureFetch() {
  const calls: Array<{ url: string; body: any }> = []
  globalThis.fetch = vi.fn(async (url: any, init: any) => {
    calls.push({ url: String(url), body: JSON.parse(init.body) })
    return okStream()
  }) as unknown as typeof fetch
  return calls
}

function snapshot(overrides: Partial<ChartSnapshotPayload> = {}): ChartSnapshotPayload {
  return {
    schema: 'sida-chan-snapshot-v2',
    symbol: '600111',
    market: 'CN',
    instrument: 'stock',
    interval: '1d',
    days: 250,
    digest: DIGEST_A,
    ...overrides,
  }
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('chart_snapshot 请求体', () => {
  it('传了快照 → 请求体带上 7 个协议字段（不含 owner/时间戳）', async () => {
    const calls = captureFetch()
    await chatApi.sendMessageStream(
      21,
      '这个区间的笔怎么画',
      {},
      undefined,
      undefined,
      { symbol: '600111', market: 'CN' },
      [],
      snapshot(),
    )
    expect(calls).toHaveLength(1)
    expect(calls[0].url).toContain('/chat/conversations/21/messages/stream')
    expect(calls[0].body.chart_snapshot).toEqual(snapshot())
    expect(Object.keys(calls[0].body.chart_snapshot).sort()).toEqual(
      ['days', 'digest', 'instrument', 'interval', 'market', 'schema', 'symbol'].sort(),
    )
  })

  it('不传快照（旧调用）→ 请求体里没有这个字段', async () => {
    const calls = captureFetch()
    await chatApi.sendMessageStream(21, '你好', {})
    expect(calls[0].body).not.toHaveProperty('chart_snapshot')
    expect(JSON.stringify(calls[0].body)).not.toContain('digest')
  })

  it('显式传 null → 同样不带（不给后端塞 null）', async () => {
    const calls = captureFetch()
    await chatApi.sendMessageStream(21, '你好', {}, undefined, undefined, undefined, undefined, null)
    expect(calls[0].body).not.toHaveProperty('chart_snapshot')
  })

  it('摘要不进正文：content 仍是用户那句话', async () => {
    const calls = captureFetch()
    await chatApi.sendMessageStream(
      21,
      '看一下',
      {},
      undefined,
      undefined,
      undefined,
      undefined,
      snapshot(),
    )
    expect(calls[0].body.content).toBe('看一下')
    expect(calls[0].body.content).not.toContain(DIGEST_A)
  })

  it('两轮各带各的：第二轮换摘要，第一轮的块不会被沿用', async () => {
    const calls = captureFetch()
    await chatApi.sendMessageStream(21, '第一轮', {}, undefined, undefined, undefined, undefined, snapshot())
    await chatApi.sendMessageStream(
      22,
      '第二轮',
      {},
      undefined,
      undefined,
      undefined,
      undefined,
      snapshot({ symbol: '600519', digest: DIGEST_B }),
    )
    expect(calls[0].body.chart_snapshot.digest).toBe(DIGEST_A)
    expect(calls[0].body.chart_snapshot.symbol).toBe('600111')
    expect(calls[1].body.chart_snapshot.digest).toBe(DIGEST_B)
    expect(calls[1].body.chart_snapshot.symbol).toBe('600519')
  })
})
