import StockContextMenu from '@/components/StockContextMenu'
import { StocksContext } from './stocks/context'
import { useStocksState } from './stocks/useStocksState'
import { useStocksData } from './stocks/useStocksData'
import { useStocksDerived } from './stocks/useStocksDerived'
import { useStocksActions } from './stocks/useStocksActions'
import { StocksHeader } from './stocks/StocksHeader'
import { PortfolioSummarySection } from './stocks/PortfolioSummarySection'
import { StocksTabsBar } from './stocks/StocksTabsBar'
import { AddStockDialog } from './stocks/AddStockDialog'
import { AccountsSection } from './stocks/AccountsSection'
import { WatchlistSection } from './stocks/WatchlistSection'
import { StockDialogs } from './stocks/StockDialogs'
import { WatchlistDialogs } from './stocks/WatchlistDialogs'
import { PositionDialog } from './stocks/PositionDialog'
import { AgentDialogs } from './stocks/AgentDialogs'
import { NewsDialog } from './stocks/NewsDialog'
import { StocksSkeleton } from './stocks/StocksSkeleton'

export default function StocksPage() {
  const state = useStocksState()
  const data = useStocksData(state)
  const derived = useStocksDerived(state, data)
  const actions = useStocksActions(state, data)
  const ctx = { ...state, ...data, ...derived, ...actions }

  return (
    <StocksContext.Provider value={ctx}>
      {state.loading ? (
        <StocksSkeleton />
      ) : (
      <div className="sida-page-enter">
        <StocksHeader />
        <PortfolioSummarySection />
        <StocksTabsBar />
        <AddStockDialog />
        <AccountsSection />
        <WatchlistSection />
        <StockDialogs />
        <WatchlistDialogs />
        <PositionDialog />
        <AgentDialogs />
        <NewsDialog />
        <StockContextMenu
          menu={ctx.stockCtxMenu}
          onClose={() => ctx.setStockCtxMenu(null)}
          onAddWatchlist={ctx.addToWatchlistFromMenu}
          onViewDetail={ctx.viewDetailFromMenu}
          onPaperTrade={ctx.paperTradeFromMenu}
          onOpenQuote={ctx.openQuoteFromMenu}
        />
      </div>
      )}
    </StocksContext.Provider>
  )
}
