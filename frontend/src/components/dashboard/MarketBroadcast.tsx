import { useEffect, useState } from 'react'

import { fetchAPI } from '@sida/api'
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

  const num = (v?: number) => (typeof v === 'number' ? v : null)
  const pct = (v?: number) => (typeof v === 'number' ? `${safeFixed(v, 1)}%` : null)
  const join = (xs: (string | null | undefined)[]) => xs.filter(Boolean).join('·')

  // ① 梯队：上方 KPI 已有「涨停 82」，这里只补结构（不重复主数字）
  const ladder = join([
    num(d.first_board_count) !== null ? `首板${d.first_board_count}` : null,
    num(d.ge2_count) !== null && (d.ge2_count ?? 0) > 0 ? `2板${d.ge2_count}` : null,
    num(d.ge3_count) !== null && (d.ge3_count ?? 0) > 0 ? `3板+${d.ge3_count}` : null,
    num(d.limit_up_max_streak) !== null && (d.limit_up_max_streak ?? 0) > 1
      ? `最高${d.limit_up_max_streak}板`
      : null,
  ])

  // ② 质量：封板率 / 炸板 / 晋级率 / 跌停 —— 全是比率，比家数更能说明承接强弱
  const quality = join([
    pct(d.seal_rate_pct) ? `封板${pct(d.seal_rate_pct)}` : null,
    num(d.broken_count) !== null ? `炸${d.broken_count}` : null,
    pct(d.break_rate_pct) ? `炸板率${pct(d.break_rate_pct)}` : null,
    pct(d.promo_rate_pct) ? `晋级${pct(d.promo_rate_pct)}` : null,
    num(d.limit_down_count) !== null && (d.limit_down_count ?? 0) > 0
      ? `跌停${d.limit_down_count}`
      : null,
  ])

  // ③ 涨停潮：窄格只放 4 个，全写会换行挤爆卡片高度
  const sectors = (d.top_sectors ?? [])
    .slice(0, 4)
    .map((x) => `${x.name}${x.count}`)
    .join(' · ')

  // ④ 龙头：连板数 + 首封时间（1 板不写"1板"，那是噪音）
  const leaders = (d.leaders ?? [])
    .slice(0, 3)
    .map((l) => {
      const bits = [l.name ?? l.symbol ?? '—']
      if (typeof l.board_days === 'number' && l.board_days > 1) bits.push(`${l.board_days}板`)
      if (l.first_seal) bits.push(l.first_seal.slice(0, 5))
      return bits.join(' ')
    })
    .join(' · ')

  return { ladder, quality, sectors, leaders }
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
      <div className="text-[12px] text-muted-foreground" data-testid="broadcast-loading">
        盘面播报加载中…
      </div>
    )
  }

  if (err) {
    return (
      <div className="text-[12px] text-muted-foreground" data-testid="broadcast-error">
        {err}（可点右上「刷新」重试）
      </div>
    )
  }

  if (!data?.available) {
    return (
      <div className="text-[12px] text-muted-foreground" data-testid="broadcast-empty">
        {data?.reason || '今日盘后复盘尚未生成，暂无播报'}
      </div>
    )
  }

  const f = computeBroadcast(data)
  const dateText =
    data.trade_date && data.trade_date.length === 8
      ? `${data.trade_date.slice(0, 4)}-${data.trade_date.slice(4, 6)}-${data.trade_date.slice(6, 8)}`
      : ''

  return (
    <div data-testid="broadcast">
      {/* 横向 4 格：标题加重、值一行内截断 —— 原来是 5 行竖排，把卡片撑高、中间留空 */}
      <div className="flex items-baseline gap-2">
        <span className="text-[12px] font-semibold text-foreground">今日盘面</span>
        {dateText && (
          <span className="text-[12px] text-muted-foreground tabular-nums">基准日 {dateText}</span>
        )}
        {data.phase_label && (
          <span className="rounded bg-s1 px-1.5 py-0.5 text-[12px] text-foreground">{data.phase_label}</span>
        )}
        {data.quality && data.quality !== 'ok' && (
          <span className="text-[12px] text-amber-500">数据{data.quality === 'partial' ? '不全' : '缺失'}</span>
        )}
      </div>

      <div className="mt-1 grid grid-cols-2 gap-x-4 gap-y-1 lg:grid-cols-4">
        <Cell title="涨停梯队" testId="broadcast-ladder" value={f.ladder} />
        <Cell title="封板质量" testId="broadcast-quality" value={f.quality} />
        <Cell
          title="涨停潮"
          hint="按涨停家数"
          hintTitle={data.sector_caliber || '涨停家数按行业聚合（非板块涨跌幅榜）'}
          testId="broadcast-sectors"
          caliberId="broadcast-caliber"
          value={f.sectors}
        />
        <Cell title="龙头" testId="broadcast-leaders" value={f.leaders} />
      </div>
    </div>
  )
}

function Cell({
  title,
  hint,
  hintTitle,
  value,
  testId,
  caliberId,
}: {
  title: string
  hint?: string
  /** 悬停显示完整口径（窄格里放不下，但不该丢） */
  hintTitle?: string
  value?: string
  testId: string
  caliberId?: string
}) {
  if (!value) return null
  return (
    <div className="min-w-0">
      <div className="flex items-baseline gap-1">
        <span className="text-[12px] font-semibold text-foreground">{title}</span>
        {hint && (
          <span
            className="cursor-help text-[12px] text-muted-foreground"
            data-testid={caliberId}
            title={hintTitle}
          >
            （{hint}）
          </span>
        )}
      </div>
      <div
        className="mt-0.5 truncate text-[12px] leading-tight text-muted-foreground tabular-nums"
        data-testid={testId}
      >
        {value}
      </div>
    </div>
  )
}
