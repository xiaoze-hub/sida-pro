import { useState } from 'react'
import { ChevronDown, ChevronRight } from 'lucide-react'

import type { CopilotRunRow } from '@/lib/copilot/api'
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
}

const STATUS_CLS: Record<string, string> = {
  ok: 'text-muted-foreground',
  degraded: 'text-amber-500',
  error: 'text-stock-down',
  running: 'text-muted-foreground',
}

function clip(s: string | null | undefined, n = 46): string {
  if (!s) return ''
  const t = s.replace(/\s+/g, ' ').trim()
  return t.length > n ? `${t.slice(0, n)}…` : t
}

export default function InlineSteps({ run, steps, loading, onLoad, failed }: InlineStepsProps) {
  const [open, setOpen] = useState(false)

  const toolCalls = typeof run.tool_calls === 'number' ? run.tool_calls : null
  const statusCls = STATUS_CLS[run.status] || 'text-muted-foreground'

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
        {run.status !== 'ok' && <span className={statusCls}>· {run.status}</span>}
      </button>

      {open && (
        <div className="mt-1 border-l border-border pl-2" data-testid="inline-steps-list">
          {loading && !steps && (
            <div className="py-1 text-[12px] text-muted-foreground">步骤加载中…</div>
          )}
          {failed && !steps && !loading && (
            <div className="py-1 text-[12px] text-muted-foreground">这一轮的步骤没取到</div>
          )}
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
