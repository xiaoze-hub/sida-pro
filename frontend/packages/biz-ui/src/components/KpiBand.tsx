import { useNavigate } from 'react-router-dom'
import { Activity, Crown } from 'lucide-react'
import { useCallback, useEffect, useRef, useState } from 'react'
import { fetchAPI } from '@panwatch/api'
import { safeFixed } from '@/lib/format'

/**
 * 首页 KPI 带(v0.4.6, 借鉴 TSP tick-stock-panel 设计)。
 *
 * 6 格「数字优先」KPI: 涨/平/跌 · 主力净流入 · 成交额 · 情绪周期阶段 ·
 * 涨停/跌停(来自异动池当日) · 主线 Top1。
 * 数据全部复用首页已有接口(marketFlow / anomalies / mainline / phase),
 * 不新增后端调用 — phase/mainline 由子卡自身轮询, 这里通过 props 传入快照。
 */

export interface MainlineTop1 {
  name: string
  limit_up_count: number
  max_boards: number
  leader_name?: string
}


/** v0.4.7: 数字滚动动画(300ms requestAnimationFrame 过渡) */
function useCountUp(target: number | null, duration = 300): number | null {
  const [display, setDisplay] = useState<number | null>(target)
  const prevRef = useRef<number | null>(null)
  useEffect(() => {
    if (target == null) {
      setDisplay(null)
      return
    }
    const from = prevRef.current ?? target
    prevRef.current = target
    if (from === target) {
      setDisplay(target)
      return
    }
    const start = performance.now()
    let raf = 0
    const tick = (now: number) => {
      const p = Math.min(1, (now - start) / duration)
      const eased = 1 - Math.pow(1 - p, 3)
      setDisplay(+(from + (target - from) * eased).toFixed(2))
      if (p < 1) raf = requestAnimationFrame(tick)
    }
    raf = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(raf)
  }, [target, duration])
  return display
}

function Cell({
  label,
  value,
  sub,
  tone,
  title,
}: {
  label: React.ReactNode
  value: React.ReactNode
  sub?: string
  tone?: 'bull' | 'bear' | 'accent' | null
  /** 悬停说明: 失败原因 / 缺值口径 —— 失败与缺值必须可区分(不本地编造原因, 后端 note 原文透传) */
  title?: string
}) {
  const toneCls =
    tone === 'bull'
      ? 'text-stock-up'
      : tone === 'bear'
        ? 'text-stock-down'
        : tone === 'accent'
          ? 'text-primary'
          : 'text-foreground'
  return (
    <div className="min-w-0 px-2 py-1.5" title={title}>
      <div className="truncate text-[10px] text-muted-foreground">{label}</div>
      <div className={`font-num tabular-nums text-[15px] font-semibold leading-tight ${toneCls}`}>
        {value}
      </div>
      {sub && <div className="truncate text-[9.5px] text-muted-foreground">{sub}</div>}
    </div>
  )
}

/** 失败态文案(与 '--' 缺值态严格区分: 拉取失败绝不能渲染成"没有数据") */
const FAIL_TEXT = '加载失败'
const FAIL_CLS = 'text-[13px] text-amber-600 dark:text-amber-500'
/** 跌停家数: 现接线数据源(/market/phase)不提供该字段 —— 按仓库缺值约定渲染 '--', 不用"暂无"伪装成"今天没有跌停股" */
const LIMIT_DOWN_MISSING_TITLE = '当前数据源(/market/phase)未提供跌停家数, 以 -- 表示缺值'

export default function KpiBand({
  upCount,
  downCount,
  mainFlowYi,
  amountYi,
  phase,
  mainlineTop1,
  mainlineLoading,
  mainlineError,
}: {
  upCount: number | null
  downCount: number | null
  mainFlowYi: number | null
  amountYi: number | null
  /** /market/phase 快照(含加载/失败/无数据三态, 由 usePhaseLabel 提供) */
  phase: PhaseKpi
  mainlineTop1: MainlineTop1 | null
  mainlineLoading: boolean
  /** 主线 Top1 拉取失败原文(有值时显式失败, 不渲染成 '--' 缺值) */
  mainlineError: string | null
}) {
  const navigate = useNavigate()
  const flowTone = mainFlowYi == null ? null : mainFlowYi >= 0 ? 'bull' : 'bear'
  // v0.4.7: 数字滚动动画
  const flowAnim = useCountUp(mainFlowYi)
  const amountAnim = useCountUp(amountYi)

  // 情绪周期: 有好值优先展示(失败只进 title 提示); 从未拿到 → 显式失败; available=false → '--'
  const phaseHasValue = !!(phase.label || phase.limitUp != null)
  const phaseFailed = !!phase.error && !phaseHasValue
  const phaseValue = phaseFailed
    ? FAIL_TEXT
    : phase.loading && !phase.label && !phase.unavailableNote
      ? '…'
      : phase.label || '--'
  const phaseTitle = phase.error
    ? (phaseHasValue ? `本次刷新失败: ${phase.error} — 仍展示上次成功数据` : `情绪周期数据加载失败: ${phase.error}`)
    : phase.unavailableNote || undefined

  return (
    <div className="card grid grid-cols-3 divide-x divide-border/40 md:grid-cols-6">
      <Cell
        label="涨 / 跌"
        value={
          <>
            <span className="text-stock-up">{upCount ?? '--'}</span>
            <span className="mx-0.5 text-muted-foreground">/</span>
            <span className="text-stock-down">{downCount ?? '--'}</span>
          </>
        }
      />
      <Cell
        label="主力净流入"
        value={flowAnim == null ? '--' : `${mainFlowYi! >= 0 ? '+' : ''}${safeFixed(flowAnim, 0)}亿`}
        tone={flowTone as 'bull' | 'bear' | null}
      />
      <Cell label="两市成交额" value={amountAnim == null ? '--' : `${safeFixed(amountAnim, 0)}亿`} />
      <button
        type="button"
        className="cursor-pointer text-left transition-colors hover:bg-accent/20"
        onClick={() => navigate('/theme-mood')}
        title="查看情绪周期详情"
      >
        <Cell
          label={
            <span className="inline-flex items-center gap-1">
              <Activity className="h-3 w-3" />情绪周期
            </span>
          }
          value={phaseFailed ? <span className={FAIL_CLS}>{phaseValue}</span> : phaseValue}
          tone="accent"
          title={phaseTitle}
        />
      </button>
      <button
        type="button"
        className="cursor-pointer text-left transition-colors hover:bg-accent/20"
        onClick={() => navigate('/opportunities')}
        title="查看主线识别"
      >
        <Cell
          label={
            <span className="inline-flex items-center gap-1">
              <Crown className="h-3 w-3" />主线 Top1
            </span>
          }
          value={
            mainlineError ? (
              <span className={FAIL_CLS}>{FAIL_TEXT}</span>
            ) : mainlineLoading ? (
              '…'
            ) : (
              mainlineTop1?.name || '--'
            )
          }
          sub={mainlineTop1 ? `涨停${mainlineTop1.limit_up_count}家 · 高度${mainlineTop1.max_boards}板` : undefined}
          tone="accent"
          title={mainlineError ? `主线数据加载失败: ${mainlineError}` : undefined}
        />
      </button>
      {/* v0.4.7: 涨停/跌停 + 封板率(数据来自 /market/phase) */}
      <Cell
        label="涨停 / 跌停"
        title={phaseFailed ? `涨停家数加载失败: ${phase.error}` : LIMIT_DOWN_MISSING_TITLE}
        value={
          phaseFailed ? (
            <span className={FAIL_CLS}>{FAIL_TEXT}</span>
          ) : (
            <>
              <span className="text-stock-up">{phase.limitUp ?? '--'}</span>
              <span className="mx-0.5 text-muted-foreground">/</span>
              <span className="text-stock-down">--</span>
            </>
          )
        }
        sub={phase.sealRate != null ? `封板率 ${safeFixed(phase.sealRate * 100, 0)}%` : undefined}
      />
    </div>
  )
}

/** /api/market/phase 响应(前端只取本卡需要的字段) */
export interface PhaseResp {
  available: boolean
  current: { label: string; ge2_count: number | null; first_board: number | null; seal_rate: number | null } | null
  note?: string | null
}

/** 轻量拉取 phase 当前阶段标签(KpiBand 用; 完整卡在 MarketPhaseCard)
 *
 *  三态显式区分(2026-09-14 首页走查 ①②):
 *   - error 非空   = 拉取失败 → 渲染"加载失败"(绝不塌成 '--' 缺值态)
 *   - available=false = 数据源明确无数据 → '--' + unavailableNote(后端 note 原文, 不本地编造原因)
 *   - 其余 = 正常值
 */
export interface PhaseKpi {
  label: string | null
  loading: boolean
  error: string | null
  unavailableNote: string | null
  limitUp: number | null
  sealRate: number | null
  reload: () => void
}
export function usePhaseLabel(): PhaseKpi {
  const [label, setLabel] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [unavailableNote, setUnavailableNote] = useState<string | null>(null)
  const [limitUp, setLimitUp] = useState<number | null>(null)
  const [sealRate, setSealRate] = useState<number | null>(null)
  const aliveRef = useRef(true)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      // 走 fetchAPI 默认 30s GET 缓存(与轮询同频): 首页多组件同打 /market/phase 时
      // 共享一份响应, 不再各自 cacheMode:reload 叠并发(2026-09-18 走查「请求超时」根因之一)。
      // UX 走查 2026-09-15: 单 worker 下首页并发高峰时默认 20s 超时仍偶发; 显式 30s。
      const res = await fetchAPI<PhaseResp>('/market/phase', { timeoutMs: 30000 })
      if (!aliveRef.current) return
      const cur = res?.available ? res.current : null
      if (cur) {
        setLabel(cur.label ?? null)
        // 涨停≈首板+≥2板(当日入池口径); 跌停该接口无此字段 → 由 Cell 用 '--' 表示缺值
        setLimitUp((cur.first_board ?? 0) + (cur.ge2_count ?? 0))
        setSealRate(cur.seal_rate)
        setUnavailableNote(null)
        setError(null)
      } else {
        setLabel(null)
        setLimitUp(null)
        setSealRate(null)
        // 后端自己的 note 原样透传(如"尚未同步阶段数据…"), 不在前端编造原因
        setUnavailableNote(res?.note || '阶段数据源暂无数据')
      }
    } catch (e) {
      if (!aliveRef.current) return
      // stale-on-error: 保留上次成功值, 只标 error(与 PhaseGaugeCard 同规则)
      setError(e instanceof Error ? e.message : '加载失败')
    } finally {
      if (aliveRef.current) setLoading(false)
    }
  }, [])

  useEffect(() => {
    aliveRef.current = true
    void load()
    return () => {
      aliveRef.current = false
    }
  }, [load])

  return { label, loading, error, unavailableNote, limitUp, sealRate, reload: load }
}


/** 轻量拉取主线 Top1(KpiBand 用; 完整榜在 MarketMainlineCard) */
export interface MainlineKpi {
  top: MainlineTop1 | null
  loading: boolean
  error: string | null
}
export function useMainlineTop1(): MainlineKpi {
  const [top, setTop] = useState<MainlineTop1 | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const aliveRef = useRef(true)
  useEffect(() => {
    aliveRef.current = true
    void (async () => {
      try {
        const res = await fetchAPI<{ ranked_groups: MainlineTop1[] }>('/market/mainline')
        if (!aliveRef.current) return
        setTop(res?.ranked_groups?.[0] ?? null)
        setError(null)
      } catch (e) {
        // 失败不再静默: 由 KpiBand 渲染"加载失败", 与 '--' 缺值态区分
        if (aliveRef.current) setError(e instanceof Error ? e.message : '加载失败')
      } finally {
        if (aliveRef.current) setLoading(false)
      }
    })()
    return () => {
      aliveRef.current = false
    }
  }, [])
  return { top, loading, error }
}
