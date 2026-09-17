import ErrorBanner from '@/components/ErrorBanner'
import { Skeleton } from '@panwatch/base-ui/components/ui/skeleton'
import { Card } from '@panwatch/base-ui/components/ui/card'
import Stat from '@panwatch/biz-ui/components/Stat'
import { dailyPnlDisplayLabel } from './shared'
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
 */
const fmtAmount = (v: unknown) => safeMoneyUnsigned(v)

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
  errors={loadError ? [{ source: t('stocks.loadError'), message: loadError, retry: () => { void load(); void loadPortfolio() } }] : []}
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
  <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-3 md:gap-4 mb-6">
    <Card variant="plain" className="border-l border-l-border/40 p-3">
      <Stat
        label={t('stocks.totalMarketValue')}
        value={fmtAmount(portfolio.total.total_market_value)}
        className="[&>div:nth-child(2)]:text-[20px]"
      />
    </Card>
    <Card variant="plain" className="border-l border-l-border/40 p-3">
      <Stat
        label={t('stocks.totalPnl')}
        value={
          <>
            {formatMoney(portfolio.total.total_pnl)}
            <span className="text-[13px] ml-1.5">
              ({safeNum(portfolio.total.total_pnl_pct) === null ? '--' : `${portfolio.total.total_pnl_pct >= 0 ? '+' : ''}${safeFixed(portfolio.total.total_pnl_pct)}%`})
            </span>
          </>
        }
        tone={portfolio.total.total_pnl >= 0 ? 'text-stock-up' : 'text-stock-down'}
        className="[&>div:nth-child(2)]:text-[20px]"
      />
    </Card>

    {(() => {
      const dayPnl = portfolio.total.total_daily_pnl
      const totalMv = portfolio.total.total_market_value
      const prevMv = totalMv - dayPnl
      const pct = prevMv > 0 ? (dayPnl / prevMv * 100) : 0
      const isUp = dayPnl >= 0
      return (
        <Card variant="plain" className="border-l border-l-border/40 p-3">
          <Stat
            label={dailyPnlDisplayLabel(portfolio.total)}
            value={
              <>
                {formatMoney(dayPnl)}
                <span className="text-[13px] ml-1.5">({pct != null && Number.isFinite(pct) ? `${pct >= 0 ? '+' : ''}${pct.toFixed(2)}%` : '--'})</span>
              </>
            }
            sub={portfolioMarketStatusLabel || undefined}
            tone={isUp ? 'text-stock-up' : 'text-stock-down'}
            className="[&>div:nth-child(2)]:text-[20px]"
          />
        </Card>
      )
    })()}

    <Card variant="plain" className="border-l border-l-border/40 p-3">
      <Stat label={t('stocks.availableCash')} value={fmtAmount(portfolio.total.available_funds)} className="[&>div:nth-child(2)]:text-[20px]" />
    </Card>
    <Card variant="plain" className="border-l border-l-border/40 p-3">
      <Stat label={t('stocks.totalAssets')} value={fmtAmount(portfolio.total.total_assets)} className="[&>div:nth-child(2)]:text-[20px]" />
    </Card>

    <Card variant="plain" className="border-l border-l-border/40 p-3">
      <Stat
        label={t('stocks.positionRatio')}
        value={positionRatio && safeNum(positionRatio.pct) !== null ? `${positionRatio.pct.toFixed(1)}%` : '--'}
        sub={positionRatio ? `${t('stocks.portfolio')} ${fmtAmount(positionRatio.mv)} / ${t('stocks.totalAssets')} ${fmtAmount(positionRatio.assets)}` : '—'}
        className="[&>div:nth-child(2)]:text-[20px]"
      />
    </Card>
  </div>
) : null}
    </>
  )
}
