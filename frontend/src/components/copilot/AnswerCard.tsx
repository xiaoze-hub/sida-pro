import { useMemo, useState } from 'react'
import { Check, ChevronDown, Copy, Sparkles } from 'lucide-react'
import type { ParsedAnswer } from '@/lib/copilot/answer-blocks'
import {
  anchorKindLabel,
  extractPriceAnchors,
  isAnchorClickable,
  splitByAnchors,
  type PriceAnchorKind,
} from '@/lib/copilot/price-anchors'
import { DecisionCompassCard } from '@/components/copilot/DecisionCompassCard'
import {
  MultiDimensionEvidencePanel,
  type DimensionPerspective,
} from '@/components/copilot/MultiAgentDebatePanel'
import { ChartOverlayService } from '@/lib/copilot/chartOverlayService'
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
 * 从参谋结构化回答提取真实支撑与阻力位（严格语义匹配，不把止损或目标混淆为支撑阻力，缺项保持 null）
 */
function extractKeyLevels(parsed: ParsedAnswer): {
  supportPrice: number | null
  resistancePrice: number | null
  stopLossPrice: number | null
  targetPrice: number | null
  currentPrice: number | null
  costPrice: number | null
} {
  const allText = [parsed.verdict, ...parsed.evidence, parsed.plan].filter(Boolean).join('\n')
  const anchors = extractPriceAnchors(allText).filter(isAnchorClickable)

  let supportPrice: number | null = null
  let resistancePrice: number | null = null
  let stopLossPrice: number | null = null
  let targetPrice: number | null = null
  let currentPrice: number | null = null
  let costPrice: number | null = null

  for (const a of anchors) {
    const label = anchorKindLabel(a.kind, a.context)
    if (label === '支撑' && supportPrice == null) {
      supportPrice = a.value
    } else if (label === '阻力' && resistancePrice == null) {
      resistancePrice = a.value
    } else if (label === '止损' && stopLossPrice == null) {
      stopLossPrice = a.value
    } else if (label === '目标' && targetPrice == null) {
      targetPrice = a.value
    } else if (
      (a.context.includes('现价') || a.context.includes('最新') || a.context.includes('当前')) &&
      currentPrice == null
    ) {
      currentPrice = a.value
    } else if (a.context.includes('成本') && costPrice == null) {
      costPrice = a.value
    }
  }

  return { supportPrice, resistancePrice, stopLossPrice, targetPrice, currentPrice, costPrice }
}

/**
 * 从参谋依据中提取真实的多维度依据透视（从真实依据拆解，无匹配则返回空数组，杜绝虚构）
 */
function extractRealPerspectives(evidence: string[]): DimensionPerspective[] {
  const perspectives: DimensionPerspective[] = []
  for (const item of evidence) {
    if (/技术|均线|形态|MACD|K线|突破|回踩/i.test(item) && !perspectives.some((p) => p.role === 'technical')) {
      perspectives.push({
        role: 'technical',
        agentName: '技术面分析',
        status: item.slice(0, 10),
        conclusion: item,
      })
    } else if (/资金|主力|暗盘|龙虎榜|流入|流出|净额/i.test(item) && !perspectives.some((p) => p.role === 'funds')) {
      perspectives.push({
        role: 'funds',
        agentName: '资金与筹码',
        status: item.slice(0, 10),
        conclusion: item,
      })
    } else if (/题材|板块|舆情|热度|消息|概念|行业/i.test(item) && !perspectives.some((p) => p.role === 'theme')) {
      perspectives.push({
        role: 'theme',
        agentName: '板块与题材',
        status: item.slice(0, 10),
        conclusion: item,
      })
    } else if (/基本面|业绩|估值|财报|营收|利润|PE|PB/i.test(item) && !perspectives.some((p) => p.role === 'fundamentals')) {
      perspectives.push({
        role: 'fundamentals',
        agentName: '基本面研判',
        status: item.slice(0, 10),
        conclusion: item,
      })
    }
  }
  return perspectives
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
            className="mx-[1px] inline-flex items-baseline gap-0.5 rounded border border-border px-1 align-baseline font-mono text-[12px] tabular-nums text-foreground hover:border-role-watch/60 hover:text-role-watch"
          >
            <span>{seg.text}</span>
            {label !== '--' ? (
              <span className="text-[12px] text-muted-foreground">{label}</span>
            ) : null}
          </button>
        )
      })}
    </>
  )
}

/**
 * 参谋回答卡片（2026-10-19, P1 排版；2026-10-28 顶级金融终端罗盘与多智能体实测升级）。
 *
 * 分层顺序固定为「裁决 → 罗盘 → 会诊证据链 → 依据 → 预案 → 口径」：
 *   1. 裁决条：一句定性 + 置信度徽标（置信度来自原文 x/10，拿不到就不显示，不编）；
 *   2. 决策罗盘：当且仅当原文存在真实支撑与阻力时呈现，提供水平价位罗盘与一键画线；
 *   3. 会诊面板：当依据具备多维度特征时结构化展现真实证据链；
 *   4. 依据：默认 3 条，可展开全部；
 *   5. 预案：原文保留（持仓者/空仓者的分叉在模型输出里）；
 *   6. 口径 + 免责：固定尾部，回答里到底用的是哪套口径必须可见。
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

  const keyLevels = useMemo(() => extractKeyLevels(parsed), [parsed])
  const realPerspectives = useMemo(() => extractRealPerspectives(parsed.evidence), [parsed.evidence])

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
        <div className="card-surface rounded-lg bg-role-watch/8 px-2.5 py-2">
          <div className="flex flex-wrap items-center gap-1.5">
            <Sparkles className="h-3.5 w-3.5 shrink-0 text-role-watch" />
            <span className="text-[12px] font-medium text-role-watch">{ANSWER_VERDICT_LABEL}</span>
            {parsed.confidence != null ? (
              <span
                data-testid="copilot-answer-confidence"
                className="rounded border border-role-watch/40 px-1 text-[12px] text-role-watch"
              >
                {ANSWER_CONFIDENCE_LABEL} {parsed.confidence}/10
              </span>
            ) : null}
          </div>
          <div className="mt-1 text-[14px] leading-relaxed text-foreground">
            <AnchoredText text={parsed.verdict} onAnchor={onAnchoredPrice} />
          </div>
        </div>
      ) : null}

      {/* 2. 数智决策罗盘（仅在解析出真实支撑与阻力位时展现） */}
      {keyLevels.supportPrice != null && keyLevels.resistancePrice != null ? (
        <DecisionCompassCard
          ticker={symbol}
          currentPrice={keyLevels.currentPrice ?? undefined}
          costPrice={keyLevels.costPrice ?? undefined}
          supportPrice={keyLevels.supportPrice}
          resistancePrice={keyLevels.resistancePrice}
          stopLossPrice={keyLevels.stopLossPrice ?? undefined}
          targetPrice={keyLevels.targetPrice ?? undefined}
          biasLabel={parsed.verdict ? parsed.verdict.slice(0, 8) : '多空裁决'}
          confidence={parsed.confidence != null ? Math.round(parsed.confidence * 10) : undefined}
          tactics={
            parsed.plan
              ? parsed.plan
                  .split('\n')
                  .map((l) => l.trim())
                  .filter((l) => l.length > 0 && !l.startsWith('【'))
                  .slice(0, 3)
              : []
          }
          onDrawOnChart={() => {
            if (keyLevels.supportPrice != null && keyLevels.resistancePrice != null) {
              const saved = ChartOverlayService.saveOverlay(symbol, [
                { price: keyLevels.supportPrice, title: `支撑 ${keyLevels.supportPrice}`, kind: 'support' },
                { price: keyLevels.resistancePrice, title: `阻力 ${keyLevels.resistancePrice}`, kind: 'pressure' },
              ])
              if (!saved) {
                window.alert('画线未保存：请确认已登录、股票代码有效且浏览器允许会话存储。')
                return
              }
              onAnchoredPrice?.({
                value: keyLevels.supportPrice,
                kind: 'level',
                context: '支撑',
              })
              // 若当前不在该个股工作台且提供了 symbol，自动引导切入工作台
              if (symbol && typeof window !== 'undefined') {
                const target = `/stocks/${encodeURIComponent(symbol)}`
                if (!window.location.pathname.startsWith(target)) {
                  window.location.href = target
                }
              }
            }
          }}
          onSetAlert={symbol ? (p) => {
            if (typeof window !== 'undefined' && Number.isFinite(p) && p > 0) {
              window.location.href = `/notifications?tab=alerts&symbol=${encodeURIComponent(
                symbol,
              )}&price=${encodeURIComponent(p)}&intent=support_break`
            }
          } : undefined}
          onCopy={() => void copy()}
        />
      ) : null}

      {/* 3. 多维依据透视（仅在依据具备多维度特征时展现真实多维视角链） */}
      {realPerspectives.length > 0 ? (
        <MultiDimensionEvidencePanel perspectives={realPerspectives} />
      ) : null}

      {/* 4. 依据 */}
      {parsed.evidence.length ? (
        <div className="card-surface rounded-lg bg-s1 px-2.5 py-2">
          <div className="flex items-center justify-between">
            <span className="text-[12px] font-medium text-muted-foreground">{ANSWER_EVIDENCE_LABEL}</span>
            <span className="text-[12px] text-muted-foreground">{parsed.evidence.length} 条</span>
          </div>
          <ul className="mt-1 space-y-1">
            {shownEvidence.map((e, i) => (
              <li key={`${i}-${e.slice(0, 12)}`} className="flex gap-1.5 text-[12px] leading-relaxed">
                <span className="mt-[2px] h-1 w-1 shrink-0 rounded-full bg-role-watch/70" />
                <span className="min-w-0 text-foreground">
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
              className="mt-1.5 flex items-center gap-1 text-[12px] text-role-watch hover:underline"
            >
              <ChevronDown className={`h-3.5 w-3.5 transition-transform ${expanded ? 'rotate-180' : ''}`} />
              {expanded
                ? ANSWER_EVIDENCE_COLLAPSE
                : `${ANSWER_EVIDENCE_EXPAND_PREFIX} ${hiddenCount} ${ANSWER_EVIDENCE_EXPAND_SUFFIX}`}
            </button>
          ) : null}
        </div>
      ) : null}

      {/* 5. 预案 */}
      {parsed.plan ? (
        <div className="card-surface rounded-lg bg-s1 px-2.5 py-2">
          <div className="text-[12px] font-medium text-muted-foreground">{ANSWER_PLAN_LABEL}</div>
          <div className="mt-1 whitespace-pre-wrap text-[12px] leading-relaxed text-foreground">
            {parsed.plan}
          </div>
        </div>
      ) : null}

      {/* 6. 口径 + 动作 */}
      <div className="flex flex-wrap items-center gap-1.5">
        {parsed.caliber ? (
          <span className="rounded border border-border px-1.5 py-0.5 text-[12px] text-muted-foreground">
            {ANSWER_CALIBER_LABEL}: {parsed.caliber}
          </span>
        ) : null}
        {symbol ? (
          <a
            href={`/stocks/${symbol}`}
            className="rounded border border-role-watch/40 px-1.5 py-0.5 text-[12px] text-role-watch hover:bg-role-watch/10"
          >
            {symbol} {ANSWER_ACTION_WORKBENCH} ↗
          </a>
        ) : null}
        <button
          type="button"
          onClick={() => void copy()}
          className="flex items-center gap-1 rounded border border-border px-1.5 py-0.5 text-[12px] text-muted-foreground hover:text-foreground"
        >
          {copied ? <Check className="h-3 w-3" /> : <Copy className="h-3 w-3" />}
          {copied ? ANSWER_ACTION_COPIED : ANSWER_ACTION_COPY}
        </button>
      </div>

      {/* 7. 追问：答完不知道问什么是最常见的断点 */}
      {onFollowUp ? (
        <div className="flex flex-wrap items-center gap-1.5 pt-0.5">
          <span className="text-[12px] text-muted-foreground">{ANSWER_FOLLOWUP_LABEL}</span>
          {FOLLOW_UP_QUESTIONS.map((q) => (
            <button
              key={q}
              type="button"
              onClick={() => onFollowUp(q)}
              className="rounded-full border border-border px-2 py-0.5 text-[12px] text-muted-foreground hover:border-role-watch/50 hover:text-foreground"
            >
              {q}
            </button>
          ))}
        </div>
      ) : null}

      <div className="text-[12px] leading-relaxed text-muted-foreground">{parsed.disclaimer}</div>
    </div>
  )
}
