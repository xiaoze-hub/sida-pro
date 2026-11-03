/**
 * 盘面指数总览（2026-11-03）。
 *
 * 数据口径：TQ `get_exday_data` 对指数返回**成分股聚合的四档资金**
 * （行=特大/大/中/小，列=[买入,卖出,主动买,主动卖]），`caliber = tdx_index_agg`。
 * 与东财/同花顺「主力净流入」不同源、不可换算，只可同向比较——页面必须带口径标注。
 *
 * 三个坑（后端已处理，这里只负责展示，不再自行换算）：
 * 1. 指数快照 `Amount` 是万元，四档金额是元（后端统一成元）；
 * 2. `get_exday_data` 只返当日 1 行 → 历史柱子来自日表累积，首日为空是正常的；
 * 3. 取数失败不能展示 0，一律显示 `--` 并标「数据未取到」。
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import { usePausedInterval } from '@sida/base-ui'
import PageHeader from '@/components/PageHeader'
import { safeFixed, safePercent, toAmount } from '@/lib/format'

type IndexItem = {
  symbol: string
  name: string | null
  trade_date: string | null
  close: number | null
  pct_chg: number | null
  amount: number | null
  up_home: number | null
  down_home: number | null
  outside: number | null
  inside: number | null
  xl_net: number | null
  lg_net: number | null
  md_net: number | null
  sm_net: number | null
  main_net: number | null
  net_all: number | null
  active_buy: number | null
  active_sell: number | null
  quality: string | null
  caliber: string | null
}

type Overview = {
  items: IndexItem[]
  stale: boolean
  as_of: string | null
  caliber: string
  reason?: string
}

type HistoryPoint = {
  trade_date: string
  close: number | null
  pct_chg: number | null
  amount: number | null
  main_net: number | null
  net_all: number | null
}

const REFRESH_MS = 15_000
const HISTORY_DAYS = 60

function authHeaders(): Record<string, string> {
  const token = localStorage.getItem('token')
  return token ? { Authorization: `Bearer ${token}` } : {}
}

function toneOf(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v)) return 'text-muted-foreground'
  if (v > 0) return 'text-stock-up'
  if (v < 0) return 'text-stock-down'
  return 'text-muted-foreground'
}

function signed(v: number | null | undefined): string {
  if (v === null || v === undefined) return '--'
  const s = toAmount(Math.abs(v))
  return `${v > 0 ? '+' : v < 0 ? '-' : ''}${s}`
}

/** 四档资金：横向堆叠条（左为正流入，右为负流出），宽度按最大绝对档位归一。 */
function TierBars({ item }: { item: IndexItem }) {
  const tiers = [
    { key: '特大单', v: item.xl_net },
    { key: '大单', v: item.lg_net },
    { key: '中单', v: item.md_net },
    { key: '小单', v: item.sm_net },
  ]
  const max = Math.max(1, ...tiers.map((t) => Math.abs(t.v ?? 0)))
  return (
    <div className="space-y-2">
      {tiers.map((t) => {
        const pct = Math.min(100, (Math.abs(t.v ?? 0) / max) * 100)
        const positive = (t.v ?? 0) >= 0
        return (
          <div key={t.key} className="grid grid-cols-[4.5rem_1fr_5.5rem] items-center gap-2 text-xs">
            <span className="text-muted-foreground">{t.key}</span>
            <div className="h-2 w-full rounded-sm bg-s2">
              <div
                className="h-2 rounded-sm"
                style={{
                  width: `${pct}%`,
                  marginLeft: positive ? 0 : `${100 - pct}%`,
                  background: positive ? 'var(--stock-up)' : 'var(--stock-down)',
                }}
              />
            </div>
            <span className={`text-right font-mono tabular-nums ${toneOf(t.v)}`}>{signed(t.v)}</span>
          </div>
        )
      })}
    </div>
  )
}

/** 主力净额历史柱（SVG，纯计算无依赖）；正红负绿，零轴居中。 */
function MainNetHistory({ points }: { points: HistoryPoint[] }) {
  const valid = points.filter((p) => typeof p.main_net === 'number')
  if (valid.length === 0) {
    return <p className="text-xs text-muted-foreground">暂无历史记录（日表从本次上线开始累积）。</p>
  }
  const max = Math.max(1, ...valid.map((p) => Math.abs(p.main_net as number)))
  const w = 100 / valid.length
  return (
    <div className="space-y-1">
      <svg viewBox={`0 0 100 32`} className="h-24 w-full" preserveAspectRatio="none" role="img"
        aria-label={`近 ${valid.length} 个交易日主力净额`}>
        <line x1="0" y1="16" x2="100" y2="16" className="stroke-border" strokeWidth="0.3" />
        {valid.map((p, i) => {
          const v = p.main_net as number
          const h = (Math.abs(v) / max) * 15
          const y = v >= 0 ? 16 - h : 16
          return (
            <rect
              key={p.trade_date}
              x={i * w}
              y={y}
              width={Math.max(0.4, w * 0.62)}
              height={Math.max(0.3, h)}
              className={v >= 0 ? 'fill-stock-up' : 'fill-stock-down'}
            />
          )
        })}
      </svg>
      <div className="flex justify-between text-xs text-muted-foreground">
        <span>{valid[0].trade_date}</span>
        <span>{valid[valid.length - 1].trade_date}</span>
      </div>
    </div>
  )
}

/** 涨跌家数条（宽度比）。 */
function BreadthBar({ up, down }: { up: number | null; down: number | null }) {
  if (!up && !down) return <span className="text-xs text-muted-foreground">--</span>
  const total = (up ?? 0) + (down ?? 0)
  const upPct = total > 0 ? ((up ?? 0) / total) * 100 : 0
  return (
    <div className="space-y-1">
      <div className="flex h-2 w-full overflow-hidden rounded-sm bg-s2">
        <div className="h-2" style={{ width: `${upPct}%`, background: 'var(--stock-up)' }} />
        <div className="h-2" style={{ width: `${100 - upPct}%`, background: 'var(--stock-down)' }} />
      </div>
      <div className="flex justify-between text-xs">
        <span className="text-stock-up">涨 {up ?? '--'}</span>
        <span className="text-stock-down">跌 {down ?? '--'}</span>
      </div>
    </div>
  )
}

export default function IndexesPage() {
  const [data, setData] = useState<Overview | null>(null)
  const [history, setHistory] = useState<HistoryPoint[]>([])
  const [selected, setSelected] = useState<string>('000001')
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)

  const loadOverview = useCallback(async () => {
    setLoading(true)
    try {
      const res = await fetch('/api/indexes/overview', { headers: authHeaders(), credentials: 'include' })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const json = (await res.json()) as Overview
      setData(json)
      setError(null)
      if (json.items.length > 0 && !json.items.some((i) => i.symbol === selected)) {
        setSelected(json.items[0].symbol)
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : '取数失败')
    } finally {
      setLoading(false)
    }
  }, [selected])

  const loadHistory = useCallback(async (symbol: string) => {
    try {
      const res = await fetch(`/api/indexes/${symbol}/history?days=${HISTORY_DAYS}`, {
        headers: authHeaders(),
        credentials: 'include',
      })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const json = (await res.json()) as { items: HistoryPoint[] }
      setHistory(json.items ?? [])
    } catch {
      setHistory([])
    }
  }, [])

  // 页签 keep-alive：隐藏时停表、回前台补一次（裸 setInterval 会在隐藏页签里
  // 一直打 TQ —— 每个指数 3 次 RPC，N 个页签就是 N 路）。
  usePausedInterval(() => void loadOverview(), REFRESH_MS)

  useEffect(() => {
    void loadHistory(selected)
  }, [selected, loadHistory])

  const current = useMemo(
    () => data?.items.find((i) => i.symbol === selected) ?? data?.items[0] ?? null,
    [data, selected],
  )

  const activeBias = useMemo(() => {
    if (!current?.active_buy || !current?.active_sell) return null
    const total = current.active_buy + current.active_sell
    return total > 0 ? (current.active_buy / total) * 100 : null
  }, [current])

  return (
    <div className="sida-page-enter space-y-4">
      <PageHeader
        title="盘面指数"
        description={`八大宽基指数的涨跌幅、成交额与四档资金结构；口径 ${data?.caliber ?? 'tdx_index_agg'}（成分股按单笔金额分档聚合），与东财/同花顺主力净流入不可换算。`}
        actions={
          <button
            type="button"
            onClick={() => void loadOverview()}
            disabled={loading}
            className="rounded-md px-3 py-1 text-[14px] text-foreground disabled:opacity-60"
          >
            {loading ? '刷新中…' : '刷新'}
          </button>
        }
        metadata={
          <>
            <span className="text-xs text-muted-foreground">
              数据时点 {data?.as_of ?? '--'}
              {data?.stale ? '（实时取数失败，展示库内最近一日）' : '（TQ 实时，15 秒刷新）'}
            </span>
            {error && <span className="text-xs text-stock-down">取数失败：{error}</span>}
          </>
        }
      />

      <section className="analysis-section">
        <div className="grid grid-cols-2 gap-x-6 gap-y-3 sm:grid-cols-4">
          {(data?.items ?? []).map((item) => {
            const active = item.symbol === selected
            return (
              <button
                key={item.symbol}
                type="button"
                onClick={() => setSelected(item.symbol)}
                className={`flex flex-col items-start gap-1 rounded-md px-2 py-2 text-left ${active ? 'bg-s3' : 'hover:bg-s2'}`}
              >
                <span className="text-[14px] text-foreground">{item.name ?? item.symbol}</span>
                <span className={`font-mono text-base tabular-nums ${toneOf(item.pct_chg)}`}>
                  {safePercent(item.pct_chg, 2)}
                </span>
                <span className="text-xs text-muted-foreground">
                  {safeFixed(item.close, 2)} · {toAmount(item.amount)}
                </span>
                <span className={`text-xs font-mono tabular-nums ${toneOf(item.main_net)}`}>
                  主力 {signed(item.main_net)}
                </span>
              </button>
            )
          })}
          {!data && <p className="text-[14px] text-muted-foreground">加载中…</p>}
          {data && data.items.length === 0 && (
            <p className="text-[14px] text-muted-foreground">无数据：{data.reason ?? 'TQ 不可达且库内无记录'}</p>
          )}
        </div>
      </section>

      {current && (
        <section className="analysis-section divide-y divide-border">
          <div className="grid gap-6 pb-4 lg:grid-cols-3">
            <div className="space-y-2">
              <h2 className="text-base text-foreground">四档资金（主动净额）</h2>
              <TierBars item={current} />
              <p className="text-xs text-muted-foreground">
                主力（特大+大）{signed(current.main_net)} · 全单净额 {signed(current.net_all)}
                {current.quality && current.quality !== 'ok' ? ` · 口径校验异常：${current.quality}` : ''}
              </p>
            </div>

            <div className="space-y-2">
              <h2 className="text-base text-foreground">近 {HISTORY_DAYS} 日主力净额</h2>
              <MainNetHistory points={history} />
            </div>

            <div className="space-y-3">
              <h2 className="text-base text-foreground">宽度与内外盘</h2>
              <BreadthBar up={current.up_home} down={current.down_home} />
              <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs">
                <dt className="text-muted-foreground">成交额</dt>
                <dd className="text-right font-mono tabular-nums">{toAmount(current.amount)}</dd>
                <dt className="text-muted-foreground">主动买占比</dt>
                <dd className="text-right font-mono tabular-nums">
                  {activeBias === null ? '--' : `${safeFixed(activeBias, 1)}%`}
                </dd>
                <dt className="text-muted-foreground">外盘 / 内盘</dt>
                <dd className="text-right font-mono tabular-nums">
                  {current.outside === null && current.inside === null
                    ? '--'
                    : `${safeFixed(current.outside, 0)} / ${safeFixed(current.inside, 0)}`}
                </dd>
                <dt className="text-muted-foreground">交易日</dt>
                <dd className="text-right font-mono tabular-nums">{current.trade_date ?? '--'}</dd>
              </dl>
            </div>
          </div>

          <p className="pt-3 text-xs text-muted-foreground">
            口径说明：四档 = 成分股按**单笔成交金额**分档后求和（特大/大/中/小），主动净额 = 主动买 − 主动卖；
            主力 = 特大 + 大。该口径与东财四档（eastmoney4）、腾讯逐笔（tick）不可换算，只用于同口径内的
            横向比较与历史分位。取不到数据时显示「--」，不以 0 代替。
          </p>
        </section>
      )}
    </div>
  )
}
