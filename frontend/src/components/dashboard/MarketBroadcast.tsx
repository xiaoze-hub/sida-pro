import { useEffect, useState } from 'react'

import { fetchAPI } from '@panwatch/api'
import { safeFixed } from '@/lib/format'

/**
 * 今日市场播报（2026-10-25）。
 *
 * 目的：填掉总结卡里那块空白，用**可验算的事实**播报盘面，而不是让 LLM 写一段「震荡上行」。
 *
 * 口径（必须显示，见组件底部的 caliber 行）：
 *   · 涨停/炸板/封板率/晋级率来自盘后复盘快照 `market_replay_daily`；
 *   · `top_sectors` 是**涨停家数按行业聚合**，不是板块涨跌幅榜 ——
 *     东财板块榜接口在云服务器持续断连（push2 / push2delay 均 RemoteDisconnected），
 *     所以用「板块涨停潮」口径替代。「涨停潮」对短线比涨幅榜更有信息量（谁在被资金主攻），
 *     但**不说清口径就是误导**，所以口径行常显。
 *
 * 诚实四态：加载 / 无数据（当日尚未生成）/ 读取失败 / 有数据。绝不拿 0 假装有数据。
 */

type Sector = { name: string; count: number }
type Leader = { sector?: string; symbol?: string; name?: string; board_days?: number; first_seal?: string }
type Replay = {
  available: boolean
  reason?: string
  trade_date?: string
  quality?: string
  phase_label?: string
  limit_up_count?: number
  first_board_count?: number
  ge2_count?: number
  ge3_count?: number
  limit_up_max_streak?: number
  one_word_count?: number
  broken_count?: number
  break_rate_pct?: number
  seal_rate_pct?: number
  promo_rate_pct?: number
  limit_down_count?: number
  top_sectors?: Sector[]
  leaders?: Leader[]
  sector_caliber?: string
}

export type BroadcastFacts = {
  ladder: string
  quality: string
  sectors: string
  leaders: string
}

/** 把接口数据压成「一行一句」的播报，纯事实、无判断词。导出供测试。 */
export function computeBroadcast(d: Replay | null): BroadcastFacts {
  if (!d || !d.available) return { ladder: '', quality: '', sectors: '', leaders: '' }

  const n = (v?: number) => (typeof v === 'number' ? v : null)
  const pct = (v?: number) => (typeof v === 'number' ? `${safeFixed(v, 2)}%` : null)

  const parts: string[] = []
  if (n(d.limit_up_count) !== null) parts.push(`涨停 ${d.limit_up_count}`)
  if (n(d.first_board_count) !== null) parts.push(`首板 ${d.first_board_count}`)
  if (n(d.ge2_count) !== null) parts.push(`2板+ ${d.ge2_count}`)
  if (n(d.ge3_count) !== null && (d.ge3_count ?? 0) > 0) parts.push(`3板+ ${d.ge3_count}`)
  if (n(d.limit_up_max_streak) !== null && (d.limit_up_max_streak ?? 0) > 0) {
    parts.push(`最高 ${d.limit_up_max_streak} 板`)
  }
  if (n(d.limit_down_count) !== null) parts.push(`跌停 ${d.limit_down_count}`)

  const qualityParts: string[] = []
  if (pct(d.seal_rate_pct)) qualityParts.push(`封板率 ${pct(d.seal_rate_pct)}`)
  if (n(d.broken_count) !== null) qualityParts.push(`炸板 ${d.broken_count}`)
  if (pct(d.break_rate_pct)) qualityParts.push(`炸板率 ${pct(d.break_rate_pct)}`)
  if (pct(d.promo_rate_pct)) qualityParts.push(`晋级率 ${pct(d.promo_rate_pct)}`)
  if (n(d.one_word_count) !== null && (d.one_word_count ?? 0) > 0) {
    qualityParts.push(`一字 ${d.one_word_count}`)
  }

  const sectors = (d.top_sectors ?? [])
    .slice(0, 6)
    .map((s) => `${s.name} ${s.count}`)
    .join(' · ')

  const leaders = (d.leaders ?? [])
    .slice(0, 3)
    .map((l) => {
      const bits = [l.name ?? l.symbol ?? '—']
      if (typeof l.board_days === 'number' && l.board_days > 1) bits.push(`${l.board_days}板`)
      if (l.first_seal) bits.push(l.first_seal.slice(0, 5))
      return bits.join(' ')
    })
    .join(' · ')

  return {
    ladder: parts.join(' ｜ '),
    quality: qualityParts.join(' ｜ '),
    sectors,
    leaders,
  }
}

export default function MarketBroadcast() {
  const [data, setData] = useState<Replay | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let alive = true
    setLoading(true)
    fetchAPI<Replay>('/market/replay-summary')
      .then((res) => {
        if (!alive) return
        // 缺字段按空处理，不伪装成有数据
        setData({
          ...res,
          available: Boolean(res?.available),
          top_sectors: Array.isArray(res?.top_sectors) ? res.top_sectors : [],
          leaders: Array.isArray(res?.leaders) ? res.leaders : [],
        })
        setErr(null)
      })
      .catch(() => {
        if (!alive) return
        setErr('盘面播报读取失败')
        setData(null)
      })
      .finally(() => {
        if (alive) setLoading(false)
      })
    return () => {
      alive = false
    }
  }, [])

  if (loading) {
    return (
      <div className="text-[11px] text-muted-foreground" data-testid="broadcast-loading">
        盘面播报加载中…
      </div>
    )
  }

  if (err) {
    return (
      <div className="text-[11px] text-muted-foreground" data-testid="broadcast-error">
        {err}（可点右上「刷新」重试）
      </div>
    )
  }

  if (!data?.available) {
    return (
      <div className="text-[11px] text-muted-foreground" data-testid="broadcast-empty">
        {data?.reason || '今日盘后复盘尚未生成，暂无播报'}
      </div>
    )
  }

  const f = computeBroadcast(data)
  const isStale = Boolean(data.trade_date && data.trade_date.length === 8)
  const dateText = isStale
    ? `${data.trade_date!.slice(0, 4)}-${data.trade_date!.slice(4, 6)}-${data.trade_date!.slice(6, 8)}`
    : ''

  return (
    <div className="space-y-1" data-testid="broadcast">
      <div className="flex items-baseline gap-2">
        <span className="text-[11px] font-medium text-foreground">今日盘面</span>
        {dateText && <span className="text-[11px] text-muted-foreground tabular-nums">基准日 {dateText}</span>}
        {data.phase_label && (
          <span className="rounded bg-s1 px-1.5 py-0.5 text-[11px] text-foreground">{data.phase_label}</span>
        )}
        {data.quality && data.quality !== 'ok' && (
          <span className="text-[11px] text-amber">数据{data.quality === 'partial' ? '不全' : '缺失'}</span>
        )}
      </div>

      {f.ladder && (
        <div className="text-[11px] text-foreground tabular-nums" data-testid="broadcast-ladder">
          {f.ladder}
        </div>
      )}
      {f.quality && (
        <div className="text-[11px] text-muted-foreground tabular-nums" data-testid="broadcast-quality">
          {f.quality}
        </div>
      )}
      {f.sectors && (
        <div className="text-[11px] text-foreground" data-testid="broadcast-sectors">
          <span className="text-muted-foreground">涨停潮 </span>
          <span className="tabular-nums">{f.sectors}</span>
        </div>
      )}
      {f.leaders && (
        <div className="text-[11px] text-muted-foreground tabular-nums" data-testid="broadcast-leaders">
          龙头 {f.leaders}
        </div>
      )}
      <div className="text-[11px] text-muted-foreground" data-testid="broadcast-caliber">
        {data.sector_caliber || '板块为涨停家数聚合（非涨跌幅榜）'}
      </div>
    </div>
  )
}
