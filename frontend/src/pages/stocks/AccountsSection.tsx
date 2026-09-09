import StockPriceAlertPanel from '@panwatch/biz-ui/components/stock-price-alert-panel'
import { Activity } from 'lucide-react'
import { Badge } from '@panwatch/base-ui/components/ui/badge'
import { BarChart3 } from 'lucide-react'
import { Bot } from 'lucide-react'
import { Brain } from 'lucide-react'
import { Building2 } from 'lucide-react'
import { Button } from '@panwatch/base-ui/components/ui/button'
import { ChevronDown } from 'lucide-react'
import { ChevronRight } from 'lucide-react'
import { Newspaper } from 'lucide-react'
import { Pencil } from 'lucide-react'
import { Plus } from 'lucide-react'
import { SuggestionBadge } from '@panwatch/biz-ui/components/suggestion-badge'
import { Trash2 } from 'lucide-react'
import { dailyPnlDisplayLabel } from './shared'
import { safeFixed } from '@/lib/format'
import { safeNum } from '@/lib/format'
import { safePrice } from '@/lib/format'
import { summarizeDailyPnlPeriod } from './shared'
import { useStocks } from './context'

export function AccountsSection() {
  const {
    stocks,
    accounts,
    agents,
    portfolio,
    portfolioRaw,
    expandedAccounts,
    viewTab,
    setAgentDialogStock,
    runningAgents,
    draggingPositionId,
    setDraggingPositionId,
    draggingPositionAccountId,
    setDraggingPositionAccountId,
    positionDragSnapshotRef,
    openDeepAnalysis,
    previewPositionReorder,
    commitPositionReorder,
    loadPriceAlertSummaries,
    openKlineDialog,
    openMinuteDialog,
    openNewsDialog,
    openStockDetail,
    openStockContextMenu,
    toggleAccountExpanded,
    openAccountDialog,
    handleDeleteAccount,
    openPositionDialog,
    handleDeletePosition,
    formatMoney,
    marketBadge,
    formatPrice,
    getPriceAlertSummary,
    getSuggestionForStock,
  } = useStocks()
  return (
    <>
{/* Accounts & Positions */}
{viewTab === 'positions' && (
  portfolio && portfolio.accounts.length === 0 ? (
    <div className="flex flex-col items-center justify-center py-20">
      <div className="w-14 h-14 rounded-xl bg-primary/10 flex items-center justify-center mb-4">
        <Building2 className="w-6 h-6 text-primary" />
      </div>
      <p className="text-[15px] font-semibold text-foreground">还没有账户</p>
      <p className="text-[13px] text-muted-foreground mt-1.5">点击"添加账户"创建你的第一个交易账户</p>
    </div>
  ) : (
    <div className="space-y-4">
      {portfolio?.accounts.map(account => (
        <div key={account.id} className="border-b border-border/40">
        {/* Account Header */}
        <div
          className="flex flex-col md:flex-row md:items-center justify-between p-3 md:p-4 cursor-pointer hover:bg-accent/30 transition-colors gap-2"
          onClick={() => toggleAccountExpanded(account.id)}
        >
          <div className="flex items-center gap-2 md:gap-3">
            {expandedAccounts.has(account.id) ? (
              <ChevronDown className="w-4 h-4 text-muted-foreground" />
            ) : (
              <ChevronRight className="w-4 h-4 text-muted-foreground" />
            )}
            <Building2 className="w-4 h-4 text-primary" />
            <span className="text-[14px] md:text-[15px] font-semibold text-foreground">{account.name}</span>
            <span className="text-[11px] md:text-[12px] text-muted-foreground">
              {account.positions.length} 只
            </span>
          </div>
          <div className="flex items-center justify-between md:justify-end gap-2 md:gap-6 pl-6 md:pl-0">
            <div className="flex items-center gap-2.5 md:gap-6 min-w-0">
              <div className="text-left md:text-right">
                <div className="text-[10px] md:text-[11px] text-muted-foreground">市值</div>
                <div className="text-[12px] md:text-[13px] font-mono font-medium whitespace-nowrap tabular-nums">{formatMoney(account.total_market_value)}</div>
              </div>
              <div className="text-left md:text-right">
                <div className="text-[10px] md:text-[11px] text-muted-foreground">盈亏</div>
                <div className={`text-[12px] md:text-[13px] font-mono font-medium whitespace-nowrap tabular-nums ${account.total_pnl >= 0 ? 'text-stock-up' : 'text-stock-down'}`}>
                  {account.total_pnl >= 0 ? '+' : ''}{formatMoney(account.total_pnl)}
                  <span className="text-[10px] md:text-[11px] ml-1 hidden md:inline">({safeNum(account.total_pnl_pct) === null ? '--' : `${account.total_pnl_pct >= 0 ? '+' : ''}${safeFixed(account.total_pnl_pct)}%`})</span>
                </div>
              </div>
              <div className="text-left md:text-right">
                <div
                  className="text-[10px] md:text-[11px] text-muted-foreground"
                  title={dailyPnlDisplayLabel(account)}
                >
                  {dailyPnlDisplayLabel(account, true)}
                </div>
                <div className={`text-[12px] md:text-[13px] font-mono font-medium whitespace-nowrap tabular-nums ${account.total_daily_pnl >= 0 ? 'text-stock-up' : 'text-stock-down'}`}>
                  {account.total_daily_pnl >= 0 ? '+' : ''}{formatMoney(account.total_daily_pnl)}
                </div>
              </div>
              <div className="text-left md:text-right hidden sm:block">
                <div className="text-[10px] md:text-[11px] text-muted-foreground">可用</div>
                <div className="text-[12px] md:text-[13px] font-mono whitespace-nowrap tabular-nums">{formatMoney(account.available_funds)}</div>
              </div>
            </div>
            <div className="flex items-center gap-0 md:gap-1 shrink-0" onClick={e => e.stopPropagation()}>
              <Button variant="ghost" size="icon" className="h-7 w-7 md:h-8 md:w-8" onClick={() => openPositionDialog(account.id)}>
                <Plus className="w-3 md:w-3.5 h-3 md:h-3.5" />
              </Button>
              <Button variant="ghost" size="icon" className="h-7 w-7 md:h-8 md:w-8" onClick={() => openAccountDialog(accounts.find(a => a.id === account.id))}>
                <Pencil className="w-3 md:w-3.5 h-3 md:h-3.5" />
              </Button>
              <Button variant="ghost" size="icon" className="h-7 w-7 md:h-8 md:w-8 hover:text-destructive" onClick={() => handleDeleteAccount(account.id)}>
                <Trash2 className="w-3 md:w-3.5 h-3 md:h-3.5" />
              </Button>
            </div>
          </div>
        </div>

        {/* Positions */}
        {expandedAccounts.has(account.id) && (
          <div className="border-t border-border/30">
            {account.positions.length === 0 ? (
              <p className="text-[13px] text-muted-foreground text-center py-8">暂无持仓，点击 + 添加</p>
            ) : (
              <>
                {/* Desktop Table */}
                <div className="hidden md:block overflow-x-auto">
                  <table className="w-full">
                    <thead>
                      <tr className="border-b border-border/30 bg-accent/20">
                        <th className="text-left px-4 py-2 text-[11px] font-semibold text-muted-foreground">股票</th>
                        <th className="text-right px-4 py-2 text-[11px] font-semibold text-muted-foreground">现价</th>
                        <th className="text-right px-4 py-2 text-[11px] font-semibold text-muted-foreground">涨跌</th>
                        <th className="text-right px-4 py-2 text-[11px] font-semibold text-muted-foreground">成本</th>
                        <th className="text-right px-4 py-2 text-[11px] font-semibold text-muted-foreground">持仓</th>
                        <th className="text-right px-4 py-2 text-[11px] font-semibold text-muted-foreground">市值</th>
                        <th className="text-right px-4 py-2 text-[11px] font-semibold text-muted-foreground">盈亏</th>
                        <th
                          className="text-right px-4 py-2 text-[11px] font-semibold text-muted-foreground"
                          title={dailyPnlDisplayLabel(account)}
                        >
                          {dailyPnlDisplayLabel(account, true)}
                        </th>
                        <th className="hidden lg:table-cell text-center px-4 py-2 text-[11px] font-semibold text-muted-foreground">风格</th>
                        <th className="hidden lg:table-cell text-left px-4 py-2 text-[11px] font-semibold text-muted-foreground">Agent</th>
                        <th className="text-center px-4 py-2 text-[11px] font-semibold text-muted-foreground">操作</th>
                      </tr>
                    </thead>
                    <tbody>
                      {account.positions.map((pos, i) => {
                        const stock = stocks.find(s => s.id === pos.stock_id)
                        const badge = marketBadge(pos.market)
                        const isForeign = pos.market === 'HK' || pos.market === 'US'
                        const changeColor = pos.change_pct != null
                          ? (pos.change_pct > 0 ? 'text-stock-up' : pos.change_pct < 0 ? 'text-stock-down' : 'text-muted-foreground')
                          : 'text-muted-foreground'
                        const pnlColor = pos.pnl != null
                          ? (pos.pnl > 0 ? 'text-stock-up' : pos.pnl < 0 ? 'text-stock-down' : 'text-muted-foreground')
                          : 'text-muted-foreground'
                        return (
                          <tr
                            key={pos.id}
                            draggable
                            onContextMenu={(e) => openStockContextMenu(e, { symbol: pos.symbol, name: pos.name, market: pos.market, hasPosition: true })}
                            onDragStart={(e) => {
                              positionDragSnapshotRef.current = portfolioRaw ? JSON.parse(JSON.stringify(portfolioRaw)) : null
                              setDraggingPositionId(pos.id)
                              setDraggingPositionAccountId(account.id)
                              e.dataTransfer.effectAllowed = 'move'
                            }}
                            onDragOver={(e) => {
                              e.preventDefault()
                              e.dataTransfer.dropEffect = 'move'
                              if (draggingPositionId != null && draggingPositionAccountId === account.id) {
                                previewPositionReorder(account.id, draggingPositionId, pos.id)
                              }
                            }}
                            onDrop={(e) => {
                              e.preventDefault()
                              if (draggingPositionId != null && draggingPositionAccountId === account.id) {
                                commitPositionReorder(account.id)
                              }
                              setDraggingPositionId(null)
                              setDraggingPositionAccountId(null)
                              positionDragSnapshotRef.current = null
                            }}
                            onDragEnd={() => {
                              setDraggingPositionId(null)
                              setDraggingPositionAccountId(null)
                              positionDragSnapshotRef.current = null
                            }}
                            className={`group hover:bg-accent/30 transition-colors ${i > 0 ? 'border-t border-border/20' : ''} ${draggingPositionId === pos.id ? 'opacity-60' : ''}`}
                          >
                            <td className="px-4 py-2.5">
                              <span className={`text-[9px] px-1 py-0.5 rounded mr-1.5 ${badge.style}`}>{badge.label}</span>
                              <span className="font-mono text-[12px] font-semibold text-foreground">
                                {pos.symbol}
                              </span>
                              <button
                                className="ml-1.5 text-[12px] text-muted-foreground hover:text-primary"
                                onClick={() => openStockDetail(pos.symbol, pos.market, pos.name, true)}
                              >
                                {pos.name}
                              </button>
                              {(() => {
                                const { suggestion, kline } = getSuggestionForStock(pos.symbol, pos.market, true)
                                return (suggestion || kline) ? (
                                  <span className="ml-2">
                                    <SuggestionBadge
                                      suggestion={suggestion}
                                      stockName={pos.name}
                                      stockSymbol={pos.symbol}
                                      kline={kline}
                                      market={pos.market}
                                      hasPosition={true}
                                    />
                                  </span>
                                ) : null
                              })()}
                            </td>
                            <td className={`px-4 py-2.5 text-right font-mono tabular-nums text-[12px] ${changeColor}`}>
                              {pos.current_price != null && Number.isFinite(Number(pos.current_price)) ? <span>{Number(pos.current_price).toFixed(2)}{isForeign ? (pos.market === 'HK' ? ' HKD' : ' USD') : ''}</span> : '—'}
                            </td>
                            <td className={`px-4 py-2.5 text-right font-mono tabular-nums text-[12px] ${changeColor}`}>
                              {pos.change_pct != null && Number.isFinite(Number(pos.change_pct)) ? `${pos.change_pct >= 0 ? '+' : ''}${Number(pos.change_pct).toFixed(2)}%` : '—'}
                            </td>
                            <td className="px-4 py-2.5 text-right font-mono tabular-nums text-[12px] text-muted-foreground">{formatPrice(pos.cost_price)}</td>
                            <td className="px-4 py-2.5 text-right font-mono tabular-nums text-[12px] text-muted-foreground">{pos.quantity}</td>
                            <td className="px-4 py-2.5 text-right font-mono tabular-nums text-[12px] text-muted-foreground">
                              {pos.market_value != null ? (
                                <div className="flex flex-col items-end">
                                  {isForeign ? (
                                    <>
                                      <span>{formatMoney(pos.market_value)} {pos.market === 'HK' ? 'HKD' : 'USD'}</span>
                                      {pos.market_value_cny && <span className="text-[10px] text-muted-foreground/60">≈{formatMoney(pos.market_value_cny)}</span>}
                                    </>
                                  ) : <span>{formatMoney(pos.market_value)}</span>}
                                </div>
                              ) : '—'}
                            </td>
                            <td className={`px-4 py-2.5 text-right font-mono tabular-nums text-[12px] ${pnlColor}`}>
                              {pos.pnl != null ? (
                                <div className="flex flex-col items-end">
                                  <span>{pos.pnl >= 0 ? '+' : ''}{formatMoney(pos.pnl)}</span>
                                  <span className="text-[10px] opacity-70">{pos.pnl_pct != null && Number.isFinite(Number(pos.pnl_pct)) ? `${pos.pnl_pct >= 0 ? '+' : ''}${Number(pos.pnl_pct).toFixed(2)}%` : ''}{isForeign && ' CNY'}</span>
                                </div>
                              ) : '—'}
                            </td>
                            <td
                              className={`px-4 py-2.5 text-right font-mono tabular-nums text-[12px] ${pos.daily_pnl != null ? (pos.daily_pnl >= 0 ? 'text-stock-up' : 'text-stock-down') : ''}`}
                              title={pos.quote_time ? `行情时间：${pos.quote_time}` : undefined}
                            >
                              {pos.daily_pnl != null ? (
                                <div className="flex flex-col items-end">
                                  <span>{pos.daily_pnl >= 0 ? '+' : ''}{formatMoney(pos.daily_pnl)}</span>
                                  <span className="text-[10px] opacity-70">{pos.daily_pnl_pct != null && Number.isFinite(Number(pos.daily_pnl_pct)) ? `${pos.daily_pnl_pct >= 0 ? '+' : ''}${Number(pos.daily_pnl_pct).toFixed(2)}%` : ''}</span>
                                </div>
                              ) : '—'}
                            </td>
                            <td className="hidden lg:table-cell px-4 py-2.5 text-center">
                              {pos.trading_style ? (
                                <span className={`text-[10px] px-1.5 py-0.5 rounded ${pos.trading_style === 'short' ? 'bg-rose-500/10 text-rose-600' : pos.trading_style === 'long' ? 'bg-blue-500/10 text-blue-600' : 'bg-amber-500/10 text-amber-600'}`}>
                                  {pos.trading_style === 'short' ? '短线' : pos.trading_style === 'long' ? '长线' : '波段'}
                                </span>
                              ) : (
                                <span className="text-[10px] text-muted-foreground/50">-</span>
                              )}
                            </td>
                            <td className="hidden lg:table-cell px-4 py-2.5">
                              {stock && (
                                <button onClick={() => setAgentDialogStock(stock)} className="flex items-center gap-1.5 hover:opacity-70 transition-opacity">
                                  {stock.agents && stock.agents.length > 0 ? (
                                    <div className="flex items-center gap-1.5 flex-wrap">
                                      {stock.agents.map(sa => {
                                        const agent = agents.find(a => a.name === sa.agent_name)
                                        const isRunning = runningAgents[stock.id] === sa.agent_name
                                        return (
                                          <span key={sa.agent_name} className="inline-flex items-center gap-1">
                                            <Badge variant="default" className="text-[10px]">{agent?.display_name || sa.agent_name}</Badge>
                                            {isRunning && (
                                              <span className="inline-flex items-center gap-1 text-[10px] text-amber-600">
                                                <span className="w-3 h-3 border-2 border-current/30 border-t-current rounded-full animate-spin" />
                                                执行中
                                              </span>
                                            )}
                                          </span>
                                        )
                                      })}
                                    </div>
                                  ) : (
                                    <span className="text-[11px] text-muted-foreground/50 flex items-center gap-1"><Bot className="w-3 h-3" /> 未配置</span>
                                  )}
                                </button>
                              )}
                            </td>
                            <td className="px-4 py-2.5 text-center">
                              <div className="flex items-center justify-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
                                {(() => { const { suggestion, kline } = getSuggestionForStock(pos.symbol, pos.market, true); return (!suggestion && !kline) ? (
                                  <>
                                  <Button variant="ghost" size="icon" className="h-7 w-7" onClick={() => openMinuteDialog(pos.symbol, pos.market, pos.name)} title="分时"><Activity className="w-3 h-3" /></Button>
                                  <Button variant="ghost" size="icon" className="h-7 w-7" onClick={() => openKlineDialog(pos.symbol, pos.market, pos.name, true)} title="K线指标"><BarChart3 className="w-3 h-3" /></Button>
                                  </>
                                ) : null })()}
                                <StockPriceAlertPanel
                                  mode="icon"
                                  stockId={pos.stock_id}
                                  symbol={pos.symbol}
                                  market={pos.market}
                                  stockName={pos.name}
                                  initialTotal={getPriceAlertSummary(pos.symbol, pos.market).total}
                                  initialEnabled={getPriceAlertSummary(pos.symbol, pos.market).enabled}
                                  onChanged={loadPriceAlertSummaries}
                                />
                                <Button variant="ghost" size="icon" className="h-7 w-7" onClick={() => openNewsDialog(pos.name)} title="相关资讯"><Newspaper className="w-3 h-3" /></Button>
                                <Button variant="ghost" size="icon" className="h-7 w-7 hover:text-primary" title="深度分析(TradingAgents)" onClick={() => openDeepAnalysis(pos.stock_id, pos.symbol, pos.name)}><Brain className="w-3 h-3" /></Button>
                                <Button variant="ghost" size="icon" className="h-7 w-7" onClick={() => openPositionDialog(account.id, pos)}><Pencil className="w-3 h-3" /></Button>
                                <Button variant="ghost" size="icon" className="h-7 w-7 hover:text-destructive" onClick={() => handleDeletePosition(pos.id)}><Trash2 className="w-3 h-3" /></Button>
                              </div>
                            </td>
                          </tr>
                        )
                      })}
                    </tbody>
                  </table>
                </div>

                {/* Mobile Cards */}
                <div className="md:hidden divide-y divide-border/30">
                  {account.positions.map(pos => {
                    const stock = stocks.find(s => s.id === pos.stock_id)
                    const badge = marketBadge(pos.market)
                    const changeColor = pos.change_pct != null
                      ? (pos.change_pct > 0 ? 'text-stock-up' : pos.change_pct < 0 ? 'text-stock-down' : 'text-muted-foreground')
                      : 'text-muted-foreground'
                    const pnlColor = pos.pnl != null
                      ? (pos.pnl > 0 ? 'text-stock-up' : pos.pnl < 0 ? 'text-stock-down' : 'text-muted-foreground')
                      : 'text-muted-foreground'
                    return (
                      <div
                        key={pos.id}
                        draggable
                        onContextMenu={(e) => openStockContextMenu(e, { symbol: pos.symbol, name: pos.name, market: pos.market, hasPosition: true })}
                        onDragStart={(e) => {
                          positionDragSnapshotRef.current = portfolioRaw ? JSON.parse(JSON.stringify(portfolioRaw)) : null
                          setDraggingPositionId(pos.id)
                          setDraggingPositionAccountId(account.id)
                          e.dataTransfer.effectAllowed = 'move'
                        }}
                        onDragOver={(e) => {
                          e.preventDefault()
                          e.dataTransfer.dropEffect = 'move'
                          if (draggingPositionId != null && draggingPositionAccountId === account.id) {
                            previewPositionReorder(account.id, draggingPositionId, pos.id)
                          }
                        }}
                        onDrop={(e) => {
                          e.preventDefault()
                          if (draggingPositionId != null && draggingPositionAccountId === account.id) {
                            commitPositionReorder(account.id)
                          }
                          setDraggingPositionId(null)
                          setDraggingPositionAccountId(null)
                          positionDragSnapshotRef.current = null
                        }}
                        onDragEnd={() => {
                          setDraggingPositionId(null)
                          setDraggingPositionAccountId(null)
                          positionDragSnapshotRef.current = null
                        }}
                        className={`px-3 py-2.5 hover:bg-accent/30 transition-colors ${draggingPositionId === pos.id ? 'opacity-60' : ''}`}
                      >
                        {/* Row 1: Stock info + Current price */}
                        <div className="flex items-center justify-between gap-2 mb-1.5">
                          <div className="flex items-center gap-1.5 min-w-0">
                            <span className={`shrink-0 text-[9px] px-1 py-0.5 rounded ${badge.style}`}>{badge.label}</span>
                            <span className="shrink-0 font-mono text-[12px] font-semibold text-foreground">
                              {pos.symbol}
                            </span>
                            <button
                              className="text-[12px] text-muted-foreground hover:text-primary truncate"
                              onClick={() => openStockDetail(pos.symbol, pos.market, pos.name, true)}
                            >
                              {pos.name}
                            </button>
                            {pos.trading_style && (
                              <span className={`shrink-0 text-[9px] px-1 py-0.5 rounded ${pos.trading_style === 'short' ? 'bg-rose-500/10 text-rose-600' : pos.trading_style === 'long' ? 'bg-blue-500/10 text-blue-600' : 'bg-amber-500/10 text-amber-600'}`}>
                                {pos.trading_style === 'short' ? '短' : pos.trading_style === 'long' ? '长' : '波'}
                              </span>
                            )}
                          </div>
                          <div className={`font-mono text-[13px] font-medium whitespace-nowrap shrink-0 tabular-nums ${changeColor}`}>
                            {safePrice(pos.current_price) === '--' ? '—' : safePrice(pos.current_price)}
                            {pos.change_pct != null && Number.isFinite(Number(pos.change_pct)) && <span className="text-[11px] ml-1">{pos.change_pct >= 0 ? '+' : ''}{Number(pos.change_pct).toFixed(2)}%</span>}
                          </div>
                        </div>
                        {/* Row 2 (Suggestion badge, dedicated row to avoid wrapping mess) */}
                        {(() => {
                          const { suggestion, kline } = getSuggestionForStock(pos.symbol, pos.market, true)
                          return (suggestion || kline) ? (
                            <div className="mb-1.5">
                              <SuggestionBadge
                                suggestion={suggestion}
                                stockName={pos.name}
                                stockSymbol={pos.symbol}
                                kline={kline}
                                market={pos.market}
                                hasPosition={true}
                              />
                            </div>
                          ) : null
                        })()}
                        {/* Row 3: Stats grid (4 cols, whitespace-nowrap to prevent "万" wrapping) */}
                        <div className="grid grid-cols-4 gap-2 text-[11px]">
                          <div className="min-w-0">
                            <div className="text-[10px] text-muted-foreground">成本</div>
                            <div className="font-mono text-foreground truncate tabular-nums" title={String(pos.cost_price)}>{formatPrice(pos.cost_price)}</div>
                          </div>
                          <div className="min-w-0">
                            <div className="text-[10px] text-muted-foreground">数量</div>
                            <div className="font-mono text-foreground truncate tabular-nums" title={String(pos.quantity)}>{pos.quantity}</div>
                          </div>
                          <div className="min-w-0">
                            <div className="text-[10px] text-muted-foreground">盈亏</div>
                            <div className={`font-mono whitespace-nowrap tabular-nums ${pnlColor}`}>
                              {pos.pnl != null ? `${pos.pnl >= 0 ? '+' : ''}${formatMoney(pos.pnl)}` : '—'}
                            </div>
                            {pos.pnl_pct != null && (
                              <div className={`text-[10px] font-mono tabular-nums ${pnlColor} opacity-80`}>
                                {safeNum(pos.pnl_pct) === null ? '' : `${pos.pnl_pct >= 0 ? '+' : ''}${safeFixed(pos.pnl_pct)}%`}
                              </div>
                            )}
                          </div>
                          <div className="min-w-0">
                            <div className="text-[10px] text-muted-foreground">
                              {dailyPnlDisplayLabel(summarizeDailyPnlPeriod([
                                { period: pos.daily_pnl_period, date: pos.quote_date },
                              ]), true)}
                            </div>
                            <div className={`font-mono whitespace-nowrap tabular-nums ${pos.daily_pnl != null ? (pos.daily_pnl >= 0 ? 'text-stock-up' : 'text-stock-down') : 'text-muted-foreground'}`}>
                              {pos.daily_pnl != null ? `${pos.daily_pnl >= 0 ? '+' : ''}${formatMoney(pos.daily_pnl)}` : '—'}
                            </div>
                          </div>
                        </div>
                        {/* Row 4: Actions */}
                        <div className="flex items-center justify-between mt-1.5 pt-1.5 border-t border-border/20">
                          <div>
                            {stock && stock.agents && stock.agents.length > 0 ? (
                              <button onClick={() => setAgentDialogStock(stock)} className="flex items-center gap-1">
                                {stock.agents.slice(0, 2).map(sa => {
                                  const agent = agents.find(a => a.name === sa.agent_name)
                                  const isRunning = runningAgents[stock.id] === sa.agent_name
                                  return (
                                    <span key={sa.agent_name} className="inline-flex items-center gap-1">
                                      <Badge variant="secondary" className="text-[9px]">{agent?.display_name || sa.agent_name}</Badge>
                                      {isRunning && (
                                        <span className="inline-flex items-center gap-1 text-[10px] text-amber-600">
                                          <span className="w-3 h-3 border-2 border-current/30 border-t-current rounded-full animate-spin" />
                                          执行中
                                        </span>
                                      )}
                                    </span>
                                  )
                                })}
                              </button>
                            ) : (
                              <button onClick={() => stock && setAgentDialogStock(stock)} className="text-[10px] text-muted-foreground/50 flex items-center gap-1">
                                <Bot className="w-3 h-3" /> Agent
                              </button>
                            )}
                          </div>
                          <div className="flex items-center gap-1">
                            {(() => { const { suggestion, kline } = getSuggestionForStock(pos.symbol, pos.market, true); return (!suggestion && !kline) ? (
                              <>
                              <Button variant="ghost" size="icon" className="h-7 w-7" onClick={() => openMinuteDialog(pos.symbol, pos.market, pos.name)} title="分时"><Activity className="w-3 h-3" /></Button>
                              <Button variant="ghost" size="icon" className="h-7 w-7" onClick={() => openKlineDialog(pos.symbol, pos.market, pos.name, true)} title="K线指标"><BarChart3 className="w-3 h-3" /></Button>
                              </>
                            ) : null })()}
                            <StockPriceAlertPanel
                              mode="icon"
                              stockId={pos.stock_id}
                              symbol={pos.symbol}
                              market={pos.market}
                              stockName={pos.name}
                              initialTotal={getPriceAlertSummary(pos.symbol, pos.market).total}
                              initialEnabled={getPriceAlertSummary(pos.symbol, pos.market).enabled}
                              onChanged={loadPriceAlertSummaries}
                            />
                            <Button variant="ghost" size="icon" className="h-7 w-7" onClick={() => openNewsDialog(pos.name)}><Newspaper className="w-3 h-3" /></Button>
                            <Button variant="ghost" size="icon" className="h-7 w-7 hover:text-primary" title="深度分析(TradingAgents)" onClick={() => openDeepAnalysis(pos.stock_id, pos.symbol, pos.name)}><Brain className="w-3 h-3" /></Button>
                            <Button variant="ghost" size="icon" className="h-7 w-7" onClick={() => openPositionDialog(account.id, pos)}><Pencil className="w-3 h-3" /></Button>
                            <Button variant="ghost" size="icon" className="h-7 w-7 hover:text-destructive" onClick={() => handleDeletePosition(pos.id)}><Trash2 className="w-3 h-3" /></Button>
                          </div>
                        </div>
                      </div>
                    )
                  })}
                </div>
              </>
            )}
          </div>
        )}
      </div>
    ))}
  </div>
  )
)}
    </>
  )
}
