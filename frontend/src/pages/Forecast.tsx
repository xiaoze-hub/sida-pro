import { useEffect, useRef, useState } from 'react'
import { TrendingUp, LineChart, RefreshCw, Activity, Download, History, FileText, Send } from 'lucide-react'
import SafeMarkdown from '@/components/SafeMarkdown'
import { fetchAPI, getToken, stocksApi, type StockItem } from '@panwatch/api'
import { Button } from '@panwatch/base-ui/components/ui/button'
import { Input } from '@panwatch/base-ui/components/ui/input'
import { Label } from '@panwatch/base-ui/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@panwatch/base-ui/components/ui/select'
import { useToast } from '@panwatch/base-ui/components/ui/toast'
import { usePausedInterval } from '@panwatch/base-ui'
import { safeFixed, safeNum } from '@/lib/format'
import ForecastConeChart from '@/components/ForecastConeChart'
import { AiRefereePanel, RefereeStatsCard, type AiRefereeInfo, type RefereeStats } from '@/components/AiRefereePanel'

interface KronosResult {
  median: number[]
  p5: number[]
  p95: number[]
  n_samples: number
}

interface PredictResult {
  symbol: string
  stock_name?: string
  last_close: number
  last_date: string
  pred_days: number
  prediction: number[]
  direction: string
  expected_pct: number
  models: {
    kronos: KronosResult
    chronos?: { median: number[]; p10: number[]; p90: number[]; n_samples: number } | null
    xgboost: number[] | null
    linreg: number[] | null
  }
  sentiment?: {
    events: { source: string; title?: string; text?: string; date?: string }[]
    market_sentiment?: { limit_up_count: number; top_sectors: { name: string; count: number }[] } | null
    adjustment_pct: number
    notes: string[]
  }
  // AI 裁判结论(P0-2): 引擎 /predict 响应字段。verdict=adjust 时 direction 已覆盖
  // 最终方向; 旧响应可能缺该字段 ⇒ 组件侧显式『无裁判结论』。
  ai_referee?: AiRefereeInfo | null
  recommendation?: {
    action: string
    tone: string
    confidence: string
    risk_note?: string
    target_price: number
    expected_pct: number
    stop_loss: number
    summary: string
  }
  elapsed_ms: number
}

interface ForecastHistoryItem {
  id: number
  symbol: string
  stock_name?: string
  last_close: number
  last_date: string
  target_date?: string
  pred_days: number
  direction: string
  expected_pct: number
  prediction: number[]
  action: string
  confidence: string
  target_price: number
  stop_loss: number
  summary: string
  sentiment_adj: number
  created_at: string
  // 到期对照(2026-09-29 落地): 应用侧 /forecast/history 已按 target_date 对照实际行情补以下字段
  // (实现见 src/core/forecast_outcome.py): outcome_return_pct=实际涨跌幅%,
  // outcome_status='hit'|'miss'|'pending'|'no_data'(取不到行情=null + 'no_data', 不填 0)。
  // 前端历史表据此自动展示"到期对照"列, 无需再改这里。
  outcome_return_pct?: number
  outcome_status?: 'hit' | 'miss' | 'pending' | string
}

interface BacktestResult {
  symbol: string
  windows_tested: number
  direction_hits: number
  direction_accuracy_pct: number
  recent_samples: { date: string; pred_close: number; actual_close: number; hit: boolean }[]
}

interface PredictionReport {
  report_id: number
  run_id: number
  symbol: string
  dashboard_md: string
  detail_md: string
}

interface BacktestReport {
  report_id: number
  backtest_id: number
  symbol: string
  dashboard_md: string
  detail_md: string
}

/**
 * 到期对照判定(2026-10-24 修 P0): 后端四态 `hit|miss|pending|no_data`
 * (见 `src/core/forecast_outcome.py::_judge_status` / `evaluate_history_item`)。
 *
 * 旧逻辑只分 `hit` / `!hit` 两支, 于是 `no_data`(到期但取不到行情)被画成 `✗` ——
 * **把缺失数据当结论**, 违反「缺失必显式标无数据」铁律。这里把 `no_data` 提成独立态:
 *   - `pending`  目标日未到 / 未过收盘 → 未到期, **不进命中率分母**
 *   - `no_data`  到期但行情缺失 / 方向无法判定 → 无数据, **不进命中率分母**, 中性灰印章
 *   - `hit`/`miss` 方向兑现 / 未兑现 → 只有这两态进分母
 *   - `none`     后端连 outcome 字段都没给 → 不是一次评估, 显示 `—`
 */
export type OutcomeKind = 'hit' | 'miss' | 'pending' | 'no_data' | 'none'

/** 判定输入(字段全可选: 后端各字段都可能缺; 缺就按缺失处理, 不补默认值)。 */
export interface OutcomeInput {
  direction?: string
  outcome_status?: string
  outcome_return_pct?: number
}

/** 方向 vs 实际涨跌幅 → hit/miss; 方向不可识别或无涨跌幅 → no_data(与后端 `_judge_status` 同口径)。 */
export function judgeOutcomeByDirection(direction?: string, pct?: number): OutcomeKind {
  if (typeof pct !== 'number' || !Number.isFinite(pct)) return 'no_data'
  const dir = String(direction ?? '').trim().toLowerCase()
  if (dir === 'up') return pct > 0 ? 'hit' : 'miss'
  if (dir === 'down') return pct < 0 ? 'hit' : 'miss'
  if (dir === 'flat') return pct === 0 ? 'hit' : 'miss'
  return 'no_data'
}

/**
 * 单条历史记录的到期对照态: 优先后端 `outcome_status`; 后端没给时才按
 * 「预测方向 vs 实际涨跌」自行推算(此时若涨跌幅缺失 → no_data, 不猜 hit/miss)。
 */
export function resolveOutcome(h: OutcomeInput): OutcomeKind {
  const status = String(h.outcome_status ?? '').trim().toLowerCase()
  if (status === 'hit' || status === 'miss' || status === 'pending' || status === 'no_data') return status
  // 未知状态串一律按「无数据」处理, 绝不退化成 hit/miss
  if (status) return 'no_data'
  if (typeof h.outcome_return_pct !== 'number' || !Number.isFinite(h.outcome_return_pct)) return 'none'
  return judgeOutcomeByDirection(h.direction, h.outcome_return_pct)
}

/**
 * 到期对照汇总。**命中率分母 = hit + miss**, `pending` 与 `no_data` 一律排除并单独计数
 * (样本数铁律: 只报命中率等于隐去分母)。`hitRatePct=null` 表示没有任何已判定样本,
 * 调用方须显示「暂无已判定记录」而不是 0%。
 */
export function summarizeOutcomes(items: OutcomeInput[]) {
  const s = { hit: 0, miss: 0, pending: 0, noData: 0 }
  for (const it of items) {
    const k = resolveOutcome(it)
    if (k === 'hit') s.hit += 1
    else if (k === 'miss') s.miss += 1
    else if (k === 'pending') s.pending += 1
    else if (k === 'no_data') s.noData += 1
  }
  const judged = s.hit + s.miss
  return { ...s, judged, hitRatePct: judged > 0 ? (s.hit / judged) * 100 : null }
}

/** 权重展示顺序(与 forecast_lib/model_weights.py 的 MODEL_NAMES 对齐, 含 timesfm)。 */
const WEIGHT_MODEL_ORDER: { key: string; label: string }[] = [
  { key: 'kronos', label: 'Kronos' },
  { key: 'chronos', label: 'Chronos' },
  { key: 'xgboost', label: 'XGB' },
  { key: 'linreg', label: '线性回归' },
  { key: 'timesfm', label: 'TimesFM' },
]

export interface WeightRow { key: string; label: string; pct: number }

/**
 * 权重行: **只渲染 `/forecast/weights` 真实返回的数值**; 接口没给就返回空数组。
 *
 * 禁止回落历史回测快照(kronos 0.4414 / chronos 0.3379 / xgboost 0.1103 / linreg 0.1103,
 * 2026-08-13 backtest)。那组数字与当前模型无关, 却刚好四行合计 100.00%, 视觉上完全自洽,
 * 属最难被发现的「假精确」(见 docs/design/20261023_04 §1.2)。
 */
export function buildWeightRows(weights: Record<string, number> | null): WeightRow[] {
  if (!weights) return []
  const rows: WeightRow[] = []
  for (const { key, label } of WEIGHT_MODEL_ORDER) {
    const v = weights[key]
    if (typeof v === 'number' && Number.isFinite(v)) rows.push({ key, label, pct: v * 100 })
  }
  return rows
}

/**
 * 权重透明度区块(权重区 UI 唯一出口, 便于独立回归)。
 *
 * 三态: loading → 「权重加载中…」; unavailable → 「权重不可用（接口未就绪）」+ 一行
 * 「历史参考快照仅用于回测，不代表当前权重」; ok → 只渲染接口返回的真实百分比。
 */
export function WeightTransparency({
  weights,
  source,
  state,
}: {
  weights: Record<string, number> | null
  source: string
  state: 'loading' | 'ok' | 'unavailable'
}) {
  const weightRows = buildWeightRows(weights)
  if (weightRows.length === 0) {
    return (
      <div className="mb-2">
        <div className="flex flex-wrap items-center gap-x-1.5 text-xs text-muted-foreground">
          <span>当前模型权重</span>
          <span className="text-[11px] font-medium text-muted-foreground">
            {state === 'loading' ? '权重加载中…' : '权重不可用（接口未就绪）'}
          </span>
        </div>
        {state !== 'loading' && (
          <div className="mt-0.5 text-[11px] text-muted-foreground/80">
            历史参考快照仅用于回测，不代表当前权重
          </div>
        )}
      </div>
    )
  }
  const weightSum = weightRows.reduce((acc, r) => acc + r.pct, 0)
  const sourceText =
    source === 'history'
      ? '实时回测统计'
      : source === 'file'
        ? '最近回测落盘'
        : source === 'default'
          ? '暂无回测数据, 使用引擎默认权重'
          : '权重来源未标注'
  return (
    <div className="mb-2 flex flex-wrap items-center gap-x-1.5 text-xs text-muted-foreground">
      <span>当前模型权重</span>
      {weightRows.map((r, i) => (
        <span key={r.key} className="flex items-center gap-x-1.5">
          {i > 0 && <span>·</span>}
          <span className="font-num font-medium tabular-nums text-foreground/80">
            {r.label} {safeFixed(r.pct, 1)}%
          </span>
        </span>
      ))}
      <span className="opacity-70">
        (已返回 {weightRows.length} 支 · 合计{' '}
        <span className="font-num tabular-nums">{safeFixed(weightSum, 1)}%</span> · {sourceText})
      </span>
    </div>
  )
}

/**
 * 到期对照印章(到期对照列的唯一出口)。
 * `no_data` → 中性灰虚线「无数据」; `pending` → 「待验证」; `none` → 「—」;
 * 只有 `hit`/`miss` 才出 ✓/✗。旧实现在 `!hit` 分支里把 no_data 画成 ✗。
 */
export function OutcomeBadge({ item }: { item: OutcomeInput & { expected_pct?: number } }) {
  const kind = resolveOutcome(item)
  const pct = item.outcome_return_pct
  const pctText = typeof pct === 'number' && Number.isFinite(pct) ? `${pct > 0 ? '+' : ''}${pct}%` : null
  const predText = `预测${
    typeof item.expected_pct === 'number' ? `${item.expected_pct > 0 ? '+' : ''}${item.expected_pct}%` : '无数据'
  }`
  // 未到期: 不是错也不是对, 不进命中率分母
  if (kind === 'pending') {
    return <span className="text-[11px] text-muted-foreground">待验证</span>
  }
  // 后端连 outcome 字段都没给: 一次评估都没发生, 不摆结论
  if (kind === 'none') {
    return <span className="text-[11px] text-muted-foreground">—</span>
  }
  // 无数据(取不到行情/方向不可判): 独立中性态 —— 灰虚线印章 + 「无数据」,
  // 不带任何涨跌色语义, 也不计入命中率分母。
  if (kind === 'no_data') {
    return (
      <span className="inline-flex flex-col items-end gap-0.5">
        <span className="rounded border border-dashed border-border/60 px-1.5 py-0.5 text-[10px] font-medium text-muted-foreground">
          无数据
        </span>
        <span className="font-num tabular-nums text-[10px] text-muted-foreground/80">
          {predText}
          {pctText && <> vs 实际{pctText}</>}
        </span>
      </span>
    )
  }
  const hit = kind === 'hit'
  return (
    <span className={hit ? 'text-emerald-500' : 'text-rose-500'}>
      {hit ? '✓' : '✗'}{' '}
      <span className="font-mono text-xs">
        {predText}
        {pctText && <> vs 实际{pctText}</>}
      </span>
    </span>
  )
}

/**
 * 四模型分歧度区间条图(纯 CSS/div):每模型一行,条 = median 起点→终点,
 * 宽度按全局价格区间(含基准价,上下 2% 边距)归一;Kronos 额外画 P5-P95 全区间阴影带。
 */
function ModelDivergenceChart({ result }: { result: PredictResult }) {
  const { models, last_close } = result
  interface ModelRow { label: string; sub?: string; start: number; end: number; band?: { lo: number; hi: number } }
  const rows: ModelRow[] = []
  const kronos = models.kronos
  rows.push({
    label: 'Kronos',
    sub: `MC${kronos.n_samples}采样`,
    start: kronos.median[0],
    end: kronos.median[kronos.median.length - 1],
    // 全预测区间 P5 下沿 ~ P95 上沿 = 采样路径总离散度
    band: { lo: Math.min(...kronos.p5), hi: Math.max(...kronos.p95) },
  })
  if (models.chronos) {
    const m = models.chronos.median
    rows.push({ label: 'Chronos-Bolt', sub: '时序基础模型', start: m[0], end: m[m.length - 1] })
  }
  if (models.xgboost) rows.push({ label: 'XGBoost', start: models.xgboost[0], end: models.xgboost[models.xgboost.length - 1] })
  if (models.linreg) rows.push({ label: '线性回归', start: models.linreg[0], end: models.linreg[models.linreg.length - 1] })

  // 归一基准:全部端点 + 基准价,上下各留 2% 边距,避免贴边
  const allPts = rows.flatMap(r => [r.start, r.end, ...(r.band ? [r.band.lo, r.band.hi] : [])])
  const lo = Math.min(last_close, ...allPts)
  const hi = Math.max(last_close, ...allPts)
  const span = hi - lo || 1
  const min = lo - span * 0.02
  const max = hi + span * 0.02
  const pct = (v: number) => ((v - min) / (max - min)) * 100
  const anchorPct = pct(last_close)

  return (
    <div className="space-y-1.5 text-[13px]">
      {/* 基准价锚点刻度 */}
      <div className="relative h-4 text-[10px] text-muted-foreground">
        <div className="absolute inset-y-0 border-l border-dashed border-muted-foreground/40" style={{ left: `${anchorPct}%` }}>
          <span className="ml-1.5">基准 {last_close}</span>
        </div>
      </div>
      {rows.map(r => {
        const up = r.end >= r.start
        const leftPct = pct(r.start)
        const widthPct = Math.max(pct(r.end) - leftPct, 0)
        return (
          <div key={r.label} className="flex items-center gap-2">
            <span className="w-24 shrink-0 truncate text-[11px] text-muted-foreground">
              {r.label}
              {r.sub && <span className="ml-0.5 text-[10px] opacity-70">({r.sub})</span>}
            </span>
            <div className="relative h-5 min-w-0 flex-1 rounded bg-muted/50">
              {/* Kronos P5-P95 不确定性阴影带 */}
              {r.band && (
                <div
                  className="absolute inset-y-0.5 rounded bg-primary/15"
                  style={{ left: `${pct(r.band.lo)}%`, width: `${Math.max(pct(r.band.hi) - pct(r.band.lo), 0)}%` }}
                />
              )}
              {/* median 起点→终点条(红涨绿跌) */}
              <div
                className={`absolute inset-y-1 rounded ${up ? 'bg-stock-up' : 'bg-stock-down'}`}
                style={{ left: `${leftPct}%`, width: `${widthPct}%`, minWidth: 2 }}
              />
              {/* 基准价锚点竖线 */}
              <div className="absolute inset-y-0 w-px bg-border/80" style={{ left: `${anchorPct}%` }} />
            </div>
            <span className="w-28 shrink-0 truncate text-right font-mono text-[11px]">
              {safeFixed(r.start)} → {safeFixed(r.end)}
              {r.band && (
                <span className="block text-[10px] text-muted-foreground/80">
                  P5 {safeFixed(r.band.lo)} ~ P95 {safeFixed(r.band.hi)}
                </span>
              )}
            </span>
          </div>
        )
      })}
    </div>
  )
}

export default function ForecastPage({ initialSymbol }: { initialSymbol?: string } = {}) {
  const [symbol, setSymbol] = useState(initialSymbol ?? '')
  const [days] = useState(5)
  const [searchText, setSearchText] = useState('')
  const [searchResults, setSearchResults] = useState<{ symbol: string; name: string }[]>([])
  const [stockName, setStockName] = useState('')
  const [targetDate, setTargetDate] = useState('')
  const [lastKlineDate] = useState('')
  const [loading, setLoading] = useState(false)
  const [result, setResult] = useState<PredictResult | null>(null)
  const [backtest, setBacktest] = useState<BacktestResult | null>(null)
  const [engineStatus, setEngineStatus] = useState<'checking' | 'ok' | 'down'>('checking')
  const [taskStatus, setTaskStatus] = useState('')
  const [taskLogs, setTaskLogs] = useState<string[]>([])
  const [history, setHistory] = useState<ForecastHistoryItem[]>([])
  const [historyLoading, setHistoryLoading] = useState(false)
  // 自选股(一键填入搜索框): stocksApi.list() → GET /stocks(用户自选+全局)
  const [watchlist, setWatchlist] = useState<StockItem[]>([])
  const [watchlistLoading, setWatchlistLoading] = useState(false)
  const [watchPick, setWatchPick] = useState('')
  const [detail, setDetail] = useState<ForecastHistoryItem | null>(null)
  const [report, setReport] = useState<PredictionReport | null>(null)
  const [reportLoading, setReportLoading] = useState(false)
  // P1(audit-20260915): 组件卸载守卫 — runPredict 的 while 轮询循环依赖此标志退出
  const pageCancelledRef = useRef(false)
  const [backtestReport, setBacktestReport] = useState<BacktestReport | null>(null)
  const [showDetail, setShowDetail] = useState(false)
  const [modelWeights, setModelWeights] = useState<Record<string, number> | null>(null)
  const [weightsSource, setWeightsSource] = useState('')
  // 权重三态: loading(请求中) / ok(拿到真实值) / unavailable(接口未就绪或无权重)。
  // 不再有「静默回退硬编码快照」这一态 —— 拿不到就显式说拿不到。
  const [weightsState, setWeightsState] = useState<'loading' | 'ok' | 'unavailable'>('loading')
  // 裁判战绩卡片(P0-2): 消费 /api/forecast/referee-stats(代理引擎 referee_impact_stats)。
  const [refereeStats, setRefereeStats] = useState<RefereeStats | null>(null)
  const [refereeStatsLoading, setRefereeStatsLoading] = useState(false)
  const { toast } = useToast()

  // 股票搜索(输入名称/代码) — 用 数智分析 自带 /stocks/search(返回 list)
  const doSearch = async (q: string) => {
    if (!q.trim()) return
    try {
      const d = await fetchAPI<{ symbol: string; name: string }[] | { items: { symbol: string; name: string }[] }>(
        `/stocks/search?q=${encodeURIComponent(q)}`
      )
      // 数智分析 返回 list;兼容 {items} 结构;过滤 A 股主板(6/0 开头,排除 300/688/8xx/港股)
      const list = (Array.isArray(d) ? d : (d as any)?.items || []).filter((s: any) => {
        const sym = s.symbol || ''
        if (!/^\d{6}$/.test(sym)) return false
        return sym.startsWith('60') || sym.startsWith('000') || sym.startsWith('002')
      })
      setSearchResults(list)
    } catch {
      setSearchResults([])
    }
  }

  // 输入防抖搜索
  const handleSearchInput = (e: React.ChangeEvent<HTMLInputElement>) => {
    const v = e.target.value
    setSearchText(v)
    setStockName('')
    // 纯数字6位 = 直接当代码
    if (/^\d{6}$/.test(v)) {
      setSymbol(v)
      setSearchResults([])
      return
    }
    if (v.length >= 2) {
      setTimeout(() => doSearch(v), 300)
    } else {
      setSearchResults([])
    }
  }

  // 选中搜索结果
  const selectStock = (s: { symbol: string; name: string }) => {
    setSymbol(s.symbol)
    setStockName(s.name)
    setSearchText(`${s.name} ${s.symbol}`)
    setSearchResults([])
  }

  // 加载自选股列表(每次展开下拉时刷新, 保持最新)
  const loadWatchlist = async () => {
    setWatchlistLoading(true)
    try {
      const list = await stocksApi.list()
      setWatchlist(list || [])
    } catch {
      setWatchlist([])
    } finally {
      setWatchlistLoading(false)
    }
  }

  // 从自选下拉选中 → 复用 selectStock 填充搜索框并选中
  const pickFromWatchlist = (id: string) => {
    const s = watchlist.find(w => String(w.id) === id)
    if (s) selectStock({ symbol: s.symbol, name: s.name || s.symbol })
    setWatchPick('') // 重置, 下次打开仍是 placeholder
  }

  // 加载历史预测列表
  const loadHistory = async () => {
    setHistoryLoading(true)
    try {
      const d = await fetchAPI<{ items: ForecastHistoryItem[] }>(`/forecast/history?limit=30`)
      setHistory(d?.items || [])
    } catch {
      setHistory([])
    } finally {
      setHistoryLoading(false)
    }
  }

  // 导出历史预测 CSV(/api/export/predictions, 带 token 直接 fetch blob)
  const exportPredictions = async () => {
    try {
      const token = getToken()
      const res = await fetch('/api/export/predictions', {
        headers: token ? { Authorization: `Bearer ${token}` } : {},
      })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const blob = await res.blob()
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `预测记录_${new Date().toISOString().slice(0, 10)}.csv`
      document.body.appendChild(a)
      a.click()
      a.remove()
      URL.revokeObjectURL(url)
      toast('预测记录已导出', 'success')
    } catch (e) {
      toast(e instanceof Error ? e.message : '导出失败', 'error')
    }
  }

  useEffect(() => {
    // P1(audit-20260915): 挂载时复位, 卸载时置位 — 供 runPredict 轮询/异步回调提前退出
    pageCancelledRef.current = false
    return () => { pageCancelledRef.current = true }
  }, [])

  useEffect(() => { loadHistory() }, [])

  // 运行时拉取模型权重(权重透明度: 后端按历史回测命中率动态调整)
  // 2026-10-24: 删掉「失败回退硬编码快照」—— 那组 0.4414/0.3379/0.1103 与当前权重无关,
  // 却刚好合计 100%, 属假精确。取不到就置 unavailable, 由渲染处显式说明。
  useEffect(() => {
    let cancelled = false
    fetchAPI<{ weights: Record<string, number>; source?: string }>('/forecast/weights')
      .then(d => {
        if (cancelled) return
        if (d?.weights && Object.keys(d.weights).length > 0) {
          setModelWeights(d.weights)
          setWeightsSource(d.source || '')
          setWeightsState('ok')
        } else {
          setModelWeights(null)
          setWeightsState('unavailable')
        }
      })
      .catch(() => {
        if (cancelled) return
        setModelWeights(null)
        setWeightsState('unavailable')
      })
    return () => { cancelled = true }
  }, [])

  // P0-2: 裁判战绩(介入前后命中率) — 出结果后随标的拉取; 契约「永不 500」,
  // 网络层异常也降级成显式空态, 不阻塞页面。
  const loadRefereeStats = async (sym: string) => {
    if (!/^\d{6}$/.test(sym)) return
    setRefereeStatsLoading(true)
    try {
      const d = await fetchAPI<RefereeStats>(`/forecast/referee-stats?symbol=${sym}`)
      setRefereeStats(d)
    } catch (e: any) {
      setRefereeStats({ total: 0, symbol: sym, message: e?.message || '裁判战绩暂不可得' })
    } finally {
      setRefereeStatsLoading(false)
    }
  }

  // 依赖 result.symbol: 换标的重新拉; 未出结果时不请求(空态由卡片文案承载)
  useEffect(() => {
    if (result?.symbol) loadRefereeStats(result.symbol)
  }, [result?.symbol])

  // 检测引擎状态(可手动刷新调用)
  const checkEngine = () => {
    setEngineStatus('checking')
    fetchAPI<{ status: string }>('/forecast/health')
      .then(d => {
        // 引擎可能返回 {status: "unreachable"} (代理活着但引擎没起)
        setEngineStatus(d?.status === 'ok' ? 'ok' : 'down')
      })
      .catch(() => setEngineStatus('down'))
  }

  // 引擎状态轮询(引擎可能在页面打开后启动): 挂载补一次 + 每 30s 一次。
  // 页签 keep-alive(2026-10-15): 隐藏页签停表, 回前台补一次。
  usePausedInterval(checkEngine, 30000)

  const refreshEngine = () => checkEngine()

  // 下载预测卡片图片
  const downloadCard = async () => {
    // P1(audit-20260915): 统一走 getToken(), 空 token 中止避免 Bearer null; symbol 必须编码
    const token = getToken()
    if (!token) {
      toast('未登录或登录已过期,请重新登录', 'error')
      return
    }
    try {
      const res = await fetch(`/api/forecast/card?symbol=${encodeURIComponent(symbol)}`, {
        headers: { 'Authorization': `Bearer ${token}` },
      })
      if (!res.ok) throw new Error('卡片生成失败')
      const blob = await res.blob()
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `forecast_${symbol}_${new Date().toISOString().slice(0, 10)}.png`
      a.click()
      URL.revokeObjectURL(url)
      toast('卡片已下载', 'success')
    } catch (e: any) {
      toast(e?.message || '卡片下载失败', 'error')
    }
  }

  const runPredict = async () => {
    if (!/^\d{6}$/.test(symbol)) {
      toast('请输入 6 位股票代码', 'error')
      return
    }
    setLoading(true)
    setResult(null)
    setTaskLogs([])
    setTaskStatus('running')
    const tid = `task_${Date.now()}`
    try {
      // 并行: 启动预测 + 轮询进度
      // ⚠️ predict 需要长超时(Kronos MC30 推理 20-30s,默认 20s 会 abort)
      const targetParam = targetDate ? `&target_date=${targetDate}` : ''
      const predictPromise = fetchAPI<PredictResult>(
        `/forecast/predict?symbol=${symbol}&days=${days}&task_id=${tid}${targetParam}`,
        { timeoutMs: 300000 }
      )
      let pollDone = false
      void (async () => {
        while (!pollDone) {
          // P1(audit-20260915): 组件卸载后停止轮询, 避免 setState on unmounted
          if (pageCancelledRef.current) return
          await new Promise(res => setTimeout(res, 1500))
          if (pageCancelledRef.current) return
          try {
            const s = await fetchAPI<any>(`/forecast/predict/status?task_id=${tid}`)
            if (pageCancelledRef.current) return
            if (s?.logs) setTaskLogs([...s.logs])
            if (s?.status === 'done') {
              setTaskStatus('done')
              setResult(s.result)
              pollDone = true
              break
            }
            if (s?.status === 'error' || s?.status === 'not_found') {
              setTaskStatus(s.status)
              break
            }
          } catch { /* 忽略轮询错误 */ }
        }
      })()
      const d = await predictPromise
      pollDone = true
      if (!pageCancelledRef.current) {
        setResult(d)
        setTaskStatus('done')
        loadHistory()
      }
    } catch (e: any) {
      // P1(audit-20260915): 卸载后不再 toast/setState
      if (!pageCancelledRef.current) {
        toast(e?.message || '预测失败(请检查股票代码是否正确)', 'error')
        setTaskStatus('error')
      }
    } finally {
      if (!pageCancelledRef.current) setLoading(false)
    }
  }

  const runBacktest = async () => {
    if (!/^\d{6}$/.test(symbol)) {
      toast('请输入 6 位股票代码', 'error')
      return
    }
    setLoading(true)
    try {
      const d = await fetchAPI<BacktestResult>(`/forecast/backtest?symbol=${symbol}`)
      setBacktest(d)
    } catch (e: any) {
      toast(e?.message || '回测失败', 'error')
    } finally {
      setLoading(false)
    }
  }

  // 生成预测报告(双格式) — 调用 8000 代理 /forecast/report/generate
  const runGenerateReport = async (tid: string = '') => {
    // 优先用预测结果的 6 位代码(输入框可能是中文名)
    const sym = result?.symbol || symbol
    if (!/^\d{6}$/.test(sym)) {
      toast('请先完成一次预测再生成报告', 'error')
      return
    }
    setReportLoading(true)
    try {
      const q = tid ? `&task_id=${tid}` : ''
      const d = await fetchAPI<PredictionReport>(
        `/forecast/report/generate?symbol=${sym}${q}`,
        { timeoutMs: 180000 }
      )
      setReport(d)
      setShowDetail(false)
      toast('报告已生成', 'success')
    } catch (e: any) {
      toast(e?.message || '报告生成失败', 'error')
    } finally {
      setReportLoading(false)
    }
  }

  // 生成回测报告(双格式)
  const runGenerateBacktestReport = async () => {
    const sym = result?.symbol || symbol
    if (!/^\d{6}$/.test(sym)) {
      toast('请先完成一次预测再生成回测报告', 'error')
      return
    }
    setReportLoading(true)
    try {
      const d = await fetchAPI<BacktestReport>(
        `/forecast/report/backtest?symbol=${sym}`,
        { timeoutMs: 180000 }
      )
      setBacktestReport(d)
      toast('回测报告已生成', 'success')
    } catch (e: any) {
      toast(e?.message || '回测报告生成失败', 'error')
    } finally {
      setReportLoading(false)
    }
  }

  // 推送报告到企微(通过 Hermes webhook 中转)
  const pushReportToWeCom = async (kind: 'prediction' | 'backtest') => {
    const r = kind === 'prediction' ? report : backtestReport
    if (!r) return
    try {
      const res = await fetchAPI<{ ok: boolean; message?: string }>('/forecast/report/push', {
        method: 'POST',
        body: JSON.stringify({
          kind,
          symbol: r.symbol,
          dashboard_md: r.dashboard_md,
          detail_md: r.detail_md,
        }),
      })
      if (res?.ok) {
        toast('已推送到企业微信', 'success')
      } else {
        toast(res?.message || '推送失败', 'error')
      }
    } catch (e: any) {
      toast(e?.message || '推送失败', 'error')
    }
  }

  const dirColor = (dir: string) =>
    dir === 'up' ? 'text-stock-up' : dir === 'down' ? 'text-stock-down' : 'text-muted-foreground'

  // 到期对照: 仅当后端 /forecast/history 返回 outcome 字段时展示该列(见 ForecastHistoryItem)
  const historyHasOutcome = history.some(
    h => h.outcome_return_pct !== undefined || h.outcome_status !== undefined
  )
  // 到期对照汇总: 命中率分母只含 hit/miss; pending(未验证)与 no_data(无数据)单独计数
  const outcomeSummary = summarizeOutcomes(history)
  const outcomeSummaryTitle =
    outcomeSummary.judged > 0
      ? `已判定 ${outcomeSummary.judged} 条: 命中 ${outcomeSummary.hit} / 未命中 ${outcomeSummary.miss}` +
        `（${safeFixed(outcomeSummary.hitRatePct, 0)}%）; 无数据 ${outcomeSummary.noData} 条、待验证 ` +
        `${outcomeSummary.pending} 条不计入分母`
      : `暂无已判定记录; 无数据 ${outcomeSummary.noData} 条、待验证 ${outcomeSummary.pending} 条不计入分母`
  const renderOutcome = (h: ForecastHistoryItem) => <OutcomeBadge item={h} />

  return (
    <div className="sida-page-enter space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-[20px] font-bold flex items-center gap-2">
            <TrendingUp className="h-6 w-6" /> 预测回测
          </h1>
          <p className="text-[13px] text-muted-foreground mt-1">
            Kronos + Chronos-Bolt + XGBoost + 线性回归 四模型加权投票预测（AI裁判评估，数据源：baostock 不复权）
          </p>
        </div>
        <div className="flex items-center gap-2 text-[13px]">
          <span className={`h-2.5 w-2.5 rounded-full ${engineStatus === 'ok' ? 'bg-green-500' : engineStatus === 'down' ? 'bg-red-500' : 'bg-yellow-400'}`} />
          <span className="text-muted-foreground">
            预测引擎 {engineStatus === 'ok' ? '运行中' : engineStatus === 'down' ? '未启动' : '检测中'}
          </span>
          <Button
            variant="ghost"
            size="sm"
            className="h-8 px-2"
            onClick={refreshEngine}
            disabled={engineStatus === 'checking'}
            title="重新检测引擎状态"
          >
            <RefreshCw className={`h-3.5 w-3.5 ${engineStatus === 'checking' ? 'animate-spin' : ''}`} />
          </Button>
        </div>
      </div>

      {/* 三列工作台(设计蓝本): 输入(窄) | 结果/模型对比(宽); 移动端堆叠 */}
      <div className="grid grid-cols-1 xl:grid-cols-12 gap-4 items-start">
      {/* 输入区 */}
      <div className="border-b border-border/60 pb-4 xl:col-span-3 xl:border-b-0 xl:border-r xl:pb-0 xl:pr-4">
        <div className="mb-3">
          <div className="text-[16px] font-bold">发起预测</div>
        </div>
        <div className="flex flex-wrap items-end gap-4">
          <div className="space-y-1.5">
            <Label>股票</Label>
            <div className="relative">
              <Input
                value={searchText}
                onChange={handleSearchInput}
                onFocus={() => searchText && doSearch(searchText)}
                placeholder="输入名称或代码,如 神剑/002361"
                className="w-48"
                disabled={loading}
              />
              {searchResults.length > 0 && (
                <div className="absolute z-20 mt-1 w-56 max-h-56 overflow-y-auto bg-popover border rounded-md shadow-float">
                  {searchResults.map((s, i) => (
                    <button
                      key={i}
                      type="button"
                      className="w-full text-left px-3 py-2 hover:bg-muted/50 text-[13px] flex justify-between"
                      onClick={() => selectStock(s)}
                    >
                      <span>{s.name}</span>
                      <span className="font-mono text-muted-foreground">{s.symbol}</span>
                    </button>
                  ))}
                </div>
              )}
            </div>
            {stockName && (
              <div className="text-xs text-muted-foreground mt-1">已选：{symbol} {stockName}</div>
            )}
          </div>
          <div className="space-y-1.5">
            <Label>从自选选择</Label>
            <Select
              value={watchPick}
              onValueChange={pickFromWatchlist}
              onOpenChange={open => { if (open) loadWatchlist() }}
              disabled={loading}
            >
              <SelectTrigger className="w-40">
                <SelectValue placeholder="选择自选股" />
              </SelectTrigger>
              <SelectContent>
                {watchlistLoading && watchlist.length === 0 && (
                  <SelectItem value="__loading" disabled>加载中...</SelectItem>
                )}
                {!watchlistLoading && watchlist.length === 0 && (
                  <SelectItem value="__empty" disabled>暂无自选股（股票页添加后刷新）</SelectItem>
                )}
                {watchlist.map(w => (
                  <SelectItem key={w.id} value={String(w.id)}>
                    {w.name || w.symbol}
                    <span className="ml-2 font-mono text-muted-foreground">{w.symbol}</span>
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-1.5">
            <Label>预测至</Label>
            <Input
              type="date"
              value={targetDate}
              min={lastKlineDate ? lastKlineDate : undefined}
              onChange={e => setTargetDate(e.target.value)}
              className="w-36"
              disabled={loading}
            />
          </div>
          <Button onClick={runPredict} disabled={loading}>
            <Activity className="mr-2 h-4 w-4" /> {loading ? '预测中(约30-60s)...' : '开始预测'}
          </Button>
          <Button variant="outline" onClick={runBacktest} disabled={loading}>
            <LineChart className="mr-2 h-4 w-4" /> 历史回测
          </Button>
        </div>
      </div>

      {/* 预测进度日志 */}
      {(loading || taskLogs.length > 0) && (
        <div className="border-t border-border/60 pt-3 xl:col-span-9">
          <div className="flex items-center gap-2 mb-2">
            <span className={`h-2.5 w-2.5 rounded-full ${taskStatus === 'done' ? 'bg-green-500' : taskStatus === 'error' ? 'bg-red-500' : 'bg-blue-500 animate-pulse'}`} />
            <span className="font-medium">
              {taskStatus === 'done' ? '预测完成' : taskStatus === 'error' ? '预测失败' : '预测进行中...'}
            </span>
            {loading && <RefreshCw className="h-4 w-4 animate-spin text-muted-foreground" />}
          </div>
          <div className="bg-muted/50 rounded p-3 font-mono text-xs space-y-1 max-h-48 overflow-y-auto">
            {taskLogs.length === 0 ? (
              <div className="text-muted-foreground">正在连接预测引擎...</div>
            ) : (
              taskLogs.map((log, i) => (
                <div key={i} className="text-muted-foreground">{log}</div>
              ))
            )}
          </div>
        </div>
      )}

      {/* 预测结果 */}
      {result && (
        <div className="border-t border-border/60 pt-3 xl:col-span-9">
          <div className="mb-3">
            <div className="flex items-center justify-between">
              <span>预测结果：{result.symbol}{result.stock_name ? ` ${result.stock_name}` : ''}</span>
              <span className={`text-[16px] font-bold ${dirColor(result.direction)}`}>
                {/* 反AI模板⑤: 预测方向/幅度必须带"模型预测"限定,防既成事实表述(用户幻觉敏感) */}
                {/* P0-2 修订: 若裁判 adjust 已覆盖最终方向, 限定词改为"裁判调整后" —— 不能把裁判改过的方向标成纯模型输出 */}
                <span className="mr-1.5 align-middle text-xs font-medium text-muted-foreground">
                  {result.ai_referee?.verdict === 'adjust' && (result.ai_referee.direction === 'up' || result.ai_referee.direction === 'down')
                    ? '裁判调整后'
                    : '模型预测'}
                </span>
                {result.direction === 'up' ? '↑ 看多' : result.direction === 'down' ? '↓ 看空' : '→ 横盘'}
                {' '}({result.expected_pct > 0 ? '+' : ''}{result.expected_pct}%)
              </span>
              <span className="flex items-center gap-2">
                <Button variant="outline" size="sm" className="h-8" onClick={downloadCard}>
                  <Download className="mr-1 h-3.5 w-3.5" /> 下载卡片
                </Button>
                <Button
                  variant="outline"
                  size="sm"
                  className="h-8"
                  onClick={() => runGenerateReport('')}
                  disabled={reportLoading}
                >
                  <FileText className={`mr-1 h-3.5 w-3.5 ${reportLoading ? 'animate-spin' : ''}`} />
                  {reportLoading ? '生成中...' : '生成报告'}
                </Button>
              </span>
            </div>
          </div>
          <div className="space-y-4">
            <div className="text-[13px] text-muted-foreground">
              基准价 {result.last_close}（{result.last_date}）→ 预测 {result.pred_days} 天，耗时 {result.elapsed_ms}ms
              {(() => {
                const today = new Date().toISOString().slice(0, 10);
                if (result.last_date < today) {
                  return (
                    <span className="ml-2 text-yellow-500/80" title={`baostock 收盘后才入库当日数据;${result.last_date} 是最新可用历史,基准价已滞后 1 个交易日`}>
                      ⚠️ 基准日滞后今日 ({today})
                    </span>
                  );
                }
                return null;
              })()}
            </div>

            {/* 操作建议 */}
            {result.recommendation && (
              <div className={`rounded-md border px-4 py-3 ${result.direction === 'up' ? 'border-stock-up/30 bg-stock-up/5' : result.direction === 'down' ? 'border-stock-down/30 bg-stock-down/5' : 'border-gray-500/30 bg-gray-500/5'}`}>
                <div className="flex items-center gap-2 mb-1">
                  <span className="font-bold text-[16px]">操作建议：{result.recommendation.action}</span>
                  <span className={`text-xs px-2 py-0.5 rounded-full ${result.recommendation.confidence === '高' ? 'bg-emerald-500/15 text-emerald-500' : result.recommendation.confidence === '中' ? 'bg-amber-500/15 text-amber-500' : 'bg-rose-500/15 text-rose-500'}`}>
                    置信度{result.recommendation.confidence}
                  </span>
                </div>
                <div className="flex flex-wrap gap-x-6 gap-y-1 text-[13px] text-muted-foreground">
                  {/* 反AI模板⑤: 目标价/预期均为模型输出,标签带"模型"限定 */}
                  <span>模型目标价：<span className="font-num font-bold text-foreground tabular-nums">{result.recommendation.target_price}</span></span>
                  <span>止损参考：<span className="font-num font-bold text-foreground tabular-nums">{result.recommendation.stop_loss}</span></span>
                  <span>模型预期：<span className={`font-num font-bold tabular-nums ${result.expected_pct >= 0 ? 'text-stock-up' : 'text-stock-down'}`}>{result.expected_pct > 0 ? '+' : ''}{result.expected_pct}%</span></span>
                  {result.recommendation.risk_note && <span className="text-amber-500">{result.recommendation.risk_note}</span>}
                </div>
              </div>
            )}

            {/* 预测锥图(P1-5): 历史收盘 + 预测延伸 + P5-P95 带 */}
            <ForecastConeChart
              symbol={result.symbol}
              lastClose={result.last_close}
              lastDate={result.last_date}
              prediction={result.prediction}
              direction={result.direction}
              p5={result.models.kronos.p5}
              p95={result.models.kronos.p95}
            />

            {/* 预测价格序列 */}
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
            <div>
              <div className="text-[13px] font-medium mb-2">预测价格（综合投票）</div>
              <div className="flex flex-wrap gap-2">
                {result.prediction.map((p, i) => (
                  <div key={i} className="border border-border/50 rounded px-3 py-2 text-center">
                    <div className="text-xs text-muted-foreground">T+{i + 1}</div>
                    <div className={`font-num font-bold tabular-nums ${p > result.last_close ? 'text-stock-up' : 'text-stock-down'}`}>
                      {safeFixed(p)}
                    </div>
                    <div className="text-xs text-muted-foreground">
                      {(() => {
                        // 修复(M-8, 2026-08-23): last_close 可能为字符串/null, 原 .toFixed 抛 TypeError
                        const lc = safeNum(result.last_close)
                        const pp = safeNum(p)
                        if (lc === null || pp === null || lc === 0) return '--'
                        return `${((pp / lc - 1) * 100).toFixed(1)}%`
                      })()}
                    </div>
                  </div>
                ))}
              </div>
            </div>

            {/* 四模型对比 + 分歧度区间条图 */}
            <div>
              <div className="mb-2 flex items-center justify-between">
                <span className="text-[13px] font-medium">四模型对比</span>
                <span className={`text-xs font-medium ${dirColor(result.direction)}`}>
                  {/* 反AI模板⑤: 加权方向括号内标"模型预测" */}
                  模型加权方向 {result.direction === 'up' ? '↑ 看多' : result.direction === 'down' ? '↓ 看空' : '→ 横盘'} (模型预测 {result.expected_pct > 0 ? '+' : ''}{result.expected_pct}%)
                </span>
              </div>
              {/* 模型权重透明度: 5 模型投票权重(含 timesfm)。
                  数据源: 引擎 /forecast/weights(按历史回测命中率动态调整,
                  贝叶斯收缩 + 双写闭环, 见 forecast_lib/model_weights.py)。
                  2026-10-24 修 P0: 接口失败/未返回时**不展示任何数字** —— 旧代码用
                  `?? 0.4414 / 0.3379 / 0.1103 / 0.1103` 回落 2026-08-13 回测快照,
                  四行刚好合计 100%, 用户无法分辨那是历史快照而非当前权重。 */}
              <WeightTransparency weights={modelWeights} source={weightsSource} state={weightsState} />
              <ModelDivergenceChart result={result} />
            </div>
            </div>{/* 预测价格 + 四模型对比 两栏 grid 结束 */}

            {/* AI 裁判结论(P0-2): verdict/方向/理由全文; adjust 覆盖最终方向时显式标注 */}
            <AiRefereePanel referee={result.ai_referee} />

            {/* 裁判战绩卡片(P0-2): 介入前后命中率对比, 样本不足显式 */}
            <RefereeStatsCard stats={refereeStats} loading={refereeStatsLoading} />

            {/* 消息情绪面 */}
            {result.sentiment && (
              <div>
                <div className="text-[13px] font-medium mb-2">消息情绪面</div>
                <div className={`rounded px-3 py-2 text-[13px] mb-2 ${result.sentiment.adjustment_pct >= 0 ? 'bg-stock-up/10 text-stock-up' : 'bg-stock-down/10 text-stock-down'}`}>
                  情绪修正系数：{result.sentiment.adjustment_pct > 0 ? '+' : ''}{result.sentiment.adjustment_pct}%
                  {result.sentiment.notes?.length > 0 && (
                    <span className="text-muted-foreground ml-2 text-xs">
                      ({result.sentiment.notes.join('；')})
                    </span>
                  )}
                </div>
                {result.sentiment.market_sentiment && (
                  <div className="text-xs text-muted-foreground mb-2">
                    今日涨停 {result.sentiment.market_sentiment.limit_up_count} 家，
                    板块分布：{result.sentiment.market_sentiment.top_sectors?.map(s => `${s.name}×${s.count}`).join('、') || '无'}
                  </div>
                )}
                {result.sentiment.events?.length > 0 && (
                  <div className="text-xs space-y-1">
                    {result.sentiment.events.slice(0, 5).map((e, i) => (
                      <div key={i} className="flex gap-2">
                        <span className="text-muted-foreground shrink-0">[{e.source}]</span>
                        <span className="truncate">{e.title || String(e.text || '').slice(0, 60)}</span>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )}
          </div>
        </div>
      )}
      </div>{/* 三列工作台 grid 结束 */}

      {/* 预测报告（双格式） */}
      {report && (
        <div className="border-t border-border/60 pt-3 space-y-3">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-2">
              <FileText className="h-4 w-4" />
              <span className="text-[16px] font-bold">预测报告</span>
              <span className="text-xs text-muted-foreground">report_id #{report.report_id}</span>
            </div>
            <div className="flex items-center gap-2">
              <Button variant="outline" size="sm" className="h-8" onClick={() => setShowDetail(v => !v)}>
                {showDetail ? '收起完整版' : '查看完整版'}
              </Button>
              <Button
                variant="outline"
                size="sm"
                className="h-8"
                onClick={() => pushReportToWeCom('prediction')}
              >
                <Send className="mr-1 h-3.5 w-3.5" /> 推送企微
              </Button>
            </div>
          </div>
          {/* Dashboard 短版 */}
          <div className="border border-border/50 p-4">
            <div className="text-xs text-muted-foreground mb-2">精简版（Dashboard）</div>
            <div className="prose prose-sm dark:prose-invert max-w-none prose-headings:mt-2 prose-headings:mb-1 prose-p:my-1 prose-table:my-2 prose-th:px-2 prose-th:py-1 prose-td:px-2 prose-td:py-1 prose-table:text-[12px] prose-strong:text-foreground">
              <SafeMarkdown>{report.dashboard_md}</SafeMarkdown>
            </div>
          </div>
          {/* Detail 完整版（可折叠） */}
          {showDetail && (
            <div className="rounded-md border border-border/50 p-4">
              <div className="text-xs text-muted-foreground mb-2">完整版（Detail）</div>
              <div className="prose prose-sm dark:prose-invert max-w-none prose-headings:mt-3 prose-headings:mb-1 prose-p:my-1.5 prose-table:my-3 prose-th:px-3 prose-th:py-1.5 prose-td:px-3 prose-td:py-1.5 prose-table:text-[12px] prose-strong:text-foreground">
                <SafeMarkdown>{report.detail_md}</SafeMarkdown>
              </div>
            </div>
          )}
        </div>
      )}

      {/* 回测结果 */}
      {backtest && (
        <div className="border-t border-border/60 pt-3">
          <div className="mb-3">
            <div className="flex items-center justify-between">
              <div className="text-[16px] font-bold">回测结果：{backtest.symbol}</div>
              <Button
                variant="outline"
                size="sm"
                className="h-8"
                onClick={runGenerateBacktestReport}
                disabled={reportLoading}
              >
                <FileText className={`mr-1 h-3.5 w-3.5 ${reportLoading ? 'animate-spin' : ''}`} />
                生成回测报告
              </Button>
            </div>
          </div>
          <div className="space-y-4">
            <div className="flex gap-6">
              <div>
                <div className="text-[20px] font-bold">{backtest.direction_accuracy_pct}%</div>
                <div className="text-xs text-muted-foreground">方向命中率</div>
              </div>
              <div>
                <div className="text-[20px] font-bold">{backtest.direction_hits}/{backtest.windows_tested}</div>
                <div className="text-xs text-muted-foreground">命中/测试窗口</div>
              </div>
            </div>
            <div className="text-xs text-muted-foreground">
              注：回测统计的是模型方向命中率（预测方向 vs 实际方向），不含交易成本与收益测算；完整信号回测见本地回测内核。
            </div>
            {backtest.recent_samples.length > 0 && (
              <div className="overflow-x-auto">
                <table className="w-full text-[13px]">
                  <thead>
                    <tr className="border-b">
                      <th className="text-left py-2">日期</th>
                      <th className="text-right">预测</th>
                      <th className="text-right">实际</th>
                      <th className="text-right">方向</th>
                    </tr>
                  </thead>
                  <tbody>
                    {backtest.recent_samples.slice().reverse().map((s, i) => (
                      <tr key={i} className="border-b">
                        <td className="py-1.5">{s.date}</td>
                        <td className="text-right font-mono">{safeFixed(s.pred_close)}</td>
                        <td className="text-right font-mono">{safeFixed(s.actual_close)}</td>
                        <td className={`text-right ${s.hit ? 'text-emerald-500' : 'text-rose-500'}`}>
                          {s.hit ? '✓' : '✗'}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}

          {/* space-y-4 容器关闭 (backtest 结果内部) */}
          </div>

          {/* 回测报告（生成后显示） */}
          {backtestReport && (
            <div className="mt-4 space-y-3">
              <div className="flex items-center justify-between">
                <span className="text-[13px] font-medium">回测报告（双格式）</span>
                <Button
                  variant="outline"
                  size="sm"
                  className="h-8"
                  onClick={() => pushReportToWeCom('backtest')}
                >
                  <Send className="mr-1 h-3.5 w-3.5" /> 推送企微
                </Button>
              </div>
              <div className="border border-border/50 p-4">
                <div className="prose prose-sm dark:prose-invert max-w-none prose-headings:mt-2 prose-headings:mb-1 prose-p:my-1 prose-table:my-2 prose-th:px-2 prose-th:py-1 prose-td:px-2 prose-td:py-1 prose-table:text-[12px] prose-strong:text-foreground">
                  <SafeMarkdown>{backtestReport.dashboard_md}</SafeMarkdown>
                </div>
              </div>
              <details className="rounded-md border border-border/50 p-4">
                <summary className="cursor-pointer text-[13px] text-muted-foreground">查看完整版回测报告</summary>
                <div className="mt-3 prose prose-sm dark:prose-invert max-w-none prose-headings:mt-3 prose-headings:mb-1 prose-p:my-1.5 prose-table:my-3 prose-th:px-3 prose-th:py-1.5 prose-td:px-3 prose-td:py-1.5 prose-table:text-[12px] prose-strong:text-foreground">
                  <SafeMarkdown>{backtestReport.detail_md}</SafeMarkdown>
                </div>
              </details>
            </div>
          )}
        </div>
      )}

      {/* 历史预测列表 */}
      <div className="border-t border-border/60 pt-3">
        <div className="flex items-center justify-between mb-3">
          <div className="flex items-center gap-2">
            <History className="h-4 w-4" />
            <span className="text-[16px] font-bold">历史预测</span>
          </div>
          <div className="flex items-center gap-1.5">
            <Button variant="ghost" size="sm" className="h-8" onClick={exportPredictions}>
              <Download className="h-3.5 w-3.5" />
              <span className="hidden sm:inline ml-1">导出</span>
            </Button>
            <Button variant="ghost" size="sm" className="h-8" onClick={loadHistory} disabled={historyLoading}>
              <RefreshCw className={`h-3.5 w-3.5 ${historyLoading ? 'animate-spin' : ''}`} />
            </Button>
          </div>
        </div>
        {history.length === 0 ? (
          <div className="text-[13px] text-muted-foreground py-4 text-center">
            暂无预测记录，先发起一次预测
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-[13px]">
              <thead>
                <tr className="border-b text-muted-foreground">
                  <th className="text-left py-2">代码</th>
                  <th className="text-left">基准日</th>
                  <th className="text-left">目标日</th>
                  <th className="text-right">方向</th>
                  {/* 反AI模板⑤: 表格列头也带"模型"限定,明确历史记录均为模型预测值 */}
                  <th className="text-right">模型预期</th>
                  <th className="text-right">模型目标价</th>
                  <th className="text-right">止损</th>
                  <th className="text-left">操作建议</th>
                  <th className="text-left">置信</th>
                  {/* 到期对照列: 后端返回 outcome 字段时自动出现(见 ForecastHistoryItem)。
                      title 报出分母: 命中率只按「已判定」算, 无数据/待验证单独计数。 */}
                  {historyHasOutcome && (
                    <th className="text-right" title={outcomeSummaryTitle}>
                      到期对照
                    </th>
                  )}
                </tr>
              </thead>
              <tbody>
                {history.map(h => (
                  <tr key={h.id} className="border-b hover:bg-muted/30 cursor-pointer" onClick={() => setDetail(h)}>
                    <td className="py-2 font-mono">{h.symbol}{h.stock_name ? ` ${h.stock_name}` : ''}</td>
                    <td>{h.last_date}</td>
                    <td className="font-mono">{h.target_date || '-'}</td>
                    <td className={`text-right font-bold ${h.direction === 'up' ? 'text-stock-up' : h.direction === 'down' ? 'text-stock-down' : ''}`}>
                      {h.direction === 'up' ? '↑' : h.direction === 'down' ? '↓' : '→'}
                    </td>
                    <td className={`text-right font-mono ${h.expected_pct >= 0 ? 'text-stock-up' : 'text-stock-down'}`}>
                      {h.expected_pct > 0 ? '+' : ''}{h.expected_pct}%
                    </td>
                    <td className="text-right font-mono">{h.target_price ?? '-'}</td>
                    <td className="text-right font-mono">{h.stop_loss ?? '-'}</td>
                    <td>{h.action || '-'}</td>
                    <td>{h.confidence || '-'}</td>
                    {historyHasOutcome && <td className="text-right">{renderOutcome(h)}</td>}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <div className="text-xs text-muted-foreground">
        免责声明：预测结果基于历史数据统计模型，不构成投资建议。模型存在偏差，请结合基本面/情绪面综合判断。
      </div>

      {/* 历史预测详情弹窗 */}
      {detail && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4" onClick={() => setDetail(null)}>
          <div className="bg-background border rounded-md shadow-float max-w-lg w-full max-h-[80vh] overflow-y-auto p-5" onClick={e => e.stopPropagation()}>
            <div className="flex items-center justify-between mb-3">
              <div className="text-[16px] font-bold">
                {detail.symbol} {detail.stock_name || ''}
              </div>
              <button className="text-muted-foreground hover:text-foreground" onClick={() => setDetail(null)}>✕</button>
            </div>

            <div className="space-y-3 text-[13px]">
              <div className={`rounded-md border px-3 py-2 ${detail.direction === 'up' ? 'border-stock-up/30 bg-stock-up/5' : detail.direction === 'down' ? 'border-stock-down/30 bg-stock-down/5' : ''}`}>
                <div className="font-bold">
                  {/* 反AI模板⑤: 历史详情弹窗的方向/幅度同样标"模型预测" */}
                  <span className="mr-1 text-xs font-medium text-muted-foreground">模型预测</span>
                  {detail.direction === 'up' ? '↑ 看多' : detail.direction === 'down' ? '↓ 看空' : '→ 横盘'}
                  {' '}{detail.expected_pct > 0 ? '+' : ''}{detail.expected_pct}%
                </div>
                <div className="text-muted-foreground text-xs mt-1">
                  操作建议：{detail.action || '-'} | 置信度：{detail.confidence || '-'}
                </div>
              </div>

              <div className="grid grid-cols-2 gap-2 text-xs">
                <div className="bg-muted/40 rounded px-2 py-1.5">
                  <div className="text-muted-foreground">基准日</div>
                  <div className="font-mono font-bold">{detail.last_date}</div>
                </div>
                <div className="bg-muted/40 rounded px-2 py-1.5">
                  <div className="text-muted-foreground">目标日</div>
                  <div className="font-mono font-bold">{detail.target_date || '-'}</div>
                </div>
                <div className="bg-muted/40 rounded px-2 py-1.5">
                  <div className="text-muted-foreground">基准价</div>
                  <div className="font-mono font-bold">{detail.last_close}</div>
                </div>
                <div className="bg-muted/40 rounded px-2 py-1.5">
                  <div className="text-muted-foreground">模型目标价</div>
                  <div className="font-mono font-bold">{detail.target_price ?? '-'}</div>
                </div>
                <div className="bg-muted/40 rounded px-2 py-1.5">
                  <div className="text-muted-foreground">止损参考</div>
                  <div className="font-mono font-bold">{detail.stop_loss ?? '-'}</div>
                </div>
                <div className="bg-muted/40 rounded px-2 py-1.5">
                  <div className="text-muted-foreground">情绪修正</div>
                  <div className={`font-mono font-bold ${(detail.sentiment_adj || 0) >= 0 ? 'text-stock-up' : 'text-stock-down'}`}>
                    {(detail.sentiment_adj || 0) > 0 ? '+' : ''}{detail.sentiment_adj || 0}%
                  </div>
                </div>
              </div>

              {detail.prediction && detail.prediction.length > 0 && (
                <div>
                  <div className="text-muted-foreground text-xs mb-1">预测序列（T+1 起）</div>
                  <div className="flex flex-wrap gap-1.5">
                    {detail.prediction.map((p, i) => (
                      <div key={i} className="bg-muted/40 rounded px-2 py-1 text-center">
                        <div className="text-[10px] text-muted-foreground">T+{i + 1}</div>
                        <div className={`font-mono font-bold text-xs ${p >= detail.last_close ? 'text-stock-up' : 'text-stock-down'}`}>{p}</div>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {detail.summary && (
                <div className="text-xs text-muted-foreground border-t pt-2">
                  {detail.summary}
                </div>
              )}

              <div className="text-[10px] text-muted-foreground">
                预测时间：{detail.created_at}
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
