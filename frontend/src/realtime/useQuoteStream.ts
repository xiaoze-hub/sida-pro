/**
 * 行情实时流消费(KI-025, 2026-09-09)。
 *
 * 后端 `/api/quotes/ws` 下行信封帧 `{seq, ts, topic, user_id, payload}`:
 *  - topic=quote.tick / quote.snapshot, payload={type:'quotes'|'snapshot', data:{symbol:{price,change_pct,...}}}
 *  - 兼容旧裸帧(parseFrame 兜底 seq=0, 走 payload.type 分支)
 *
 * 本 hook 承接 `envelope.ts`(此前就绪但无页面消费), 负责:
 *  1. 鉴权走 `Sec-WebSocket-Protocol: sida.auth.bearer, <jwt>`(不进 URL / access log);
 *  2. 断线重连带 `last_seq`, 服务端补发 ring 内 missed 帧;
 *  3. 指数退避(1s→30s 封顶), 4401 鉴权失败不重连(重连也白搭);
 *  4. 回传连接状态, 页面据此决定是否退回轮询。
 */
import { useEffect, useRef, useState } from 'react'
import { getToken } from '@sida/api'

import { useTabActive } from '@sida/base-ui'

import { parseFrame, reconnectUrl } from './envelope'

export interface QuoteTick {
  price: number
  change_pct?: number | null
  prev_close?: number | null
  name?: string
  /**
   * 行情源报时（可选，2026-10-29 起后端真的会下发）。
   *
   * `src/web/api/quote_stream.py::_proven_quote_stamp` 只透传**行情源报文自带**的时刻
   * （腾讯 parts[30]）；源不报时（TQ 快照/降级源）就是 `null` + `daily_pnl_period=unknown`。
   * 下游（`quote-ticks.applyQuoteTicks`）只有拿到非空 `quote_time` 才用它更新行情**时效**；
   * 没有就只更价、时刻判未知。**绝不由前端补一个时刻**（假新鲜比没时刻更糟）。
   */
  quote_time?: string | null
  quote_date?: string | null
  /** 时刻语义：`snapshot_refresh`=CN 源快照刷新（非成交）/`vendor_stamp`=港美报时未核实。 */
  quote_time_kind?: string | null
  /** 日期证据来源：`daily_bar_close`=TQ 同标的日线收盘价配对（只证到日）。 */
  quote_date_basis?: string | null
  daily_pnl_period?: string | null
}
export type QuoteTickMap = Record<string, QuoteTick>
/** connecting=首连/重连中; live=已连通; offline=未启用/无 token/鉴权失败 */
export type QuoteStreamStatus = 'connecting' | 'live' | 'offline'

const WS_PATH = '/api/quotes/ws'
const SWP_BEARER = 'sida.auth.bearer'
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
  // 隐藏页签不占 WS(keep-alive 成本控制: N 页签 = N 路连接)。切回前台自动重连,
  // 带 `last_seq` ⇒ 服务端补发 ring 内漏掉的帧, 不"开天窗"。
  const tabActive = useTabActive()

  useEffect(() => {
    if (!enabled || !tabActive) {
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
  }, [enabled, tabActive])

  return status
}
