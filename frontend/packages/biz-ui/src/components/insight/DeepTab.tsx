import { useInsight } from './context'
import { Button } from '@panwatch/base-ui/components/ui/button'
import { DeepAnalysisSection } from './deep-analysis'

export function DeepTab() {
  const {
    symbol,
    deepResult,
    deepLoading,
    deepLoaded,
    deepShowAnalyst,
    setDeepShowAnalyst,
    deepShowDebate,
    setDeepShowDebate,
    deepHistory,
    deepHistoryLoading,
    loadDeepResult,
  } = useInsight()
  return (
    <div className="space-y-3">
      {deepResult && (
        <div className="flex justify-end">
          <Button
            variant="outline"
            size="sm"
            className="h-8 px-2.5"
            onClick={() =>
              window.open(
                `/analysis/${symbol}/${deepResult.timestamp ? String(deepResult.timestamp).slice(0, 10) : new Date().toISOString().slice(0, 10)}`,
                '_blank',
              )
            }
          >
            打开详情页 ↗
          </Button>
        </div>
      )}
      <DeepAnalysisSection
        loading={deepLoading}
        loaded={deepLoaded}
        result={deepResult}
        history={deepHistory}
        historyLoading={deepHistoryLoading}
        showAnalyst={deepShowAnalyst}
        setShowAnalyst={setDeepShowAnalyst}
        showDebate={deepShowDebate}
        setShowDebate={setDeepShowDebate}
        onRefresh={loadDeepResult}
      />
    </div>
  )
}
