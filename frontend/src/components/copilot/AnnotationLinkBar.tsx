/**
 * K 线上「服务端标注」状态条（2026-10-28 P2 四收口；2026-10-29 加撤销）。
 *
 * 为什么要有它：图上多出两条线，用户有权知道**这两条线是哪来的、能不能撤**。
 * 所以这里只做一件事 —— 把 `annotationLedger` 里**当前目标**的快照逐条念出来：
 *
 *  - `ready`：画了几条、服务端标注 id、确认动作 id（现在由服务端回显，不再"不可用"）；
 *  - `refused`：服务端有行但一条都核对不过（无属主/无确认动作/目标不符/指数或同号代码）
 *    → **没上图**，并说明为什么；
 *  - `error`：读取失败 → **没上图**，给出原因 + 重试；
 *  - `notice`：确认/撤销的结果说明（落在别的标的/周期、服务端读回不同步…）逐字展示。
 *
 * 撤销（2026-10-29, P2 撤销闭合）：每条线一个「撤销」，点了**先展开确认** ——
 * 撤销不可恢复，一步点掉太容易误触。确认之后才把 id 交出去（`onRevoke`）；
 * 请求在途时按钮变「撤销中…」且不再可点（连点不会发第二个请求）。
 * 是否真的从图上移除由接线层按**服务端读回**决定，这里不预删。
 *
 * 视觉：路线是 K 线页的辅助带，不抢主图 —— 一条 hairline + 12px 文本，
 * 零阴影、零彩色块；唯一着色是"已上图"的支撑/压力前缀（与图上虚线同色系）。
 */
import { useEffect, useState } from 'react'
import { PenLine, RefreshCw, Undo2 } from 'lucide-react'

import type { AnnotationSnapshot } from '@/lib/copilot/annotationLedger'
import { SERVER_ANNOTATION_CONTRACT } from '@/lib/copilot/serverAnnotations'
import { safeFixed } from '@/lib/format'

/** 撤销不可用的原因（单一事实来源：后端契约里 undoSupported）。 */
export const UNDO_UNAVAILABLE_NOTE = SERVER_ANNOTATION_CONTRACT.undoSupported
  ? ''
  : '撤销不可用：后端 annotation 动作不支持回滚（rollback 直接 400），也没有删除图注的接口。'

export function AnnotationLinkBar({
  snapshot,
  className = '',
  onRetry,
  onRevoke,
}: {
  snapshot: AnnotationSnapshot
  className?: string
  onRetry?: () => void
  /** 用户**二次确认后**才调用；不传 = 当前入口不提供撤销（不渲染按钮）。 */
  onRevoke?: (annotationId: number) => void
}) {
  const {
    phase,
    lines,
    annotationIds,
    actionIds,
    message,
    notice,
    noticeLevel,
    retryable,
    revoking,
  } = snapshot
  /** 正在等用户二次确认的那条标注（null = 没有展开确认） */
  const [confirmingId, setConfirmingId] = useState<number | null>(null)

  // 线消失（撤销成功 / 切标的）后确认块必须收起：否则会留下一个指向
  // 不存在标注的确认条，点下去只会得到"不在当前图上"。
  useEffect(() => {
    if (confirmingId !== null && !lines.some((l) => l.annotationId === confirmingId)) {
      setConfirmingId(null)
    }
  }, [confirmingId, lines])

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

  const confirmingLine =
    confirmingId === null ? null : lines.find((l) => l.annotationId === confirmingId) ?? null

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
            {/* 读回失败 / 读回与撤销结论不一致时也要能重读（撤销成功则无需） */}
            {retryButton}
          </div>
          <div className="flex flex-wrap gap-x-3 gap-y-1">
            {lines.map((line) => {
              const inFlight = revoking.includes(line.annotationId)
              return (
                <span
                  key={line.annotationId}
                  className="flex items-baseline gap-1 font-mono tabular-nums"
                  data-testid="copilot-annotation-line"
                >
                  <span className={line.kind === 'support' ? 'text-role-opp' : 'text-role-risk'}>
                    {line.kind === 'support' ? '支撑' : '阻力'}
                  </span>{' '}
                  <span className="text-foreground">{safeFixed(line.price, 2)}</span>
                  <span className="ml-1 font-sans text-muted-foreground">{line.label}</span>
                  {onRevoke ? (
                    inFlight ? (
                      <span
                        className="ml-1 flex items-center gap-1 text-muted-foreground"
                        data-testid="copilot-annotation-revoking"
                      >
                        <RefreshCw className="h-3 w-3 animate-spin" aria-hidden="true" />
                        撤销中…
                      </span>
                    ) : (
                      <button
                        type="button"
                        onClick={() => setConfirmingId(line.annotationId)}
                        className="ml-1 flex items-center gap-1 text-muted-foreground hover:text-foreground hover:underline"
                        aria-label={`撤销标注 #${line.annotationId}`}
                        data-testid="copilot-annotation-revoke"
                      >
                        <Undo2 className="h-3 w-3" aria-hidden="true" />
                        撤销
                      </button>
                    )
                  ) : null}
                </span>
              )
            })}
          </div>
          {confirmingLine ? (
            <div
              className="flex flex-wrap items-baseline gap-x-2 gap-y-1 border-t border-border pt-1"
              data-testid="copilot-annotation-revoke-confirm"
              role="group"
              aria-label={`撤销标注 #${confirmingLine.annotationId} 确认`}
            >
              <span className="text-foreground">
                撤销标注 #{confirmingLine.annotationId}（
                {confirmingLine.kind === 'support' ? '支撑' : '阻力'}{' '}
                {safeFixed(confirmingLine.price, 2)} {confirmingLine.label}）？
              </span>
              {/* 事实：DELETE 成功**且**服务端读回不再返回这条之后，图上才移除；
                  请求在途时线一直在（服务端还没删）。别承诺"立即移除"。 */}
              <span className="text-role-risk">
                撤销不可恢复：确认后等服务端删除成功并读回，图上才移除这条线。
              </span>
              <button
                type="button"
                onClick={() => {
                  const id = confirmingLine.annotationId
                  setConfirmingId(null)
                  onRevoke?.(id)
                }}
                className="flex items-center gap-1 font-medium text-role-risk hover:underline"
                data-testid="copilot-annotation-revoke-yes"
              >
                确认撤销
              </button>
              <button
                type="button"
                onClick={() => setConfirmingId(null)}
                className="text-muted-foreground hover:text-foreground hover:underline"
                data-testid="copilot-annotation-revoke-cancel"
              >
                取消
              </button>
            </div>
          ) : null}
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
