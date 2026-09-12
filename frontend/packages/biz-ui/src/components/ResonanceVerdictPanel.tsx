import { useEffect, useState } from 'react'
import { fetchAPI } from '@panwatch/api'
import { Sparkles } from 'lucide-react'
import { safeFixed } from '@/lib/format'

/**
 * 数智决策共振判定面板(2026-09-11, 老板"要接入ai分析, 给出是否共振; 现在没办法知道到底有没有共振")。
 *
 * 上半: 规则三灯(趋势/强度/资金, 日线口径, 来自 GET /api/resonance/symbol/{symbol});
 * 下半: 点「AI 分析」→ POST /api/resonance/analyze/{symbol} → 结构化结论
 *       (强共振/弱共振/未共振/无法判定 + 依据/风险/关注点 + 缺项)。
 * 诚实边界: 无数据/AI 不可用都如实标注, 不编造。
 */

interface RuleResp {
  symbol: string
  available: boolean
  reason?: string
  trade_date?: string
  trend?: string
  activity?: number | null
  level?: string | null
  fund_net?: number | null
  level3?: string | null
  hits?: boolean[]
}

interface AiVerdict {
  resonance: string
  confidence: number | null
  summary: string
  reasons: string[]
  risks: string[]
  watch: string[]
  missing: string[]
  parse_error?: boolean
}

interface AnalyzeResp {
  symbol: string
  available: boolean
  reason?: string
  rule?: RuleResp
  ai?: AiVerdict | null
}

export interface ResonanceVerdictPanelProps {
  symbol: string
}

function lampClass(ok: boolean | undefined, known: boolean): string {
  if (!known) return 'bg-muted-foreground/30'
  return ok ? 'bg-stock-up' : 'bg-muted-foreground/40'
}

const VERDICT_CLASS: Record<string, string> = {
  强共振: 'text-stock-up',
  弱共振: 'text-amber-500',
  未共振: 'text-muted-foreground',
  无法判定: 'text-muted-foreground',
}

export default function ResonanceVerdictPanel({ symbol }: ResonanceVerdictPanelProps) {
  const [rule, setRule] = useState<RuleResp | null>(null)
  const [ruleState, setRuleState] = useState<'idle' | 'loading' | 'done' | 'error'>('idle')
  const [ai, setAi] = useState<AnalyzeResp | null>(null)
  const [aiLoading, setAiLoading] = useState(false)

  // 规则三灯: 挂载即取(轻量, 无 LLM)
  useEffect(() => {
    void (async () => {
      if (!symbol) return
      setRuleState('loading')
      try {
        const res = await fetchAPI<RuleResp>(`/resonance/symbol/${encodeURIComponent(symbol)}`, { cacheMode: 'reload' })
        setRule(res)
        setRuleState('done')
      } catch {
        setRuleState('error')
      }
    })()
  }, [symbol])

  const runAi = async () => {
    if (aiLoading) return
    setAiLoading(true)
    try {
      const res = await fetchAPI<AnalyzeResp>(`/resonance/analyze/${encodeURIComponent(symbol)}`, {
        method: 'POST',
        cacheMode: 'reload',
        timeoutMs: 90_000,
      })
      setAi(res)
    } catch (e) {
      setAi({ symbol, available: false, reason: e instanceof Error ? e.message : 'AI 分析失败', ai: null })
    } finally {
      setAiLoading(false)
    }
  }

  const hits = rule?.hits ?? []
  const known = rule?.available === true

  return (
    <div className="space-y-1.5">
      <div className="flex items-center gap-3 text-[11px]">
        <span className="text-muted-foreground">三指标</span>
        <span className="inline-flex items-center gap-1">
          <span className={`h-2 w-2 rounded-full ${lampClass(hits[0], known)}`} />
          <span className={hits[0] ? 'text-stock-up' : 'text-muted-foreground'}>
            趋势 {rule?.trend || '—'}
          </span>
        </span>
        <span className="inline-flex items-center gap-1">
          <span className={`h-2 w-2 rounded-full ${lampClass(hits[1], known)}`} />
          <span className={hits[1] ? 'text-stock-up' : 'text-muted-foreground'}>
            强度 {rule?.activity == null ? '—' : safeFixed(rule.activity, 2)}
            {rule?.level ? `(${rule.level})` : ''}
          </span>
        </span>
        <span className="inline-flex items-center gap-1">
          <span className={`h-2 w-2 rounded-full ${lampClass(hits[2], known)}`} />
          <span className={hits[2] ? 'text-stock-up' : 'text-muted-foreground'}>
            资金 {rule?.fund_net == null ? '—' : `${safeFixed(rule.fund_net / 1e8, 2)}亿`}
          </span>
        </span>
        <span className="ml-auto font-mono text-[10px] text-muted-foreground">
          {known ? `规则: ${rule?.level3 || '无'}` : ruleState === 'loading' ? '加载中…' : '数据缺失'}
        </span>
      </div>

      <div className="flex items-start gap-2">
        <button
          type="button"
          onClick={() => void runAi()}
          disabled={aiLoading || !known}
          className="inline-flex shrink-0 items-center gap-1 rounded border border-primary/40 bg-primary/10 px-2 py-0.5 text-[11px] text-foreground transition-colors hover:bg-primary/20 disabled:opacity-50"
        >
          <Sparkles className="h-3 w-3" />
          {aiLoading ? 'AI 判定中…' : 'AI 分析'}
        </button>
        <div className="min-w-0 flex-1 text-[11px]">
          {ai == null ? (
            <span className="text-muted-foreground/70">
              点「AI 分析」→ 给出是否共振 + 依据/风险(仅参考, 不构成投资建议)
            </span>
          ) : !ai.available || !ai.ai ? (
            <span className="text-muted-foreground">{ai.reason || 'AI 不可用'}(规则判定见左侧三灯)</span>
          ) : (
            <div className="space-y-0.5">
              <div className="flex items-baseline gap-1.5">
                <span className={`font-semibold ${VERDICT_CLASS[ai.ai.resonance] || 'text-foreground'}`}>
                  {ai.ai.resonance}
                </span>
                {ai.ai.confidence != null ? (
                  <span className="font-mono text-[10px] text-muted-foreground">
                    置信 {safeFixed(ai.ai.confidence, 2)}
                  </span>
                ) : null}
                {ai.ai.summary ? <span className="text-foreground/90">{ai.ai.summary}</span> : null}
              </div>
              {ai.ai.reasons.length > 0 ? (
                <div className="text-muted-foreground">
                  <span className="text-foreground/70">依据:</span> {ai.ai.reasons.join('；')}
                </div>
              ) : null}
              {ai.ai.risks.length > 0 ? (
                <div className="text-amber-600 dark:text-amber-500">
                  <span>风险:</span> {ai.ai.risks.join('；')}
                </div>
              ) : null}
              {ai.ai.watch.length > 0 ? (
                <div className="text-muted-foreground">
                  <span className="text-foreground/70">关注:</span> {ai.ai.watch.join('；')}
                </div>
              ) : null}
              {ai.ai.missing.length > 0 ? (
                <div className="text-muted-foreground/70">缺项: {ai.ai.missing.join('、')}</div>
              ) : null}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
