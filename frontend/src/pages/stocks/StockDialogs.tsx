import StockInsightModal from '@panwatch/biz-ui/components/stock-insight-modal'
import { DeepAnalysisModal } from '@panwatch/biz-ui/components/deep-analysis-modal'
import { KlineSummaryDialog } from '@panwatch/biz-ui/components/kline-summary-dialog'
import { MinuteDialog } from '@panwatch/biz-ui/components/minute-dialog'
import { useStocks } from './context'

export function StockDialogs() {
  const {
    klineDialogOpen,
    setKlineDialogOpen,
    klineDialogSymbol,
    klineDialogMarket,
    klineDialogName,
    klineDialogHasPosition,
    klineDialogInitialSummary,
    minuteDialogOpen,
    setMinuteDialogOpen,
    minuteDialogSymbol,
    minuteDialogMarket,
    minuteDialogName,
    insightOpen,
    setInsightOpen,
    insightSymbol,
    insightMarket,
    insightName,
    insightHasPosition,
    deepAnalysisTarget,
    setDeepAnalysisTarget,
  } = useStocks()
  return (
    <>
{/* Kline Dialog */}
<KlineSummaryDialog
  open={klineDialogOpen}
  onOpenChange={setKlineDialogOpen}
  symbol={klineDialogSymbol}
  market={klineDialogMarket}
  stockName={klineDialogName}
  hasPosition={klineDialogHasPosition}
  initialSummary={klineDialogInitialSummary as any}
/>

<MinuteDialog
  open={minuteDialogOpen}
  onOpenChange={setMinuteDialogOpen}
  symbol={minuteDialogSymbol}
  market={minuteDialogMarket}
  stockName={minuteDialogName}
/>

<StockInsightModal
  open={insightOpen}
  onOpenChange={setInsightOpen}
  symbol={insightSymbol}
  market={insightMarket}
  stockName={insightName}
  hasPosition={insightHasPosition}
/>

{/* TradingAgents 深度分析弹窗 */}
{deepAnalysisTarget && (
  <DeepAnalysisModal
    open={!!deepAnalysisTarget}
    onOpenChange={(open) => { if (!open) setDeepAnalysisTarget(null) }}
    stockId={deepAnalysisTarget.stockId}
    stockSymbol={deepAnalysisTarget.symbol}
    stockName={deepAnalysisTarget.name}
  />
)}
    </>
  )
}
