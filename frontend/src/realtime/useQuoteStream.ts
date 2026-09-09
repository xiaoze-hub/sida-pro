/**
 * 行情实时流消费(KI-025, 2026-09-09)。
 *
 * 后端 `/api/quotes/ws` 下行信封帧 `{seq, ts, topic, user_id, payload}`:
 *  - topic=quote.tick / quote.snapshot, payload={type:'quotes'|'snapshot', data:{symbol:{price,change_pct,...}}}
 *  - 兼容旧裸帧(parseFrame 兜底 seq=0, 走 payload.type 分支)
 *
 * 本 hook 承接 `envelope.ts`(此前就绪但无页面消费), 负责:
 *  1. 鉴权走 `Sec-WebSocket-Protocol: panwatch.auth.bearer, <jwt>`(不进 URL / access log);
 *  2. 断线重连带 `last_seq`, 服务端补发 ring 内 missed 帧;
 *  3. 指数退避(1s→30s 封顶), 4401 鉴权失败不重连(重连也白搭);
 *  4. 回传连接状态, 页面据此决定是否退回轮询。
 */
import { useEffect, useRef, useState } from 'react'
import { getToken } from '@panwatch/api'

import { parseFrame, reconnectUrl } from './envelope'

export interface QuoteTick {
  price: number
  change_pct?: number | null
  prev_close?: number | null
  name?: string
}
export type QuoteTickMap = Record<string, QuoteTick>
/** connecting=首连/重连中; live=已连通; offline=未启用/无 token/鉴权失败 */
export type QuoteStreamStatus = 'connecting' | 'live' | 'offline'

const WS_PATH = '/api/quotes/ws'
const SWP_BEARER = 'panwatch.auth.bearer'
const MAX_BACKOFF_MS = 30_000

/** 从一帧里取出 {symbol: tick}; 非行情帧返回 null。 */
function extractTicks(frame: ReturnType<typeof parseFrame>): QuoteTickMap | null {
  const p = frame.payload as { type?: string; data?: unknown } | null
  if (!p || typeof p !== 'object') return null
  const isQuoteTopic = frame.topic === 'quote.tick' || frame.topic === 'quote.snapshot'
  if (p.type !== 'quotes' && p.type !== 'snapshot' && !isQuoteTopic) return null
  const data = p.data
  if (!data || typeof data !== 'object') return null
  return data as QuoteTickMap
}

export function useQuoteStream(
  onTicks: (data: QuoteTickMap) => void,
  enabled = true,
): QuoteStreamStatus {
  const handlerRef = useRef(onTicks)
  handlerRef.current = onTicks
  const lastSeqRef = useRef(0)
  const [status, setStatus] = useState<QuoteStreamStatus>('connecting')

  useEffect(() => {
    if (!enabled) {
      setStatus('offline')
      return
    }
    const token = getToken()
    if (!token) {
      setStatus('offline')
      return
    }

    let closed = false
    let ws: WebSocket | null = null
    let retryTimer: ReturnType<typeof setTimeout> | null = null
    let attempt = 0
    const scheme = location.protocol === 'https:' ? 'wss' : 'ws'
    const base = `${scheme}://${location.host}${WS_PATH}`

    const connect = () => {
      if (closed) return
      setStatus('connecting')
      ws = new WebSocket(reconnectUrl(base, lastSeqRef.current), [SWP_BEARER, token])
      ws.onopen = () => {
        attempt = 0
        setStatus('live')
      }
      ws.onmessage = (ev) => {
        try {
          const frame = parseFrame(JSON.parse(ev.data))
          if (frame.seq > lastSeqRef.current) lastSeqRef.current = frame.seq
          const data = extractTicks(frame)
          if (data) handlerRef.current(data)
        } catch {
          /* 坏帧忽略, 不影响连接 */
        }
      }
      ws.onerror = () => {
        try {
          ws?.close()
        } catch {
          /* noop */
        }
      }
      ws.onclose = (ev) => {
        if (closed) return
        setStatus('offline')
        if (ev.code === 4401) return // 鉴权失败: 重连仍会被拒, 等重新登录/挂载
        attempt += 1
        const delay = Math.min(MAX_BACKOFF_MS, 1000 * 2 ** Math.min(attempt - 1, 5))
        retryTimer = setTimeout(connect, delay)
      }
    }

    connect()
    return () => {
      closed = true
      if (retryTimer) clearTimeout(retryTimer)
      try {
        ws?.close()
      } catch {
        /* noop */
      }
    }
  }, [enabled])

  return status
}
