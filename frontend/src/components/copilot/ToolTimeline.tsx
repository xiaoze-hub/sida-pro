import { useState } from 'react'
import { ChevronDown, ChevronRight } from 'lucide-react'
import { safeFixed, safeInt, safeNum } from '@/lib/format'
import { formatTime } from '@/lib/utils'
import { Skeleton } from '@/components/Skeleton'
import type { CopilotRunDetail, CopilotRunRow, CopilotRunStep } from '@/lib/copilot/api'

/**
 * 工具时间线（对话内展开看"它到底调了什么工具、多久、拿到什么"）。
 *
 * 口径铁律（与全站一致）：
 *  - 缺失一律 `--`，**不补 0**（耗时/工具数拿不到就是"没测到"，不是"0 毫秒"）；
 *  - 状态色用语义色（灰=正常 / 橙=降级 / 红=失败），不做渐变、不做彩色背景；
 *  - 平铺列表 + hairline 分隔，**不做卡片堆砌、不用阴影**表达层级。
 *
 * 组件是纯展示：数据由上层给（`runs`），点开某轮只回调 `onLoadDetail(run_id)`，
 * 详情回来后由上层塞进 `details`。组件自己不发请求。
 */

/** 后端 step 的扩展形状：`error` 字段在库里存在，接口可能不带，缺失按"无失败原因"处理。 */
export interface ToolTimelineStep extends CopilotRunStep {
  error?: string | null
}

/** 单轮详情（宽容形状：run 字段缺失也不炸）。 */
export interface ToolTimelineDetail {
  run?: CopilotRunDetail['run']
  steps: ToolTimelineStep[]
}

export type ToolTimelineDetails = Record<string, ToolTimelineDetail>

export interface ToolTimelineProps {
  /** 每轮问答的骨架（后端 /copilot/runs 的行） */
  runs: CopilotRunRow[]
  /** 点开某轮时的回调：上层负责拉详情并回填 `details` */
  onLoadDetail?: (runId: string) => void
  /** 已加载的单轮详情：run_id → 详情。未命中的轮次显示"加载中"骨架 */
  details?: ToolTimelineDetails
  className?: string
}

/** 状态徽标：灰=ok / 橙=degraded / 红=error（未知状态按灰处理，不编造语义） */
const STATUS_META: Record<string, { label: string; cls: string }> = {
  ok: { label: 'ok', cls: 'text-muted-foreground' },
  degraded: { label: 'degraded', cls: 'text-amber-500' },
  error: { label: 'error', cls: 'text-red-500' },
  running: { label: 'running', cls: 'text-muted-foreground' },
}

/** 耗时单位自动：<1000ms 给毫秒，否则给秒（不裸写 toFixed，走 @/lib/format） */
export function formatElapsed(ms: number | null | undefined): string {
  const n = safeNum(ms)
  if (n === null) return '--'
  if (Math.abs(n) < 1000) return `${Math.round(n)}ms`
  return `${safeFixed(n / 1000, 1)}s`
}

/** 摘要截断：工具返回可能有整段 JSON，列表里只给一眼能扫完的长度 */
function digestOf(text: string | null | undefined): string {
  const t = (text ?? '').trim()
  if (!t) return ''
  return t.length > 120 ? `${t.slice(0, 120)}…` : t
}

export function ToolTimeline({ runs, onLoadDetail, details, className }: ToolTimelineProps) {
  const [expanded, setExpanded] = useState<Record<string, boolean>>({})

  const toggle = (runId: string) => {
    const willOpen = !expanded[runId]
    setExpanded((prev) => ({ ...prev, [runId]: willOpen }))
    // 只在展开时拉详情：收起不该触发请求
    if (willOpen) onLoadDetail?.(runId)
  }

  const list = runs || []

  if (list.length === 0) {
    return (
      <div className={className} data-testid="copilot-tool-timeline">
        <div
          className="py-1 text-[12px] text-muted-foreground"
          data-testid="copilot-tool-timeline-empty"
        >
          本轮没有工具调用
        </div>
      </div>
    )
  }

  return (
    <div className={className} data-testid="copilot-tool-timeline">
      <ul className="m-0 list-none p-0">
        {list.map((run) => {
          const open = !!expanded[run.run_id]
          const detail = details?.[run.run_id]
          const meta = STATUS_META[run.status] ?? {
            label: run.status || '未知',
            cls: 'text-muted-foreground',
          }
          const toolCount = safeNum(run.tool_calls)
          // 空态判定：详情已回且无 step；或骨架本身就写着 0 次调用（没调过工具）
          const knownEmpty = detail ? detail.steps.length === 0 : toolCount === 0

          return (
            <li key={run.run_id} className="border-b border-border last:border-b-0">
              <button
                type="button"
                onClick={() => toggle(run.run_id)}
                aria-expanded={open}
                title={run.question || undefined}
                data-testid={`copilot-run-row-${run.run_id}`}
                className="flex w-full items-center gap-2 py-1 text-left hover:bg-s2"
              >
                {open ? (
                  <ChevronDown className="h-3 w-3 shrink-0 text-muted-foreground" aria-hidden />
                ) : (
                  <ChevronRight className="h-3 w-3 shrink-0 text-muted-foreground" aria-hidden />
                )}
                <span className="w-[42px] shrink-0 font-mono text-[12px] text-muted-foreground">
                  {formatTime(run.created_at) || '--'}
                </span>
                <span className="min-w-0 flex-1 truncate text-[12px] text-foreground">
                  {run.recipe || '常规'}
                </span>
                <span className="shrink-0 font-mono text-[12px] text-muted-foreground">
                  {safeInt(run.tool_calls)} 工具
                </span>
                <span className="shrink-0 font-mono text-[12px] text-muted-foreground">
                  {formatElapsed(run.elapsed_ms)}
                </span>
                <span
                  className={`shrink-0 text-[12px] ${meta.cls}`}
                  data-testid={`copilot-run-status-${run.run_id}`}
                >
                  {meta.label}
                </span>
              </button>

              {open ? (
                <div
                  className="border-l border-border pb-1 pl-2"
                  data-testid={`copilot-run-steps-${run.run_id}`}
                >
                  {knownEmpty ? (
                    <div
                      className="py-1 text-[12px] text-muted-foreground"
                      data-testid="copilot-run-empty"
                    >
                      本轮没有工具调用
                    </div>
                  ) : !detail ? (
                    <div className="space-y-1 py-1" data-testid="copilot-run-loading">
                      <div className="text-[12px] text-muted-foreground">加载中</div>
                      <Skeleton shape="text" className="h-2 w-1/2" />
                    </div>
                  ) : (
                    <ul className="m-0 list-none space-y-0.5 p-0 py-1">
                      {detail.steps.map((step) => {
                        // 失败原因：优先 error 字段，接口没带就退回结果摘要
                        const reason = digestOf(step.error) || digestOf(step.result_digest)
                        return (
                          <li
                            key={step.seq}
                            className="flex items-baseline gap-2"
                            title={step.args || undefined}
                            data-testid={`copilot-step-${step.seq}`}
                          >
                            <span className="font-mono text-[12px] text-foreground">
                              {step.tool}
                            </span>
                            <span className="shrink-0 font-mono text-[12px] text-muted-foreground">
                              {formatElapsed(step.elapsed_ms)}
                            </span>
                            {step.ok ? (
                              <span className="shrink-0 text-[12px] text-muted-foreground">ok</span>
                            ) : (
                              <span className="shrink-0 text-[12px] text-red-500">失败</span>
                            )}
                            {reason ? (
                              <span className="min-w-0 flex-1 truncate text-[12px] text-muted-foreground">
                                {step.ok ? reason : `原因：${reason}`}
                              </span>
                            ) : null}
                          </li>
                        )
                      })}
                    </ul>
                  )}
                </div>
              ) : null}
            </li>
          )
        })}
      </ul>
    </div>
  )
}

export default ToolTimeline
