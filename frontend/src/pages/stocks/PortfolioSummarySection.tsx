import ErrorBanner from '@/components/ErrorBanner'
import { Skeleton } from '@sida/base-ui/components/ui/skeleton'
import { Card } from '@sida/base-ui/components/ui/card'
import Stat from '@sida/biz-ui/components/Stat'
import { dailyPnlDisplayLabel } from './shared'
import type { QuotesCoverage } from './shared'
import { safeFixed, safeMoneyUnsigned } from '@/lib/format'
import { safeNum } from '@/lib/format'
import { useStocks } from './context'
import { useI18n } from '@/hooks/useI18n'

/**
 * 持仓页金额口径(2026-09-14 走查缺陷修复):
 * - **存量/规模读数**(总市值、可用资金、总资产、持仓市值/总资产明细) → `safeMoneyUnsigned`
 *   (不带 '+'): 它们是"有多少", 旧代码走 formatMoney(= safeMoney) 会渲染出 `+4.50万`,
 *   被读成变化量; 且总市值为 0 时旧代码渲染 `0`(无号) —— 同一行里一个带 + 一个不带,
 *   自相矛盾。
 * - **涨跌/盈亏读数**(总盈亏、当日盈亏) → 保留 `formatMoney` 的带符号口径(红涨绿跌需要方向)。
 *
 * 2026-10-29 缺行情修复: 总市值/总盈亏/总资产可能是 **null = 未知**(有持仓但一只都没行情)。
 * 未知一律渲染 `--`、**不给涨跌色**、也**不参与减法**(旧代码 `null - dayPnl` 会得 0 再算出
 * `+0.00%`, `null >= 0` 会把"不知道"染成上涨色)。
 */
const fmtAmount = (v: unknown) => safeMoneyUnsigned(v)

/** 未知不当 0: 只有拿到数值才给涨跌色, 否则交给默认中性色。 */
const pnlTone = (v: unknown): string | undefined => {
  const n = safeNum(v)
  if (n === null) return undefined
  return n > 0 ? 'text-stock-up' : n < 0 ? 'text-stock-down' : undefined
}

/**
 * 行情覆盖度提示（2026-10-29 缺行情修复）。
 *
 * 合计金额只在**已定价子集**上算 —— 只要有一只持仓没有行情，就必须说出来，
 * 否则用户会把"部分持仓的盈亏"当成"全部持仓的盈亏"。全覆盖时不显示（不制造噪声）。
 */
function CoverageNote({ coverage }: { coverage?: QuotesCoverage }) {
  if (!coverage || !coverage.requested) return null
  if (coverage.positions_total === 0 || coverage.complete) return null
  const costPct =
    coverage.cost_coverage_pct === null ? null : `${safeFixed(coverage.cost_coverage_pct, 0)}%`
  return (
    <p
      className="mb-6 -mt-3 text-[12px] text-role-watch"
      data-testid="portfolio-coverage-note"
    >
      行情覆盖 {coverage.positions_priced}/{coverage.positions_total} 只，
      {coverage.positions_missing} 只缺行情：市值与盈亏只按已定价持仓计算
      {costPct ? `（成本覆盖率 ${costPct}）` : ''}，缺行情的持仓未计入合计。
    </p>
  )
}

export function PortfolioSummarySection() {
  const { t } = useI18n()
  const {
    loadError,
    setLoadError,
    portfolio,
    portfolioLoading,
    load,
    loadPortfolio,
    positionRatio,
    portfolioMarketStatusLabel,
    formatMoney,
  } = useStocks()
  return (
    <>
{/* Portfolio Total Summary */}
{/* 2026-08-17: 加载失败横幅统一为 ErrorBanner(闭环修正 P0-3: 错误体系统一) */}
<ErrorBanner
  errors={loadError && !loadError.includes('429') ? [{ source: t('stocks.loadError'), message: loadError, retry: () => { void load(); void loadPortfolio() } }] : []}
  onDismiss={() => setLoadError && setLoadError(null)}
/>
{portfolioLoading && !portfolio ? (
  // 首次加载时显示骨架屏
  <div className="grid grid-cols-2 md:grid-cols-4 gap-3 md:gap-4 mb-6">
    {[...Array(4)].map((_, i) => (
      <div key={i}>
        <div className="flex items-center gap-2 mb-2">
          <Skeleton className="h-4 w-4 rounded" />
          <Skeleton className="h-3 w-12" />
        </div>
        <Skeleton className="h-6 w-20" />
      </div>
    ))}
  </div>
) : portfolio ? (
  <>
  <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-3 md:gap-4 mb-6">
    <Card variant="plain" className=" p-3">
      <Stat
        label={t('stocks.totalMarketValue')}
        value={fmtAmount(portfolio.total.total_market_value)}
        className="[&>div:nth-child(2)]:text-[20px]"
      />
    </Card>
    <Card variant="plain" className=" p-3">
      <Stat
        label={t('stocks.totalPnl')}
        value={
          <>
            {formatMoney(portfolio.total.total_pnl)}
            <span className="text-[14px] ml-1.5">
              ({safeNum(portfolio.total.total_pnl_pct) === null ? '--' : `${portfolio.total.total_pnl_pct! >= 0 ? '+' : ''}${safeFixed(portfolio.total.total_pnl_pct)}%`})
            </span>
          </>
        }
        tone={pnlTone(portfolio.total.total_pnl)}
        className="[&>div:nth-child(2)]:text-[20px]"
      />
    </Card>

    {(() => {
      const dayPnl = portfolio.total.total_daily_pnl
      const totalMv = safeNum(portfolio.total.total_market_value)
      // 总市值未知(缺行情)时,"上日市值 = 今日市值 - 当日盈亏" 无从算起 —— 给 --, 不写 +0.00%。
      const prevMv = totalMv !== null ? totalMv - (safeNum(dayPnl) ?? 0) : null
      const pct = prevMv !== null && prevMv > 0 ? (dayPnl / prevMv) * 100 : null
      return (
        <Card variant="plain" className=" p-3">
          <Stat
            // 6 栏网格里全称会换行/截断成 3 行；紧凑档「上日 10-23」单行可读，全称仍在 hover 语义里
            label={dailyPnlDisplayLabel(portfolio.total, true)}
            value={
              <>
                {formatMoney(dayPnl)}
                <span className="text-[14px] ml-1.5">({pct != null && Number.isFinite(pct) ? `${pct >= 0 ? '+' : ''}${pct.toFixed(2)}%` : '--'})</span>
              </>
            }
            sub={portfolioMarketStatusLabel || undefined}
            tone={pnlTone(dayPnl)}
            className="[&>div:nth-child(2)]:text-[20px]"
          />
        </Card>
      )
    })()}

    <Card variant="plain" className=" p-3">
      <Stat label={t('stocks.availableCash')} value={fmtAmount(portfolio.total.available_funds)} className="[&>div:nth-child(2)]:text-[20px]" />
    </Card>
    <Card variant="plain" className=" p-3">
      <Stat label={t('stocks.totalAssets')} value={fmtAmount(portfolio.total.total_assets)} className="[&>div:nth-child(2)]:text-[20px]" />
    </Card>

    <Card variant="plain" className=" p-3">
      <Stat
        label={t('stocks.positionRatio')}
        value={positionRatio && safeNum(positionRatio.pct) !== null ? `${safeFixed(positionRatio.pct, 1)}%` : '--'}
        sub={positionRatio ? `${t('stocks.portfolio')} ${fmtAmount(positionRatio.mv)} / ${t('stocks.totalAssets')} ${fmtAmount(positionRatio.assets)}` : '—'}
        className="[&>div:nth-child(2)]:text-[20px]"
      />
    </Card>
  </div>
  <CoverageNote coverage={portfolio.total.quotes_coverage} />
</>) : null}
    </>
  )
}
