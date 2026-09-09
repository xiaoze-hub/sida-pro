import ErrorBanner from '@/components/ErrorBanner'
import { ArrowDownRight } from 'lucide-react'
import { ArrowUpRight } from 'lucide-react'
import { Bell } from 'lucide-react'
import { PiggyBank } from 'lucide-react'
import { Skeleton } from '@panwatch/base-ui/components/ui/skeleton'
import { TrendingUp } from 'lucide-react'
import { Wallet } from 'lucide-react'
import { dailyPnlDisplayLabel } from './shared'
import { safeFixed } from '@/lib/format'
import { safeNum } from '@/lib/format'
import { useStocks } from './context'

export function PortfolioSummarySection() {
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
  errors={loadError ? [{ source: '持仓/账户', message: loadError, retry: () => { void load(); void loadPortfolio() } }] : []}
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
    <div className="border-l border-border/40 pl-3">
      <div className="flex items-center gap-2 text-muted-foreground mb-1">
        <TrendingUp className="w-4 h-4" />
        <span className="text-[12px]">总市值</span>
      </div>
      <div className="text-[20px] font-bold text-foreground font-num tabular-nums">
        {formatMoney(portfolio.total.total_market_value)}
      </div>
    </div>
    <div className="border-l border-border/40 pl-3">
      <div className="flex items-center gap-2 text-muted-foreground mb-1">
        {portfolio.total.total_pnl >= 0 ? (
          <ArrowUpRight className="w-4 h-4 text-stock-up" />
        ) : (
          <ArrowDownRight className="w-4 h-4 text-stock-down" />
        )}
        <span className="text-[12px]">总盈亏</span>
      </div>
      <div className={`text-[20px] font-bold font-num tabular-nums ${portfolio.total.total_pnl >= 0 ? 'text-stock-up' : 'text-stock-down'}`}>
        {portfolio.total.total_pnl >= 0 ? '+' : ''}{formatMoney(portfolio.total.total_pnl)}
        <span className="text-[13px] ml-1.5">
          ({safeNum(portfolio.total.total_pnl_pct) === null ? '--' : `${portfolio.total.total_pnl_pct >= 0 ? '+' : ''}${safeFixed(portfolio.total.total_pnl_pct)}%`})
        </span>
      </div>
    </div>

    {(() => {
      const dayPnl = portfolio.total.total_daily_pnl
      const totalMv = portfolio.total.total_market_value
      const prevMv = totalMv - dayPnl
      const pct = prevMv > 0 ? (dayPnl / prevMv * 100) : 0
      const isUp = dayPnl >= 0
      return (
        <div className="border-l border-border/40 pl-3">
          <div className="flex flex-wrap items-center gap-2 text-muted-foreground mb-1">
            {isUp ? (
              <ArrowUpRight className="w-4 h-4 text-stock-up" />
            ) : (
              <ArrowDownRight className="w-4 h-4 text-stock-down" />
            )}
            <span className="text-[12px]">{dailyPnlDisplayLabel(portfolio.total)}</span>
            {portfolioMarketStatusLabel && (
              <span className="rounded-full bg-accent/60 px-1.5 py-0.5 text-[10px] font-normal text-muted-foreground">
                {portfolioMarketStatusLabel}
              </span>
            )}
          </div>
          <div className={`text-[20px] font-bold font-mono tabular-nums ${isUp ? 'text-stock-up' : 'text-stock-down'}`}>
            {isUp ? '+' : ''}{formatMoney(dayPnl)}
            <span className="text-[13px] ml-1.5">({pct != null && Number.isFinite(pct) ? `${pct >= 0 ? '+' : ''}${pct.toFixed(2)}%` : '--'})</span>
          </div>
        </div>
      )
    })()}

    <div className="border-l border-border/40 pl-3">
      <div className="flex items-center gap-2 text-muted-foreground mb-1">
        <Wallet className="w-4 h-4" />
        <span className="text-[12px]">可用资金</span>
      </div>
      <div className="text-[20px] font-bold text-foreground font-mono tabular-nums">
        {formatMoney(portfolio.total.available_funds)}
      </div>
    </div>
    <div className="border-l border-border/40 pl-3">
      <div className="flex items-center gap-2 text-muted-foreground mb-1">
        <PiggyBank className="w-4 h-4" />
        <span className="text-[12px]">总资产</span>
      </div>
      <div className="text-[20px] font-bold text-foreground font-mono tabular-nums">
        {formatMoney(portfolio.total.total_assets)}
      </div>
    </div>

    <div className="border-l border-border/40 pl-3">
      <div className="flex items-center gap-2 text-muted-foreground mb-1">
        <Bell className="w-4 h-4" />
        <span className="text-[12px]">仓位占比</span>
      </div>
      <div className="text-[20px] font-bold text-foreground font-mono tabular-nums">
        {positionRatio && safeNum(positionRatio.pct) !== null ? `${positionRatio.pct.toFixed(1)}%` : '--'}
      </div>
      <div className="mt-1 text-[11px] text-muted-foreground line-clamp-1">
        {positionRatio ? `持仓市值 ${formatMoney(positionRatio.mv)} / 总资产 ${formatMoney(positionRatio.assets)}` : '—'}
      </div>
    </div>
  </div>
) : null}
    </>
  )
}
