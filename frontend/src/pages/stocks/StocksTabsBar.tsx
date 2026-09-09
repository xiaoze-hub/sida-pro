
import { useStocks } from './context'

export function StocksTabsBar() {
  const {
    viewTab,
    setViewTab,
    positionsCount,
    watchlistCount,
  } = useStocks()
  return (
    <>
{/* Tabs: Positions / Watchlist */}
<div className="mb-4">
  <div className="inline-flex items-center gap-1 p-1 rounded-md bg-accent/30">
    <button
      onClick={() => setViewTab('positions')}
      className={`px-3 py-1.5 rounded-md text-[12px] transition-colors ${
        viewTab === 'positions'
          ? 'bg-background text-foreground shadow-sm'
          : 'text-muted-foreground hover:text-foreground'
      }`}
    >
      持仓 <span className="ml-1 font-mono text-[11px] opacity-70">{positionsCount}</span>
    </button>
    <button
      onClick={() => setViewTab('watchlist')}
      className={`px-3 py-1.5 rounded-md text-[12px] transition-colors ${
        viewTab === 'watchlist'
          ? 'bg-background text-foreground shadow-sm'
          : 'text-muted-foreground hover:text-foreground'
      }`}
    >
      关注 <span className="ml-1 font-mono text-[11px] opacity-70">{watchlistCount}</span>
    </button>
  </div>
</div>
    </>
  )
}
