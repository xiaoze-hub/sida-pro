import { useState } from 'react'
import { ChevronDown, ChevronRight } from 'lucide-react'

import type { CopilotRunRow } from '@/lib/copilot/api'
import type { RunAssociation } from '@/lib/copilot/evidence-provenance'
import { formatElapsed } from '@/components/copilot/ToolTimeline'
import type { ToolTimelineStep } from '@/components/copilot/ToolTimeline'

/**
 * 内联步骤条（2026-10-25）。
 *
 * **为什么要有**：此前「它调了什么工具」藏在右侧「过程」面板里 —— 用户读完答案还得切面板
 * 才能验证结论出处，等于把可验证性做成了隐藏功能。Codex / deepseek harness 的做法是
 * **把工具调用直接挂在答案下面**：默认一行摘要，展开才是明细。
 *
 * 三条纪律（与全站一致）：
 *  1. **默认收起**，一行摘要：`过程 · 7 步 · 12.4s`。不抢答案的视觉重量。
 *  2. **展开才取数**（`onLoad` 只在第一次展开时调）—— 打开会话不该为每轮都打一次详情请求。
 *  3. 缺失一律 `--`，不补 0；失败步骤显式标红，不隐藏。
 *
 * 组件是纯展示：不自己发请求，详情由上层给。
 */

export interface InlineStepsProps {
  run: CopilotRunRow
  /** 已加载的步骤；未加载时展开显示"加载中" */
  steps?: ToolTimelineStep[]
  loading?: boolean
  /** 首次展开时回调（上层负责拉详情并回填 steps） */
  onLoad?: () => void
  /** 详情拿不到时的静默提示（不改成长篇报错：轨迹是旁路证据） */
  failed?: boolean
  /**
   * 这个 run 是怎么对到这条回答的（2026-10-29 P1）。
   *
   * 默认 `question-text`：现有唯一调用点（`AiCopilot.tsx` 的 `matchRunForMessage`）
   * 就是按提问文本匹配的 —— 消息表没有 run_id，可能对错轮。默认值取「最保守」：
   * 没说清就不许自称已核验。只有真的从服务端拿到 run_id 关联时才传 `run-id`。
   */
  association?: RunAssociation
}

const STATUS_CLS: Record<string, string> = {
  ok: 'text-muted-foreground',
  degraded: 'text-amber-500',
  error: 'text-stock-down',
  running: 'text-muted-foreground',
}

/**
 * 状态人话（2026-10-29）：摘要里原来直接甩 `degraded` / `error` 这类英文枚举，
 * 用户读到的是一串日志字段。这里给中文口径，但**原始状态码一个字都不删** ——
 * 它是排障时唯一能对上后端 `runs.status` 的锚点。未知状态照原样显示，不猜。
 */
const STATUS_HINT: Record<string, string> = {
  degraded: '部分降级',
  error: '出错',
  failed: '失败',
  cancelled: '已取消',
  canceled: '已取消',
  running: '进行中',
  timeout: '超时',
  ok: '完成',
}

function statusHint(status: string | null | undefined): string | null {
  const key = (status || '').toLowerCase()
  return STATUS_HINT[key] ?? null
}

function clip(s: string | null | undefined, n = 46): string {
  if (!s) return ''
  const t = s.replace(/\s+/g, ' ').trim()
  return t.length > n ? `${t.slice(0, n)}…` : t
}

export default function InlineSteps({
  run,
  steps,
  loading,
  onLoad,
  failed,
  association = 'question-text',
}: InlineStepsProps) {
  const [open, setOpen] = useState(false)

  const toolCalls = typeof run.tool_calls === 'number' ? run.tool_calls : null
  const statusCls = STATUS_CLS[run.status] || 'text-muted-foreground'
  const hint = statusHint(run.status)
  /** 失败步数：只有详情到手才数得出（未加载时是 null，不显示 0）。 */
  const failedSteps = steps ? steps.filter((s) => !s.ok).length : null
  /**
   * run 级状态是否正常。**零失败步骤 ≠ 这一轮全部成功**：harness 可能整轮 `error`
   * （模型调用失败、后面的步骤根本没机会跑），明细里看得见的几步都 ok 说明不了什么。
   * 只有 run 自己 ok 才敢说「全部成功」，否则只陈述事实「各步未见失败」。
   */
  const runOk = !run.status || run.status === 'ok'

  const toggle = () => {
    const next = !open
    setOpen(next)
    if (next && !steps && !loading) onLoad?.()
  }

  return (
    <div className="mt-1.5" data-testid="inline-steps">
      <button
        type="button"
        onClick={toggle}
        className="flex items-center gap-1 text-[12px] text-muted-foreground transition-colors hover:text-foreground"
        data-testid="inline-steps-toggle"
        aria-expanded={open}
      >
        {open ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
        <span>过程</span>
        <span className="tabular-nums">
          · {toolCalls === null ? '-- 步' : `${toolCalls} 步`} · {formatElapsed(run.elapsed_ms)}
        </span>
        {run.status !== 'ok' && (
          <span className={statusCls}>
            · {run.status}
            {hint ? <span className="text-muted-foreground">（{hint}）</span> : null}
          </span>
        )}
        {/*
          关联可信度（2026-10-29 P1）：这些步骤是「按提问文本匹配」挂上来的，
          用户读到的却是「这条结论这一轮调了这些工具」。不标出处就等于默认它可核验。
          run-id 关联是服务端给的，此时不必喊「未验证」。
        */}
        <span
          data-testid="inline-steps-association"
          data-association={association}
          title={
            association === 'run-id'
              ? '这一轮的 run 由服务端 run_id 关联'
              : '这一轮的 run 是按提问文本匹配到的：消息表不存 run_id，可能对错轮'
          }
          className={association === 'run-id' ? 'text-muted-foreground' : 'text-amber-500'}
        >
          {association === 'run-id' ? '· 已核验关联' : '· 未验证关联'}
        </span>
      </button>

      {open && (
        <div className="mt-1 border-l border-border pl-2" data-testid="inline-steps-list">
          {loading && !steps && (
            <div className="py-1 text-[12px] text-muted-foreground">步骤加载中…</div>
          )}
          {failed && !steps && !loading && (
            <div className="py-1 text-[12px] text-muted-foreground">这一轮的步骤没取到</div>
          )}
          {/*
            展开后先给一行「共 N 步 / 失败 M 步」：失败步骤虽然逐条标红，但要先让人知道
            这一轮到底有没有失败，否则得自己数完 7 行才发现第 3 步是 ✗。
            M 为 0 时不写「失败 0 步」—— 那是噪音，不是信息；但 run 级非 ok 时也不能把
            「明细里 0 步失败」说成「全部成功」。
          */}
          {steps && steps.length > 0 ? (
            <div
              data-testid="inline-steps-summary"
              className="flex items-baseline gap-1.5 py-1 text-[12px] tabular-nums text-muted-foreground"
            >
              <span>共 {steps.length} 步</span>
              {failedSteps ? (
                <span className="text-stock-down">失败 {failedSteps} 步</span>
              ) : runOk ? (
                <span>全部成功</span>
              ) : (
                <span className="text-amber-500">各步未见失败 · 本轮 {run.status}</span>
              )}
            </div>
          ) : null}
          {(steps ?? []).map((s) => (
            <div
              key={`${s.seq}-${s.tool}`}
              className="flex items-baseline gap-2 border-b border-border py-1 last:border-b-0"
              data-testid="inline-step"
            >
              <span className="w-4 shrink-0 text-right text-[12px] text-muted-foreground tabular-nums">
                {s.seq}
              </span>
              <span className="shrink-0 font-mono text-[12px] text-foreground">{s.tool}</span>
              {s.args && (
                <span className="min-w-0 truncate font-mono text-[12px] text-muted-foreground">
                  {clip(s.args)}
                </span>
              )}
              <span className="ml-auto shrink-0 text-[12px] text-muted-foreground tabular-nums">
                {formatElapsed(s.elapsed_ms)}
              </span>
              <span
                className={`w-3 shrink-0 text-[12px] ${s.ok ? 'text-muted-foreground' : 'text-stock-down'}`}
                title={s.ok ? '成功' : '失败'}
              >
                {s.ok ? '✓' : '✗'}
              </span>
            </div>
          ))}
          {(steps ?? []).length === 0 && !loading && !failed && (
            <div className="py-1 text-[12px] text-muted-foreground">本轮没有工具调用</div>
          )}
        </div>
      )}
    </div>
  )
}
