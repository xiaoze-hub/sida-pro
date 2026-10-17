import { useEffect, useState } from 'react'
import { fetchAPI } from '@panwatch/api'
import { Activity, Flame, ShieldAlert, Sparkles } from 'lucide-react'
import { PHASE_LABEL, type PhaseKey } from '@/lib/market-phase'
import { safeFixed } from '@/lib/format'

/**
 * 本土三层滤网结论行 (P2 阶段件):
 * 1. 市场情绪周期 (退潮/冰点/发酵/高潮等) - 来自 /market/phase
 * 2. 题材/产业催化 (政策/涨价/事件日历) - 来自 /catalyst/calendar
 * 3. 威科夫量价结构 (Spring/LPS/SOS/放量滞涨等) - 来自 /klines/{symbol}/wyckoff
 *
 * 铁律:
 * - 纯数据展示与证据陈述，不堆砌大卡片，不给单点买卖指令；
 * - 缺数据显式 '--' 或标注缺口，绝不编造数字或臆测主力意图。
 */

interface PhaseResp {
  current_phase?: {
    phase: PhaseKey
    label: string
  } | null
  current_label?: string
}

interface CatalystResp {
  calendar?: Array<{
    title?: string
    date?: string
    category?: string
  }>
}

interface WyckoffResp {
  verdict?: {
    stage?: string
    stage_label?: string
    effort_result?: string
    markers_count?: number
    note?: string
  }
}

interface SummaryResp {
  cancel_order_intent?: {
    available?: boolean
    intent_tag?: string
    cancel_bias?: number
    b_cancel_ratio?: number
    s_cancel_ratio?: number
  }
}

interface DecisionPioneerResp {
  resonance_state?: string
  resonance_action?: string
  resonance_warning?: string
  institution_activity?: {
    activity?: number
    level?: string
  }
}

export function ThreeTierVerdictRow({ symbol, market }: { symbol: string; market: string }) {
  const [phaseData, setPhaseData] = useState<string | null>(null)
  const [catalystCount, setCatalystCount] = useState<number | null>(null)
  const [wyckoffStage, setWyckoffStage] = useState<string | null>(null)
  const [cancelIntent, setCancelIntent] = useState<string | null>(null)
  const [resonanceState, setResonanceState] = useState<{ state: string; action: string; warning: string } | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let cancelled = false
    setLoading(true)

    const pPhase = fetchAPI<PhaseResp>('/market/phase')
      .then((res) => {
        if (cancelled) return
        const p = res?.current_phase?.phase
        const lbl = p ? PHASE_LABEL[p] || res?.current_label || p : res?.current_label || null
        setPhaseData(lbl)
      })
      .catch(() => {
        if (!cancelled) setPhaseData(null)
      })

    const pCat = fetchAPI<CatalystResp>('/catalyst/calendar?days=14')
      .then((res) => {
        if (cancelled) return
        const items = res?.calendar || []
        setCatalystCount(items.length)
      })
      .catch(() => {
        if (!cancelled) setCatalystCount(null)
      })

    const pWyc = fetchAPI<WyckoffResp>(`/klines/${encodeURIComponent(symbol)}/wyckoff?market=${encodeURIComponent(market)}`)
      .then((res) => {
        if (cancelled) return
        const v = res?.verdict
        if (v && v.stage && v.stage !== 'unknown') {
          setWyckoffStage(`${v.stage}阶段 · ${v.stage_label || ''}`)
        } else if (v?.stage_label) {
          setWyckoffStage(v.stage_label)
        } else {
          setWyckoffStage('区间震荡 / 无法定位')
        }
      })
      .catch(() => {
        if (!cancelled) setWyckoffStage(null)
      })

    const pSum = fetchAPI<SummaryResp>(`/klines/${encodeURIComponent(symbol)}/summary?market=${encodeURIComponent(market)}`)
      .then((res) => {
        if (cancelled) return
        const ci = res?.cancel_order_intent
        if (ci && ci.available && ci.intent_tag && ci.intent_tag !== '无数据') {
          const b = ci.b_cancel_ratio != null ? `${safeFixed(ci.b_cancel_ratio * 100, 0)}%` : '--'
          const s = ci.s_cancel_ratio != null ? `${safeFixed(ci.s_cancel_ratio * 100, 0)}%` : '--'
          setCancelIntent(`${ci.intent_tag} (买撤${b}/卖撤${s})`)
        } else {
          setCancelIntent(null)
        }
      })
      .catch(() => {
        if (!cancelled) setCancelIntent(null)
      })

    const pDp = fetchAPI<DecisionPioneerResp>(`/decision-pioneer/${encodeURIComponent(symbol)}?market=${encodeURIComponent(market)}`)
      .then((res) => {
        if (cancelled) return
        if (res && res.resonance_state) {
          setResonanceState({
            state: res.resonance_state,
            action: res.resonance_action || '',
            warning: res.resonance_warning || 'safe',
          })
        } else {
          setResonanceState(null)
        }
      })
      .catch(() => {
        if (!cancelled) setResonanceState(null)
      })

    Promise.allSettled([pPhase, pCat, pWyc, pSum, pDp]).finally(() => {
      if (!cancelled) setLoading(false)
    })

    return () => {
      cancelled = true
    }
  }, [symbol, market])

  return (
    <div
      className="flex flex-wrap items-center gap-x-4 gap-y-2 rounded border border-border/40 bg-card/40 px-3 py-2 text-[11px] text-muted-foreground"
      data-testid="three-tier-verdict-row"
    >
      <div className="flex items-center gap-1.5 font-medium text-foreground">
        <Sparkles className="h-3.5 w-3.5 text-accent" />
        <span>三层滤网决策参考</span>
      </div>

      <span className="text-border/60">|</span>

      {/* 第一层: 市场情绪 */}
      <div className="flex items-center gap-1">
        <Flame className="h-3 w-3 text-orange-500/80" />
        <span className="text-muted-foreground">情绪层:</span>
        <span className="font-medium text-foreground">
          {loading ? '加载中...' : phaseData ? phaseData : '--'}
        </span>
      </div>

      {/* 第二层: 催化驱动 */}
      <div className="flex items-center gap-1">
        <Activity className="h-3 w-3 text-blue-500/80" />
        <span className="text-muted-foreground">催化层:</span>
        <span className="font-medium text-foreground">
          {loading ? '加载中...' : catalystCount !== null ? (catalystCount > 0 ? `近14天${catalystCount}项事件` : '近期暂无主线催化') : '--'}
        </span>
      </div>

      {/* 第三层: 威科夫量价结构 */}
      <div className="flex items-center gap-1">
        <ShieldAlert className="h-3 w-3 text-purple-500/80" />
        <span className="text-muted-foreground">量价层:</span>
        <span className="font-medium text-foreground">
          {loading ? '加载中...' : wyckoffStage ? wyckoffStage : '--'}
        </span>
      </div>

      {/* 第四维度: 微观撤单意图 (对标广发金工与通达信盘口) */}
      {cancelIntent ? (
        <div className="flex items-center gap-1">
          <span className="text-muted-foreground">撤单层:</span>
          <span className="font-medium text-foreground">
            {cancelIntent}
          </span>
        </div>
      ) : null}

      {/* 选股铁三角: 同花顺决策先锋三指标共振状态机 */}
      {resonanceState ? (
        <div className="flex items-center gap-1">
          <span className="text-muted-foreground">铁三角共振:</span>
          <span
            className={`font-semibold ${
              resonanceState.warning === 'opportunity'
                ? 'text-red-500 animate-pulse'
                : resonanceState.warning === 'warning'
                ? 'text-green-500'
                : resonanceState.warning === 'caution'
                ? 'text-yellow-500'
                : 'text-foreground'
            }`}
            title={resonanceState.action}
          >
            {resonanceState.state}
          </span>
        </div>
      ) : null}

      {/* 综合纪律约束与行动禁令 */}
      <div className="flex items-center gap-1">
        <span className="text-muted-foreground">纪律约束:</span>
        <span
          className={`rounded px-1.5 py-0.5 text-[10px] font-semibold ${
            phaseData?.includes('退潮') || phaseData?.includes('冰点')
              ? 'bg-rose-500/20 text-rose-300 border border-rose-500/40'
              : wyckoffStage?.includes('突破') || wyckoffStage?.includes('弹簧')
              ? 'bg-emerald-500/20 text-emerald-300 border border-emerald-500/40'
              : 'bg-muted/40 text-muted-foreground border border-border/40'
          }`}
          data-testid="three-tier-discipline-constraint"
        >
          {loading
            ? '评估中...'
            : phaseData?.includes('退潮') || phaseData?.includes('冰点')
            ? '⚠️ 退潮期严禁盲目加仓'
            : wyckoffStage?.includes('放量滞涨')
            ? '⚠️ 滞涨背离禁止追高'
            : wyckoffStage?.includes('弹簧试探')
            ? '⏳ 弹簧待确认·切勿抢跑'
            : '纪律观望·无右侧信号不动作'}
        </span>
      </div>

      <span className="ml-auto text-[10px] text-muted-foreground/70">
        客观证据 · 不构成买卖指令
      </span>
    </div>
  )
}
