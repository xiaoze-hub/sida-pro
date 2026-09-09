import { useInsight } from './context'
import { AGENT_LABELS } from './types'
import { StockReportMarkdown } from './helpers'

export function ReportsTab() {
  const {
    symbol,
    reportTab,
    setReportTab,
    activeReport,
  } = useInsight()
  return (
    <div className="space-y-3">
      <div className="card p-3">
        <div className="flex items-center gap-1">
          {([
            { key: 'premarket_outlook', label: '盘前' },
            { key: 'daily_report', label: '盘后' },
            { key: 'news_digest', label: '新闻' },
          ] as const).map(item => (
            <button
              key={item.key}
              onClick={() => setReportTab(item.key)}
              className={`text-[11px] px-2.5 py-1 rounded ${
                reportTab === item.key ? 'bg-primary text-primary-foreground' : 'bg-accent/60 text-muted-foreground hover:bg-accent'
              }`}
            >
              {item.label}
            </button>
          ))}
        </div>
      </div>
      {!activeReport ? (
        <div className="card p-6 text-[12px] text-muted-foreground text-center">暂无报告</div>
      ) : (
        <div className="card p-4 space-y-3">
          <div className="text-[11px] text-muted-foreground">
            {AGENT_LABELS[activeReport.agent_name] || activeReport.agent_name} · {activeReport.analysis_date}
          </div>
          <div className="text-[15px] font-medium">{activeReport.title || '报告摘要'}</div>
          {activeReport.suggestions && (activeReport.suggestions as any)?.[symbol]?.action_label && (
            <div className="text-[11px] inline-flex px-2 py-0.5 rounded bg-primary/10 text-primary">
              {(activeReport.suggestions as any)[symbol].action_label}
            </div>
          )}
          <div className="rounded-lg bg-accent/10 p-3">
            <div className="prose prose-sm dark:prose-invert max-w-none text-foreground/90 break-words">
              <StockReportMarkdown content={activeReport.content || '暂无报告内容'} />
            </div>
          </div>
          {(activeReport.prompt_context || activeReport.context_payload || activeReport.news_debug) && (
            <details className="rounded-lg border border-border/40 bg-accent/10 p-3">
              <summary className="cursor-pointer text-[12px] text-muted-foreground select-none">查看分析上下文</summary>
              {activeReport.prompt_stats ? (
                <div className="mt-2">
                  <div className="text-[11px] text-muted-foreground mb-1">Prompt统计</div>
                  <pre className="text-[11px] text-muted-foreground whitespace-pre-wrap break-words overflow-x-auto">{JSON.stringify(activeReport.prompt_stats, null, 2)}</pre>
                </div>
              ) : null}
              {activeReport.news_debug ? (
                <div className="mt-2">
                  <div className="text-[11px] text-muted-foreground mb-1">新闻注入明细</div>
                  <pre className="text-[11px] text-muted-foreground whitespace-pre-wrap break-words overflow-x-auto">{JSON.stringify(activeReport.news_debug, null, 2)}</pre>
                </div>
              ) : null}
              {activeReport.context_payload ? (
                <div className="mt-2">
                  <div className="text-[11px] text-muted-foreground mb-1">上下文快照</div>
                  <pre className="text-[11px] text-muted-foreground whitespace-pre-wrap break-words overflow-x-auto max-h-[220px] overflow-y-auto">{JSON.stringify(activeReport.context_payload, null, 2)}</pre>
                </div>
              ) : null}
              {activeReport.prompt_context ? (
                <div className="mt-2">
                  <div className="text-[11px] text-muted-foreground mb-1">Prompt原文</div>
                  <pre className="text-[11px] text-muted-foreground whitespace-pre-wrap break-words overflow-x-auto max-h-[220px] overflow-y-auto">{activeReport.prompt_context}</pre>
                </div>
              ) : null}
            </details>
          )}
        </div>
      )}
    </div>
  )
}
