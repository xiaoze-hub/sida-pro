import { useNavigate } from 'react-router-dom'
import { Activity, Crown } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { fetchAPI } from '@panwatch/api'

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
}: {
  label: React.ReactNode
  value: React.ReactNode
  sub?: string
  tone?: 'bull' | 'bear' | 'accent' | null
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
    <div className="min-w-0 px-2 py-1.5">
      <div className="truncate text-[10px] text-muted-foreground">{label}</div>
      <div className={`font-num tabular-nums text-[15px] font-semibold leading-tight ${toneCls}`}>
        {value}
      </div>
      {sub && <div className="truncate text-[9.5px] text-muted-foreground">{sub}</div>}
    </div>
  )
}

export default function KpiBand({
  upCount,
  downCount,
  mainFlowYi,
  amountYi,
  phaseLabel,
  phaseLoading,
  mainlineTop1,
  mainlineLoading,
  limitUp,
  limitDown,
  sealRate,
}: {
  upCount: number | null
  downCount: number | null
  mainFlowYi: number | null
  amountYi: number | null
  phaseLabel: string | null
  phaseLoading: boolean
  mainlineTop1: MainlineTop1 | null
  mainlineLoading: boolean
  limitUp: number | null
  limitDown: number | null
  sealRate: number | null
}) {
  const navigate = useNavigate()
  const flowTone = mainFlowYi == null ? null : mainFlowYi >= 0 ? 'bull' : 'bear'
  // v0.4.7: 数字滚动动画
  const flowAnim = useCountUp(mainFlowYi)
  const amountAnim = useCountUp(amountYi)

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
        value={flowAnim == null ? '--' : `${mainFlowYi! >= 0 ? '+' : ''}${flowAnim.toFixed(0)}亿`}
        tone={flowTone as 'bull' | 'bear' | null}
      />
      <Cell label="两市成交额" value={amountAnim == null ? '--' : `${amountAnim.toFixed(0)}亿`} />
      <button
        type="button"
        className="cursor-pointer text-left transition-colors hover:bg-accent/20"
        onClick={() => navigate('/')}
        title="查看情绪周期详情"
      >
        <Cell
          label={
            <span className="inline-flex items-center gap-1">
              <Activity className="h-3 w-3" />情绪周期
            </span>
          }
          value={phaseLoading ? '…' : phaseLabel || '--'}
          tone="accent"
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
          value={mainlineLoading ? '…' : mainlineTop1?.name || '--'}
          sub={mainlineTop1 ? `涨停${mainlineTop1.limit_up_count}家 · 高度${mainlineTop1.max_boards}板` : undefined}
          tone="accent"
        />
      </button>
      {/* v0.4.7: 涨停/跌停 + 封板率(数据来自 /market/phase) */}
      <Cell
        label="涨停 / 跌停"
        value={
          <>
            <span className="text-stock-up">{limitUp ?? '--'}</span>
            <span className="mx-0.5 text-muted-foreground">/</span>
            <span className="text-stock-down">{limitDown ?? '暂无'}</span>
          </>
        }
        sub={sealRate != null ? `封板率 ${(sealRate * 100).toFixed(0)}%` : undefined}
      />
    </div>
  )
}

/** 轻量拉取 phase 当前阶段标签(KpiBand 用; 完整卡在 MarketPhaseCard) */
export interface PhaseKpi {
  label: string | null
  loading: boolean
  limitUp: number | null
  limitDown: number | null
  sealRate: number | null
}
export function usePhaseLabel(): PhaseKpi {
  const [label, setLabel] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [limitUp, setLimitUp] = useState<number | null>(null)
  const [limitDown] = useState<number | null>(null)
  const [sealRate, setSealRate] = useState<number | null>(null)
  useEffect(() => {
    let alive = true
    void (async () => {
      try {
        const res = await fetchAPI<{
          available: boolean
          current: { label: string; ge2_count: number | null; first_board: number | null; seal_rate: number | null } | null
        }>('/market/phase')
        if (!alive) return
        setLabel(res?.current?.label ?? null)
        // 涨停≈首板+≥2板(当日入池口径), 跌停接口无 — 显式 null 不编造
        const cur = res?.current
        if (cur) {
          setLimitUp((cur.first_board ?? 0) + (cur.ge2_count ?? 0))
          setSealRate(cur.seal_rate)
        }
      } catch {
        /* 静默 — KPI 格显示 -- */
      } finally {
        if (alive) setLoading(false)
      }
    })()
    return () => {
      alive = false
    }
  }, [])
  return { label, loading, limitUp, limitDown, sealRate }
}


/** 轻量拉取主线 Top1(KpiBand 用; 完整榜在 MarketMainlineCard) */
export function useMainlineTop1(): { top: MainlineTop1 | null; loading: boolean } {
  const [top, setTop] = useState<MainlineTop1 | null>(null)
  const [loading, setLoading] = useState(true)
  useEffect(() => {
    let alive = true
    void (async () => {
      try {
        const res = await fetchAPI<{ ranked_groups: MainlineTop1[] }>("/market/mainline")
        if (alive) setTop(res?.ranked_groups?.[0] ?? null)
      } catch {
        /* 静默 */
      } finally {
        if (alive) setLoading(false)
      }
    })()
    return () => {
      alive = false
    }
  }, [])
  return { top, loading }
}
