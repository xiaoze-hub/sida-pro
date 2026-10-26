import { useEffect, useState } from 'react'
import { fetchAPI } from '@panwatch/api'
import { AlertTriangle, ShieldX } from 'lucide-react'
import { PHASE_LABEL, type PhaseKey } from '@/lib/market-phase'

/**
 * 盘中防守与冷静期警示条 (DisciplineCoolDownBanner)
 *
 * 核心职能:
 * 1. 监控大盘情绪周期: 当处于退潮 (ebb) 或 冰点 (ice) 时，强制高亮冷静期防守警示，
 *    阻断用户冲动抄底/补仓幻想；
 * 2. 诚实展示历史统计胜率与环境风险，不编造数字。
 */

interface PhaseResp {
  current_phase?: {
    phase: PhaseKey
    label: string
  } | null
  current_label?: string
}

export function DisciplineCoolDownBanner() {
  const [phase, setPhase] = useState<PhaseKey | null>(null)
  const [phaseLabel, setPhaseLabel] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    fetchAPI<PhaseResp>('/market/phase')
      .then((res) => {
        if (cancelled) return
        const p = res?.current_phase?.phase || null
        setPhase(p)
        const lbl = p ? PHASE_LABEL[p] || res?.current_label || p : res?.current_label || null
        setPhaseLabel(lbl)
      })
      .catch(() => {
        if (!cancelled) {
          setPhase(null)
          setPhaseLabel(null)
        }
      })
    return () => {
      cancelled = true
    }
  }, [])

  // 仅在退潮期 (ebb) 或 冰点期 (ice) 触发警示条
  const isEbb = phase === 'ebb'
  const isIce = phase === 'ice'

  if (!isEbb && !isIce) return null

  return (
    <div
      className="card-surface mt-2 flex items-center justify-between gap-3 rounded bg-rose-950/20 px-3 py-2 text-[12px] text-rose-200"
      data-testid="discipline-cooldown-banner"
    >
      <div className="flex items-center gap-2">
        <ShieldX className="h-4 w-4 shrink-0 text-rose-400" />
        <span className="font-semibold text-rose-300">
          【冷静期警示 · 市场{phaseLabel || (isEbb ? '退潮期' : '冰点期')}】
        </span>
        <span className="text-rose-200/90">
          全市场处于风险释放与退潮杀跌阶段。历史统计此环境下任何逆势抄底或盲目加仓动作胜率极低，请严格执行防守纪律，手绑背后切勿逆势抗单！
        </span>
      </div>
      <div className="flex shrink-0 items-center gap-1 text-[12px] text-rose-400/80">
        <AlertTriangle className="h-3.5 w-3.5" />
        <span>风控铁律：保命第一</span>
      </div>
    </div>
  )
}
