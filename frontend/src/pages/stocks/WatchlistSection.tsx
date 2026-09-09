import StockPriceAlertPanel from '@panwatch/biz-ui/components/stock-price-alert-panel'
import { Badge } from '@panwatch/base-ui/components/ui/badge'
import { BarChart3 } from 'lucide-react'
import { Brain } from 'lucide-react'
import { Button } from '@panwatch/base-ui/components/ui/button'
import { ExternalLink } from 'lucide-react'
import { Newspaper } from 'lucide-react'
import { SuggestionBadge } from '@panwatch/biz-ui/components/suggestion-badge'
import { X } from 'lucide-react'
import { useStocks } from './context'

export function WatchlistSection() {
  const {
    stocks,
    agents,
    loadError,
    viewTab,
    setAgentDialogStock,
    runningAgents,
    stockListFilter,
    setStockListFilter,
    watchlistOnlyAlerts,
    setWatchlistOnlyAlerts,
    setRemoveWatchStock,
    draggingWatchStockId,
    setDraggingWatchStockId,
    watchDragSnapshotRef,
    openDeepAnalysis,
    previewWatchlistReorder,
    commitWatchlistReorder,
    isSuppressCardClick,
    loadPriceAlertSummaries,
    openKlineDialog,
    openNewsDialog,
    openStockDetail,
    openStockContextMenu,
    marketBadge,
    getStockQuote,
    getPriceAlertSummary,
    getSuggestionForStock,
  } = useStocks()
  return (
    <>
{/* Watchlist */}
{viewTab === 'watchlist' && (
  <div className="border-b border-border/40 pb-4">
    <div className="flex items-center justify-between mb-3">
      <h3 className="text-[13px] font-semibold text-foreground">关注列表</h3>
      <div className="flex items-center gap-1">
        {[
          { value: '', label: '全部', count: stocks.length },
          { value: 'CN', label: 'A股', count: stocks.filter(s => s.market === 'CN').length },
          { value: 'HK', label: '港股', count: stocks.filter(s => s.market === 'HK').length },
          { value: 'US', label: '美股', count: stocks.filter(s => s.market === 'US').length },
        ].map(opt => (
          <button
            key={opt.value}
            onClick={() => setStockListFilter(opt.value)}
            className={`text-[11px] px-2 py-0.5 rounded transition-colors ${
              stockListFilter === opt.value
                ? 'bg-primary text-primary-foreground'
                : 'bg-accent/50 text-muted-foreground hover:bg-accent'
            }`}
          >
            {opt.label} ({opt.count})
          </button>
        ))}
      </div>
    </div>

    <div className="flex items-center justify-between mb-3">
      <div className="text-[11px] text-muted-foreground">筛选</div>
      <div className="flex items-center gap-2">
        <button
          onClick={() => setWatchlistOnlyAlerts(!watchlistOnlyAlerts)}
          className={`text-[11px] px-2.5 py-1 rounded-md border transition-colors ${
            watchlistOnlyAlerts
              ? 'bg-rose-500/10 border-rose-500/30 text-rose-600'
              : 'bg-accent/30 border-border/50 text-muted-foreground hover:border-rose-500/30'
          }`}
          title="只显示需要关注/预警的股票"
        >
          仅预警
        </button>
      </div>
    </div>
    {stocks.length === 0 ? (
      loadError ? null : (
        <div className="py-12 text-center">
          <div className="text-[13px] text-muted-foreground">还没有添加关注股票</div>
          <div className="mt-2 text-[11px] text-muted-foreground/70">点击右上角“添加股票”开始</div>
        </div>
      )
    ) : (
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
        {stocks
          .filter(s => !stockListFilter || s.market === stockListFilter)
          .sort((a, b) => Number(a.sort_order || 0) - Number(b.sort_order || 0) || a.id - b.id)
          .filter(stock => {
            if (!watchlistOnlyAlerts) return true
            const { suggestion } = getSuggestionForStock(stock.symbol, stock.market, false)
            return !!suggestion?.should_alert
          })
          .map((stock) => {
          const quote = getStockQuote(`${stock.market}:${stock.symbol}`)
          const changeColor = quote?.change_pct != null
            ? (quote.change_pct > 0 ? 'text-stock-up' : quote.change_pct < 0 ? 'text-stock-down' : 'text-muted-foreground')
            : 'text-muted-foreground'
          const { suggestion, kline } = getSuggestionForStock(stock.symbol, stock.market, false)
          return (
            <div
              key={stock.id}
              draggable={stockListFilter === '' && !watchlistOnlyAlerts}
              onContextMenu={(e) => openStockContextMenu(e, { symbol: stock.symbol, name: stock.name, market: stock.market, hasPosition: false })}
              onDragStart={(e) => {
                if (stockListFilter !== '' || watchlistOnlyAlerts) return
                watchDragSnapshotRef.current = stocks
                setDraggingWatchStockId(stock.id)
                e.dataTransfer.effectAllowed = 'move'
              }}
              onDragOver={(e) => {
                if (stockListFilter !== '' || watchlistOnlyAlerts) return
                e.preventDefault()
                e.dataTransfer.dropEffect = 'move'
                if (draggingWatchStockId != null) {
                  previewWatchlistReorder(draggingWatchStockId, stock.id)
                }
              }}
              onDrop={(e) => {
                if (stockListFilter !== '' || watchlistOnlyAlerts) return
                e.preventDefault()
                if (draggingWatchStockId != null) commitWatchlistReorder()
                setDraggingWatchStockId(null)
                watchDragSnapshotRef.current = null
              }}
              onDragEnd={() => {
                setDraggingWatchStockId(null)
                watchDragSnapshotRef.current = null
              }}
              className={`group rounded-md border border-border/40 bg-background/30 hover:bg-accent/20 transition-colors px-3 py-2.5 cursor-pointer ${draggingWatchStockId === stock.id ? 'opacity-60' : ''}`}
              onClick={() => {
                if (isSuppressCardClick()) return
                setAgentDialogStock(stock)
              }}
            >
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <div className="flex items-center gap-2 min-w-0">
                    <span className={`text-[9px] px-1 py-0.5 rounded ${marketBadge(stock.market).style}`}>
                      {marketBadge(stock.market).label}
                    </span>
                    <button
                      className="font-mono text-[12px] font-semibold text-foreground hover:text-primary"
                      onClick={(e) => { e.stopPropagation(); openStockDetail(stock.symbol, stock.market, stock.name, false) }}
                    >
                      {stock.symbol}
                    </button>
                    <button
                      className="text-[12px] text-muted-foreground truncate hover:text-primary"
                      onClick={(e) => { e.stopPropagation(); openStockDetail(stock.symbol, stock.market, stock.name, false) }}
                    >
                      {stock.name}
                    </button>
                  </div>
                </div>
                <div className="text-right shrink-0 whitespace-nowrap">
                  <div className={`font-mono text-[14px] font-bold leading-tight tabular-nums ${changeColor}`}>
                    {quote?.current_price != null && Number.isFinite(Number(quote.current_price)) ? Number(quote.current_price).toFixed(2) : '--'}
                  </div>
                  <div className={`font-mono text-[11px] leading-tight tabular-nums ${changeColor}`}>
                    {quote?.change_pct != null && Number.isFinite(Number(quote.change_pct)) ? `${quote.change_pct >= 0 ? '+' : ''}${Number(quote.change_pct).toFixed(2)}%` : '--'}
                  </div>
                </div>
              </div>

              <div className="mt-1.5">
                {(suggestion || kline) ? (
                  <SuggestionBadge
                    suggestion={suggestion}
                    stockName={stock.name}
                    stockSymbol={stock.symbol}
                    kline={kline}
                    market={stock.market}
                    hasPosition={false}
                  />
                ) : (
                  <div className="text-[11px] text-muted-foreground/70 py-2">暂无技术面/AI 分析</div>
                )}
              </div>

              <div className="mt-1.5 pt-1.5 border-t border-border/30 flex items-center justify-between gap-2">
                <div className="flex items-center gap-1 flex-wrap">
                  {stock.agents && stock.agents.length > 0 ? (
                    <Badge variant="secondary" className="text-[10px]">{stock.agents.length} Agent</Badge>
                  ) : (
                    <span className="text-[10px] text-muted-foreground/60">未配置 Agent</span>
                  )}
                  {runningAgents[stock.id] && (
                    <span className="inline-flex items-center gap-1 text-[10px] text-amber-600">
                      <span className="w-3 h-3 border-2 border-current/30 border-t-current rounded-full animate-spin" />
                      {agents.find(a => a.name === runningAgents[stock.id])?.display_name || runningAgents[stock.id]}
                    </span>
                  )}
                </div>
                <div
                  className="flex items-center gap-1 sm:opacity-0 sm:group-hover:opacity-100 transition-opacity"
                  onClick={(e) => e.stopPropagation()}
                >
                  <Button
                    variant="ghost"
                    size="icon"
                    className="h-7 w-7"
                    onClick={() => openKlineDialog(stock.symbol, stock.market, stock.name, false)}
                    title="K线指标"
                  >
                    <BarChart3 className="w-3.5 h-3.5" />
                  </Button>
                  <StockPriceAlertPanel
                    mode="icon"
                    stockId={stock.id}
                    symbol={stock.symbol}
                    market={stock.market}
                    stockName={stock.name}
                    initialTotal={getPriceAlertSummary(stock.symbol, stock.market).total}
                    initialEnabled={getPriceAlertSummary(stock.symbol, stock.market).enabled}
                    onChanged={loadPriceAlertSummaries}
                  />
                  <Button
                    variant="ghost"
                    size="icon"
                    className="h-7 w-7"
                    onClick={() => openNewsDialog(stock.name)}
                    title="相关资讯"
                  >
                    <Newspaper className="w-3.5 h-3.5" />
                  </Button>
                  <Button
                    variant="ghost"
                    size="icon"
                    className="h-7 w-7 hover:text-primary"
                    title="深度分析(TradingAgents)"
                    onClick={() => openDeepAnalysis(stock.id, stock.symbol, stock.name)}
                  >
                    <Brain className="w-3.5 h-3.5" />
                  </Button>
                  <Button
                    variant="ghost"
                    size="icon"
                    className="h-7 w-7"
                    onClick={() => openStockDetail(stock.symbol, stock.market, stock.name, false)}
                    title="详情"
                  >
                    <ExternalLink className="w-3.5 h-3.5" />
                  </Button>
                  <Button
                    variant="ghost"
                    size="icon"
                    className="h-7 w-7 hover:text-destructive"
                    onClick={() => setRemoveWatchStock(stock)}
                    title="删除股票"
                  >
                    <X className="w-3.5 h-3.5" />
                  </Button>
                </div>
              </div>
            </div>
          )
        })}
      </div>
    )}
  </div>
)}
    </>
  )
}
