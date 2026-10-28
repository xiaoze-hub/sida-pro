// @vitest-environment jsdom
/**
 * 流式终稿与异常帧（P0, 2026-10-28）契约钉。
 *
 * 依据 `src/web/api/chat.py::send_message_stream` 的真实契约（不是推测）：
 *   - `stage` / `delta` / `done` / `error` 四种事件，空行分隔；
 *   - `done.content` 是**落库全文**，与 delta 拼接不等价 —— 服务端在首轮
 *     「问具体标的却零工具作答」时用零工具拒答替换正文（此时一个 delta 都没发）、
 *     数字对账附言只加在终稿里、流式失败时也以 done 收尾；
 *   - 断流（拿到 HTTP 200 但流提前结束）不会有任何收尾事件。
 *
 * 因此本文件钉住：终稿优先、拒答可见、坏帧计数可见、无终稿不伪称成功、并发不串。
 * 全程走**真** `chatApi.sendMessageStream`（只 stub `globalThis.fetch`），不另写解析器。
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { chatApi, createChatStreamSession, type ChatStreamResult } from '@sida/api'

function sse(event: string, data: unknown): string {
  return `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`
}

/** 把若干文本块按顺序灌进一个真 ReadableStream（模拟网络分片，含被切断的帧）。 */
function responseFrom(chunks: string[]): Response {
  const enc = new TextEncoder()
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(enc.encode(chunk))
      controller.close()
    },
  })
  return { ok: true, status: 200, body } as unknown as Response
}

function stubFetch(...payloads: string[][]) {
  let call = 0
  const fn = vi.fn(async () => {
    const chunks = payloads[Math.min(call, payloads.length - 1)]
    call += 1
    return responseFrom(chunks)
  })
  globalThis.fetch = fn as unknown as typeof fetch
  return fn
}

function collect() {
  const seen = {
    stages: [] as string[],
    deltas: [] as string[],
    done: [] as string[],
    errors: [] as string[],
    malformed: [] as { reason: string; raw: string }[],
  }
  const handlers = {
    onStage: (s: string) => seen.stages.push(s),
    onDelta: (d: string) => seen.deltas.push(d),
    onDone: (m: { content: string }) => seen.done.push(m.content),
    onError: (e: string) => seen.errors.push(e),
    onMalformed: (info: { reason: string; raw: string }) => seen.malformed.push(info),
  }
  return { seen, handlers }
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('分片与逐帧', () => {
  it('帧被任意切断（含多字节中文中间）也能完整还原', async () => {
    const full =
      sse('stage', { message: '正在取数 1/2：get_stock_quote' }) +
      sse('delta', { content: '主力净流出 ' }) +
      sse('delta', { content: '552 万。' }) +
      sse('done', { id: 7, role: 'assistant', content: '主力净流出 552 万。', created_at: '' })
    const enc = new TextEncoder()
    const bytes = enc.encode(full)
    // 按 7 字节切：必然切断中文与 JSON 结构
    const chunks: string[] = []
    const dec = new TextDecoder('utf-8')
    for (let i = 0; i < bytes.length; i += 7) {
      chunks.push(dec.decode(bytes.slice(i, i + 7), { stream: true }))
    }
    chunks.push(dec.decode())
    stubFetch(chunks)

    const { seen, handlers } = collect()
    const result = (await chatApi.sendMessageStream(21, '600111 怎么样', handlers)) as ChatStreamResult

    expect(seen.stages).toEqual(['正在取数 1/2：get_stock_quote'])
    expect(seen.deltas.join('')).toBe('主力净流出 552 万。')
    expect(seen.done).toEqual(['主力净流出 552 万。'])
    expect(result.authoritative).toBe(true)
    expect(result.corruptedFrames).toBe(0)
    expect(result.failure).toBeUndefined()
    expect(result.content).toBe('主力净流出 552 万。')
  })

  it('CRLF 分隔（代理改写换行）不被当成一整条坏帧', async () => {
    stubFetch([
      'event: delta\r\ndata: {"content":"A"}\r\n\r\n' + 'event: done\r\ndata: {"id":1,"role":"assistant","content":"A","created_at":""}\r\n\r\n',
    ])
    const { seen, handlers } = collect()
    const result = (await chatApi.sendMessageStream(1, 'q', handlers)) as ChatStreamResult
    expect(seen.deltas).toEqual(['A'])
    expect(result.authoritative).toBe(true)
    expect(result.corruptedFrames).toBe(0)
  })

  it('收尾没有空行的最后一帧仍要派发（别把终稿丢在 buffer 里）', async () => {
    stubFetch(['event: delta\ndata: {"content":"尾"}\n\nevent: done\ndata: {"id":2,"role":"assistant","content":"尾","created_at":""}'])
    const { seen, handlers } = collect()
    const result = (await chatApi.sendMessageStream(1, 'q', handlers)) as ChatStreamResult
    expect(seen.deltas).toEqual(['尾'])
    expect(result.authoritative).toBe(true)
  })
})

describe('终稿（done）与 delta 不同', () => {
  it('done 比拼接多出对账附言：正文以终稿为准，附言不能丢', async () => {
    stubFetch([
      sse('delta', { content: '主力净流出 552 万。' }) +
        sse('done', {
          id: 9,
          role: 'assistant',
          content: '主力净流出 552 万。\n\n（数字对账：2 个数字未在工具原文中找到，已标注）',
          created_at: '',
        }),
    ])
    const { seen, handlers } = collect()
    const result = (await chatApi.sendMessageStream(1, 'q', handlers)) as ChatStreamResult
    expect(seen.done[0]).toContain('数字对账')
    expect(result.content).toContain('数字对账')
    expect(result.authoritative).toBe(true)
  })

  it('零工具拒答：一个 delta 都没有，只有终稿', async () => {
    stubFetch([sse('done', { id: 10, role: 'assistant', content: '这次没取到可核验的数据，不做结论。', created_at: '' })])
    const { seen, handlers } = collect()
    const result = (await chatApi.sendMessageStream(1, 'q', handlers)) as ChatStreamResult
    expect(seen.deltas).toEqual([])
    expect(result.content).toBe('这次没取到可核验的数据，不做结论。')
    expect(result.authoritative).toBe(true)
  })

  it('终稿为空串但已流出正文：不拿空终稿覆盖，且标记非权威', async () => {
    stubFetch([
      sse('delta', { content: '已流出的正文' }) +
        sse('done', { id: 11, role: 'assistant', content: '', created_at: '' }),
    ])
    const { handlers } = collect()
    const result = (await chatApi.sendMessageStream(1, 'q', handlers)) as ChatStreamResult
    expect(result.content).toBe('已流出的正文')
    expect(result.authoritative).toBe(false)
    expect(result.failure).toBeTruthy()
  })
})

describe('坏帧不静默 / 失败不伪装成功', () => {
  it('JSON 截断的坏帧被计数并上报，不是静默丢弃', async () => {
    stubFetch([
      'event: delta\ndata: {"content": "半截\n\n' +
        sse('delta', { content: '好的' }) +
        sse('done', { id: 12, role: 'assistant', content: '好的', created_at: '' }),
    ])
    const { seen, handlers } = collect()
    const result = (await chatApi.sendMessageStream(1, 'q', handlers)) as ChatStreamResult
    expect(seen.malformed).toHaveLength(1)
    expect(seen.malformed[0].reason).toBe('invalid-json')
    expect(result.corruptedFrames).toBe(1)
    // 有终稿 → 可以安全继续，但仍要让调用方知道有帧坏了
    expect(result.authoritative).toBe(true)
    expect(result.failure).toBeUndefined()
  })

  it('delta 载荷缺 content 也按坏帧处理（不能 `|| ""` 吞掉）', async () => {
    stubFetch([
      sse('delta', { text: '字段名不对' }) + sse('done', { id: 13, role: 'assistant', content: 'x', created_at: '' }),
    ])
    const { seen, handlers } = collect()
    const result = (await chatApi.sendMessageStream(1, 'q', handlers)) as ChatStreamResult
    expect(result.corruptedFrames).toBe(1)
    expect(seen.malformed[0].reason).toBe('delta-shape')
  })

  it('流正常结束但没有 done：必须给出 failure，不能当成功', async () => {
    stubFetch([sse('delta', { content: '只有一半' })])
    const { handlers } = collect()
    const result = (await chatApi.sendMessageStream(1, 'q', handlers)) as ChatStreamResult
    expect(result.authoritative).toBe(false)
    expect(result.content).toBe('只有一半')
    expect(result.failure).toContain('终稿')
  })

  it('坏帧且没有 done：failure 要说明数据损坏，而不是只报"没终稿"', async () => {
    stubFetch([sse('delta', { content: 'abc' }) + 'event: delta\ndata: {oops\n\n'])
    const { handlers } = collect()
    const result = (await chatApi.sendMessageStream(1, 'q', handlers)) as ChatStreamResult
    expect(result.corruptedFrames).toBe(1)
    expect(result.authoritative).toBe(false)
    expect(result.failure).toContain('损坏')
  })

  it('服务端 error 帧：原文透出，且不算成功', async () => {
    stubFetch([sse('error', { message: '对话不存在' })])
    const { seen, handlers } = collect()
    const result = (await chatApi.sendMessageStream(1, 'q', handlers)) as ChatStreamResult
    expect(seen.errors).toEqual(['对话不存在'])
    expect(result.errors).toEqual(['对话不存在'])
    expect(result.failure).toBe('对话不存在')
    expect(result.authoritative).toBe(false)
  })
})

describe('并发不串消息', () => {
  it('两个流并行，各自的 delta/终稿互不串', async () => {
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/7/')) {
        return responseFrom([sse('delta', { content: '会话7' }) + sse('done', { id: 71, role: 'assistant', content: '会话7终稿', created_at: '' })])
      }
      return responseFrom([sse('delta', { content: '会话8' }) + sse('done', { id: 81, role: 'assistant', content: '会话8终稿', created_at: '' })])
    }) as unknown as typeof fetch

    const a = collect()
    const b = collect()
    const [ra, rb] = (await Promise.all([
      chatApi.sendMessageStream(7, 'q7', a.handlers),
      chatApi.sendMessageStream(8, 'q8', b.handlers),
    ])) as ChatStreamResult[]

    expect(a.seen.deltas).toEqual(['会话7'])
    expect(a.seen.done).toEqual(['会话7终稿'])
    expect(ra.content).toBe('会话7终稿')
    expect(b.seen.deltas).toEqual(['会话8'])
    expect(b.seen.done).toEqual(['会话8终稿'])
    expect(rb.content).toBe('会话8终稿')
  })

  it('session 是每次新建的：两个 session 的 buffer 不共享', () => {
    const s1 = createChatStreamSession()
    const s2 = createChatStreamSession()
    expect(s1.feed('event: delta\ndata: {"content":"甲"}\n')).toEqual([])
    expect(s2.feed('event: delta\ndata: {"content":"乙"}\n\n').map((e) => e.type)).toEqual(['delta'])
    expect(s1.feed('\n').map((e) => e.type)).toEqual(['delta'])
    expect(s1.text).toBe('甲')
    expect(s2.text).toBe('乙')
  })
})
