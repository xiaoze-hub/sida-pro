import { useInsight } from './context'
import AddPositionCalculator from '@panwatch/biz-ui/components/add-position-calculator'
import { AGENT_LABELS } from './types'
import { Button } from '@panwatch/base-ui/components/ui/button'
import { InfoTip } from './helpers'
import { SuggestionBadge } from '@panwatch/biz-ui/components/suggestion-badge'
import { TechnicalIndicatorStrip } from './helpers'
import { formatCompactNumber } from './helpers'
import { formatMarketCap } from './helpers'
import { formatNumber } from './helpers'
import { formatTime } from './helpers'
import { markdownToPlainText } from './helpers'

export function OverviewTab() {
  const {
    symbol,
    market,
    setTab,
    quote,
    moreInfo,
    moreInfoLoading,
    darkFlowTq,
    klineSummary,
    miniKlines,
    miniKlineLoading,
    miniHoverIdx,
    setMiniHoverIdx,
    suggestions,
    news,
    autoSuggesting,
    holdingAgg,
    resolvedName,
    miniKlineExtrema,
    technicalScored,
    technicalFallbackSuggestion,
    changeColor,
    priceColor,
    levelColor,
    stockColors,
    amplitudePct,
    latestReport,
    props,
  } = useInsight()
  return (
    <div className="space-y-3">
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-3 items-stretch">
        <div className="card p-4 h-full">
          <div className="mt-1 flex items-end justify-between gap-3">
            <div className={`text-[34px] leading-none font-bold font-mono ${priceColor}`}>
              {quote?.current_price != null ? formatNumber(quote.current_price) : '--'}
            </div>
            <div className={`text-[16px] font-mono ${changeColor}`}>
              {quote?.change_pct != null ? `${quote.change_pct >= 0 ? '+' : ''}${quote.change_pct.toFixed(2)}%` : '--'}
            </div>
          </div>
          <div className="mt-3 grid grid-cols-3 gap-2 text-[12px]">
            <div className="rounded bg-accent/15 px-2 py-1.5"><div className="text-[10px] text-muted-foreground">今开</div><div className={`font-mono ${levelColor(quote?.open_price)}`}>{formatNumber(quote?.open_price)}</div></div>
            <div className="rounded bg-accent/15 px-2 py-1.5"><div className="text-[10px] text-muted-foreground">最高</div><div className={`font-mono ${levelColor(quote?.high_price)}`}>{formatNumber(quote?.high_price)}</div></div>
            <div className="rounded bg-accent/15 px-2 py-1.5"><div className="text-[10px] text-muted-foreground">最低</div><div className={`font-mono ${levelColor(quote?.low_price)}`}>{formatNumber(quote?.low_price)}</div></div>
            <div className="rounded bg-accent/15 px-2 py-1.5"><div className="text-[10px] text-muted-foreground">成交量</div><div className="font-mono">{formatCompactNumber(quote?.volume)}</div></div>
            <div className="rounded bg-accent/15 px-2 py-1.5"><div className="text-[10px] text-muted-foreground">成交额</div><div className="font-mono">{formatCompactNumber(quote?.turnover)}</div></div>
            <div className="rounded bg-accent/15 px-2 py-1.5"><div className="text-[10px] text-muted-foreground">振幅</div><div className="font-mono">{amplitudePct != null ? `${amplitudePct.toFixed(2)}%` : '--'}</div></div>
            <div className="rounded bg-accent/15 px-2 py-1.5"><div className="text-[10px] text-muted-foreground">换手率</div><div className="font-mono">{quote?.turnover_rate != null ? `${Number(quote.turnover_rate).toFixed(2)}%` : '--'}</div></div>
            <div className="rounded bg-accent/15 px-2 py-1.5"><div className="text-[10px] text-muted-foreground">量比</div><div className="font-mono">{quote?.volume_ratio != null ? Number(quote.volume_ratio).toFixed(2) : '--'}</div></div>
            <div className="rounded bg-accent/15 px-2 py-1.5"><div className="text-[10px] text-muted-foreground">市盈率</div><div className="font-mono">{quote?.pe_ratio != null ? Number(quote.pe_ratio).toFixed(2) : '--'}</div></div>
            <div className="rounded bg-accent/15 px-2 py-1.5"><div className="text-[10px] text-muted-foreground">总市值</div><div className="font-mono">{formatMarketCap(quote?.total_market_value, market)}</div></div>
            <div className="rounded bg-accent/15 px-2 py-1.5"><div className="text-[10px] text-muted-foreground">流通市值</div><div className="font-mono">{formatMarketCap(quote?.circulating_market_value, market)}</div></div>
          </div>
          <div className="mt-3 border-t border-border/50 pt-3">
            <div className="text-[11px] text-muted-foreground mb-2">持仓信息</div>
            {holdingAgg ? (
              <div className="grid grid-cols-2 gap-2 text-[12px]">
                <div className="rounded bg-emerald-500/10 px-2 py-1.5">
                  <div className="text-[10px] text-muted-foreground">持仓数量</div>
                  <div className="font-mono">{holdingAgg.quantity}</div>
                </div>
                <div className="rounded bg-emerald-500/10 px-2 py-1.5">
                  <div className="text-[10px] text-muted-foreground">持仓成本(单价)</div>
                  <div
                    className={`font-mono ${
                      quote?.current_price != null
                        ? quote.current_price > holdingAgg.unitCost
                          ? 'text-stock-up'
                          : quote.current_price < holdingAgg.unitCost
                            ? 'text-stock-down'
                            : 'text-foreground'
                        : 'text-foreground'
                    }`}
                  >
                    {formatNumber(holdingAgg.unitCost)}
                  </div>
                </div>
                <div className="rounded bg-emerald-500/10 px-2 py-1.5">
                  <div className="text-[10px] text-muted-foreground">持仓市值</div>
                  <div className="font-mono">{formatCompactNumber(holdingAgg.marketValue)}</div>
                </div>
                <div className="rounded bg-emerald-500/10 px-2 py-1.5">
                  <div className="text-[10px] text-muted-foreground">总盈亏</div>
                  <div className={`font-mono ${holdingAgg.pnl >= 0 ? 'text-stock-up' : 'text-stock-down'}`}>
                    {holdingAgg.pnl >= 0 ? '+' : ''}{formatCompactNumber(holdingAgg.pnl)}
                  </div>
                </div>
              </div>
            ) : (
              <div className="text-[11px] text-muted-foreground">未在持仓中</div>
            )}
            <AddPositionCalculator
              symbol={symbol}
              market={market}
              currentQuantity={holdingAgg?.quantity ?? 0}
              currentCost={holdingAgg?.unitCost ?? 0}
              currentPrice={quote?.current_price ?? null}
            />
          </div>
        </div>

        <div className="card p-4 h-full">
          <div className="text-[12px] text-muted-foreground mb-2">迷你K线</div>
          {!klineSummary ? (
            <div className="text-[12px] text-muted-foreground py-8">暂无K线摘要</div>
          ) : (
            <>
              {miniKlineLoading ? (
                <div className="h-32 rounded bg-accent/30 animate-pulse" />
              ) : miniKlines.length > 0 && miniKlineExtrema ? (
                <svg
                  viewBox="0 0 320 120"
                  className="w-full h-32 cursor-pointer"
                  onClick={() => setTab('kline')}
                  onMouseLeave={() => setMiniHoverIdx(null)}
                  onMouseMove={(e) => {
                    const rect = e.currentTarget.getBoundingClientRect()
                    const x = e.clientX - rect.left
                    const ratio = rect.width > 0 ? x / rect.width : 0
                    const idx = Math.floor(ratio * miniKlines.length)
                    setMiniHoverIdx(Math.max(0, Math.min(miniKlines.length - 1, idx)))
                  }}
                >
                  <title>点击进入交互式K线</title>
                  {miniKlines.map((k, idx) => {
                    const xStep = 320 / miniKlines.length
                    const x = xStep * idx + xStep / 2
                    const bodyW = Math.max(2, xStep * 0.5)
                    const toY = (v: number) => 114 - ((v - miniKlineExtrema.low) / (miniKlineExtrema.high - miniKlineExtrema.low)) * 100
                    const yOpen = toY(Number(k.open))
                    const yClose = toY(Number(k.close))
                    const yHigh = toY(Number(k.high))
                    const yLow = toY(Number(k.low))
                    const up = Number(k.close) >= Number(k.open)
                    const color = up ? stockColors.up : stockColors.down
                    const bodyTop = Math.min(yOpen, yClose)
                    const bodyH = Math.max(1.4, Math.abs(yOpen - yClose))
                    const active = miniHoverIdx === idx
                    return (
                      <g key={`${k.date}-${idx}`}>
                        {active && <rect x={x - xStep / 2} y={6} width={xStep} height={108} fill="rgba(59,130,246,0.10)" />}
                        <line x1={x} y1={yHigh} x2={x} y2={yLow} stroke={color} strokeWidth="1" />
                        <rect x={x - bodyW / 2} y={bodyTop} width={bodyW} height={bodyH} fill={color} rx="0.6" />
                      </g>
                    )
                  })}
                </svg>
              ) : (
                <div className="h-32 text-[11px] text-muted-foreground flex items-center justify-center">暂无迷你K线</div>
              )}
              <div className="mt-2 rounded bg-accent/10 p-2.5">
                <TechnicalIndicatorStrip
                  klineSummary={klineSummary}
                  technicalSuggestion={technicalFallbackSuggestion}
                  stockName={resolvedName}
                  stockSymbol={symbol}
                  market={market}
                  hasPosition={!!props.hasPosition}
                  score={Number(technicalScored?.score ?? 0)}
                  evidence={technicalScored?.evidence || []}
                />
              </div>
            </>
          )}
        </div>
      </div>

      {/* TQ 扩展指标 · 104字段精简18项（委比/封单/竞价/连板/涨幅/估值） */}
      {moreInfo && (
        <div className="card p-4">
          <div className="flex items-center justify-between mb-2">
            <div className="text-[11px] text-muted-foreground">扩展指标 · TQ实时</div>
            <div className="text-[10px] text-muted-foreground">{moreInfoLoading ? '更新中…' : moreInfo.quote_time ? formatTime(moreInfo.quote_time) : ''}</div>
          </div>
          <div className="grid grid-cols-3 md:grid-cols-4 xl:grid-cols-6 gap-2 text-[11px]">
            <div className="rounded bg-accent/10 px-2 py-1.5"><div className="text-[10px] text-muted-foreground flex items-center">委比<InfoTip k="commission_ratio" /></div><div className="font-mono">{moreInfo.commission_ratio != null ? `${Number(moreInfo.commission_ratio).toFixed(2)}%` : '--'}</div></div>
            <div className="rounded bg-accent/10 px-2 py-1.5"><div className="text-[10px] text-muted-foreground flex items-center">封单额<InfoTip k="limit_up_amount" /></div><div className="font-mono">{moreInfo.limit_up_amount != null && moreInfo.limit_up_amount !== 0 ? `${(moreInfo.limit_up_amount/10000).toFixed(2)}亿` : '--'}</div></div>
            <div className="rounded bg-accent/10 px-2 py-1.5"><div className="text-[10px] text-muted-foreground flex items-center">封成比<InfoTip k="limit_up_ratio" /></div><div className="font-mono">{moreInfo.limit_up_ratio != null ? Number(moreInfo.limit_up_ratio).toFixed(2) : '--'}</div></div>
            <div className="rounded bg-accent/10 px-2 py-1.5"><div className="text-[10px] text-muted-foreground flex items-center">竞价金额<InfoTip k="open_amount" /></div><div className="font-mono">{moreInfo.open_amount != null && moreInfo.open_amount !== 0 ? `${(moreInfo.open_amount/10000).toFixed(2)}亿` : '--'}</div></div>
            <div className="rounded bg-accent/10 px-2 py-1.5"><div className="text-[10px] text-muted-foreground flex items-center">竞价涨停买<InfoTip k="open_limit_buy" /></div><div className="font-mono">{moreInfo.open_limit_buy != null && moreInfo.open_limit_buy !== 0 ? `${(moreInfo.open_limit_buy/10000).toFixed(2)}亿` : '--'}</div></div>
            <div className="rounded bg-accent/10 px-2 py-1.5"><div className="text-[10px] text-muted-foreground flex items-center">连板天<InfoTip k="consecutive_limit_days" /></div><div className="font-mono">{moreInfo.consecutive_limit_days ?? '--'}</div></div>
            <div className="rounded bg-accent/10 px-2 py-1.5"><div className="text-[10px] text-muted-foreground flex items-center">连涨天<InfoTip k="consecutive_up_days" /></div><div className="font-mono">{moreInfo.consecutive_up_days ?? '--'}</div></div>
            <div className="rounded bg-accent/10 px-2 py-1.5"><div className="text-[10px] text-muted-foreground flex items-center">5日涨幅<InfoTip k="change_pct_5d" /></div><div className={`font-mono ${moreInfo.change_pct_5d != null && moreInfo.change_pct_5d>0 ? 'text-stock-up' : moreInfo.change_pct_5d!=null&&moreInfo.change_pct_5d<0 ? 'text-stock-down' : ''}`}>{moreInfo.change_pct_5d != null ? `${moreInfo.change_pct_5d>0?'+':''}${moreInfo.change_pct_5d.toFixed(2)}%` : '--'}</div></div>
            <div className="rounded bg-accent/10 px-2 py-1.5"><div className="text-[10px] text-muted-foreground flex items-center">20日涨幅<InfoTip k="change_pct_20d" /></div><div className={`font-mono ${moreInfo.change_pct_20d != null && moreInfo.change_pct_20d>0 ? 'text-stock-up' : moreInfo.change_pct_20d!=null&&moreInfo.change_pct_20d<0 ? 'text-stock-down' : ''}`}>{moreInfo.change_pct_20d != null ? `${moreInfo.change_pct_20d>0?'+':''}${moreInfo.change_pct_20d.toFixed(2)}%` : '--'}</div></div>
            <div className="rounded bg-accent/10 px-2 py-1.5"><div className="text-[10px] text-muted-foreground flex items-center">年初至今<InfoTip k="change_pct_ytd" /></div><div className={`font-mono ${moreInfo.change_pct_ytd != null && moreInfo.change_pct_ytd>0 ? 'text-stock-up' : moreInfo.change_pct_ytd!=null&&moreInfo.change_pct_ytd<0 ? 'text-stock-down' : ''}`}>{moreInfo.change_pct_ytd != null ? `${moreInfo.change_pct_ytd>0?'+':''}${moreInfo.change_pct_ytd.toFixed(2)}%` : '--'}</div></div>
            <div className="rounded bg-accent/10 px-2 py-1.5"><div className="text-[10px] text-muted-foreground flex items-center">动态PE<InfoTip k="pe_dynamic" /></div><div className="font-mono">{moreInfo.pe_dynamic != null ? Number(moreInfo.pe_dynamic).toFixed(2) : '--'}</div></div>
            <div className="rounded bg-accent/10 px-2 py-1.5"><div className="text-[10px] text-muted-foreground flex items-center">市净率<InfoTip k="pb" /></div><div className="font-mono">{moreInfo.pb != null ? Number(moreInfo.pb).toFixed(2) : '--'}</div></div>
            <div className="rounded bg-accent/10 px-2 py-1.5"><div className="text-[10px] text-muted-foreground flex items-center">股息率<InfoTip k="dividend_yield" /></div><div className="font-mono">{moreInfo.dividend_yield != null ? `${Number(moreInfo.dividend_yield).toFixed(2)}%` : '--'}</div></div>
            <div className="rounded bg-accent/10 px-2 py-1.5"><div className="text-[10px] text-muted-foreground flex items-center">Beta<InfoTip k="beta" /></div><div className="font-mono">{moreInfo.beta != null ? Number(moreInfo.beta).toFixed(2) : '--'}</div></div>
            <div className="rounded bg-accent/10 px-2 py-1.5"><div className="text-[10px] text-muted-foreground flex items-center">5日均价<InfoTip k="ma5_price" /></div><div className="font-mono">{moreInfo.ma5_price != null ? formatNumber(moreInfo.ma5_price) : '--'}</div></div>
            <div className="rounded bg-accent/10 px-2 py-1.5"><div className="text-[10px] text-muted-foreground flex items-center">52周高/低<InfoTip k="high_52w_low_52w" /></div><div className="font-mono text-[10px]">{moreInfo.high_52w != null ? Number(moreInfo.high_52w).toFixed(2) : '--'} / {moreInfo.low_52w != null ? Number(moreInfo.low_52w).toFixed(2) : '--'}</div></div>
          </div>
          {/* L2 逐笔（需Level2，开通后实时） */}
          <div className="mt-3 pt-3 border-t border-border/40 grid grid-cols-3 md:grid-cols-6 gap-2 text-[11px]">
            <div className="rounded bg-amber-500/10 px-2 py-1.5"><div className="text-[10px] text-muted-foreground flex items-center">L2逐笔数<InfoTip k="l2_tick_num" /></div><div className="font-mono">{moreInfo.l2_tick_num != null ? Number(moreInfo.l2_tick_num).toLocaleString() : '--'}</div></div>
            <div className="rounded bg-amber-500/10 px-2 py-1.5"><div className="text-[10px] text-muted-foreground flex items-center">L2委托数<InfoTip k="l2_order_num" /></div><div className="font-mono">{moreInfo.l2_order_num != null ? Number(moreInfo.l2_order_num).toLocaleString() : '--'}</div></div>
            <div className="rounded bg-accent/10 px-2 py-1.5"><div className="text-[10px] text-muted-foreground flex items-center">总买量<InfoTip k="total_buy_vol" /></div><div className="font-mono">{moreInfo.total_buy_vol != null ? `${(moreInfo.total_buy_vol/100).toFixed(0)}手` : '--'}</div></div>
            <div className="rounded bg-accent/10 px-2 py-1.5"><div className="text-[10px] text-muted-foreground flex items-center">总卖量<InfoTip k="total_sell_vol" /></div><div className="font-mono">{moreInfo.total_sell_vol != null ? `${(moreInfo.total_sell_vol/100).toFixed(0)}手` : '--'}</div></div>
            <div className="rounded bg-rose-500/10 px-2 py-1.5"><div className="text-[10px] text-muted-foreground flex items-center">撤买<InfoTip k="cancel_buy" /></div><div className="font-mono">{moreInfo.cancel_buy != null ? `${(moreInfo.cancel_buy).toFixed(0)}` : '--'}</div></div>
            <div className="rounded bg-emerald-500/10 px-2 py-1.5"><div className="text-[10px] text-muted-foreground flex items-center">撤卖<InfoTip k="cancel_sell" /></div><div className="font-mono">{moreInfo.cancel_sell != null ? `${(moreInfo.cancel_sell).toFixed(0)}` : '--'}</div></div>
            <div className="rounded bg-accent/10 px-2 py-1.5"><div className="text-[10px] text-muted-foreground flex items-center">主力净流入<InfoTip k="zjl_hb" /></div><div className={`font-mono ${moreInfo.zjl_hb != null ? (moreInfo.zjl_hb >= 0 ? 'text-stock-up' : 'text-stock-down') : ''}`}>{moreInfo.zjl_hb != null ? `${(moreInfo.zjl_hb).toFixed(0)}万` : '--'}</div></div>
            <div className="rounded bg-accent/10 px-2 py-1.5"><div className="text-[10px] text-muted-foreground flex items-center">主买净额<InfoTip k="zjl" /></div><div className={`font-mono ${moreInfo.zjl != null ? (moreInfo.zjl >= 0 ? 'text-stock-up' : 'text-stock-down') : ''}`}>{moreInfo.zjl != null ? `${(moreInfo.zjl).toFixed(0)}万` : '--'}</div></div>
          </div>
          {moreInfo.total_buy_vol != null && moreInfo.total_sell_vol != null && (moreInfo.total_buy_vol + moreInfo.total_sell_vol) > 0 && (
            <div className="mt-2 h-1.5 w-full flex rounded overflow-hidden bg-muted">
              <div className="bg-rose-500" style={{width: `${(moreInfo.total_buy_vol / (moreInfo.total_buy_vol + moreInfo.total_sell_vol) * 100).toFixed(1)}%`}} />
              <div className="bg-emerald-500 flex-1" />
            </div>
          )}
        </div>
      )}

      {/* 暗盘资金 · TQ逐笔还原(盘后, ZCode TQ4 采集) */}
      {darkFlowTq && darkFlowTq.data_status === 'complete' && (
        <div className="card p-4">
          <div className="flex items-center justify-between mb-2">
            <div className="text-[11px] text-muted-foreground">暗盘资金 · TQ逐笔还原</div>
            <div className="text-[10px] text-muted-foreground">{darkFlowTq.date ? `盘后 ${darkFlowTq.date}` : '盘后'}</div>
          </div>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-2 text-[11px]">
            <div className="rounded bg-violet-500/10 px-2 py-1.5"><div className="text-[10px] text-muted-foreground">超大单净额</div><div className={`font-mono ${darkFlowTq.xl_net != null ? (darkFlowTq.xl_net >= 0 ? 'text-stock-up' : 'text-stock-down') : ''}`}>{darkFlowTq.xl_net != null ? `${darkFlowTq.xl_net.toFixed(0)}万` : '--'}</div></div>
            <div className="rounded bg-accent/10 px-2 py-1.5"><div className="text-[10px] text-muted-foreground">大单净额</div><div className={`font-mono ${darkFlowTq.large_net != null ? (darkFlowTq.large_net >= 0 ? 'text-stock-up' : 'text-stock-down') : ''}`}>{darkFlowTq.large_net != null ? `${darkFlowTq.large_net.toFixed(0)}万` : '--'}</div></div>
            <div className="rounded bg-accent/10 px-2 py-1.5"><div className="text-[10px] text-muted-foreground">中单净额</div><div className={`font-mono ${darkFlowTq.mid_net != null ? (darkFlowTq.mid_net >= 0 ? 'text-stock-up' : 'text-stock-down') : ''}`}>{darkFlowTq.mid_net != null ? `${darkFlowTq.mid_net.toFixed(0)}万` : '--'}</div></div>
            <div className="rounded bg-accent/10 px-2 py-1.5"><div className="text-[10px] text-muted-foreground">小单净额</div><div className={`font-mono ${darkFlowTq.small_net != null ? (darkFlowTq.small_net >= 0 ? 'text-stock-up' : 'text-stock-down') : ''}`}>{darkFlowTq.small_net != null ? `${darkFlowTq.small_net.toFixed(0)}万` : '--'}</div></div>
          </div>
          <div className="mt-3 pt-3 border-t border-border/40 grid grid-cols-3 md:grid-cols-5 gap-2 text-[11px]">
            <div className="rounded bg-accent/10 px-2 py-1.5"><div className="text-[10px] text-muted-foreground">拆单委托</div><div className="font-mono">{darkFlowTq.split_order_count != null ? darkFlowTq.split_order_count.toLocaleString() : '--'}</div></div>
            <div className="rounded bg-accent/10 px-2 py-1.5"><div className="text-[10px] text-muted-foreground">平均拆单份数</div><div className="font-mono">{darkFlowTq.avg_split_parts != null ? darkFlowTq.avg_split_parts.toFixed(1) : '--'}</div></div>
            <div className="rounded bg-accent/10 px-2 py-1.5"><div className="text-[10px] text-muted-foreground">撤单比</div><div className="font-mono">{darkFlowTq.cancel_ratio != null ? `${(darkFlowTq.cancel_ratio * 100).toFixed(1)}%` : '--'}</div></div>
            <div className="rounded bg-rose-500/10 px-2 py-1.5"><div className="text-[10px] text-muted-foreground">撤买量</div><div className="font-mono">{darkFlowTq.cancel_buy_vol != null ? `${darkFlowTq.cancel_buy_vol.toFixed(0)}` : '--'}</div></div>
            <div className="rounded bg-emerald-500/10 px-2 py-1.5"><div className="text-[10px] text-muted-foreground">撤卖量</div><div className="font-mono">{darkFlowTq.cancel_sell_vol != null ? `${darkFlowTq.cancel_sell_vol.toFixed(0)}` : '--'}</div></div>
          </div>
          {(darkFlowTq.tuopan || darkFlowTq.yapan || darkFlowTq.suopan) && (
            <div className="mt-2 flex flex-wrap gap-1.5">
              {darkFlowTq.tuopan && <span className="rounded bg-amber-500/15 px-2 py-0.5 text-[10px] text-amber-600">⚠️ 托盘</span>}
              {darkFlowTq.yapan && <span className="rounded bg-sky-500/15 px-2 py-0.5 text-[10px] text-sky-600">压盘</span>}
              {darkFlowTq.suopan && <span className="rounded bg-violet-500/15 px-2 py-0.5 text-[10px] text-violet-600">锁盘</span>}
            </div>
          )}
        </div>
      )}

      <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3 items-stretch">
        <div className="card p-4 h-full flex flex-col">
          <div className="flex items-center justify-between mb-2">
            <div className="text-[12px] text-muted-foreground">AI建议</div>
            <Button variant="ghost" size="sm" className="h-7 px-2 text-[11px] text-muted-foreground" onClick={() => setTab('suggestions')}>
              更多
            </Button>
            {autoSuggesting && suggestions.length > 0 && (
              <div className="text-[10px] text-primary">更新中...</div>
            )}
          </div>
          {suggestions.length > 0 ? (
            <div className="space-y-2">
              <SuggestionBadge
                suggestion={suggestions[0]}
                stockName={resolvedName}
                stockSymbol={symbol}
                market={market}
                hasPosition={!!props.hasPosition}
                showTechnicalCompanion={false}
              />
              <div className="rounded bg-accent/10 p-2 text-[11px]">
                <div className="text-muted-foreground">核心判断</div>
                <div className="mt-1 text-foreground line-clamp-2">{suggestions[0].signal || suggestions[0].reason || '暂无说明'}</div>
                <div className="mt-1 text-muted-foreground">动作: {suggestions[0].action_label || suggestions[0].action || '--'}</div>
                <div className="mt-1 text-foreground line-clamp-2">依据: {suggestions[0].reason || '暂无补充依据'}</div>
                <div className="mt-1 text-muted-foreground">
                  来源: {suggestions[0].agent_label || suggestions[0].agent_name || 'AI'}{suggestions[0].created_at ? ` · ${formatTime(suggestions[0].created_at)}` : ''}
                </div>
              </div>
              {suggestions.length > 1 && (
                <div className="rounded bg-accent/10 p-2 text-[11px]">
                  <div className="text-muted-foreground mb-1">近期补充建议</div>
                  {suggestions.slice(1, 3).map((item, idx) => (
                    <div key={`${item.created_at || 'extra'}-${idx}`} className="line-clamp-1 text-foreground">
                      {item.action_label || item.action} · {item.signal || item.reason || '--'}
                    </div>
                  ))}
                </div>
              )}
              <div className="text-[10px] text-primary min-h-[14px]">{autoSuggesting && suggestions.length === 0 ? '正在自动生成 AI 建议...' : ''}</div>
            </div>
          ) : (
            <div className="text-[12px] text-muted-foreground py-6">
              {autoSuggesting ? '正在自动生成 AI 建议（通常 5-15 秒）...' : '暂无 AI 建议'}
            </div>
          )}
        </div>

        <div className="card p-4 h-full flex flex-col">
          <div className="flex items-center justify-between mb-2">
            <div className="text-[12px] text-muted-foreground">新闻</div>
            <Button variant="ghost" size="sm" className="h-7 px-2 text-[11px] text-muted-foreground" onClick={() => setTab('news')}>
              更多
            </Button>
          </div>
          <div className="flex-1 space-y-2">
            {news.length === 0 ? (
              <div className="text-[12px] text-muted-foreground py-6">暂无相关新闻</div>
            ) : (
              news.slice(0, 3).map((item, idx) => (
                <a
                  key={`${item.publish_time || 'n'}-${idx}`}
                  href={item.url}
                  target="_blank"
                  rel="noreferrer"
                  className="block rounded-lg border border-border/30 bg-accent/10 p-2.5 hover:bg-accent/20 transition-colors"
                >
                  <div className="text-[12px] text-foreground line-clamp-2">{item.title}</div>
                  <div className="mt-1 text-[10px] text-muted-foreground">{item.source_label || item.source} · {formatTime(item.publish_time)}</div>
                </a>
              ))
            )}
          </div>
        </div>
        <div className="card p-4 h-full flex flex-col">
          <div className="flex items-center justify-between gap-2 mb-2">
            <div className="text-[12px] text-muted-foreground">AI报告</div>
            <Button variant="ghost" size="sm" className="h-7 px-2 text-[11px] text-muted-foreground" onClick={() => setTab('reports')}>
              更多
            </Button>
          </div>
          {!latestReport ? (
            <div className="text-[12px] text-muted-foreground py-3">暂无报告</div>
          ) : (
            <div className="rounded-lg border border-border/30 bg-accent/10 p-2.5">
              <div className="text-[11px] text-muted-foreground">
                {AGENT_LABELS[latestReport.agent_name] || latestReport.agent_name} · {latestReport.analysis_date}
              </div>
              <div className="mt-1 text-[13px] font-medium line-clamp-1">{latestReport.title || '报告摘要'}</div>
              <div className="mt-1 text-[12px] text-foreground/90 line-clamp-3">
                {markdownToPlainText(latestReport.content) || '暂无报告内容'}
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
