import type { ReactNode } from 'react'
import type { AnswerTrace } from '@/lib/copilot/evidence-provenance'
import { associationLabel } from '@/lib/copilot/evidence-provenance'

/**
 * 证据溯源条（2026-10-29 P1）—— **把「能查的」和「查不到的」分开写**。
 *
 * 这一条不是装饰，是对回答出处的一次公开审计。四行，各对应一条真凭据：
 *  1. 逐句来源：模型输出文本。当前链路**没有**「某条依据 → 某次工具结果」的关联，
 *     所以恒写「无逐句来源」，绝不按关键词猜一条出处；
 *  2. 运行关联：run_id + 创建时间 + 关联方式。文本匹配来的必须带「未验证」；
 *  3. 工具：只列已加载步骤里真实出现过的工具名，并明说顺序与依据条文不对应；
 *  4. 缺口：本轮取数里「未取到 / 未确认」的真实条数；没有取数痕迹就写没有，不写 0。
 *
 * 版式纪律：不含 card-surface、不含背景色、12px、tabular-nums；它是一条页脚注，
 * 视觉上不许比依据本身更重。
 */

function Line({ label, children, testId }: { label: string; children: ReactNode; testId: string }) {
  return (
    <div data-testid={testId} className="flex gap-1.5 text-[12px] leading-relaxed">
      <span className="shrink-0 text-muted-foreground">{label}</span>
      <span className="min-w-0 text-muted-foreground">{children}</span>
    </div>
  )
}

export function EvidenceTraceNote({
  trace,
  rowCount,
}: {
  trace: AnswerTrace
  /** 这条回答里实际展示的依据条数（可选；未知就不写条数） */
  rowCount?: number
}) {
  const { verified, association, runId, createdAt, stepCount, tools, failedCount, unknownCount } = trace

  return (
    <div
      data-testid="copilot-evidence-trace"
      data-verified={verified ? 'true' : 'false'}
      data-association={association}
      className="space-y-0.5 text-[12px] leading-relaxed"
    >
      <Line label="逐句来源" testId="evidence-trace-rows">
        {typeof rowCount === 'number' && rowCount > 0 ? `${rowCount} 条依据 · ` : ''}
        模型输出文本 · 无逐句来源（当前不记录某条依据来自哪一次工具结果，因此不逐条标注出处）
      </Line>

      <Line label="运行关联" testId="evidence-trace-run">
        {runId ? (
          <>
            <span className="font-mono text-foreground">{runId}</span>
            {createdAt ? <span className="tabular-nums"> · {createdAt}</span> : null}
            {stepCount != null ? (
              <span className="tabular-nums"> · {stepCount} 步</span>
            ) : (
              <span> · 步骤未加载</span>
            )}
            <span> · {associationLabel(association)}</span>
          </>
        ) : (
          <>未关联到运行记录（没有 run_id，也没有可匹配的提问）</>
        )}
      </Line>

      {tools.length ? (
        <Line label="本轮工具" testId="evidence-trace-tools">
          <span className="font-mono">{tools.join('、')}</span>
          <span>（真实步骤；顺序与依据条文不对应）</span>
        </Line>
      ) : null}

      <Line label="取数缺口" testId="evidence-trace-gaps">
        {failedCount == null && unknownCount == null ? (
          <>无取数痕迹（本条回答没有折到取数事件）</>
        ) : (
          <>
            {(failedCount ?? 0) > 0 ? <span className="tabular-nums">未取到 {failedCount}</span> : null}
            {(failedCount ?? 0) > 0 && (unknownCount ?? 0) > 0 ? <span> · </span> : null}
            {(unknownCount ?? 0) > 0 ? <span className="tabular-nums">未确认 {unknownCount}</span> : null}
            {(failedCount ?? 0) === 0 && (unknownCount ?? 0) === 0 ? <span>本轮取数未见未取到或未确认</span> : null}
          </>
        )}
      </Line>
    </div>
  )
}

export default EvidenceTraceNote
