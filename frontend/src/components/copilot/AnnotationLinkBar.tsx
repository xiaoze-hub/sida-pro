/**
 * K 线上「服务端标注」状态条（2026-10-28 P2 四收口）。
 *
 * 为什么要有它：图上多出两条线，用户有权知道**这两条线是哪来的、能不能撤**。
 * 所以这里只做一件事 —— 把 `annotationLedger` 里**当前目标**的快照逐条念出来：
 *
 *  - `ready`：画了几条、服务端标注 id、确认动作 id（现在由服务端回显，不再"不可用"）；
 *  - `refused`：服务端有行但一条都核对不过（无属主/无确认动作/目标不符/指数或同号代码）
 *    → **没上图**，并说明为什么；
 *  - `error`：读取失败 → **没上图**，给出原因 + 重试；
 *  - `notice`：确认成功但落在别的标的/周期 → 逐字展示接线层给的说明（不谎称已上图）。
 *
 * 视觉：路线是 K 线页的辅助带，不抢主图 —— 一条 hairline + 12px 文本，
 * 零阴影、零彩色块；唯一着色是"已上图"的支撑/压力前缀（与图上虚线同色系）。
 */
import { PenLine, RefreshCw } from 'lucide-react'

import type { AnnotationSnapshot } from '@/lib/copilot/annotationLedger'
import { SERVER_ANNOTATION_CONTRACT } from '@/lib/copilot/serverAnnotations'
import { safeFixed } from '@/lib/format'

/** 撤销不可用的原因（单一事实来源：后端契约里 undoSupported=false）。 */
export const UNDO_UNAVAILABLE_NOTE = SERVER_ANNOTATION_CONTRACT.undoSupported
  ? ''
  : '撤销不可用：后端 annotation 动作不支持回滚（rollback 直接 400），也没有删除图注的接口。'

export function AnnotationLinkBar({
  snapshot,
  className = '',
  onRetry,
}: {
  snapshot: AnnotationSnapshot
  className?: string
  onRetry?: () => void
}) {
  const { phase, lines, annotationIds, actionIds, message, notice, noticeLevel, retryable } =
    snapshot
  // 空态不占位：没有已确认标注、也没出事时，不在 K 线上挂一条常驻说明。
  if (phase === 'idle' && !notice) return null

  const retryButton =
    onRetry && retryable ? (
      <button
        type="button"
        onClick={onRetry}
        className="flex items-center gap-1 text-role-watch hover:underline"
        data-testid="copilot-annotation-retry"
      >
        <RefreshCw className="h-3 w-3" aria-hidden="true" />
        重新读取
      </button>
    ) : null

  return (
    <div
      data-testid="copilot-annotation-link"
      data-kind={phase}
      className={`flex flex-col gap-1 border-t border-border pt-2 text-[12px] leading-relaxed text-muted-foreground ${className}`}
    >
      {phase === 'loading' ? (
        <span data-testid="copilot-annotation-loading">正在核对服务端标注…</span>
      ) : null}

      {phase === 'ready' ? (
        <>
          <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
            <span className="flex items-center gap-1 font-medium text-foreground">
              <PenLine className="h-3 w-3" aria-hidden="true" />
              已确认标注
              <span className="tabular-nums">{lines.length}</span> 条
            </span>
            <span className="tabular-nums" data-testid="copilot-annotation-ids">
              服务端标注 #{annotationIds.join('、')}
            </span>
            <span className="tabular-nums" data-testid="copilot-annotation-action-ids">
              确认动作 #{actionIds.join('、')}
            </span>
          </div>
          <div className="flex flex-wrap gap-x-3 gap-y-1">
            {lines.map((line) => (
              <span
                key={line.annotationId}
                className="font-mono tabular-nums"
                data-testid="copilot-annotation-line"
              >
                <span className={line.kind === 'support' ? 'text-role-opp' : 'text-role-risk'}>
                  {line.kind === 'support' ? '支撑' : '阻力'}
                </span>{' '}
                <span className="text-foreground">{safeFixed(line.price, 2)}</span>
                <span className="ml-1 text-muted-foreground">{line.label}</span>
              </span>
            ))}
          </div>
          {message ? (
            <p data-testid="copilot-annotation-merge-note" className="text-muted-foreground">
              {message}
            </p>
          ) : null}
          {UNDO_UNAVAILABLE_NOTE ? (
            <p data-testid="copilot-annotation-undo-note" className="text-muted-foreground">
              {UNDO_UNAVAILABLE_NOTE}
            </p>
          ) : null}
        </>
      ) : null}

      {phase === 'refused' ? (
        <div className="flex flex-wrap items-baseline gap-2">
          <span data-testid="copilot-annotation-unattributed">未上图：{message}</span>
          {retryButton}
        </div>
      ) : null}

      {phase === 'error' ? (
        <div className="flex flex-wrap items-baseline gap-2">
          <span data-testid="copilot-annotation-error" className="text-role-risk">
            {message}
          </span>
          {retryButton}
        </div>
      ) : null}

      {notice ? (
        <p
          data-testid="copilot-annotation-notice"
          data-level={noticeLevel}
          className={noticeLevel === 'error' ? 'text-role-risk' : 'text-role-watch'}
        >
          {notice}
        </p>
      ) : null}
    </div>
  )
}

export default AnnotationLinkBar
