import { useMemo } from 'react'
import { AlertOctagon, ShieldAlert, ShieldPlus, CircleSlash } from 'lucide-react'

import { safeFixed, safePercent, safePrice } from '@/lib/format'
import {
  buildPositionRiskReport,
  NEAR_LINE_PCT,
  type PositionRiskReport,
  type PositionRiskRow,
} from '@/lib/position-risk'
import { useStocks } from './context'

/**
 * 持仓风险检查（P2 持仓风险切片, 2026-10-29）—— 持仓页的逐只纪律清单。
 *
 * ## 与既有能力的关系（先盘点再动手，不重复建设）
 * | 既有 | 覆盖 | 缺什么 |
 * |---|---|---|
 * | `PortfolioSummarySection` | 组合合计（市值/盈亏/仓位） | 逐只状态零视图 |
 * | `DefenseLineBanner`（工作台） | **击穿时**弹一条，单只 | 持仓页看不到；未设防线/缺现价不显示 |
 * | `GET /portfolio/diagnostics` | 集中度 HHI / 单仓上限（**无前端消费者**） | 无逐只纪律判定 |
 *
 * 本组件不新建数据源：只消费持仓页**已经加载**的 `GET /portfolio/summary`（`useStocks().portfolio`），
 * 判定收敛在 `@/lib/position-risk`（`DefenseLineBanner` 也已改为共用同一判定）。
 *
 * ## 四条不许越的线
 * 1. **不告警未设防线**：没设就是「未设防线」，不拿成本折算成纪律位（方案 B）。
 * 2. **缺现价说缺**：`无现价` 单独成态，不混进「安全」，也不从分母里消失。
 * 3. **不产出结论**：只列状态 / 距离 / 缺口，不写买卖、目标价、加减仓。
 * 4. **时点用后端口径**：`daily_pnl_period` + `quote_date`，不在前端自己判「今天」。
 */

const STATE_META: Record<
  PositionRiskRow['defenseState'],
  { label: string; tone: string; icon: typeof ShieldAlert }
> = {
  breached: { label: '已破位', tone: 'text-rose-400', icon: AlertOctagon },
  near: { label: `逼近（${NEAR_LINE_PCT}%内）`, tone: 'text-amber-400', icon: ShieldAlert },
  safe: { label: '防线之上', tone: 'text-muted-foreground', icon: ShieldAlert },
  unset: { label: '未设防线', tone: 'text-muted-foreground', icon: ShieldPlus },
  unknown: { label: '无现价', tone: 'text-role-watch', icon: CircleSlash },
}

function periodLabel(row: PositionRiskRow): string {
  // 没有现价就**没有报价时点**可说。后端此时通常给 unknown，但旧快照/合并行可能残留
  // 上一次的 `daily_pnl_period='today'` —— 照抄会把「没有行情」标成「当日」，故先看价。
  if (row.currentPrice === null) return '无行情'
  if (row.pricePeriod === 'today') return '当日'
  if (row.pricePeriod === 'previous_trading_day') {
    return row.quoteDate ? `上一交易日 ${row.quoteDate}` : '上一交易日'
  }
  if (row.pricePeriod === 'mixed') return '时点混合'
  return row.quoteDate ? `时点未知 · ${row.quoteDate}` : '时点未知'
}

function pnlTone(v: number | null): string {
  if (v === null) return 'text-muted-foreground'
  return v > 0 ? 'text-stock-up' : v < 0 ? 'text-stock-down' : 'text-muted-foreground'
}

/** 只读展示层：给报告即可渲染，方便单测直接把边界值喂进来。 */
export function PositionRiskTable({ report }: { report: PositionRiskReport }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[560px] text-[12px]" data-testid="position-risk-table">
        <thead>
          <tr className="text-muted-foreground">
            <th className="py-2 pr-3 text-left font-normal">标的</th>
            <th className="py-2 pr-3 text-right font-normal">现价</th>
            <th className="py-2 pr-3 text-right font-normal">成本</th>
            <th className="py-2 pr-3 text-right font-normal">浮亏</th>
            <th className="py-2 pr-3 text-right font-normal">你的防线</th>
            <th className="py-2 pr-3 text-right font-normal">距防线</th>
            <th className="py-2 text-left font-normal">状态</th>
          </tr>
        </thead>
        <tbody>
          {report.rows.map((row) => {
            const meta = STATE_META[row.defenseState]
            const Icon = meta.icon
            return (
              <tr
                key={row.key}
                className="row-hover border-t border-border"
                data-testid={`position-risk-row-${row.symbol}`}
                data-state={row.defenseState}
              >
                <td className="py-2 pr-3">
                  <span className="font-medium text-foreground">{row.name}</span>
                  <span className="ml-1.5 text-muted-foreground tabular-nums">{row.symbol}</span>
                </td>
                <td className="py-2 pr-3 text-right">
                  <div className="tabular-nums text-foreground">{safePrice(row.currentPrice)}</div>
                  <div className="text-muted-foreground">{periodLabel(row)}</div>
                </td>
                <td className="py-2 pr-3 text-right tabular-nums text-foreground">
                  {safeFixed(row.costPrice, 2)}
                </td>
                <td className={`py-2 pr-3 text-right tabular-nums ${pnlTone(row.pnlPct)}`}>
                  {safePercent(row.pnlPct, 2)}
                </td>
                <td className="py-2 pr-3 text-right tabular-nums text-foreground">
                  {row.defensePrice === null ? '未设' : safeFixed(row.defensePrice, 2)}
                </td>
                <td className="py-2 pr-3 text-right tabular-nums text-foreground">
                  {row.distanceToDefensePct === null ? '--' : safePercent(row.distanceToDefensePct, 2)}
                </td>
                <td className="py-2">
                  <span className={`inline-flex items-center gap-1 ${meta.tone}`}>
                    <Icon className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
                    {meta.label}
                  </span>
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

function Metric({ label, value, muted }: { label: string; value: number; muted?: boolean }) {
  return (
    <div className="flex items-baseline gap-2">
      <span className="text-[12px] text-muted-foreground">{label}</span>
      <span
        className={`text-[16px] tabular-nums ${muted ? 'text-muted-foreground' : 'text-foreground'}`}
      >
        {value}
      </span>
    </div>
  )
}

/** 持仓页容器：读页面已加载的持仓汇总，不额外发请求。 */
export function PositionRiskSection() {
  const { portfolio, viewTab } = useStocks()
  const positions = useMemo(
    () => (portfolio?.accounts ?? []).flatMap((acc) => acc.positions ?? []),
    [portfolio],
  )
  const report = useMemo(() => buildPositionRiskReport(positions), [positions])

  if (viewTab !== 'positions' || report.total === 0) return null

  return (
    <section
      className="mb-5 rounded-xl bg-card p-3"
      aria-labelledby="position-risk-title"
      data-testid="position-risk-section"
    >
      <div className="mb-2 flex flex-wrap items-baseline justify-between gap-2">
        <div className="flex flex-wrap items-baseline gap-2">
          <h2 id="position-risk-title" className="text-[16px] font-semibold text-foreground">
            持仓风险检查
          </h2>
          <span className="text-[12px] text-muted-foreground">
            口径：现价对比你自己设的防线价；未设防线不告警，系统不替你折算纪律位
          </span>
        </div>
        <span className="text-[12px] tabular-nums text-muted-foreground">
          有现价 {report.priced}/{report.total} 只
        </span>
      </div>

      <div className="mb-2 flex flex-wrap items-baseline gap-x-5 gap-y-1">
        <Metric label="已破位" value={report.breached} />
        <Metric label="逼近" value={report.near} />
        <Metric label="未设防线" value={report.noDefense} muted />
        <Metric label="无现价" value={report.noPrice} muted />
      </div>

      <PositionRiskTable report={report} />

      <div className="mt-2 space-y-1 text-[12px] text-muted-foreground">
        {report.noPrice > 0 && (
          <p>
            {report.noPrice} 只没有可用现价：这些持仓**不参与**破位/浮亏判定，也不会被算作安全。
          </p>
        )}
        {report.noDefense > 0 && (
          <p>
            {report.noDefense} 只未设防线：只有你在持仓里填了防线价才会判定破位，系统默认值仅作参考。
          </p>
        )}
        {report.worstByPnl !== null && report.worstByPnl.pnlPct !== null && (
          <p>
            当前浮亏最重：{report.worstByPnl.name}（
            <span className="tabular-nums">{safePercent(report.worstByPnl.pnlPct, 2)}</span>），
            成本 <span className="tabular-nums">{safeFixed(report.worstByPnl.costPrice, 2)}</span>，
            现价 <span className="tabular-nums">{safePrice(report.worstByPnl.currentPrice)}</span>。
          </p>
        )}
        <p>仅陈列读数与你的防线距离，不构成投资建议。</p>
      </div>
    </section>
  )
}
