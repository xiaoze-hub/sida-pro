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
import {
  DecisionCompassCard,
  MODEL_SELF_RATING_LABEL,
  normalizeSelfRating,
} from '@/components/copilot/DecisionCompassCard'
import {
  MultiDimensionEvidencePanel,
  type DimensionPerspective,
} from '@/components/copilot/MultiAgentDebatePanel'
import {
  extractGapLines,
  partitionEvidence,
  scanRisk,
  splitCaliber,
  splitEvidenceSource,
  splitLines,
  stripDisclaimer,
  stripRiskTail,
} from '@/components/copilot/answer-sections'
import { EvidenceTraceNote } from '@/components/copilot/EvidenceTraceNote'
import type { AnswerTrace } from '@/lib/copilot/evidence-provenance'
import type { EvidenceItem, UnverifiedNumber } from '@/lib/copilot/types'
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

/**
 * 风险 / 数据缺口两层的文案。
 *
 * 固化在组件旁边而不是 `lib/copilot/copy.ts`：这一轮只动「回答 / 证据」组件
 * （`components/copilot/`），而 copy.ts 同时被卡片、胶囊、简报共用，改它会把这次打磨
 * 扩散成全站文案改动。两句都是**中性描述**：不含买卖指令、不承诺收益。
 *
 * 2026-10-29 审查修复：两句话都曾**说过头**——
 *  - `风险与失效条件`：模型只写「量能萎缩则结论不成立」时，原文根本没有失效条件，
 *    标题却替它承诺了一个分类。现在只说「风险」，条目是什么就显示什么。
 *  - `…未参与上述结论`：回答里没有任何机制能证明这几条缺失数据没影响结论，这句话是
 *    替模型下的结论。现在只陈述事实：这些是回答里声明没取到的数据。
 */
const ANSWER_RISK_LABEL = '风险'
const ANSWER_GAP_LABEL = '数据缺口'
const ANSWER_GAP_NOTE = '以下为回答中声明未取到的数据'

/** 首屏只露 3 条依据：结论 + 关键证据一屏读完，其余折叠（信息量不变，首屏变短）。 */
const EVIDENCE_PREVIEW = 3

/**
 * 结构化证据区文案（2026-11-03）。与「依据」区是两件事：
 *   - 依据 = 模型写进正文的依据行（文本，出处是模型自述）；
 *   - 证据 = 后端这一轮真的调了哪些工具、什么时点、取没取到（`done.evidence`）。
 * 标题只说「证据」，不写「核验通过」——`status=ok` 只代表工具有返回，不代表结论对。
 */
const ANSWER_PROVENANCE_LABEL = '证据'
const ANSWER_PROVENANCE_MISSING = '未取到'
const ANSWER_UNVERIFIED_LABEL = (n: number) => `${n} 个数字未通过对账`
const ANSWER_UNVERIFIED_NOTE =
  '标记只说明这些数字没在本轮取数原文里找到，不等于数值算错；回答正文不做删改。'

/** 价格锚点：点 chip 时回传给上层（K 线画线由另一个模块拥有，这里只出事件）。 */
export interface AnchoredPrice {
  value: number
  kind: PriceAnchorKind
  context: string
}

/**
 * 从参谋回答**正文文本**里认出的支撑/阻力位（严格语义匹配，不把止损或目标混淆为支撑阻力，
 * 缺项保持 null）。
 *
 * ⚠️ 2026-10-28 P2：这是**文本识别**，不是核验过的图表标注 —— 价格是模型写的字，没有
 * 服务端标的/时间/来源关联，也没有用户确认。因此结果只用于「看得见的文字展示」，
 * **不得**再喂给画线服务：`ChartOverlayService` 在服务端标注读取接通前恒拒画
 * （本地对象/sessionStorage 都可伪造，不能当凭证），见下方来源提示。
 */
function extractTextLevels(parsed: ParsedAnswer): {
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

/** 节标题：左侧名称 + 右侧计数（计数走 tabular，条数变化时不抖）。 */
function SectionHead({ label, count }: { label: string; count?: string }) {
  return (
    <div className="flex items-baseline justify-between gap-2">
      <span className="text-[12px] font-medium text-foreground">{label}</span>
      {count ? (
        <span className="shrink-0 text-[12px] tabular-nums text-muted-foreground">{count}</span>
      ) : null}
    </div>
  )
}

/**
 * 参谋回答卡片（2026-10-19, P1 排版；2026-10-28 顶级金融终端罗盘与多智能体实测升级；
 * 2026-10-29 回答可读性打磨）。
 *
 * 分层顺序固定为「裁决 → 罗盘 → 风险 → 会诊证据链 → 依据 → 数据缺口 → 预案 → 口径」：
 *   1. 裁决条：一句定性 + 自评徽标（自评来自原文 x/10，拿不到就不显示，不编）；
 *   2. 决策罗盘：当且仅当原文存在真实支撑与阻力时呈现（价位只展示，不上图）；
 *   3. 风险：**只**在模型写了【风险】段时出现 —— 绝不从依据里反推风险，也不替模型
 *      承诺「失效条件」这个分类（条目是什么就说什么）；多段风险逐段收齐；
 *   4. 会诊面板：当依据具备多维度特征时结构化展现真实证据链；
 *   5. 依据：默认 3 条、可展开全部；尾部「（…口径）」拆成来源标记，正文只留读数；
 *   6. 数据缺口：回答自己声明「暂无 / 未取到」的条目，单独一层，一条不丢（含写进风险段
 *      的缺失条目）；只陈述「声明未取到」，不断言它有没有影响结论；
 *   7. 预案：原文保留（持仓者/空仓者的分叉在模型输出里）；
 *   8. 口径：拆成条目 + 固定标签，回答里到底用的是哪套口径必须一眼可读。
 *
 * 版式纪律：外层会话气泡已经是 `card-surface`，这里**不再叠卡片** —— 节与节之间只用
 * hairline（`border-t border-border`）分节，圆角底色只留给裁决条，层级靠字号与留白表达。
 */
export function AnswerCard({
  parsed,
  symbol,
  onFollowUp,
  onAnchoredPrice,
  trace,
  evidence,
  unverified,
}: {
  parsed: ParsedAnswer
  symbol?: string
  onFollowUp?: (question: string) => void
  onAnchoredPrice?: (a: AnchoredPrice) => void
  /**
   * 证据溯源（2026-10-29 P1，可选）。**由上层显式计算后传入** —— 卡片自己不去猜
   * 「这条消息对应哪一轮 run」（消息表没有 run_id，唯一的文本匹配是推断）。
   * 不传就不显示溯源条：老调用点版面零变化，也不会凭空冒出一条「来源」。
   * 数据形状见 `lib/copilot/evidence-provenance.ts`。
   */
  trace?: AnswerTrace
  /**
   * 结构化证据（2026-11-03，可选）：后端 `done.evidence` 原样传下来，
   * **不由卡片自己推算**。空数组 / 未传 → 整区不渲染（不写「0 条」，也不补占位）。
   */
  evidence?: EvidenceItem[]
  /** 后端 `done.evidence_audit.unverified` 原样；空 → 不做标记、不提示。 */
  unverified?: UnverifiedNumber[]
}) {
  const [expanded, setExpanded] = useState(false)
  const [copied, setCopied] = useState(false)

  /** 先截掉黏在结论/预案/口径尾巴上的风险段：同一句风险不能出现两次。 */
  const verdict = useMemo(() => stripRiskTail(parsed.verdict), [parsed.verdict])
  const plan = useMemo(() => stripRiskTail(parsed.plan), [parsed.plan])
  /**
   * 风险段可能有多段：一次扫出全部条目，顺带把混进风险段的「未取到」归到缺口
   * （缺失数据不是失效条件，不能顶着风险的名义展示）。
   */
  const riskScan = useMemo(() => scanRisk(parsed.raw), [parsed.raw])
  const riskLines = riskScan.risks
  const rawGaps = useMemo(() => extractGapLines(parsed.raw), [parsed.raw])
  const caliberParts = useMemo(
    () => splitCaliber(stripDisclaimer(stripRiskTail(parsed.caliber), parsed.disclaimer)),
    [parsed.caliber, parsed.disclaimer],
  )
  /**
   * 依据里只留「真的只有依据才有的字」：缺口段 / 风险段 / 预案 / 口径已经各自成节，
   * 这些行再在依据里列一遍就是重复，还会把依据条数算多。
   */
  const shownElsewhere = useMemo(
    () => [...rawGaps, ...riskScan.gaps, ...splitLines(plan), ...caliberParts],
    [rawGaps, riskScan, plan, caliberParts],
  )
  const { data: evidenceRows, gaps: evidenceGaps } = useMemo(
    () => partitionEvidence(parsed.evidence, riskLines, shownElsewhere),
    [parsed.evidence, riskLines, shownElsewhere],
  )
  /** 缺口来源三处（原文缺口段 / 风险段里的缺失 / 依据里声明的缺失），按原文顺序去重合并。 */
  const gaps = useMemo(() => {
    const seen = new Set<string>()
    const merged: string[] = []
    for (const g of [...rawGaps, ...riskScan.gaps, ...evidenceGaps]) {
      if (seen.has(g)) continue
      seen.add(g)
      merged.push(g)
    }
    return merged
  }, [rawGaps, riskScan, evidenceGaps])

  const hiddenCount = Math.max(0, evidenceRows.length - EVIDENCE_PREVIEW)
  const shownEvidence = useMemo(
    () => (expanded ? evidenceRows : evidenceRows.slice(0, EVIDENCE_PREVIEW)),
    [expanded, evidenceRows],
  )

  const keyLevels = useMemo(
    () => extractTextLevels({ ...parsed, verdict, plan }),
    [parsed, verdict, plan],
  )
  const realPerspectives = useMemo(() => extractRealPerspectives(evidenceRows), [evidenceRows])
  /**
   * 回答正文里的「置信度 8/10」是模型的**自评分**，不是统计置信度。
   * 这里只做取值校验（0~10），既不换算成百分制、也不兜底默认值。
   */
  const selfRating = useMemo(() => normalizeSelfRating(parsed.confidence), [parsed.confidence])

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(parsed.raw)
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1500)
    } catch {
      // 剪贴板不可用（非 https / 权限拒绝）→ 静默，不弹错误打断阅读
    }
  }

  /**
   * 罗盘入参：`hasLevels` 是 const 布尔，TS 的 aliased-condition 收窄只在直接引用
   * 同名局部变量时生效 —— 所以先取成局部量，避免在 JSX 里对 `keyLevels.supportPrice`
   * 反复断言（非空断言会掩盖真正的缺项）。
   */
  const supportPrice = keyLevels.supportPrice
  const resistancePrice = keyLevels.resistancePrice
  const hasLevels = supportPrice != null && resistancePrice != null

  /** 结构化证据：只按后端给的顺序展示，不排序、不补条数、不去重。 */
  const evidenceList = evidence ?? []
  const unverifiedList = unverified ?? []

  return (
    <div data-testid="copilot-answer-card" className="space-y-2.5">
      {/* 1. 裁决条（全卡唯一的圆角底色块：一眼先看到结论） */}
      {verdict ? (
        <div className="rounded-lg bg-role-watch/8 px-2.5 py-2">
          <div className="flex flex-wrap items-center gap-1.5">
            <Sparkles className="h-3.5 w-3.5 shrink-0 text-role-watch" />
            <span className="text-[12px] font-medium text-role-watch">{ANSWER_VERDICT_LABEL}</span>
            {selfRating != null ? (
              <span
                data-testid="copilot-answer-confidence"
                className="rounded border border-role-watch/40 px-1 text-[12px] text-role-watch"
                title="模型自评：模型自己对结论把握的主观打分，非统计置信度或准确率"
              >
                {ANSWER_CONFIDENCE_LABEL}（{MODEL_SELF_RATING_LABEL}） {selfRating}/10
              </span>
            ) : null}
          </div>
          <div className="mt-1 text-[14px] leading-relaxed text-foreground">
            <AnchoredText text={verdict} onAnchor={onAnchoredPrice} />
          </div>
        </div>
      ) : null}

      {/* 2. 数智决策罗盘（仅在解析出真实支撑与阻力位时展现） */}
      {hasLevels ? (
        <DecisionCompassCard
          ticker={symbol}
          currentPrice={keyLevels.currentPrice ?? undefined}
          costPrice={keyLevels.costPrice ?? undefined}
          supportPrice={supportPrice}
          resistancePrice={resistancePrice}
          stopLossPrice={keyLevels.stopLossPrice ?? undefined}
          targetPrice={keyLevels.targetPrice ?? undefined}
          biasLabel={verdict ? verdict.slice(0, 8) : '多空裁决'}
          modelConfidence={selfRating ?? undefined}
          tactics={
            plan
              ? plan
                  .split('\n')
                  .map((l) => l.trim())
                  .filter((l) => l.length > 0 && !l.startsWith('【'))
                  .slice(0, 3)
              : []
          }
          /**
           * 2026-10-28 P2 止血：**不传** `onDrawOnChart`。
           *
           * 之前的「在 K 线叠加画线」用回答正文的正则提价直接画真 K 线 —— 价位没有
           * 服务端标的/时间/来源关联，用户没确认过任何提议，等于伪造「已核验标注」。
           * 这条不可信动作保持取消；真实链路是 `/copilot/actions` 提议 → 用户确认(apply) →
           * `/copilot/annotations` 读回（接线见 `lib/copilot/annotationLink`），
           * 由工作台按服务端标注行画线，本卡片一个价位都不送进图表。
           */
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

      {/*
        来源提示（2026-10-28 P2 三次收口）：罗盘上的价位来自回答正文的文本识别，
        没有服务端标的/价格来源/时间关联，也没有用户确认 —— 所以只展示、不上图。
        真正上图的路径是「待确认动作」里确认服务端情景标注 → apply → `/copilot/annotations`
        读回（`lib/copilot/annotationLink`）。那句话必须留在卡片上，否则用户会以为
        "罗盘上的数字已经过服务端核验"。
      */}
      {hasLevels ? (
        <div
          data-testid="copilot-level-provenance-note"
          className="border-l-2 border-border pl-2 text-[12px] leading-relaxed text-muted-foreground"
        >
          以上支撑/阻力来自回答正文的文本识别，未经服务端核验，未画到 K 线上。要上图请在「待确认动作」里确认服务端的情景标注提议：确认成功后按服务端标注记录画线，刷新后服务端仍在才会继续显示。
        </div>
      ) : null}

      {/* 3. 风险（模型没写就没有这一层，绝不从依据反推；标题不替模型承诺「失效条件」） */}
      {riskLines.length ? (
        <section
          data-testid="copilot-answer-risk"
          data-answer-section="true"
          className="border-t border-border pt-2"
        >
          <SectionHead label={ANSWER_RISK_LABEL} count={`${riskLines.length} 条`} />
          <ul className="mt-1 space-y-1">
            {riskLines.map((line, i) => (
              <li key={`${i}-${line.slice(0, 12)}`} className="flex gap-1.5 text-[12px] leading-relaxed">
                <span className="mt-[2px] h-1 w-1 shrink-0 rounded-full bg-amber-500" />
                {/* 风险条目不做价位锚点：它是失效条件复述，重复的价位交互会让人误读成可交易点位 */}
                <span className="min-w-0 text-foreground">{line}</span>
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      {/* 4. 多维依据透视（仅在依据具备多维度特征时展现真实多维视角链） */}
      {realPerspectives.length > 0 ? (
        <MultiDimensionEvidencePanel perspectives={realPerspectives} />
      ) : null}

      {/* 5. 依据（读数与来源分离；缺失条目另起一层，不在这里混排） */}
      {evidenceRows.length ? (
        <section
          data-testid="copilot-answer-evidence"
          data-answer-section="true"
          className="border-t border-border pt-2"
        >
          <SectionHead label={ANSWER_EVIDENCE_LABEL} count={`${evidenceRows.length} 条`} />
          <ul className="mt-1 space-y-1">
            {shownEvidence.map((e, i) => {
              const { body, source } = splitEvidenceSource(e)
              return (
                <li key={`${i}-${body.slice(0, 12)}`} className="flex gap-1.5 text-[12px] leading-relaxed">
                  <span className="mt-[2px] h-1 w-1 shrink-0 rounded-full bg-role-watch/70" />
                  <span className="min-w-0 text-foreground">
                    <AnchoredText text={body} onAnchor={onAnchoredPrice} />
                    {source ? (
                      /*
                       * 角标里的内容是**模型在正文里自己写的口径**（从括号尾巴拆出来的字），
                       * 不是服务端核验过的来源。
                       * 2026-10-29：上一版只把「未核验」写进 title —— 移动端/触屏根本看不到
                       * title，角标读起来仍像「这是数据出处」。现在把它**写在角标正文里**：
                       * `腾讯行情 · 模型自述·未核验`。宁可长一点，也不让一句模型自述冒充来源。
                       */
                      <span
                        data-testid="copilot-evidence-source"
                        title={`模型自述的口径（未核验）：${source}`}
                        className="ml-1 whitespace-nowrap rounded border border-border px-1 text-[12px] text-muted-foreground transition-colors hover:border-role-watch/50"
                      >
                        <span className="text-foreground">{source}</span>
                        <span className="ml-1">· 模型自述·未核验</span>
                      </span>
                    ) : null}
                  </span>
                </li>
              )
            })}
          </ul>
          {hiddenCount > 0 ? (
            <button
              type="button"
              onClick={() => setExpanded((v) => !v)}
              data-testid="copilot-answer-expand"
              className="mt-1.5 flex items-center gap-1 text-[12px] text-role-watch hover:underline cursor-pointer group"
            >
              <ChevronDown className={`h-3.5 w-3.5 transition-transform duration-200 ${expanded ? 'rotate-180' : ''}`} />
              {expanded
                ? ANSWER_EVIDENCE_COLLAPSE
                : `${ANSWER_EVIDENCE_EXPAND_PREFIX} ${hiddenCount} ${ANSWER_EVIDENCE_EXPAND_SUFFIX}`}
            </button>
          ) : null}
        </section>
      ) : null}

      {/*
        证据溯源（2026-10-29 P1，可选）：只有上层算好 trace 才出现。
        它说的三件事都必须是真的 —— 逐句来源没有、run 是哪种关联、缺口几条。
        不在这里现算：卡片拿不到「这条消息的 run_id」（消息表不存它）。
      */}
      {trace ? (
        <div className="border-t border-border pt-1.5">
          <EvidenceTraceNote trace={trace} rowCount={evidenceRows.length} />
        </div>
      ) : null}

      {/* 6. 数据缺口（回答自己说没拿到的：单独一层，一条不丢，也不冒充读数） */}
      {gaps.length ? (
        <section
          data-testid="copilot-answer-gaps"
          data-answer-section="true"
          className="border-t border-border pt-2"
        >
          <SectionHead label={ANSWER_GAP_LABEL} count={`${gaps.length} 条`} />
          <div className="mt-0.5 text-[12px] leading-relaxed text-muted-foreground">{ANSWER_GAP_NOTE}</div>
          <ul className="mt-1 space-y-1">
            {gaps.map((g, i) => (
              <li key={`${i}-${g.slice(0, 12)}`} className="flex gap-1.5 text-[12px] leading-relaxed">
                <span className="mt-[2px] h-1 w-1 shrink-0 rounded-full bg-amber-500" />
                <span className="min-w-0 text-muted-foreground">{g}</span>
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      {/* 7. 预案 */}
      {plan ? (
        <section
          data-testid="copilot-answer-plan"
          data-answer-section="true"
          className="border-t border-border pt-2"
        >
          <SectionHead label={ANSWER_PLAN_LABEL} />
          <div className="mt-1 whitespace-pre-wrap text-[12px] leading-relaxed text-foreground">{plan}</div>
        </section>
      ) : null}

      {/* 8. 口径：拆成条目，回答到底用的哪套口径必须一眼可读 */}
      {caliberParts.length ? (
        <section
          data-testid="copilot-answer-caliber"
          data-answer-section="true"
          className="flex flex-wrap items-center gap-1.5 border-t border-border pt-2"
        >
          <span className="text-[12px] text-muted-foreground">{ANSWER_CALIBER_LABEL}</span>
          {caliberParts.map((p) => (
            <span
              key={p}
              data-testid="copilot-caliber-part"
              className="rounded border border-border px-1.5 py-0.5 text-[12px] text-muted-foreground"
            >
              {p}
            </span>
          ))}
        </section>
      ) : null}

      {/*
        8.5 证据（2026-11-03）：后端 `done.evidence` 的结构化取数清单 —— 工具 · 时点 · 状态。
        与上面「依据」是两件事：依据是模型写进正文的字，这里是服务端记录的真实工具调用。
        空数组整区不渲染（不写「0 条」，也不补一条「全部取到」）。
      */}
      {evidenceList.length ? (
        <section
          data-testid="copilot-answer-provenance"
          data-answer-section="true"
          className="border-t border-border pt-2"
        >
          <SectionHead label={ANSWER_PROVENANCE_LABEL} count={`${evidenceList.length} 条`} />
          <ul data-testid="copilot-evidence-list" className="mt-1 space-y-0.5">
            {evidenceList.map((ev, i) => {
              const missing = ev.status === 'missing'
              // 旧版曾把所有工具默认写成 TDX/default，这对来源不是有效证据。
              const legacyDefault = ev.caliber === 'default'
              const source = !ev.source || ev.source === 'unknown' || legacyDefault ? '未确认' : ev.source
              const caliber = !ev.caliber || ev.caliber === 'unknown' || legacyDefault ? '未确认' : ev.caliber
              return (
                <li
                  key={ev.evidence_id || `ev-${i}`}
                  data-testid="copilot-evidence-item"
                  data-status={missing ? 'missing' : 'ok'}
                  title={`${ev.tool} · 来源 ${source} · 口径 ${caliber}${
                    ev.digest ? `\n${ev.digest}` : ''
                  }`}
                  className="flex items-baseline gap-1.5 text-[12px] leading-relaxed"
                >
                  <span
                    className={`mt-[2px] h-1 w-1 shrink-0 rounded-full ${
                      missing ? 'bg-amber-500' : 'bg-role-watch/70'
                    }`}
                  />
                  <span className="min-w-0">
                    <span className={missing ? 'text-muted-foreground line-through' : 'text-foreground'}>
                      {ev.tool}
                    </span>
                    <span className="mx-1 text-muted-foreground">·</span>
                    <span className="tabular-nums text-muted-foreground">
                      {ev.as_of && !legacyDefault ? ev.as_of : '--（基准日未确认）'}
                    </span>
                    <span className="mx-1 text-muted-foreground">·</span>
                    <span className="text-muted-foreground">来源 {source} · 口径 {caliber}</span>
                    <span className="mx-1 text-muted-foreground">·</span>
                    <span className={missing ? 'text-amber-500' : 'text-muted-foreground'}>
                      {missing ? ANSWER_PROVENANCE_MISSING : '已返回（未核验）'}
                    </span>
                  </span>
                </li>
              )
            })}
          </ul>
          {/*
            未对账数字（弱化标记，不隐藏答案）：`value` 用 `select-none` + 虚线删除线降级，
            复制正文也带不走它；同时把 kind/context 原样摆在旁边，指明「凭什么是这条」。
          */}
          {unverifiedList.length ? (
            <div
              data-testid="copilot-evidence-unverified"
              className="mt-1.5 border-t border-border pt-1.5"
            >
              <div className="text-[12px] text-amber-500">
                {ANSWER_UNVERIFIED_LABEL(unverifiedList.length)}
              </div>
              <ul className="mt-0.5 flex flex-wrap gap-x-2.5 gap-y-0.5">
                {unverifiedList.map((u, i) => (
                  <li key={`${u.value}-${i}`} className="text-[12px] leading-relaxed">
                    <span
                      data-testid="copilot-unverified-number"
                      title={u.context || u.kind || '后端未给上下文'}
                      className="select-none cursor-default font-mono tabular-nums text-muted-foreground line-through decoration-dotted"
                    >
                      {u.value}
                    </span>
                    {u.kind ? <span className="ml-1 text-muted-foreground">{u.kind}</span> : null}
                  </li>
                ))}
              </ul>
              <div className="mt-0.5 text-[12px] leading-relaxed text-muted-foreground">
                {ANSWER_UNVERIFIED_NOTE}
              </div>
            </div>
          ) : null}
        </section>
      ) : null}

      {/* 9. 动作 */}
      <div className="flex flex-wrap items-center gap-1.5">
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

      {/* 10. 追问：答完不知道问什么是最常见的断点 */}
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
