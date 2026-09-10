import { useEffect, useMemo, useRef, useState } from 'react'
import { AlertTriangle, RefreshCw, Zap } from 'lucide-react'
import { useECharts } from '@panwatch/biz-ui/hooks/useECharts'
import { hslaVar, readStockColors } from '@panwatch/biz-ui/lib/stock-colors'
import {
  detectHeatAnomaly,
  formatHeatPct,
  toTreemapCells,
  type BoardHeatItem,
  type HeatAnomaly,
  type HeatAreaMetric,
  type HeatPalette,
  type TreemapCell,
} from '@panwatch/biz-ui/lib/board-heatmap'
import { fetchAPI } from '@panwatch/api'
import { safeFixed } from '@/lib/format'
import ErrorState from '@panwatch/biz-ui/components/ErrorState'
import LoadingState from '@panwatch/biz-ui/components/LoadingState'

/**
 * 板块热力图 (P1-1, 2026-09-10, 借鉴 OpenTerminal treemap 热力图):
 * 面积=成交额(缺失保底)/等权, 颜色=涨跌幅(A股红涨绿跌, ±3% 夹紧), 点击下钻成分股。
 *
 * 实时化(2026-09-10 晚, 老板拍板"60s 自动刷新 + 异动高亮, 不推送"):
 * 交易时段内后端自动用 thsdk 实时快照覆盖涨跌幅/资金/量比/涨速(live=true),
 * 页面 60s 轮询 → treemap 自动重绘; 命中异动规则(急拉/急跌/放量)的板块加警示环 +
 * 顶部"板块异动"清单(点击下钻)。仅高亮, 不发通知。
 */

export interface BoardHeatmapProps {
  onOpenBoard: (blockCode: string, name: string) => void
  className?: string
}

interface HeatmapResp {
  type: string
  trade_date: string | null
  count: number
  live?: boolean
  live_count?: number
  as_of?: string | null
  /** 实际数据源: tdx=通达信客户端(方案B 默认) / ths=同花顺 thsdk(回落) */
  source?: string
  items: BoardHeatItem[]
}

type BoardType = 'industry' | 'concept'

const POLL_MS = 60_000
const CHART_HEIGHT = 560
/** 异动清单最多展示条数(超出截断, 避免横条刷屏) */
const MAX_ANOMALY_CHIPS = 8
/** 稳定引用: 避免 useMemo 依赖每次渲染都变 (react-hooks/exhaustive-deps) */
const NO_ITEMS: BoardHeatItem[] = []

function buildPalette(): HeatPalette {
  const sc = readStockColors()
  return {
    up: sc.up,
    down: sc.down,
    neutral: hslaVar('--flat-color', '215 16% 57%', 0.18),
    labelDark: hslaVar('--foreground', '240 10% 10%'),
    labelLight: '#ffffff',
  }
}

/** 金额(元) → 亿/万 文案; 缺失显式"无数据", 不猜测。safeFixed 兼容 PG DECIMAL 字符串。 */
function fmtMoney(v: number | null): string {
  if (v === null || v === undefined || !isFinite(v)) return '无数据'
  const abs = Math.abs(v)
  if (abs >= 1e8) return `${safeFixed(v / 1e8, 2)}亿`
  if (abs >= 1e4) return `${safeFixed(v / 1e4, 0)}万`
  return safeFixed(v, 0)
}

function tooltipHtml(c: TreemapCell): string {
  const rows: Array<[string, string]> = [
    ['涨跌幅', formatHeatPct(c.changePct)],
    ['量能', fmtMoney(c.volume)],
    ['资金净流入', fmtMoney(c.fundNet)],
  ]
  if (c.volumeRatio !== null && c.volumeRatio !== undefined && isFinite(c.volumeRatio)) {
    rows.push(['量比', safeFixed(c.volumeRatio, 2)])
  }
  if (c.speed !== null && c.speed !== undefined && isFinite(c.speed)) {
    rows.push(['涨速(5m)', formatHeatPct(c.speed)])
  }
  const body = rows
    .map(
      ([k, v]) =>
        `<div style="display:flex;justify-content:space-between;gap:12px"><span style="opacity:.65">${k}</span><span style="font-family:monospace">${v}</span></div>`,
    )
    .join('')
  const anomaly = c.anomaly
    ? `<div style="margin-top:2px;font-size:11px;color:#f59e0b">异动: ${c.anomaly.label}</div>`
    : ''
  const stale = c.hasDaily ? '' : '<div style="margin-top:2px;font-size:11px;opacity:.65">暂无当日数据</div>'
  return (
    `<div style="font-size:12px;min-width:150px">` +
    `<div style="font-weight:600;margin-bottom:4px">${c.name}</div>` +
    body +
    anomaly +
    stale +
    `</div>`
  )
}

/** UTC ISO → 本地 HH:MM:SS(仅实时基准时刻展示; 非法/缺失 → 空串) */
function fmtClock(iso: string | null | undefined): string {
  if (!iso) return ''
  const d = new Date(iso)
  if (!isFinite(d.getTime())) return ''
  const p = (n: number) => String(n).padStart(2, '0')
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}

export default function BoardHeatmap({ onOpenBoard, className }: BoardHeatmapProps) {
  const { ref, chartRef } = useECharts()
  const [btype, setBtype] = useState<BoardType>('industry')
  const [areaMetric, setAreaMetric] = useState<HeatAreaMetric>('volume')
  const [data, setData] = useState<HeatmapResp | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<unknown>(null)
  const [reloadKey, setReloadKey] = useState(0)

  const onOpenRef = useRef(onOpenBoard)
  useEffect(() => {
    onOpenRef.current = onOpenBoard
  }, [onOpenBoard])

  useEffect(() => {
    let alive = true
    const load = async () => {
      try {
        const res = await fetchAPI<HeatmapResp>(`/boards/heatmap?type=${btype}`)
        if (!alive) return
        setData(res)
        setError(null)
      } catch (e) {
        if (!alive) return
        setError(e)
      } finally {
        if (alive) setLoading(false)
      }
    }
    setLoading(true)
    void load()
    const t = window.setInterval(() => void load(), POLL_MS)
    return () => {
      alive = false
      window.clearInterval(t)
    }
  }, [btype, reloadKey])

  const fresh = data !== null && data.type === btype
  const items = fresh && data ? data.items : NO_ITEMS
  const cells = useMemo(
    () => (fresh && items.length > 0 ? toTreemapCells(items, { palette: buildPalette(), areaMetric }) : null),
    [fresh, items, areaMetric],
  )

  const liveMode = fresh && data ? Boolean(data.live) : false
  const liveClock = liveMode && data?.as_of ? fmtClock(data.as_of) : ''
  /** 实时异动清单: 按 |涨速| 降序, 仅高亮不推送 */
  const liveAnomalies = useMemo(() => {
    if (!liveMode) return []
    return items
      .map((item) => ({ item, anomaly: detectHeatAnomaly(item) }))
      .filter((x): x is { item: BoardHeatItem; anomaly: HeatAnomaly } => x.anomaly !== null)
      .sort((a, b) => Math.abs(b.item.speed ?? 0) - Math.abs(a.item.speed ?? 0))
      .slice(0, MAX_ANOMALY_CHIPS)
  }, [liveMode, items])

  useEffect(() => {
    const chart = chartRef.current
    if (!chart || !cells || cells.length === 0) return
    chart.setOption(
      {
        tooltip: {
          confine: true,
          borderRadius: 8,
          formatter: (p: { data?: TreemapCell }) => (p?.data ? tooltipHtml(p.data) : ''),
        },
        series: [
          {
            type: 'treemap',
            roam: false,
            nodeClick: false,
            breadcrumb: { show: false },
            leafDepth: 1,
            top: 0,
            left: 0,
            right: 0,
            bottom: 0,
            itemStyle: { borderWidth: 0, gapWidth: 2 },
            label: {
              show: true,
              fontSize: 11,
              overflow: 'truncate',
              formatter: (p: { name?: string; data?: TreemapCell }) =>
                `${p?.name ?? ''}\n${formatHeatPct(p?.data?.changePct)}`,
            },
            data: cells,
          },
        ],
      },
      true,
    )
    chart.off('click')
    chart.on('click', (params) => {
      const d = params?.data as unknown as TreemapCell | undefined
      if (d?.blockCode) onOpenRef.current(d.blockCode, d.name)
    })
  }, [cells, chartRef])

  const stale = Boolean(error) && fresh
  const noDataCount = fresh ? items.filter((i) => !i.has_daily).length : 0

  return (
    <div className={className}>
      <div className="mb-3 flex flex-wrap items-center gap-1.5">
        {(['industry', 'concept'] as const).map((t) => (
          <button
            key={t}
            type="button"
            onClick={() => setBtype(t)}
            className={`rounded px-2.5 py-1 text-[11px] transition-colors ${
              btype === t
                ? 'bg-primary text-primary-foreground'
                : 'bg-accent/50 text-muted-foreground hover:bg-accent'
            }`}
          >
            {t === 'industry' ? '行业' : '概念'}
          </button>
        ))}
        <span className="mx-1 h-4 w-px bg-border/60" />
        {(['volume', 'equal'] as const).map((m) => (
          <button
            key={m}
            type="button"
            onClick={() => setAreaMetric(m)}
            className={`rounded px-2.5 py-1 text-[11px] transition-colors ${
              areaMetric === m
                ? 'bg-primary text-primary-foreground'
                : 'bg-accent/50 text-muted-foreground hover:bg-accent'
            }`}
          >
            {m === 'volume' ? '面积:量能' : '面积:等权'}
          </button>
        ))}
        <div className="ml-auto flex items-center gap-2">
          {fresh && data.source ? (
            <span className="rounded bg-accent/60 px-1.5 py-0.5 text-[10px] text-muted-foreground">
              {data.source === 'tdx' ? '通达信' : '同花顺'}
            </span>
          ) : null}
          {liveMode ? (
            <span className="inline-flex items-center gap-1.5 text-[11px] text-emerald-600 dark:text-emerald-500">
              <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-emerald-500" />
              实时{liveClock ? ` · ${liveClock}` : ''}
            </span>
          ) : fresh && data.trade_date ? (
            <span className="text-[11px] text-muted-foreground">
              数据截至 {data.trade_date}
              {noDataCount > 0 ? ` · ${noDataCount} 个暂无当日数据` : ''}
            </span>
          ) : null}
          <button
            type="button"
            title="刷新"
            aria-label="刷新"
            onClick={() => setReloadKey((k) => k + 1)}
            disabled={loading}
            className="inline-flex h-7 w-7 items-center justify-center rounded-md border border-border bg-secondary text-foreground transition-colors hover:bg-secondary/80 disabled:opacity-50"
          >
            {loading ? (
              <span className="h-3 w-3 animate-spin rounded-full border-2 border-current/30 border-t-current" />
            ) : (
              <RefreshCw className="h-3.5 w-3.5" />
            )}
          </button>
        </div>
      </div>

      {stale && (
        <div className="mb-2 flex items-center gap-1.5 rounded-lg border border-amber-500/30 bg-amber-500/10 px-2.5 py-1.5 text-[11px] text-amber-600 dark:text-amber-500">
          <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
          刷新失败，当前展示上次数据
        </div>
      )}

      {liveMode && liveAnomalies.length > 0 && (
        <div
          data-testid="heatmap-anomalies"
          className="mb-2 flex flex-wrap items-center gap-1.5 text-[11px]"
        >
          <span className="inline-flex items-center gap-1 font-medium text-amber-600 dark:text-amber-500">
            <Zap className="h-3.5 w-3.5" />
            板块异动
          </span>
          {liveAnomalies.map(({ item, anomaly }) => (
            <button
              key={item.block_code}
              type="button"
              title={`${item.name} ${anomaly.label}`}
              onClick={() => onOpenRef.current(item.block_code, item.name)}
              className="inline-flex items-center gap-1 rounded border border-amber-500/40 bg-amber-500/10 px-1.5 py-0.5 text-amber-600 transition-colors hover:bg-amber-500/20 dark:text-amber-500"
            >
              <span className="font-medium">{item.name}</span>
              <span className="font-mono">{formatHeatPct(item.change_pct)}</span>
              <span>{anomaly.label}</span>
            </button>
          ))}
        </div>
      )}

      {cells && cells.length > 0 ? (
        <div ref={ref} style={{ height: CHART_HEIGHT }} className="w-full" />
      ) : loading && !fresh ? (
        <LoadingState rows={3} />
      ) : error && !fresh ? (
        <ErrorState
          message={error instanceof Error ? error.message : String(error)}
          onRetry={() => setReloadKey((k) => k + 1)}
          retrying={loading}
        />
      ) : fresh && items.length === 0 ? (
        <div
          className="flex items-center justify-center text-[12px] text-muted-foreground"
          style={{ height: CHART_HEIGHT }}
        >
          暂无板块数据（等待每日同步）
        </div>
      ) : (
        <div
          className="flex items-center justify-center text-[12px] text-muted-foreground"
          style={{ height: CHART_HEIGHT }}
        >
          暂无板块数据（等待每日同步）
        </div>
      )}
    </div>
  )
}
