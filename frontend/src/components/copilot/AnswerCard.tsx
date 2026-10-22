import { useMemo, useState } from 'react'
import { Check, ChevronDown, Copy, Sparkles } from 'lucide-react'
import type { ParsedAnswer } from '@/lib/copilot/answer-blocks'
import { anchorKindLabel, splitByAnchors, type PriceAnchorKind } from '@/lib/copilot/price-anchors'
import {
  ANSWER_ACTION_COPIED,
  ANSWER_ACTION_COPY,
  ANSWER_ACTION_WORKBENCH,
  ANSWER_CALIBER_LABEL,
  ANSWER_CONFIDENCE_LABEL,
  ANSWER_EVIDENCE_COLLAPSE,
  ANSWER_EVIDENCE_EXPAND_PREFIX,
  ANSWER_EVIDENCE_EXPAND_SUFFIX,
  ANSWER_EVIDENCE_LABEL,
  ANSWER_FOLLOWUP_LABEL,
  ANSWER_PLAN_LABEL,
  ANSWER_VERDICT_LABEL,
  FOLLOW_UP_QUESTIONS,
} from '@/lib/copilot/copy'

/** 首屏只露 3 条依据：结论 + 关键证据一屏读完，其余折叠（信息量不变，首屏变短）。 */
const EVIDENCE_PREVIEW = 3

/** 价格锚点：点 chip 时回传给上层（K 线画线由另一个模块拥有，这里只出事件）。 */
export interface AnchoredPrice {
  value: number
  kind: PriceAnchorKind
  context: string
}

/**
 * 行内价位 chip（2026-10-23）。
 *
 * 只有传了 `onAnchor` 才渲染成按钮；没传时**逐字渲染原文**（老调用点行为不变,
 * 也不会有任何 testid 冒出来）。百分比不是价位，永远不会变成按钮。
 */
function AnchoredText({
  text,
  onAnchor,
}: {
  text: string
  onAnchor?: (a: AnchoredPrice) => void
}) {
  if (!onAnchor) return <>{text}</>
  const segs = splitByAnchors(text)
  if (!segs.some((s) => s.anchor)) return <>{text}</>
  return (
    <>
      {segs.map((seg, i) => {
        const a = seg.anchor
        if (!a) return <span key={i}>{seg.text}</span>
        const label = anchorKindLabel(a.kind, a.context)
        return (
          <button
            key={`${i}-${a.start}`}
            type="button"
            data-testid="answer-price-anchor"
            data-kind={a.kind}
            title={`${label === '--' ? '价位' : label} ${a.value} · 点到 K 线`}
            onClick={() => onAnchor({ value: a.value, kind: a.kind, context: a.context })}
            className="mx-[1px] inline-flex items-baseline gap-0.5 rounded border border-border/60 px-1 align-baseline font-mono text-[10px] tabular-nums text-foreground/90 hover:border-role-watch/60 hover:text-role-watch"
          >
            <span>{seg.text}</span>
            {label !== '--' ? (
              <span className="text-[10px] text-muted-foreground">{label}</span>
            ) : null}
          </button>
        )
      })}
    </>
  )
}

/**
 * 参谋回答卡片（2026-10-19, P1 排版）。
 *
 * 分层顺序固定为「裁决 → 依据 → 预案 → 口径」：
 *   1. 裁决条：一句定性 + 置信度徽标（置信度来自原文 x/10，拿不到就不显示，不编）；
 *   2. 依据：默认 3 条，可展开全部；
 *   3. 预案：原文保留（持仓者/空仓者的分叉在模型输出里）；
 *   4. 口径 + 免责：固定尾部，回答里到底用的是哪套口径必须可见。
 *
 * 不做的事：不解析数字做 tile（模型散文里的数字位置不稳定，硬解析会张冠李戴），
 * 数字仍以依据原文为准。
 * 例外（2026-10-23）：**价位**可以做成行内 chip —— 只在调用方传了 `onAnchoredPrice`
 * 时生效，且 chip 上显示的就是原文片段（不改写、不重排），点击只回传数值。
 */
export function AnswerCard({
  parsed,
  symbol,
  onFollowUp,
  onAnchoredPrice,
}: {
  parsed: ParsedAnswer
  symbol?: string
  onFollowUp?: (question: string) => void
  onAnchoredPrice?: (a: AnchoredPrice) => void
}) {
  const [expanded, setExpanded] = useState(false)
  const [copied, setCopied] = useState(false)

  const hiddenCount = Math.max(0, parsed.evidence.length - EVIDENCE_PREVIEW)
  const shownEvidence = useMemo(
    () => (expanded ? parsed.evidence : parsed.evidence.slice(0, EVIDENCE_PREVIEW)),
    [expanded, parsed.evidence],
  )

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(parsed.raw)
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1500)
    } catch {
      // 剪贴板不可用（非 https / 权限拒绝）→ 静默，不弹错误打断阅读
    }
  }

  return (
    <div data-testid="copilot-answer-card" className="space-y-2">
      {/* 1. 裁决条 */}
      {parsed.verdict ? (
        <div className="rounded-lg border border-role-watch/40 bg-role-watch/8 px-2.5 py-2">
          <div className="flex flex-wrap items-center gap-1.5">
            <Sparkles className="h-3 w-3 shrink-0 text-role-watch" />
            <span className="text-[10px] font-medium text-role-watch">{ANSWER_VERDICT_LABEL}</span>
            {parsed.confidence != null ? (
              <span
                data-testid="copilot-answer-confidence"
                className="rounded border border-role-watch/40 px-1 text-[10px] text-role-watch"
              >
                {ANSWER_CONFIDENCE_LABEL} {parsed.confidence}/10
              </span>
            ) : null}
          </div>
          <div className="mt-1 text-[13px] leading-relaxed text-foreground">
            <AnchoredText text={parsed.verdict} onAnchor={onAnchoredPrice} />
          </div>
        </div>
      ) : null}

      {/* 2. 依据 */}
      {parsed.evidence.length ? (
        <div className="rounded-lg border border-border/50 bg-s1 px-2.5 py-2">
          <div className="flex items-center justify-between">
            <span className="text-[10px] font-medium text-muted-foreground">{ANSWER_EVIDENCE_LABEL}</span>
            <span className="text-[10px] text-muted-foreground/70">{parsed.evidence.length} 条</span>
          </div>
          <ul className="mt-1 space-y-1">
            {shownEvidence.map((e, i) => (
              <li key={`${i}-${e.slice(0, 12)}`} className="flex gap-1.5 text-[12px] leading-relaxed">
                <span className="mt-[2px] h-1 w-1 shrink-0 rounded-full bg-role-watch/70" />
                <span className="min-w-0 text-foreground/90">
                  <AnchoredText text={e} onAnchor={onAnchoredPrice} />
                </span>
              </li>
            ))}
          </ul>
          {hiddenCount > 0 ? (
            <button
              type="button"
              onClick={() => setExpanded((v) => !v)}
              data-testid="copilot-answer-expand"
              className="mt-1.5 flex items-center gap-1 text-[11px] text-role-watch hover:underline"
            >
              <ChevronDown className={`h-3 w-3 transition-transform ${expanded ? 'rotate-180' : ''}`} />
              {expanded
                ? ANSWER_EVIDENCE_COLLAPSE
                : `${ANSWER_EVIDENCE_EXPAND_PREFIX} ${hiddenCount} ${ANSWER_EVIDENCE_EXPAND_SUFFIX}`}
            </button>
          ) : null}
        </div>
      ) : null}

      {/* 3. 预案 */}
      {parsed.plan ? (
        <div className="rounded-lg border border-border/50 bg-s1 px-2.5 py-2">
          <div className="text-[10px] font-medium text-muted-foreground">{ANSWER_PLAN_LABEL}</div>
          <div className="mt-1 whitespace-pre-wrap text-[12px] leading-relaxed text-foreground/90">
            {parsed.plan}
          </div>
        </div>
      ) : null}

      {/* 4. 口径 + 动作 */}
      <div className="flex flex-wrap items-center gap-1.5">
        {parsed.caliber ? (
          <span className="rounded border border-border/60 px-1.5 py-0.5 text-[10px] text-muted-foreground">
            {ANSWER_CALIBER_LABEL}: {parsed.caliber}
          </span>
        ) : null}
        {symbol ? (
          <a
            href={`/stocks/${symbol}`}
            className="rounded border border-role-watch/40 px-1.5 py-0.5 text-[10px] text-role-watch hover:bg-role-watch/10"
          >
            {symbol} {ANSWER_ACTION_WORKBENCH} ↗
          </a>
        ) : null}
        <button
          type="button"
          onClick={() => void copy()}
          className="flex items-center gap-1 rounded border border-border/60 px-1.5 py-0.5 text-[10px] text-muted-foreground hover:text-foreground"
        >
          {copied ? <Check className="h-2.5 w-2.5" /> : <Copy className="h-2.5 w-2.5" />}
          {copied ? ANSWER_ACTION_COPIED : ANSWER_ACTION_COPY}
        </button>
      </div>

      {/* 5. 追问：答完不知道问什么是最常见的断点 */}
      {onFollowUp ? (
        <div className="flex flex-wrap items-center gap-1.5 pt-0.5">
          <span className="text-[10px] text-muted-foreground/70">{ANSWER_FOLLOWUP_LABEL}</span>
          {FOLLOW_UP_QUESTIONS.map((q) => (
            <button
              key={q}
              type="button"
              onClick={() => onFollowUp(q)}
              className="rounded-full border border-border/60 px-2 py-0.5 text-[11px] text-muted-foreground hover:border-role-watch/50 hover:text-foreground"
            >
              {q}
            </button>
          ))}
        </div>
      ) : null}

      <div className="text-[10px] leading-relaxed text-muted-foreground/70">{parsed.disclaimer}</div>
    </div>
  )
}
