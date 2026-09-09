import { useInsight } from './context'
import InteractiveKline from '@panwatch/biz-ui/components/InteractiveKline'

export function KlineTab() {
  const {
    symbol,
    market,
    mainIntent,
    gsSignals,
    fundFlow,
    klineEvents,
    klineInterval,
  } = useInsight()
  return (
    <div className="card p-4">
      <InteractiveKline
        symbol={symbol}
        market={market}
        initialInterval={klineInterval}
        mainIntent={mainIntent}
        // ============== SIDA Pro: K线图层标注 (P1+ P2) (2026-09-01) ==============
        gsSignals={gsSignals as any}
        fundFlow={fundFlow as any}
        events={klineEvents as any}
      />
    </div>
  )
}
