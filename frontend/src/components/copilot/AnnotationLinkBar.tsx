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

import type { AnnotationMarkerSkipReason } from '@sida/biz-ui/klineEvents'

/**
 * 时间点标注"没画上"的逐种人话（2026-10-29 复审阻断 ④）。
 *
 * 一条都不能糊成"未上图"：用户需要据此知道下一步该做什么 ——
 * 切回日 K、翻页到那一天、打开图层、还是等 K 线/重试。
 */
export const MARKER_SKIP_TEXT: Record<AnnotationMarkerSkipReason, string> = {
  'not-daily': '当前不是日K周期，日期标注只在日K上绘制，未上图',
  'no-bar': '该日没有 K 线（非交易日/停牌），未上图',
  'out-of-range': '超出已加载的日K范围，未上图',
  'no-data': 'K 线未加载成功，无法确认是否上图',
  'layer-hidden': '时间点标注图层已关闭，未上图',
  'render-failed': '图表未能绘制该标记，未上图',
}

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
    markers,
    markerRender,
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

  /**
   * 时间点标注的落图状态（2026-10-29）：`drawn` = 图表真的画在该日 K 线上；
   * `skipped` = 图表没画，`reason` 说明为什么（不是日 K / 该日没有 K 线 / 超出已加载范围 /
   * 图层关闭 / K 线没加载成功无法确认 / 图表拒绝绘制）；`unknown` = 图表还没回报。
   *
   * 不按"服务端有行"就写"已上图" —— 这正是这一栏存在的意义；也不把不同原因糊成
   * 一句"未上图"（用户没法据此判断该切周期、该翻页、还是数据没加载出来）。
   */
  const markerState = (
    annotationId: number,
  ): { state: 'drawn' | 'skipped' | 'unknown'; reason: AnnotationMarkerSkipReason | null } => {
    if (!markerRender) return { state: 'unknown', reason: null }
    if (markerRender.drawn.includes(annotationId)) return { state: 'drawn', reason: null }
    const hit = markerRender.skipped.find((s) => s.annotationId === annotationId)
    if (hit) return { state: 'skipped', reason: hit.reason }
    return { state: 'unknown', reason: null }
  }

  /** 两条通道统一成可渲染的行（价位线 / 时间点标注），确认块与撤销按钮共用一个 id。 */
  const rows = [
    ...lines.map((line) => ({
      shape: 'line' as const,
      annotationId: line.annotationId,
      label: line.label,
      kind: line.kind,
      price: line.price,
      date: '',
      render: 'drawn' as const,
      reason: null as AnnotationMarkerSkipReason | null,
    })),
    ...markers.map((marker) => {
      const { state, reason } = markerState(marker.annotationId)
      return {
        shape: 'marker' as const,
        annotationId: marker.annotationId,
        label: marker.label,
        kind: null,
        price: null,
        date: marker.date,
        render: state,
        reason,
      }
    }),
  ]

  // 线消失（撤销成功 / 切标的）后确认块必须收起：否则会留下一个指向
  // 不存在标注的确认条，点下去只会得到"不在当前图上"。
  useEffect(() => {
    if (confirmingId !== null && !rows.some((r) => r.annotationId === confirmingId)) {
      setConfirmingId(null)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [confirmingId, lines, markers])

  // 空态不占位：没有已确认标注、也没出事、也没有可重试的原因时，不在 K 线上挂一条常驻说明。
  // 「一条不剩 + 读回失败/无法核对」会落成 idle + notice + retryable —— 那时必须给入口，
  // 否则提示里让用户"点重新读取"却无按钮可点（2026-10-29 复审补丁）。
  if (phase === 'idle' && !notice && !retryable) return null

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
    confirmingId === null ? null : rows.find((l) => l.annotationId === confirmingId) ?? null

  return (
    <div
      data-testid="copilot-annotation-link"
      data-kind={phase}
      className={`flex flex-col gap-1 border-t border-border pt-2 text-[12px] leading-relaxed text-muted-foreground ${className}`}
    >
      {phase === 'loading' ? (
        <span data-testid="copilot-annotation-loading">正在核对服务端标注…</span>
      ) : null}

      {phase === 'idle' ? (
        <div className="flex flex-wrap items-baseline gap-2">
          {!notice ? (
            <span data-testid="copilot-annotation-empty">暂无已确认标注</span>
          ) : null}
          {retryButton}
        </div>
      ) : null}

      {phase === 'ready' ? (
        <>
          <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
            <span className="flex items-center gap-1 font-medium text-foreground">
              <PenLine className="h-3 w-3" aria-hidden="true" />
              已确认标注
              <span className="tabular-nums">{rows.length}</span> 条
            </span>
            {markers.length > 0 ? (
              <span className="tabular-nums" data-testid="copilot-annotation-marker-count">
                （含时间点标记 <span className="tabular-nums">{markers.length}</span> 个）
              </span>
            ) : null}
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
            {rows.map((line) => {
              const inFlight = revoking.includes(line.annotationId)
              // 时间点标注：说清"画上去了 / 为什么没画 / 图表还没回报"——
              // 几种状态不能混成一句"已确认"（用户会以为图上一定有记号）。
              const timeNote =
                line.shape !== 'marker'
                  ? null
                  : line.render === 'skipped'
                    ? MARKER_SKIP_TEXT[line.reason ?? 'no-data']
                    : line.render === 'unknown'
                      ? '等待图表回报落图结果'
                      : '已上图'
              return (
                <span
                  key={line.annotationId}
                  className="flex items-baseline gap-1 font-mono tabular-nums"
                  data-testid="copilot-annotation-line"
                  data-shape={line.shape}
                  data-marker-state={line.shape === 'marker' ? line.render : undefined}
                  data-marker-reason={
                    line.shape === 'marker' && line.reason ? line.reason : undefined
                  }
                >
                  {line.shape === 'marker' ? (
                    <>
                      <span className="text-role-watch">时间点</span>{' '}
                      <span className="text-foreground">{line.date}</span>
                    </>
                  ) : (
                    <>
                      <span className={line.kind === 'support' ? 'text-role-opp' : 'text-role-risk'}>
                        {line.kind === 'support' ? '支撑' : '阻力'}
                      </span>{' '}
                      <span className="text-foreground">{safeFixed(line.price ?? 0, 2)}</span>
                    </>
                  )}
                  <span className="ml-1 font-sans text-muted-foreground">{line.label}</span>
                  {timeNote ? (
                    <span
                      className={
                        line.render === 'skipped'
                          ? 'ml-1 font-sans text-role-risk'
                          : 'ml-1 font-sans text-muted-foreground'
                      }
                    >
                      {timeNote}
                    </span>
                  ) : null}
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
                {confirmingLine.shape === 'marker' ? (
                  <>
                    时间点 {confirmingLine.date} {confirmingLine.label}
                  </>
                ) : (
                  <>
                    {confirmingLine.kind === 'support' ? '支撑' : '阻力'}{' '}
                    {safeFixed(confirmingLine.price ?? 0, 2)} {confirmingLine.label}
                  </>
                )}
                ）？
              </span>
              {/* 事实：DELETE 成功**且**服务端读回不再返回这条之后，图上才移除；
                  请求在途时标注一直在（服务端还没删）。别承诺"立即移除"。 */}
              <span className="text-role-risk">
                撤销不可恢复：确认后等服务端删除成功并读回，图上才移除这条标注。
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
