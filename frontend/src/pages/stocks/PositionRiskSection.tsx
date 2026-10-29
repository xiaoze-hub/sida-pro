import { useMemo } from 'react'
import { AlertOctagon, ShieldAlert, ShieldPlus, CircleSlash, ClockAlert } from 'lucide-react'

import { safePercent } from '@/lib/format'
import {
  buildPositionRiskReport,
  formatPriceWithCurrency,
  NEAR_LINE_PCT,
  quoteAgeLabel,
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
 * | `AccountsSection`（下方持仓表） | 现价/成本/市值/盈亏/当日盈亏 | **没有防线列**，不判纪律位 |
 * | `DefenseLineBanner`（工作台） | **击穿时**弹一条 | 持仓页看不到；未设防线/缺现价不显示 |
 * | `GET /portfolio/diagnostics` | 集中度 HHI / 单仓上限（**无前端消费者**） | 无逐只纪律判定 |
 *
 * 所以本组件**只补防线这一维**：不重复列成本/浮亏（那两列在下方持仓表里，重复摆两遍
 * 只会让两张表互相打架）。表头写清分工，用户知道去哪看价格、去哪看纪律位。
 *
 * 本组件不新建数据源：只消费持仓页**已经加载**的 `GET /portfolio/summary`（`useStocks().portfolio`），
 * 判定收敛在 `@/lib/position-risk`（`DefenseLineBanner` 也已改为共用同一判定）。
 *
 * ## 五条不许越的线
 * 1. **不告警未设防线**：没设就是「未设防线」，不拿成本折算成纪律位（方案 B）。
 * 2. **缺现价说缺**：`无现价` 单独成态，不混进「安全」，也不从分母里消失。
 * 3. **旧报价不判破位**：行情不是当日 → `行情滞后`，只陈列历史价位，不进破位/逼近计数。
 * 4. **不产出结论**：只列状态 / 距离 / 缺口，不写买卖、目标价、加减仓。
 * 5. **每账户一行**：同一标的多账户各自一条防线，不做任何合并或「取第一条」。
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
  stale: { label: '行情滞后', tone: 'text-role-watch', icon: ClockAlert },
}

/**
 * 行情时点：只说你**证明得了**的事。
 * - 没有现价 → 没有报价时点可说（后端可能残留旧的 `daily_pnl_period='today'`，照抄会把
 *   「没有行情」标成「当日」，故先看价）；
 * - 只有当日后端分类才是 `current`；`previous_trading_day` / `mixed` / `unknown`
 *   一律不写成「上一交易日」——后端只按「早于今天」归类，隔日与隔周共用同一个标签，
 *   写「上一交易日」就是把陈旧数据说成新鲜的。改为给真实日期 + 距今时长。
 */
function freshnessLabel(row: PositionRiskRow, now: Date): string {
  if (row.currentPrice === null) return '无行情'
  const time = row.quoteTime ? row.quoteTime.slice(11, 16) : ''
  const age = quoteAgeLabel(row.quoteTime, now)
  if (row.freshness === 'current') {
    return `${time ? `当日 ${time}` : '当日'}${age ? ` · ${age}` : ''}`
  }
  const stamp = row.quoteDate ?? (row.quoteTime ? row.quoteTime.slice(0, 10) : '')
  const when = stamp ? `历史报价 ${stamp}` : '历史报价（日期未知）'
  if (row.freshness === 'stale') return age ? `${when} · ${age}` : when
  return `${stamp ? `时点未知 ${stamp}` : '时点未知'}${age ? ` · ${age}` : ''}`
}

/** 只读展示层：给报告即可渲染，方便单测直接把边界值喂进来。 */
export function PositionRiskTable({
  report,
  now,
}: {
  report: PositionRiskReport
  now?: Date
}) {
  const clock = now ?? new Date()
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[620px] text-[12px]" data-testid="position-risk-table">
        <thead>
          <tr className="text-muted-foreground">
            <th className="py-2 pr-3 text-left font-normal">账户 / 标的</th>
            <th className="py-2 pr-3 text-right font-normal">你的防线</th>
            <th className="py-2 pr-3 text-right font-normal">现价</th>
            <th className="py-2 pr-3 text-right font-normal">距防线</th>
            <th className="py-2 pr-3 text-left font-normal">状态</th>
            <th className="py-2 text-left font-normal">行情</th>
          </tr>
        </thead>
        <tbody>
          {report.rows.map((row) => {
            const meta = STATE_META[row.defenseState]
            const Icon = meta.icon
            const stale = row.defenseState === 'stale'
            return (
              <tr
                key={row.key}
                className="row-hover border-t border-border"
                data-testid={`position-risk-row-${row.symbol}`}
                data-account={row.accountId ?? ''}
                data-state={row.defenseState}
              >
                <td className="py-2 pr-3">
                  {row.accountName && (
                    <span className="mr-1.5 rounded bg-s1 px-1 py-0.5 text-[12px] text-muted-foreground">
                      {row.accountName}
                    </span>
                  )}
                  <span className="font-medium text-foreground">{row.name}</span>
                  <span className="ml-1.5 text-muted-foreground tabular-nums">{row.symbol}</span>
                  {row.sharedSymbolConflict && (
                    <span
                      className="ml-1.5 text-[12px] text-role-watch"
                      title="同一标的多账户各自设了不同防线，这里按账户分行显示，不合并、不取其中一条"
                    >
                      多账户防线不同
                    </span>
                  )}
                </td>
                <td className="py-2 pr-3 text-right tabular-nums text-foreground">
                  {row.defensePrice === null ? '未设' : formatPriceWithCurrency(row.defensePrice, row.market)}
                </td>
                <td className="py-2 pr-3 text-right">
                  <div className="tabular-nums text-foreground">
                    {formatPriceWithCurrency(row.currentPrice, row.market)}
                  </div>
                  <div className="text-muted-foreground">
                    {row.currency === 'UNKNOWN' ? '币种未知' : row.currency}
                  </div>
                </td>
                <td className="py-2 pr-3 text-right tabular-nums text-foreground">
                  {row.distanceToDefensePct === null ? (
                    '--'
                  ) : stale ? (
                    <span className="text-muted-foreground">
                      旧价 {safePercent(row.distanceToDefensePct, 2)}
                    </span>
                  ) : (
                    safePercent(row.distanceToDefensePct, 2)
                  )}
                </td>
                <td className="py-2 pr-3">
                  <span className={`inline-flex items-center gap-1 ${meta.tone}`}>
                    <Icon className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
                    {meta.label}
                  </span>
                </td>
                <td className="py-2 text-muted-foreground">{freshnessLabel(row, clock)}</td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

function Metric({
  label,
  value,
  testId,
  muted,
}: {
  label: string
  value: number
  testId: string
  muted?: boolean
}) {
  return (
    <div className="flex items-baseline gap-2" data-testid={testId}>
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
  // 每账户一行：账户归属必须带进判定，否则同一 symbol 的多条防线无法区分。
  const positions = useMemo(
    () =>
      (portfolio?.accounts ?? []).flatMap((acc) =>
        (acc.positions ?? []).map((p) => ({
          ...p,
          account_id: acc.id,
          account_name: acc.name,
        })),
      ),
    [portfolio],
  )
  const report = useMemo(() => buildPositionRiskReport(positions), [positions])

  if (viewTab !== 'positions' || report.total === 0) return null

  const pnlLeader = report.allProfitable ? report.bestByPnl : report.worstByPnl
  const leaderIsLoss = (pnlLeader?.pnlPct ?? 0) < 0

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
            口径：现价对比你自己设的防线价；未设防线不告警，系统不替你折算纪律位。
            价格 / 成本 / 盈亏以下方持仓表为准，这里只看纪律位与行情时点。
          </span>
        </div>
        <span className="text-[12px] tabular-nums text-muted-foreground">
          有现价 {report.priced}/{report.total} 只
        </span>
      </div>

      <div
        className="mb-2 flex flex-wrap items-baseline gap-x-5 gap-y-1"
        data-testid="position-risk-metrics"
      >
        <Metric label="已破位" value={report.breached} testId="metric-breached" />
        <Metric label="逼近" value={report.near} testId="metric-near" />
        <Metric label="行情滞后" value={report.stale} testId="metric-stale" muted />
        <Metric label="未设防线" value={report.noDefense} testId="metric-no-defense" muted />
        <Metric label="无现价" value={report.noPrice} testId="metric-no-price" muted />
      </div>

      <PositionRiskTable report={report} />

      <div className="mt-2 space-y-1 text-[12px] text-muted-foreground">
        {report.noPrice > 0 && (
          <p>
            {report.noPrice} 只没有可用现价：这些持仓**不参与**破位/浮亏判定，也不会被算作安全。
          </p>
        )}
        {report.stale > 0 && (
          <p>
            {report.stale} 只的报价不是当日行情（行情滞后）：只陈列历史价位，**未计入破位**/逼近判定。
          </p>
        )}
        {report.noDefense > 0 && (
          <p>
            {report.noDefense} 只未设防线：只有你在持仓里填了防线价才会判定破位，系统默认值仅作参考。
          </p>
        )}
        {report.conflictingSymbols.length > 0 && (
          <p>
            同一标的多账户防线不同（{report.conflictingSymbols.length} 只）：已**按账户**分行显示，
            不做合并、也不替你取其中一条。
          </p>
        )}
        {pnlLeader !== null && pnlLeader.pnlPct !== null && (
          <p>
            {leaderIsLoss
              ? `当前浮亏最重：${pnlLeader.name}（${safePercent(pnlLeader.pnlPct, 2)}）`
              : report.allProfitable
                ? `当前持仓全部浮盈，最强：${pnlLeader.name}（${safePercent(pnlLeader.pnlPct, 2)}）`
                : `当前盈亏最弱：${pnlLeader.name}（${safePercent(pnlLeader.pnlPct, 2)}）`}
            ，成本 <span className="tabular-nums">{formatPriceWithCurrency(pnlLeader.costPrice, pnlLeader.market)}</span>，
            现价 <span className="tabular-nums">{formatPriceWithCurrency(pnlLeader.currentPrice, pnlLeader.market)}</span>。
          </p>
        )}
        <p>仅陈列读数与你的防线距离，不构成投资建议。</p>
      </div>
    </section>
  )
}
