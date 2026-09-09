import { useCallback, useEffect, useState } from 'react'
import { ChevronDown, ChevronRight, RefreshCw, ShieldAlert } from 'lucide-react'

import { logsApi, type ErrorEvent } from '@panwatch/api'
import { Button } from '@panwatch/base-ui/components/ui/button'

/**
 * 错误追踪面板(KI-018, 2026-09-09)。
 *
 * 消费 `GET /api/logs/errors`(owner): 后端未处理异常 + scheduler 漏跑 + 前端上报,
 * 统一来自 `src/core/error_tracker.recent_errors`(去重+聚合)。
 * 诚实口径: 拉取失败显式报错, 不把"取不到"当成"没有错误"。
 */

function fmtTs(ts?: string): string {
  if (!ts) return '--'
  const d = new Date(ts)
  if (Number.isNaN(d.getTime())) return ts
  return d.toLocaleString('zh-CN', { hour12: false })
}

function ErrorRow({ ev }: { ev: ErrorEvent }) {
  const [open, setOpen] = useState(false)
  const hasDetail = !!(ev.traceback || ev.context)
  return (
    <div className="border-b border-border/40 py-2">
      <button
        type="button"
        onClick={() => hasDetail && setOpen((v) => !v)}
        className="flex w-full items-start gap-2 text-left"
        aria-expanded={open}
      >
        {hasDetail ? (
          open ? <ChevronDown className="mt-0.5 h-3.5 w-3.5 shrink-0 text-muted-foreground" />
               : <ChevronRight className="mt-0.5 h-3.5 w-3.5 shrink-0 text-muted-foreground" />
        ) : (
          <span className="mt-0.5 h-3.5 w-3.5 shrink-0" />
        )}
        <span className="w-[150px] shrink-0 font-mono text-[11px] text-muted-foreground">{fmtTs(ev.ts)}</span>
        <span className="w-[180px] shrink-0 truncate font-mono text-[11px] text-rose-600 dark:text-rose-400">
          {ev.type || 'Error'}
        </span>
        <span className="min-w-0 flex-1 break-words text-[12px] text-foreground">{ev.message || '(empty)'}</span>
      </button>
      {open && hasDetail && (
        <div className="mt-2 space-y-2 pl-6">
          {ev.traceback && (
            <pre className="max-h-[240px] overflow-auto rounded bg-accent/20 p-2 text-[11px] text-muted-foreground whitespace-pre-wrap break-words">
              {ev.traceback}
            </pre>
          )}
          {ev.context && Object.keys(ev.context).length > 0 && (
            <pre className="max-h-[200px] overflow-auto rounded bg-accent/20 p-2 text-[11px] text-muted-foreground whitespace-pre-wrap break-words">
              {JSON.stringify(ev.context, null, 2)}
            </pre>
          )}
        </div>
      )}
    </div>
  )
}

export default function ErrorLogPage() {
  const [items, setItems] = useState<ErrorEvent[] | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const d = await logsApi.errors(100)
      setItems(d?.items ?? [])
    } catch (e) {
      setItems(null)
      setError(e instanceof Error ? e.message : '加载失败')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { void load() }, [load])

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2 text-[12px] text-muted-foreground">
          <ShieldAlert className="h-3.5 w-3.5" />
          后端未处理异常 / 调度漏跑 / 前端上报(去重聚合, 仅 owner 可见)
        </div>
        <Button variant="secondary" size="sm" className="h-7 px-2.5 text-[11px]" onClick={() => void load()} disabled={loading}>
          <RefreshCw className={`mr-1 h-3 w-3 ${loading ? 'animate-spin' : ''}`} /> 刷新
        </Button>
      </div>

      {error ? (
        <div className="card p-6 text-center text-[12px] text-rose-600 dark:text-rose-400">
          加载失败: {error}
        </div>
      ) : items === null ? (
        <div className="card p-6 text-center text-[12px] text-muted-foreground">加载中…</div>
      ) : items.length === 0 ? (
        <div className="card p-6 text-center text-[12px] text-muted-foreground">最近没有错误事件 🎉</div>
      ) : (
        <div className="card p-4">
          <div className="mb-1 flex items-center gap-2 border-b border-border/60 pb-1 text-[11px] text-muted-foreground">
            <span className="w-[150px] shrink-0">时间</span>
            <span className="w-[180px] shrink-0">类型</span>
            <span className="flex-1">消息(点击展开堆栈/上下文)</span>
          </div>
          {items.map((ev, i) => (
            <ErrorRow key={`${ev.ts || 't'}-${i}`} ev={ev} />
          ))}
        </div>
      )}
    </div>
  )
}
