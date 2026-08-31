import { useCallback, useEffect, useRef, useState } from 'react'
import { AlertTriangle, RefreshCw, ShieldAlert, Zap } from 'lucide-react'
import { fetchAPI } from '@panwatch/api'

/**
 * 异动接近度面板(任务 C, 2026-08-24)
 * 数据 GET /api/abnormal-moves?min_proximity=0.5 (fetchAPI 自动补 /api 前缀)。
 * 60s 自动刷新; 点击行触发 onOpenDetail(symbol) (默认行为: 派发
 * 'panwatch-open-stock-insight' 事件, 与现有的 stock-insight-modal 复用)。
 *
 * 数据格式与 src.core.abnormal_moves.analyze_abnormal_moves 一致:
 *   {
 *     symbol, name?, board, board_name,
 *     benchmark: { code, name, tencent },
 *     available, worst, windows: WindowRow[], status, proximity, source?
 *   }
 *   WindowRow = { rule_key, window, board, severity, up_threshold, down_threshold,
 *                 threshold_used, direction, stock_pct, index_pct, deviation_pct,
 *                 available, stock_used_n, index_used_n, proximity, status }
 *
 * 状态映射:
 *   triggered → 红色 "已触发"  (proximity >= 1.0, 交易所会发异动公告)
 *   edge      → 橙色 "边缘"    (0.7 <= proximity < 1.0)
 *   watch     → 黄色 "观察"    (0.5 <= proximity < 0.7)
 *   normal    → 绿色 "正常"    (proximity < 0.5)
 *   unknown   → 灰色 "数据不足"
 *
 * 进度条 [0~100%] 直接展示 proximity*100, 截断 0~100. 主要给 "看一眼就抓到
 * 最接近阈值的标的" 用的, >100% 表示已经超过阈值.
 *
 * 样式对齐: 复用 DarkFlowCards / OrderBookObBar 的 "rounded-xl border
 * border-border/50 bg-card p-3" 卡片风 + "text-[13px] font-semibold
 * text-foreground" 标题 + "font-mono" 数值, 严格跟现有 token。
 */

export type AbnormalStatus = 'triggered' | 'edge' | 'watch' | 'normal' | 'unknown'

export interface AbnormalWindow {
  rule_key: string
  window: number
  board: string
  severity: 'normal' | 'severe'
  up_threshold: number
  down_threshold: number
  threshold_used: number
  direction: 'up' | 'down' | 'flat' | 'na'
  stock_pct: number | null
  index_pct: number | null
  deviation_pct: number | null
  available: boolean
  stock_used_n?: number
  index_used_n?: number
  proximity: number | null
  status: AbnormalStatus
}

export interface AbnormalRow {
  symbol: string
  name?: string | null
  board: 'main' | 'cyb' | 'star' | 'bse' | string
  board_name: string
  benchmark: { code: string; name: string; tencent: string }
  available: boolean
  worst: AbnormalWindow | null
  windows: AbnormalWindow[]
  status: AbnormalStatus
  proximity: number | null
  source?: string
}

export interface AbnormalMovesResp {
  available: boolean
  min_proximity: number
  count: number
  items: AbnormalRow[]
  note?: string
}

export interface AbnormalMovesCardProps {
  /** 直接覆盖最小接近度 (默认 0.5, 任务 C 要求; 若想收到全部 6 条 cancel 用 0) */
  minProximity?: number
  /** 行点击回调; 默认派发 panwatch-open-stock-insight 事件 */
  onOpenDetail?: (symbol: string, row: AbnormalRow) => void
  /** 轮询间隔 ms; 默认 60000 (60s, 对齐后端缓存 TTL) */
  intervalMs?: number
}

const DEFAULT_INTERVAL_MS = 60000

// ---- 格式化 -----------------------------------------------------------

function fmtSignedZero(v: number | null | undefined, digits = 1): string {
  if (v == null || !Number.isFinite(v)) return '—'
  if (v === 0) return '0.0%'
  return `${v > 0 ? '+' : ''}${v.toFixed(digits)}%`
}

// ---- 状态配色 ---------------------------------------------------------

interface StatusStyle {
  /** 状态文字 + 标签背景 (Tailwind classes) */
  badgeBg: string
  badgeText: string
  /** 进度条填充色 */
  barBg: string
  /** 行 hover 留白, 全行点击 */
  rowHover: string
}

const STYLE: Record<AbnormalStatus, StatusStyle> = {
  triggered: {
    badgeBg: 'bg-rose-500/15 border-rose-500/30',
    badgeText: 'text-rose-700 dark:text-rose-400',
    barBg: 'bg-rose-500',
    rowHover: 'bg-rose-500/[0.04]',
  },
  edge: {
    badgeBg: 'bg-orange-500/15 border-orange-500/30',
    badgeText: 'text-orange-700 dark:text-orange-400',
    barBg: 'bg-orange-500',
    rowHover: 'bg-orange-500/[0.04]',
  },
  watch: {
    badgeBg: 'bg-amber-500/15 border-amber-500/30',
    badgeText: 'text-amber-700 dark:text-amber-400',
    barBg: 'bg-amber-500',
    rowHover: 'bg-amber-500/[0.04]',
  },
  normal: {
    badgeBg: 'bg-emerald-500/10 border-emerald-500/30',
    badgeText: 'text-emerald-700 dark:text-emerald-400',
    barBg: 'bg-emerald-500',
    rowHover: 'bg-emerald-500/[0.04]',
  },
  unknown: {
    badgeBg: 'bg-slate-500/10 border-slate-500/30',
    badgeText: 'text-slate-600 dark:text-slate-400',
    barBg: 'bg-slate-400',
    rowHover: 'bg-slate-500/[0.04]',
  },
}

const STATUS_LABEL: Record<AbnormalStatus, string> = {
  triggered: '已触发',
  edge: '边缘',
  watch: '观察',
  normal: '正常',
  unknown: '数据不足',
}

function statusStyle(s: AbnormalStatus | string): StatusStyle {
  return STYLE[(s as AbnormalStatus)] ?? STYLE.unknown
}

/** 最接近的窗口规则: worst; 否则 normal windows[0]. */
function worstLabel(row: AbnormalRow): string {
  if (row.worst && row.worst.rule_key) {
    const w = row.worst.window
    return `${w}日 / ${row.worst.severity === 'severe' ? '严重' : '板异'}`
  }
  return '无'
}

// ---- 行组件 -----------------------------------------------------------

interface RowProps {
  row: AbnormalRow
  onOpen: (symbol: string, row: AbnormalRow) => void
}

function Row({ row, onOpen }: RowProps) {
  const status = (row.status as AbnormalStatus) ?? 'unknown'
  const style = statusStyle(status)
  // 进度条 [0~100], 过 100% 截断
  const proxPct = row.proximity != null ? Math.max(0, Math.min(100, row.proximity * 100)) : 0
  const worst = row.worst
  const dir = worst?.direction ?? 'na'
  const deviation = worst?.deviation_pct ?? null
  const threshold = worst?.threshold_used ?? null

  return (
    <button
      type="button"
      onClick={() => onOpen(row.symbol, row)}
      className={`grid grid-cols-[68px_1fr_70px_auto_88px_64px] items-center gap-2 px-2 py-2 rounded-lg border border-transparent hover:border-border/60 hover:${style.rowHover} text-left transition-colors w-full`}
      title={`${row.symbol} ${row.name ?? ''} · 基准 ${row.benchmark?.name ?? '—'}`}
    >
      {/* 代码 */}
      <span className="font-mono text-[12px] text-foreground">{row.symbol}</span>

      {/* 名称 + 板块 + 来源 */}
      <div className="min-w-0">
        <div className="text-[12px] font-medium text-foreground truncate">{row.name || row.symbol}</div>
        <div className="text-[10px] text-muted-foreground flex items-center gap-1">
          <span>{row.board_name || '—'}</span>
          {row.source && row.source !== 'watchlist' ? (
            <span className="px-1 rounded bg-accent/40 text-[9px]">竞池</span>
          ) : null}
        </div>
      </div>

      {/* 最接近的窗口规则 */}
      <span className="text-[10.5px] text-muted-foreground font-mono truncate">
        {worstLabel(row)}
      </span>

      {/* 偏离 + 阈值 + 进度条 */}
      <div className="min-w-[88px]">
        <div className="flex items-baseline gap-1 text-[11px] font-mono">
          <span className={
            dir === 'down'
              ? 'text-emerald-700 dark:text-emerald-400'
              : dir === 'up'
                ? 'text-rose-700 dark:text-rose-400'
                : 'text-muted-foreground'
          }>
            {fmtSignedZero(deviation)}
          </span>
          <span className="text-[10px] text-muted-foreground">/{threshold != null ? threshold.toFixed(0) : '—'}%</span>
        </div>
        <div className="h-1.5 rounded-full bg-accent/40 overflow-hidden mt-1">
          <div
            className={`h-full ${style.barBg}`}
            style={{ width: `${proxPct}%` }}
          />
        </div>
      </div>

      {/* 接近度数值 */}
      <div className="text-[11px] font-mono text-right">
        <span className={style.badgeText}>
          {row.proximity != null ? (row.proximity * 100).toFixed(0) + '%' : '—'}
        </span>
      </div>

      {/* 状态标签 */}
      <div className={`justify-self-end px-1.5 py-0.5 rounded border text-[10.5px] font-medium ${style.badgeBg} ${style.badgeText}`}>
        {STATUS_LABEL[status]}
      </div>
    </button>
  )
}

// ---- 主组件 -----------------------------------------------------------

export default function AbnormalMovesCard({
  minProximity = 0.5,
  onOpenDetail,
  intervalMs = DEFAULT_INTERVAL_MS,
}: AbnormalMovesCardProps) {
  const [data, setData] = useState<AbnormalMovesResp | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string>('')
  const [updatedAt, setUpdatedAt] = useState<Date | null>(null)
  const mountedRef = useRef(true)

  const handleOpen = useCallback(
    (symbol: string, row: AbnormalRow) => {
      if (onOpenDetail) {
        onOpenDetail(symbol, row)
        return
      }
      // 默认: 复用现有 stock-insight-modal 入口
      try {
        window.dispatchEvent(
          new CustomEvent('panwatch-open-stock-insight', {
            detail: { symbol, market: 'CN', pageContext: `异动接近度 ${row.proximity != null ? (row.proximity * 100).toFixed(0) + '%' : '—'} (${STATUS_LABEL[row.status as AbnormalStatus] ?? '—'})` },
          }),
        )
      } catch {
        /* 静默 */
      }
    },
    [onOpenDetail],
  )

  const load = useCallback(async () => {
    setError('')
    try {
      const res = await fetchAPI<AbnormalMovesResp>(
        `/abnormal-moves?min_proximity=${encodeURIComponent(String(minProximity))}`,
        { cacheMode: 'reload' },
      )
      if (mountedRef.current) {
        setData(res)
        setUpdatedAt(new Date())
      }
    } catch (e) {
      if (mountedRef.current) {
        setError(e instanceof Error ? e.message : '加载失败')
      }
    } finally {
      if (mountedRef.current) setLoading(false)
    }
  }, [minProximity])

  useEffect(() => {
    mountedRef.current = true
    setLoading(true)
    void load()
    const timer = window.setInterval(() => void load(), intervalMs)
    return () => {
      mountedRef.current = false
      window.clearInterval(timer)
    }
  }, [load, intervalMs])

  // 加载占位
  if (loading && !data) {
    return (
      <div className="rounded-xl border border-border/50 bg-card p-3">
        <div className="flex items-center gap-1.5 mb-2">
          <Zap className="w-3.5 h-3.5 text-muted-foreground" />
          <span className="text-[13px] font-semibold text-foreground">异动接近度</span>
        </div>
        <div className="space-y-1.5">
          {[1, 2, 3].map((i) => (
            <div key={i} className="h-10 rounded-lg bg-accent/30 animate-pulse" />
          ))}
        </div>
      </div>
    )
  }

  // 错误占位 (失败时仍展示上次成功数据, 这里 data 为 null 时给错误条)
  const showError = !!error && !data

  return (
    <div className="rounded-xl border border-border/50 bg-card p-3">
      <div className="flex items-center justify-between gap-2 mb-2">
        <div className="flex items-center gap-1.5">
          <Zap className="w-3.5 h-3.5 text-amber-500" />
          <span className="text-[13px] font-semibold text-foreground">异动接近度</span>
          <span className="text-[10px] text-muted-foreground ml-1">
            阈值 ≥{(minProximity * 100).toFixed(0)}%
          </span>
          {updatedAt && (
            <span className="text-[10px] text-muted-foreground font-mono ml-2">
              {updatedAt.toLocaleTimeString('zh-CN', { hour12: false })}
            </span>
          )}
        </div>
        <button
          type="button"
          title="刷新"
          onClick={() => {
            setLoading(true)
            void load()
          }}
          className="text-muted-foreground hover:text-foreground transition-colors"
        >
          <RefreshCw className={`w-3.5 h-3.5 ${loading ? 'animate-spin' : ''}`} />
        </button>
      </div>

      {showError ? (
        <div className="flex items-center gap-2 text-[12px] text-amber-700 dark:text-amber-400 bg-amber-500/10 border border-amber-500/20 rounded-lg px-3 py-2">
          <ShieldAlert className="w-3.5 h-3.5 shrink-0" />
          <span>异动监控暂不可用({error})</span>
        </div>
      ) : !data || !data.available || data.count === 0 ? (
        <div className="flex items-center gap-2 text-[12px] text-muted-foreground">
          <AlertTriangle className="w-3.5 h-3.5" />
          <span>当前无接近交易所异动阈值的标的 (proximity &lt; {(minProximity * 100).toFixed(0)}%)</span>
        </div>
      ) : (
        <>
          <div className="text-[10.5px] text-muted-foreground mb-1.5">
            按最接近阈值的规则倒序; <span className="text-rose-700 dark:text-rose-400">红色=已触发</span> 交易所将发异动公告; <span className="text-orange-700 dark:text-orange-400">橙=边缘</span> 次日再涨即触发; <span className="text-amber-700 dark:text-amber-400">黄=观察</span>
          </div>
          <div className="space-y-0.5 max-h-[420px] overflow-y-auto">
            {data.items.map((row) => (
              <Row key={row.symbol} row={row} onOpen={handleOpen} />
            ))}
          </div>
          {data.note && (
            <div className="mt-2 text-[10px] text-muted-foreground">{data.note}</div>
          )}
        </>
      )}
    </div>
  )
}
