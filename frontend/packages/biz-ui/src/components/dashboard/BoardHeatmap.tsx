import { useEffect, useMemo, useRef, useState } from 'react'
import { AlertTriangle, RefreshCw, Zap } from 'lucide-react'
import { useECharts } from '@panwatch/biz-ui/hooks/useECharts'
import { hslaVar, readStockColors } from '@panwatch/biz-ui/lib/stock-colors'
import {
  detectHeatAnomaly,
  formatHeatPct,
  hasDrawableArea,
  hasUsableVolume,
  labelTierFor,
  pickLabelColor,
  toTreemapCells,
  type BoardHeatItem,
  type HeatAnomaly,
  type HeatAreaMetric,
  type HeatPalette,
  type TreemapCell,
} from '@panwatch/biz-ui/lib/board-heatmap'
import type { Rgb } from '@panwatch/biz-ui/lib/board-heatmap'
import type { EChartsType } from 'echarts/core'
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
// 2026-09-18 UI 走查 B2: 图表**有数据**才配 560px; 没数据时用紧凑高度,
// 否则整块空白(走查: heatmap 5/10「主视觉区沦为大片空白」)。
const CHART_EMPTY_HEIGHT = 168
/** 异动清单最多展示条数(超出截断, 避免横条刷屏) */
const MAX_ANOMALY_CHIPS = 8
/** 稳定引用: 避免 useMemo 依赖每次渲染都变 (react-hooks/exhaustive-deps) */
const NO_ITEMS: BoardHeatItem[] = []

/** 字色候选: 真正的深/浅两端(不跟随主题名 —— 跟随**实测底色**)。 */
const LABEL_DARK = '#10151f'
const LABEL_LIGHT = '#ffffff'

/**
 * 从**画布实测像素**取每个色块的实际底色。
 *
 * 为什么要实测: 色块填充是半透明的, 眼睛看到的是"填充叠在画布底色上"的结果, 而底色是白是黑
 * 只有画出来才知道(生产实测这块画布底色是**白**的 —— 与深色主题的直觉相反, 这正是老规则
 * 把字色判反、文字隐形的根因)。采样点取色块**上缘 12% 内缩**的三点取中位 —— 文字居中,
 * 避开文字像素, 否则会把字色当底色。
 */
function sampleTileColors(chart: EChartsType, rects: { w: number; h: number }[]): (Rgb | null)[] | null {
  try {
    const canvas = (chart.getDom() as HTMLElement).querySelector('canvas')
    const ctx = canvas?.getContext('2d')
    if (!canvas || !ctx) return null
    const d = ctx.getImageData(0, 0, canvas.width, canvas.height).data
    const W = canvas.width
    const leaves = (chart as unknown as { getModel?: () => EcLayoutProbe | undefined })
      .getModel?.()?.getSeriesByIndex?.(0)?.getData?.()?.tree?.root?.children
    if (!leaves || leaves.length !== rects.length) return null
    return leaves.map((node, i) => {
      const L = node.getLayout?.()
      const x = Number(L?.x)
      const y = Number(L?.y)
      const w = rects[i].w
      const h = rects[i].h
      if (!Number.isFinite(x) || !Number.isFinite(y) || w < 4 || h < 4) return null
      const pts = [0.15, 0.5, 0.85].map((f) => {
        const sx = Math.min(Math.max(Math.round(x + w * f), 0), W - 1)
        const sy = Math.min(Math.max(Math.round(y + h * 0.12), 0), canvas.height - 1)
        const o = (sy * W + sx) * 4
        return { r: d[o], g: d[o + 1], b: d[o + 2], a: 1 }
      })
      pts.sort((p, q) => p.r + p.g + p.b - (q.r + q.g + q.b))
      return pts[1]
    })
  } catch {
    return null // 读不到(tainted canvas / 结构变了) → 保留第一遍估算, 不硬改
  }
}

/**
 * ECharts 内部 model 的最小结构(公开 API 没暴露 getModel, 用结构类型读布局, 读不到就放弃)。
 * treemap 的布局**不在 List 上**(实测 `data.count()=129` 含根节点, 与 cells 不等), 而在
 * **树节点**上: `data.tree.root.children[i].getLayout()` → {x,y,width,height,isInView}。
 */
type EcLayoutProbe = {
  getSeriesByIndex?: (i: number) => {
    getData?: () => {
      tree?: { root?: { children?: { getLayout?: () => { x?: number; y?: number; width?: number; height?: number } | undefined }[] } }
    }
  } | undefined
}

/**
 * 二遍布局: 读 ECharts 算好的色块 rect, 按**真实像素尺寸**给每块定标签档位并写回。
 *
 * 为什么这么做(2026-09-20 缺陷修复): 老规则用"面积占比 ≥ 0.8%"当"放不放得下文字"的代理指标,
 * 在**等权模式**下必然失效(每块占比 = 1/N, N=128 时恒 0.0078) ⇒ 整张图无标签; 量能模式下
 * 也只有少数大块够阈值。真判据只能是布局后的 rect。
 *
 * 同时把统计挂到图表容器上(`data-heatmap-labels` 等)—— canvas 里的文字 DOM 量不到,
 * 不变量 **shown + tiny === total** 就是"没有哪个块静默丢了标签"的机器判据(巡检据此断言)。
 */
function applyLabelTiers(chart: EChartsType, cells: TreemapCell[]) {
  const host = (() => { try { return chart.getDom() ?? null } catch { return null } })()
  let rects: { w: number; h: number }[] | null = null
  try {
    // getModel 在 ECharts 公开类型里是 private, 但运行时存在 —— 用结构类型读布局,
    // 拿不到就放弃(不硬改, 保持第一遍全显示)。
    const model = (chart as unknown as { getModel?: () => EcLayoutProbe | undefined }).getModel?.()
    const leaves = model?.getSeriesByIndex?.(0)?.getData?.()?.tree?.root?.children
    // 叶子数必须与 cells 一一对应, 否则宁可不改(顺序错位会把标签配到别的板块上)
    if (leaves && leaves.length === cells.length) {
      rects = leaves.map((n) => {
        const L = n.getLayout?.()
        return { w: Number(L?.width), h: Number(L?.height) }
      })
    }
  } catch {
    rects = null
  }
  if (!rects) {
    // 读不到布局**不硬改**: 保持第一遍"全显示", 由 ECharts 自己裁 —— 并把"没量到"如实挂出去。
    host?.setAttribute('data-heatmap-labels', `unknown/${cells.length}`)
    return
  }
  const tiers = rects.map((r) => labelTierFor(r.w, r.h))
  const tiny = tiers.filter((t) => t === 0).length
  const shown = tiers.length - tiny
  // ── 字色用**画布实测像素**定(不猜) ──────────────────────────────────────────
  // 底色是白是黑只有画出来才知道(实测这块画布底色是**白**的, 与深色主题的直觉相反);
  // 用 WCAG 对比度在深/浅两端里选, 并回传最小对比度给巡检断言"文字真的看得见"。
  const sampled = sampleTileColors(chart, rects)
  const colors: (string | undefined)[] = []
  let minContrast = Infinity
  rects.forEach((_, i) => {
    const bg = sampled?.[i]
    if (tiers[i] === 0 || !bg) return
    const picked = pickLabelColor(bg, LABEL_DARK, LABEL_LIGHT)
    colors[i] = picked.color
    minContrast = Math.min(minContrast, picked.contrast)
  })
  const fin = (v: number) => (Number.isFinite(v) ? Math.round(v) : -1)
  const minW = Math.min(...rects.map((r) => (Number.isFinite(r.w) ? r.w : Infinity)))
  const minH = Math.min(...rects.map((r) => (Number.isFinite(r.h) ? r.h : Infinity)))
  host?.setAttribute('data-heatmap-labels', `${shown}/${cells.length}`)
  host?.setAttribute('data-heatmap-label-tiny', String(tiny))
  host?.setAttribute('data-heatmap-min-tile', `${fin(minW)}x${fin(minH)}`)
  // 最小对比度: 巡检据此断言"文字真的看得见"(WCAG 对比度 1~21; 小字号建议 ≥4.5, 这里按 ≥3 兜底)
  host?.setAttribute('data-heatmap-contrast', Number.isFinite(minContrast) ? safeFixed(minContrast, 2) : 'unknown')
  chart.setOption({
    series: [
      {
        data: cells.map((c, i) => ({
          ...c,
          labelTier: tiers[i],
          label: { ...c.label, show: tiers[i] > 0, ...(colors[i] ? { color: colors[i] as string } : {}) },
        })),
      },
    ],
  } as never)
}

function buildPalette(): HeatPalette {
  const sc = readStockColors()
  return {
    up: sc.up,
    down: sc.down,
    neutral: hslaVar('--flat-color', '215 16% 57%', 0.18),
    // 字色候选必须是**真正的深/浅两端** —— 以前 labelDark 取 `--foreground`, 深色主题下它
    // 是近白(240 15% 90%), 两个候选都是浅色 ⇒ 浅色块上的文字必然隐形。这里写死两端,
    // 由 pickLabelColor 按**实测对比度**选(浅底黑字/深底白字)。
    labelDark: '#10151f',
    labelLight: '#ffffff',
    // 图表实际底色(第一遍估算用; 二遍会用画布实测像素覆盖)
    surface: '#ffffff',
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
  /**
   * 面积:量能视图的画不画得出来(2026-09-14 缺陷修复):
   * - volumeUnavailable: 有板块但**全部**成交额缺失/为 0 ⇒ 面积不携带量能信息。此时
   *   保底面积会把 128 个块铺成一模一样的等权图, 挂在「面积:量能」标签下就是误导
   *   (生产实测这种载荷下画布曾整块空白且无任何说明)。
   * - drawable: ECharts treemap 对全 0/NaN 面积整块不画 ⇒ 只要画不出来就必须给空态,
   *   绝不允许留一个"静默空白灰框"(那是缺陷本体, 不是结果)。
   */
  const volumeUnavailable =
    fresh && areaMetric === 'volume' && items.length > 0 && !hasUsableVolume(items)
  const drawable = hasDrawableArea(cells)
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
    // 画不出来(空/全 0 面积)时不 setOption: 否则 ECharts 画一张空图 + 旧图残留,
    // 页面上就是一个没有说明的空白框。量能全缺(volumeUnavailable)同样不画 ——
    // 那张"全等权保底面积"的图挂在「面积:量能」标签下会被读成量能。
    if (!chart || volumeUnavailable || !hasDrawableArea(cells)) return
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
              // 按**档位**渲染(2026-09-20): 2 行=名称+涨跌幅; 1 行=只名称(色块矮, 两行放不下);
              // 0 行=空(色块太小, 只留 tooltip)。档位由二遍布局按真实 rect 定。
              formatter: (p: { name?: string; data?: TreemapCell }) => {
                const tier = p?.data?.labelTier ?? 2
                if (tier === 0) return ''
                if (tier === 1) return `${p?.name ?? ''}`
                return `${p?.name ?? ''}\n${formatHeatPct(p?.data?.changePct)}`
              },
            },
            data: cells,
          },
        ],
      },
      true,
    )
    // ── 二遍布局(2026-09-20): 用**真实色块像素尺寸**给每个块定标签档位 ──────────────
    // 为什么必须二遍: 布局前不知道每块多大, 而"面积占比"不是尺寸 —— 等权模式下每块占比恒相等
    // (1/128 = 0.0078 < 老阈值 0.008) ⇒ 老规则让**整张图一个标签都没有**(用户报的现象)。
    applyLabelTiers(chart, cells as TreemapCell[])

    chart.off('click')
    chart.on('click', (params) => {
      const d = params?.data as unknown as TreemapCell | undefined
      if (d?.blockCode) onOpenRef.current(d.blockCode, d.name)
    })
  }, [cells, chartRef, volumeUnavailable])

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

      {volumeUnavailable ? (
        // 有板块但全部成交额缺失/为 0: 面积不携带量能信息, 保底面积会铺成一张等权图
        // 挂在「面积:量能」标签下 ⇒ 与其静默画一张会误导的图, 不如明确说明并给出路。
        <div
          data-testid="heatmap-no-volume"
          className="flex flex-col items-center justify-center gap-2 text-center text-[12px] text-muted-foreground"
          style={{ height: CHART_HEIGHT }}
        >
          <span>{items.length} 个板块的成交额全部缺失，面积无法区分板块 —— 已停绘，避免把保底面积误读成量能。</span>
          <span className="text-[11px]">可切到「面积:等权」看涨跌分布，或稍后点刷新重试。</span>
          <button
            type="button"
            onClick={() => setAreaMetric('equal')}
            className="rounded border border-border bg-secondary px-2.5 py-1 text-[11px] text-foreground transition-colors hover:bg-secondary/80"
          >
            切换面积:等权
          </button>
        </div>
      ) : drawable ? (
        <div ref={ref} data-testid="heatmap-canvas" style={{ height: CHART_HEIGHT }} className="w-full" />
      ) : loading && !fresh ? (
        <LoadingState rows={3} />
      ) : error && !fresh ? (
        <ErrorState
          message={error instanceof Error ? error.message : String(error)}
          onRetry={() => setReloadKey((k) => k + 1)}
          retrying={loading}
        />
      ) : (
        <div
          data-testid="heatmap-empty"
          data-compact="1"
          className="flex flex-col items-center justify-center gap-1 rounded-lg border border-dashed border-border/60 bg-muted/10 text-center"
          style={{ minHeight: `${CHART_EMPTY_HEIGHT}px` }}
        >
          <span className="text-[12px] text-muted-foreground">暂无板块数据（等待每日同步）</span>
          <span className="text-[11px] text-muted-foreground/80">
            每日 15:30 后同步板块行情；若长期为空，请在「数据源」页检查东财板块源
          </span>
        </div>
      )}
    </div>
  )
}
