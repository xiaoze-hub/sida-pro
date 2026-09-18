import { useEffect, useRef, useState } from 'react'

import { fetchAPI } from '@panwatch/api'

import type { MinutePoint, MinuteResponse, MinuteSwings } from '../lib/minute-types'
import MinuteLwcChart from './MinuteLwcChart'

/**
 * 分时面板(P1, 2026-09-18)。
 *
 * 从 `InteractiveKline` 的分时模式**原样搬来**(请求序号守卫 / 60s 冷启动超时 / 30s 轮询 /
 * KJ-042「空列表是故障, 不是停牌」), 供 `KlineChart` 在开启 `enableMinute` 时挂载 ——
 * 这样淘汰 IK 之后, 指数页/分析详情/模拟盘三页的分时视图仍能保留。
 *
 * 诚实口径: 取不到就**显式说明**(加载中/失败原因/源异常/暂无数据四种态), 绝不画一条假平的 0 线。
 */
export interface MinutePaneProps {
  symbol: string
  market: string
  /** 与主图同高(默认 360) */
  height?: number
  /** 轮询间隔(ms); 盘中 30s。测试可调小 */
  pollMs?: number
}

export default function MinutePane({ symbol, market, height = 360, pollMs = 30000 }: MinutePaneProps) {
  const [points, setPoints] = useState<MinutePoint[]>([])
  const [prevClose, setPrevClose] = useState<number | null>(null)
  const [isIndex, setIsIndex] = useState(false)
  const [swings, setSwings] = useState<MinuteSwings | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  /** KI-042: 源故障 note(与非交易日真空态可分) */
  const [degradedNote, setDegradedNote] = useState<string | null>(null)
  /** 请求序号: 切股/重挂时旧响应直接丢(防数据错位) */
  const seqRef = useRef(0)

  useEffect(() => {
    let alive = true
    const load = async () => {
      const seq = ++seqRef.current
      setLoading(true)
      setError('')
      setDegradedNote(null)
      try {
        // 分钟接口冷启动可能 ~15s(swings 全量逐笔翻页) → 给足 60s; 命中缓存后毫秒级
        const res = await fetchAPI<MinuteResponse>(
          `/quotes/minute/${encodeURIComponent(symbol)}?market=${encodeURIComponent(market)}`,
          { cacheMode: 'reload', timeoutMs: 60000 },
        )
        if (!alive || seq !== seqRef.current) return
        setPoints(res.points || [])
        setPrevClose(res.prev_close ?? null)
        setIsIndex(!!res.is_index)
        setSwings(res.swings || null)
        setDegradedNote(res.degraded ? res.note || '分时源暂不可用' : null)
      } catch (e) {
        if (!alive || seq !== seqRef.current) return
        setError(e instanceof Error ? e.message : '加载分时失败')
        setPoints([])
        setPrevClose(null)
        setIsIndex(false)
        setSwings(null)
      } finally {
        if (alive && seq === seqRef.current) setLoading(false)
      }
    }
    void load()
    const timer = window.setInterval(() => void load(), pollMs)
    return () => {
      alive = false
      window.clearInterval(timer)
    }
  }, [symbol, market, pollMs])

  return (
    <div data-testid="minute-pane" className="w-full" style={{ minHeight: height }}>
      {loading && <div className="py-2 text-[11px] text-muted-foreground">分时加载中…</div>}
      {error && <div className="py-2 text-[11px] text-red-500">分时加载失败：{error}</div>}
      {!error && degradedNote && (
        <div className="py-2 text-[11px] text-amber-500">
          分时源异常：{degradedNote}（空列表是源故障，不等于停牌）
        </div>
      )}
      {!error && points.length > 0 && (
        <MinuteLwcChart points={points} prevClose={prevClose} isIndex={isIndex} swings={swings} />
      )}
      {!error && !loading && points.length === 0 && !degradedNote && (
        <div data-minute-empty className="py-4 text-center text-[11px] text-muted-foreground">
          分时暂无数据（非交易时段属正常；盘中长时间为空可能是分时源故障）
        </div>
      )}
    </div>
  )
}
