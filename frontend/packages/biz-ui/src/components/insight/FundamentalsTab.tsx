import { useInsight } from './context'
import { FundamentalsPanel } from './FundamentalsPanel'

export function FundamentalsTab() {
  const {
    fundamentals,
    fundamentalsLoading,
    fundamentalsLoaded,
  } = useInsight()
  return (
    <FundamentalsPanel data={fundamentals} loading={fundamentalsLoading} loaded={fundamentalsLoaded} />
  )
}
