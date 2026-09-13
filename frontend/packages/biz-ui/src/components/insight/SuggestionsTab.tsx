import { useInsight } from './context'
import { SuggestionBadge } from '@panwatch/biz-ui/components/suggestion-badge'
import { Switch } from '@panwatch/base-ui/components/ui/switch'

export function SuggestionsTab() {
  const {
    symbol,
    includeExpiredSuggestions,
    setIncludeExpiredSuggestions,
    klineSummary,
    suggestions,
    autoSuggesting,
    resolvedName,
    technicalFallbackSuggestion,
    props,
  } = useInsight()
  return (
    <div className="space-y-3">
      <div className="card p-3 flex items-center justify-between gap-3">
        <div className="text-[12px] text-muted-foreground">显示过期建议</div>
        <div className="flex items-center gap-2">
          <span className="text-[11px] text-muted-foreground">{includeExpiredSuggestions ? '包含过期' : '仅有效'}</span>
          <Switch
            checked={includeExpiredSuggestions}
            onCheckedChange={setIncludeExpiredSuggestions}
            aria-label="显示过期建议"
          />
        </div>
      </div>
      {suggestions.length === 0 ? (
        technicalFallbackSuggestion ? (
          <div className="card p-4">
            <SuggestionBadge suggestion={technicalFallbackSuggestion} stockName={resolvedName} stockSymbol={symbol} kline={klineSummary} hasPosition={!!props.hasPosition} />
            <div className="mt-2 text-[10px] text-muted-foreground">
              {autoSuggesting ? '正在自动生成 AI 建议（通常 5-15 秒）...' : '当前显示技术指标基础建议'}
            </div>
          </div>
        ) : (
          <div className="card p-6 text-[12px] text-muted-foreground text-center">
            {autoSuggesting ? '正在自动生成 AI 建议（通常 5-15 秒）...' : '暂无建议'}
          </div>
        )
      ) : (
        <div className="max-h-[56vh] overflow-y-auto pr-1 scrollbar space-y-3">
          {suggestions.map((item, idx) => (
            <div key={`${item.created_at || 's'}-${idx}`} className="card p-4">
              <SuggestionBadge suggestion={item} stockName={resolvedName} stockSymbol={symbol} kline={klineSummary} hasPosition={!!props.hasPosition} />
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
