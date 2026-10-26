/**
 * 证据卡（结论 ↔ 证据 ↔ 口径）—— 2026-10-25。
 *
 * **为什么要有**：结论本身不可复用，可复用的是「凭什么这么结论」。把一轮问答的
 * 问题、状态、工具链摆在一张卡里，用户可以一眼从结论回溯到每个工具调用，
 * 再顺着 args / 结果摘要核对口径。它不是 ToolTimeline 的翻版：ToolTimeline 是
 * 多轮索引（先选轮次再展开），本卡是**单轮的证据视图**（无展开、全部摊平）。
 *
 * 三条纪律（与全站一致）：
 *  1. **缺值一律 `--`**，永不补 0 —— 耗时拿不到是「没测到」，不是「0 毫秒」；
 *     步数拿不到就是「-- 步」，不拿 0 顶数。
 *  2. **失败步骤不隐藏**：`ok=false` 整行走 text-stock-down 并保留 ✗，
 *     失败是证据的一部分，藏起来等于伪造齐套。
 *  3. 层级只靠字号/字重/hairline，不用阴影、不用大圆角、不用透明度做文字降级。
 *
 * 组件是纯展示：不发请求、不读全局状态，数据全由上层给（run + steps）。
 */

import { safeInt, safeNum } from '@/lib/format'
import { formatElapsed } from '@/components/copilot/ToolTimeline'

/** 单步证据（与 CopilotRunStep 同形，允许字段缺失，宽容渲染） */
export interface EvidenceStep {
  seq: number
  tool: string
  ok: boolean
  elapsed_ms: number | null
  args?: string | null
  result_digest?: string | null
}

export interface EvidenceCardProps {
  run?: {
    run_id: string
    status: string
    recipe?: string | null
    tool_calls?: number | null
    elapsed_ms?: number | null
    question?: string | null
  } | null
  steps?: EvidenceStep[] | null
  loading?: boolean
  error?: string | null
}

/** 状态徽标：中文直说，颜色只用语义色（灰=正常/运行中，橙=降级，绿=失败） */
const STATUS_META: Record<string, { label: string; cls: string }> = {
  ok: { label: '成功', cls: 'text-muted-foreground' },
  degraded: { label: '降级', cls: 'text-amber-500' },
  error: { label: '失败', cls: 'text-stock-down' },
  running: { label: '运行中', cls: 'text-muted-foreground' },
}

/** 截断：折叠空白后按字符截，超出补省略号（不做中英混排宽度估算，宁可短一点） */
export function clip(text: string | null | undefined, n: number): string {
  const t = (text ?? '').replace(/\s+/g, ' ').trim()
  if (!t) return ''
  return t.length > n ? `${t.slice(0, n)}…` : t
}

/** 步数：有 steps 就数 steps（已到手的真数），否则退回 run.tool_calls，都没有给 `--` */
function stepCountText(steps: EvidenceStep[] | null | undefined, toolCalls: unknown): string {
  if (Array.isArray(steps)) return String(steps.length)
  return safeInt(toolCalls)
}

export function EvidenceCard({ run, steps, loading, error }: EvidenceCardProps) {
  const list = steps ?? null
  const question = clip(run?.question, 40)
  const status = run?.status ?? ''
  const meta = STATUS_META[status]
  // 未知状态不编造中文：原样显示后端给的字符串，颜色按中性灰
  const statusLabel = meta?.label ?? (status || '--')
  const statusCls = meta?.cls ?? 'text-muted-foreground'
  const countText = stepCountText(list, run?.tool_calls)
  const elapsedText = formatElapsed(run?.elapsed_ms)
  const reason = clip(error, 80)

  const body = () => {
    if (loading) {
      return (
        <div className="py-1 text-[12px] text-muted-foreground" data-testid="evidence-card-loading">
          证据加载中…
        </div>
      )
    }
    if (error) {
      return (
        <div className="py-1 text-[12px] text-stock-down" data-testid="evidence-card-error">
          证据读取失败{reason ? `（${reason}）` : ''}
        </div>
      )
    }
    if (!list || list.length === 0) {
      return (
        <div className="py-1 text-[12px] text-muted-foreground" data-testid="evidence-card-empty">
          本轮没有工具调用
        </div>
      )
    }
    return (
      <ul className="m-0 list-none p-0" data-testid="evidence-card-steps">
        {list.map((s) => {
          // 失败行整行同色：不靠图标单点表达，扫一眼就能看出这轮哪步断了
          const tone = s.ok ? 'text-foreground' : 'text-stock-down'
          const soft = s.ok ? 'text-muted-foreground' : 'text-stock-down'
          const args = clip(s.args, 60)
          const digest = clip(s.result_digest, 80)
          const ms = safeNum(s.elapsed_ms)
          return (
            <li
              key={`${s.seq}-${s.tool}`}
              className={`border-b border-border py-1 last:border-b-0 ${s.ok ? '' : 'text-stock-down'}`}
              data-testid={`evidence-step-${s.seq}`}
              data-ok={s.ok ? 'true' : 'false'}
              data-has-elapsed={ms === null ? 'false' : 'true'}
            >
              <div className="flex items-baseline gap-1.5">
                <span className={`w-5 shrink-0 text-right text-[12px] tabular-nums ${soft}`}>
                  {s.seq}
                </span>
                <span className={`shrink-0 text-[12px] ${soft}`} aria-hidden>
                  ·
                </span>
                <span className={`min-w-0 flex-1 truncate font-mono text-[12px] ${tone}`}>
                  {s.tool || '--'}
                </span>
                <span className={`shrink-0 text-[12px] tabular-nums ${soft}`}>
                  {formatElapsed(s.elapsed_ms)}
                </span>
                <span className={`shrink-0 text-[12px] ${soft}`} aria-hidden>
                  ·
                </span>
                <span
                  className={`w-3 shrink-0 text-[12px] ${s.ok ? 'text-muted-foreground' : 'text-stock-down'}`}
                  title={s.ok ? '成功' : '失败'}
                  data-testid={`evidence-step-mark-${s.seq}`}
                >
                  {s.ok ? '✓' : '✗'}
                </span>
              </div>
              {args ? (
                <div
                  className={`mt-0.5 pl-[26px] font-mono text-[12px] ${soft}`}
                  title={s.args || undefined}
                  data-testid={`evidence-step-args-${s.seq}`}
                >
                  {args}
                </div>
              ) : null}
              {digest ? (
                <div
                  className={`pl-[26px] font-mono text-[12px] ${soft}`}
                  title={s.result_digest || undefined}
                  data-testid={`evidence-step-digest-${s.seq}`}
                >
                  {digest}
                </div>
              ) : null}
            </li>
          )
        })}
      </ul>
    )
  }

  return (
    <div
      className="rounded-md border border-border px-2 py-1.5"
      data-testid="evidence-card"
      data-run-id={run?.run_id || undefined}
    >
      <div className="flex items-baseline gap-2">
        <span
          className="min-w-0 flex-1 truncate text-[12px] text-foreground"
          title={run?.question || undefined}
          data-testid="evidence-card-question"
        >
          {question || '--'}
        </span>
        <span
          className={`shrink-0 text-[12px] ${statusCls}`}
          data-testid="evidence-card-status"
          data-status={status || 'unknown'}
        >
          {statusLabel}
        </span>
        <span
          className="shrink-0 text-[12px] tabular-nums text-muted-foreground"
          data-testid="evidence-card-meta"
        >
          {countText} 步 · {elapsedText}
        </span>
      </div>
      <div className="mt-1 border-t border-border pt-1">{body()}</div>
    </div>
  )
}

export default EvidenceCard
