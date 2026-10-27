import { Loader2, RefreshCw } from 'lucide-react'
import { useCopilotBrief } from '@/hooks/useCopilotBrief'
import { safeFixed, safePercent } from '@/lib/format'
import {
  CARD_TITLE,
  DISCLAIMER,
  EVIDENCE_NAME,
  ERROR_TITLE,
  EVIDENCE_LABEL,
  GATED_HINT,
  DEGRADED_HINT,
  LOADING_HINT,
  REFRESH,
  RETRY,
  SESSION_LABEL,
  STALE_HINT,
} from '@/lib/copilot/copy'
import { confidenceOfTen, evidenceState, toneBar, toneMeta } from '@/lib/copilot/tone'
import { EVIDENCE_KEYS, type CopilotBrief } from '@/lib/copilot/types'
import { ProApplyCTA } from '@/components/ProApplyCTA'
import { CopilotDefenseBlock } from './CopilotDefenseBlock'
import { CopilotPlanBlock } from './CopilotPlanBlock'

/**
 * 个股工作台【多维数智参谋】主卡（设计稿 §8.4，2026-10-18）。
 *
 * 三段式大白话决策输出（与聊天回答模板一一对应，用户切到聊天不会看到两套话语）：
 *   ① 意图定性 —— `verdict.intent_label` + tone 徽章 + 置信度（规则引擎产出，非 LLM）
 *   ② 关键防守位 —— `verdict.defense_lines`（只渲染有数的，缺就不占位）
 *   ③ 操盘预案 —— `verdict.action_plans`（持仓/空仓两栏，价位来自后端）
 *
 * 缺失语义（§4.6，逐格实现，绝不一律 `--`）：
 *   available=true          → 正常
 *   available=false+gated   → 「升级解锁」（权限不足 ≠ 没数据）
 *   available=false         → `--` + 后端给的降级原因
 * 失败态只陈述错误原文，不推断原因、不编造结论。
 */
export function CopilotBriefCard({
  symbol,
  market = 'CN',
  hasPosition,
}: {
  symbol: string
  market?: string
  hasPosition?: boolean
}) {
  const { data, loading, error, reload } = useCopilotBrief(symbol, market)

  if (!data && loading) {
    return (
      <div
        className="card-surface rounded-xl bg-s1 px-3 py-3"
        data-testid="copilot-card-loading"
      >
        <div className="flex items-center gap-2 text-[12px] text-muted-foreground">
          <Loader2 className="h-3.5 w-3.5 animate-spin" />
          {LOADING_HINT}
        </div>
      </div>
    )
  }

  if (!data) {
    return (
      <div
        className="card-surface rounded-xl bg-s1 px-3 py-3"
        data-testid="copilot-card-error"
      >
        <div className="flex items-center gap-2">
          <span className="text-[12px] font-medium text-foreground">{ERROR_TITLE}</span>
          <button
            type="button"
            onClick={() => void reload()}
            className="ml-auto inline-flex h-6 items-center gap-1 rounded border border-border px-2 text-[12px] text-muted-foreground hover:text-foreground"
          >
            <RefreshCw className={`h-3 w-3 ${loading ? 'animate-spin' : ''}`} />
            {RETRY}
          </button>
        </div>
        <div className="mt-1 text-[12px] text-muted-foreground">{error || DEGRADED_HINT(null)}</div>
        <div className="mt-2 border-t border-border pt-1.5 text-[12px] text-muted-foreground">
          {DISCLAIMER}
        </div>
      </div>
    )
  }

  return <BriefBody data={data} hasPosition={hasPosition} loading={loading} onRefresh={reload} />
}

function BriefBody({
  data,
  hasPosition,
  loading,
  onRefresh,
}: {
  data: CopilotBrief
  hasPosition?: boolean
  loading: boolean
  onRefresh: () => Promise<void> | void
}) {
  const v = data.verdict
  const meta = toneMeta(v.intent_tone)
  const gated = EVIDENCE_KEYS.filter((k) => evidenceState(data.evidence[k]) === 'gated')
  const missing = EVIDENCE_KEYS.filter((k) => evidenceState(data.evidence[k]) === 'missing')

  return (
    <div className="card-surface overflow-hidden rounded-xl bg-s1" data-testid="copilot-card">
      {/* 头：tone 色条 + 意图定性 + 置信 + 刷新 */}
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1 border-b border-border px-3 py-2">
        <span className={`h-4 w-[3px] rounded ${toneBar(v.intent_tone)}`} />
        <span className="text-[12px] font-medium text-foreground">{CARD_TITLE}</span>
        <span className={`rounded border px-1.5 text-[12px] leading-4 ${meta.badge}`}>
          {meta.icon} {meta.label}
        </span>
        <span className="min-w-0 truncate text-[12px] text-foreground" title={v.intent_label}>
          {v.intent_label}
        </span>
        <span className="font-mono text-[12px] text-muted-foreground">
          置信 {confidenceOfTen(v.confidence)}
        </span>
        <span className="rounded border border-border px-1 text-[12px] text-muted-foreground">
          {SESSION_LABEL[data.session.phase] || data.session.phase}
        </span>
        {data.session.stale ? (
          <span className="text-[12px] text-role-watch">{STALE_HINT}</span>
        ) : null}
        <span className="ml-auto font-mono text-[12px] text-muted-foreground" title={data.as_of}>
          {safeFixed(data.quote.close, 2)} ({safePercent(data.quote.change_pct, 2, true)})
        </span>
        <button
          type="button"
          onClick={() => void onRefresh()}
          title={`${REFRESH}（${data.cache?.hit ? '命中缓存' : '重新取数'}）`}
          className="inline-flex h-5 w-5 items-center justify-center rounded border border-border text-muted-foreground hover:text-foreground"
          data-testid="copilot-refresh"
        >
          <RefreshCw className={`h-3 w-3 ${loading ? 'animate-spin' : ''}`} />
        </button>
      </div>

      <div className="space-y-2 px-3 py-2">
        {/* 三段式的大白话结论（数字全部来自证据） */}
        <p className="text-[12px] leading-relaxed text-foreground" data-testid="copilot-narrative">
          {v.narrative || '证据不足，本次不给结论。'}
        </p>

        {/* 证据条：used / gated / missing 三态可区分 */}
        <div className="flex flex-wrap items-center gap-1 text-[12px]">
          <span className="text-muted-foreground">{EVIDENCE_LABEL}:</span>
          {EVIDENCE_KEYS.map((k) => {
            const st = evidenceState(data.evidence[k])
            const cls =
              st === 'ok'
                ? 'border-border text-muted-foreground'
                : st === 'gated'
                  ? 'border-role-watch/40 text-role-watch'
                  : 'border-border text-muted-foreground line-through'
            const txt = st === 'gated' ? `${EVIDENCE_NAME[k]}·升级解锁` : EVIDENCE_NAME[k]
            return (
              <span key={k} className={`rounded border px-1 ${cls}`} data-evidence={k} data-state={st}>
                {txt}
              </span>
            )
          })}
        </div>

        {/* gated: 权限不足 ≠ 没数据 —— 给升级指引，不给 `--` */}
        {gated.length ? (
          <div className="card-surface flex flex-wrap items-center gap-x-2 gap-y-1 rounded bg-card px-2 py-1 text-[12px] text-role-watch">
            <span>{GATED_HINT}：{gated.map((k) => EVIDENCE_NAME[k]).join(' · ')}</span>
            {/* 2026-10-27: 只给提示没出路 —— 提示后面直接挂可点的申请入口 */}
            <ProApplyCTA reason={`权限不足：${gated.map((k) => EVIDENCE_NAME[k]).join('/')}`} />
          </div>
        ) : null}

        <CopilotDefenseBlock lines={v.defense_lines} close={data.quote.close} />
        <CopilotPlanBlock plans={v.action_plans} hasPosition={hasPosition} />

        {v.risk_flags?.length ? (
          <div className="flex flex-wrap gap-1 text-[12px]">
            {v.risk_flags.slice(0, 6).map((f) => (
              <span key={f} className="rounded border border-role-risk/40 px-1 text-role-risk">
                {f}
              </span>
            ))}
          </div>
        ) : null}

        {/* 口径 + 缺失原因 + 合规尾注 */}
        <div className="space-y-0.5 border-t border-border pt-1.5">
          {v.caliber_notes?.length ? (
            <div className="text-[12px] text-muted-foreground">
              口径：{v.caliber_notes.slice(0, 4).join(' · ')}
            </div>
          ) : null}
          {missing.length ? (
            <div className="text-[12px] text-muted-foreground">
              {missing
                .map((k) => `${EVIDENCE_NAME[k]} ${DEGRADED_HINT(data.evidence[k].degraded_reason)}`)
                .join(' ｜ ')}
            </div>
          ) : null}
          <div className="text-[12px] text-muted-foreground">
            {v.disclaimer || DISCLAIMER}
            {v.rule_id ? <span className="ml-1 font-mono">[{v.rule_id}]</span> : null}
          </div>
        </div>
      </div>
    </div>
  )
}
