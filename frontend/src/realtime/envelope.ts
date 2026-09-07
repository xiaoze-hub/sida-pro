/**
 * 实时信封客户端 (2026-09-07 P3, 对应后端 src/web/realtime/envelope.py)。
 *
 * 服务端帧: {seq, ts, topic, user_id, payload}
 *  - topic: quote.tick / quote.snapshot / notif.push
 *  - 断线重连: 在 url 上带 last_seq=上次最大 seq, 服务端补发 ring 内 missed 帧
 *
 * 向后兼容: parseFrame 同时接受新 envelope 与旧裸帧 (无 seq 字段时 seq=0,
 * 调用方按 payload.type 走老分支)。等全量切流后可删兼容分支。
 */

export interface EnvelopeFrame {
  seq: number
  ts: number
  topic: string
  user_id: string
  payload: Record<string, unknown>
}

export function isEnvelope(raw: unknown): raw is EnvelopeFrame {
  if (typeof raw !== 'object' || raw === null) return false
  const r = raw as Record<string, unknown>
  return typeof r.seq === 'number' && typeof r.topic === 'string' && typeof r.payload === 'object'
}

/** 解析一帧: envelope 原样返回; 旧裸帧包成 seq=0 的壳, 上层无感。 */
export function parseFrame(raw: unknown): EnvelopeFrame {
  if (isEnvelope(raw)) return raw
  return {
    seq: 0,
    ts: Date.now() / 1000,
    topic: 'legacy',
    user_id: '*',
    payload: (typeof raw === 'object' && raw !== null ? raw : { data: raw }) as Record<string, unknown>,
  }
}

/** 断线重连 url: 拼 last_seq(>0 才拼, 0 表示全新订阅不补发)。 */
export function reconnectUrl(base: string, lastSeq: number): string {
  if (!lastSeq || lastSeq <= 0) return base
  const sep = base.includes('?') ? '&' : '?'
  return `${base}${sep}last_seq=${lastSeq}`
}

/** 从已收帧里取最大 seq(重连前调用)。 */
export function maxSeq(frames: Pick<EnvelopeFrame, 'seq'>[]): number {
  let m = 0
  for (const f of frames) if (typeof f.seq === 'number' && f.seq > m) m = f.seq
  return m
}
