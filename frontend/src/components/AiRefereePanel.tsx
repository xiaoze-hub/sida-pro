import { AlertTriangle, Gavel, ShieldAlert, ShieldCheck, ShieldQuestion } from 'lucide-react'
import { safeNum } from '@/lib/format'

/**
 * AI 裁判消费组件(P0-2: 全链路审计 —— 裁判结论此前零消费)。
 *
 * 背景: 预测引擎 `forecast_server.py` /predict 响应带 `ai_referee{verdict,direction,reason}`,
 * verdict=adjust 时**已强势覆盖最终 direction**(forecast_server.py:417)。前端此前全库
 * grep 'ai_referee' 零命中 ⇒ 预测记录里方向被裁判改过, 用户却看不到 —— 幻觉敏感红线。
 *
 * 本文件把裁判结论**如实**上屏, 三条纪律:
 *  1. 三态徽标: confirm(裁判确认)/adjust(裁判调整)/abstain(裁判弃权, 维持模型方向);
 *     未知 verdict 原样展示, 不归类、不猜测。
 *  2. adjust 且方向被吃进最终方向时, **必须显式标注『最终方向已被裁判调整』** ——
 *     变更可见是红线, 不能把裁判改过的方向当成纯模型输出。
 *  3. 字段缺失(旧响应无 ai_referee)⇒ 显式『无裁判结论』, 绝不臆造 verdict/reason。
 *
 * 战绩卡片消费 `/api/forecast/referee-stats`(代理引擎 referee_impact_stats):
 * 样本不足(total=0 或 accuracy 为 null)一律显式标注, 不把 0 当真实命中率展示。
 */

export interface AiRefereeInfo {
  verdict?: string
  direction?: string | null
  reason?: string
  elapsed_ms?: number | null
}

export interface RefereeStats {
  total?: number
  symbol?: string
  confirm_count?: number
  adjust_count?: number
  baseline_hit?: number
  referee_hit?: number
  direction_changed?: number
  baseline_accuracy?: number | null
  referee_accuracy?: number | null
  confirm_accuracy?: number | null
  adjust_accuracy?: number | null
  delta_accuracy_pct?: number | null
  message?: string
}

/** 方向中文标签(空/未知显式『未给出方向』)。 */
export function refereeDirLabel(dir?: string | null): string {
  if (dir === 'up') return '↑ 看多'
  if (dir === 'down') return '↓ 看空'
  if (dir === 'flat') return '→ 横盘'
  return '未给出方向'
}

/** 三位小数→一位(避免 .toFixed, 见 scripts/check_ui_rules.mjs R6 棘轮)。 */
function oneDecimal(v: unknown): string {
  const n = safeNum(v)
  return n === null ? '--' : String(Math.round(n * 10) / 10)
}

/** AI 裁判结论面板(预测结果区)。 */
export function AiRefereePanel({ referee }: { referee?: AiRefereeInfo | null }) {
  // 纪律 3: 旧响应无 ai_referee ⇒ 显式『无裁判结论』, 不猜。
  if (!referee) {
    return (
      <div
        data-testid="ai-referee-panel"
        data-verdict="none"
        className="rounded-md border border-border/60 px-3 py-2 text-[12px] text-muted-foreground"
      >
        无裁判结论（本条预测未包含 AI 裁判结果，不作任何推测）
      </div>
    )
  }

  const verdict = referee.verdict || ''
  const isAdjust = verdict === 'adjust'
  const isAdjustOverride = isAdjust && (referee.direction === 'up' || referee.direction === 'down')
  const isAbstain = verdict === 'abstain'
  const isConfirm = verdict === 'confirm'
  const known = isConfirm || isAdjust || isAbstain

  const badgeCls = isConfirm
    ? 'border-emerald-500/30 bg-emerald-500/10 text-emerald-500'
    : isAdjust
      ? 'border-amber-500/40 bg-amber-500/10 text-amber-600'
      : isAbstain
        ? 'border-slate-400/40 bg-slate-400/10 text-muted-foreground'
        : 'border-border/60 text-foreground'

  const reason = (referee.reason || '').trim()

  return (
    <div
      data-testid="ai-referee-panel"
      data-verdict={verdict || 'unknown'}
      className="rounded-md border border-border/60 px-3 py-2 space-y-1.5"
    >
      <div className="flex flex-wrap items-center gap-2">
        <Gavel className="h-3.5 w-3.5 text-muted-foreground" />
        <span className="text-[13px] font-medium">AI 裁判</span>
        <span
          data-testid="ai-referee-badge"
          className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[12px] font-medium ${badgeCls}`}
        >
          {isConfirm && <ShieldCheck className="h-3 w-3" />}
          {isAdjust && <ShieldAlert className="h-3 w-3" />}
          {isAbstain && <ShieldQuestion className="h-3 w-3" />}
          {isConfirm ? '裁判确认' : isAdjust ? '裁判调整' : isAbstain ? '裁判弃权' : `未知结论(${verdict || '空'})`}
        </span>
        <span className="text-[12px] text-muted-foreground">
          裁判方向：{refereeDirLabel(referee.direction)}
        </span>
      </div>

      {/* 纪律 2: adjust 覆盖了最终方向 ⇒ 红线标注, 让变更可见 */}
      {isAdjustOverride && (
        <div
          data-testid="ai-referee-adjust-override"
          role="note"
          className="flex items-start gap-1.5 rounded border border-amber-500/40 bg-amber-500/10 px-2 py-1 text-[12px] text-amber-600"
        >
          <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0" />
          <span>
            <span className="font-medium">最终方向已被裁判调整</span>
            （裁定为 {refereeDirLabel(referee.direction)}；上方「模型预测」方向已按裁判结论覆盖，非纯模型输出）
          </span>
        </div>
      )}

      {/* 弃权: 显式说明维持模型方向, 不伪装成「裁判确认过」 */}
      {isAbstain && (
        <div data-testid="ai-referee-abstain-note" className="text-[12px] text-muted-foreground">
          裁判弃权：未给出方向裁定，维持上方模型方向（裁判不可用/未表态，不代表认可）。
        </div>
      )}

      {/* 未知 verdict: 原样透出, 不猜测 */
      }
      {!known && (
        <div data-testid="ai-referee-unknown-note" className="text-[12px] text-amber-600">
          裁判结论为未知取值（{verdict || '空'}），本面板不猜测其含义，请以引擎原始结论为准。
        </div>
      )}

      <div className="text-[12px] text-muted-foreground">
        <span className="text-foreground/80">裁判理由：</span>
        <span className="whitespace-pre-wrap break-words">{reason || '（裁判未给出理由）'}</span>
      </div>
    </div>
  )
}

/** 裁判战绩卡片(命中率/样本数)。 */
export function RefereeStatsCard({ stats, loading }: { stats?: RefereeStats | null; loading?: boolean }) {
  if (loading && !stats) {
    return (
      <div data-testid="referee-stats-card" className="rounded-md border border-border/60 px-3 py-2 text-[12px] text-muted-foreground">
        裁判战绩：加载中…
      </div>
    )
  }

  const total = safeNum(stats?.total) ?? 0
  // 样本不足 / 无记录 / 引擎不可用: 一律显式, 不展示 0% 命中率冒充真实战绩。
  if (!stats || total === 0) {
    return (
      <div
        data-testid="referee-stats-card"
        data-empty="true"
        className="rounded-md border border-border/60 px-3 py-2 text-[12px] text-muted-foreground space-y-1"
      >
        <div className="text-[13px] font-medium text-foreground/80">裁判战绩</div>
        <div>{stats?.message || '暂无裁判记录（裁判层尚未介入过预测）。'}</div>
        <div>样本不足，暂不展示命中率。</div>
      </div>
    )
  }

  const refereeAcc = safeNum(stats.referee_accuracy)
  const baselineAcc = safeNum(stats.baseline_accuracy)
  const delta = safeNum(stats.delta_accuracy_pct)
  const adjustAcc = safeNum(stats.adjust_accuracy)
  const confirmAcc = safeNum(stats.confirm_accuracy)
  const adjustCount = safeNum(stats.adjust_count) ?? 0

  return (
    <div
      data-testid="referee-stats-card"
      data-empty="false"
      className="rounded-md border border-border/60 px-3 py-2 space-y-1.5"
    >
      <div className="flex items-center gap-2">
        <span className="text-[13px] font-medium">裁判战绩</span>
        <span className="text-[11px] text-muted-foreground">
          {stats.symbol && stats.symbol !== 'all' ? `标的 ${stats.symbol} · ` : '全市场聚合 · '}
          样本 {total} 条
        </span>
      </div>
      <div className="flex flex-wrap gap-x-5 gap-y-1 text-[12px]">
        <span>
          裁判命中率：<span className="font-num font-bold tabular-nums">{refereeAcc === null ? '--' : `${oneDecimal(refereeAcc)}%`}</span>
        </span>
        <span>
          模型基线：<span className="font-num tabular-nums text-muted-foreground">{baselineAcc === null ? '--' : `${oneDecimal(baselineAcc)}%`}</span>
        </span>
        {delta !== null && (
          <span className={delta >= 0 ? 'text-emerald-500' : 'text-rose-500'}>
            增减 {delta >= 0 ? '+' : ''}{oneDecimal(delta)}pp
          </span>
        )}
        <span className="text-muted-foreground">
          确认 {safeNum(stats.confirm_count) ?? 0}（命中率 {confirmAcc === null ? '--' : `${oneDecimal(confirmAcc)}%`}）
        </span>
        <span className="text-muted-foreground">
          调整 {adjustCount}（命中率 {adjustCount === 0 || adjustAcc === null ? '样本不足' : `${oneDecimal(adjustAcc)}%`}）
        </span>
      </div>
      <div className="text-[11px] text-muted-foreground">
        口径：介入前方向用模型预测价中位数重算，介入后按 verdict（confirm 维持 / adjust 用裁判方向），对照 target_date 实际方向。
      </div>
    </div>
  )
}
