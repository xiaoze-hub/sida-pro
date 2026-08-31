import { useState, useEffect, useMemo } from 'react'
import { RefreshCw, Search, FileText, Calendar, Hash, Loader2, ExternalLink } from 'lucide-react'
import { reportsApi, type ReportItem } from '@panwatch/api'
import { Button } from '@panwatch/base-ui/components/ui/button'
import { Input } from '@panwatch/base-ui/components/ui/input'
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@panwatch/base-ui/components/ui/dialog'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import ErrorBanner from '@/components/ErrorBanner'

// 修复(S-5, 2026-08-23): PG DECIMAL → 字符串后 .toFixed 抛 TypeError. 改 safe 包装.
function formatBytes(n: unknown): string {
  if (typeof n !== 'number' || !Number.isFinite(n)) return '--'
  if (n < 1024) return `${n}B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)}KB`
  return `${(n / 1024 / 1024).toFixed(1)}MB`
}

function formatDate(iso: string): string {
  return iso.replace('T', ' ').slice(0, 16)
}

export default function ReportsPage() {
  const [items, setItems] = useState<ReportItem[]>([])
  const [jobs, setJobs] = useState<{ job_id: string; job_name: string }[]>([])
  const [loading, setLoading] = useState(false)
  // 初始加载失败提示(失败≠空态:不把"加载失败"误读为"暂无报告")
  const [loadError, setLoadError] = useState<string | null>(null)
  const [search, setSearch] = useState('')
  const [jobFilter, setJobFilter] = useState<string>('') // 空 = 全部
  const [selected, setSelected] = useState<{ item: ReportItem; content: string } | null>(null)
  const [loadingContent, setLoadingContent] = useState(false)

  const load = async () => {
    setLoading(true)
    setLoadError(null)
    try {
      const res = await reportsApi.list({ limit: 500 })
      setItems(res.items)
      setJobs(res.jobs)
    } catch (e) {
      console.error(e)
      setLoadError(e instanceof Error ? e.message : '加载失败')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { load() }, [])

  const filtered = useMemo(() => {
    let r = items
    if (jobFilter) r = r.filter(it => it.job_id === jobFilter)
    if (search) {
      const q = search.toLowerCase()
      r = r.filter(it =>
        it.job_name.toLowerCase().includes(q) ||
        it.file.toLowerCase().includes(q) ||
        it.title_preview.toLowerCase().includes(q)
      )
    }
    return r
  }, [items, search, jobFilter])

  // 按任务分组
  const grouped = useMemo(() => {
    const m = new Map<string, ReportItem[]>()
    for (const it of filtered) {
      if (!m.has(it.job_id)) m.set(it.job_id, [])
      m.get(it.job_id)!.push(it)
    }
    return m
  }, [filtered])

  const openItem = async (it: ReportItem) => {
    setLoadingContent(true)
    setSelected({ item: it, content: '' })
    try {
      const res = await reportsApi.content(it.job_id, it.file)
      setSelected({ item: it, content: res.content })
    } catch (e: any) {
      setSelected({ item: it, content: `加载失败: ${e?.message || e}` })
    } finally {
      setLoadingContent(false)
    }
  }

  return (
    <div className="space-y-5">
      {/* Header */}
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div>
          <h1 className="text-xl font-semibold flex items-center gap-2">
            <FileText className="w-5 h-5 text-primary" />
            报告中心
          </h1>
          <p className="text-sm text-muted-foreground mt-0.5">
            Hermes cron 历史报告存档 — 来自 <code className="text-xs">~/.hermes/cron/output/&lt;job&gt;/</code>
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Button variant="ghost" size="sm" onClick={load} disabled={loading}>
            <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
          </Button>
        </div>
      </div>

      {/* 筛选条 */}
      <div className="flex items-center gap-2 flex-wrap">
        <div className="relative flex-1 min-w-[180px]">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground" />
          <Input
            placeholder="搜索任务名 / 文件名 / 标题"
            value={search}
            onChange={e => setSearch(e.target.value)}
            className="pl-9"
          />
        </div>
        <select
          value={jobFilter}
          onChange={e => setJobFilter(e.target.value)}
          className="h-9 rounded-md border border-input bg-background px-3 text-sm"
        >
          <option value="">全部任务 ({jobs.length})</option>
          {jobs.map(j => (
            <option key={j.job_id} value={j.job_id}>{j.job_name.slice(0, 30)}</option>
          ))}
        </select>
        <div className="text-xs text-muted-foreground">
          {filtered.length} / {items.length} 条
        </div>
      </div>

      {/* 报告列表(按任务分组) */}
      {loading ? (
        <div className="flex items-center justify-center py-12 text-muted-foreground">
          <Loader2 className="w-5 h-5 animate-spin mr-2" /> 加载中...
        </div>
      ) : loadError ? (
        <ErrorBanner
          errors={loadError ? [{ source: '报告列表', message: loadError, retry: () => void load() }] : []}
          onDismiss={() => setLoadError(null)}
        />
      ) : grouped.size === 0 ? (
        <div className="card-subtle p-8 text-center text-sm text-muted-foreground">
          暂无报告
          {search || jobFilter ? ' (匹配为空)' : ''}
        </div>
      ) : (
        <div className="space-y-4">
          {Array.from(grouped.entries()).map(([jobId, files]) => {
            const jobName = files[0]?.job_name || jobId
            const latest = files[0] // 已 mtime 倒序
            return (
              <div key={jobId} className="card-subtle p-4">
                <div className="flex items-center justify-between gap-3 mb-3">
                  <div className="min-w-0">
                    <h3 className="font-medium text-sm truncate">{jobName}</h3>
                    <div className="text-xs text-muted-foreground mt-0.5 flex items-center gap-3">
                      <span className="flex items-center gap-1"><Hash className="w-3 h-3" />{files.length} 份</span>
                      <span className="flex items-center gap-1"><Calendar className="w-3 h-3" />
                        最新 {latest && formatDate(latest.mtime_iso)}
                      </span>
                    </div>
                  </div>
                </div>
                <div className="space-y-1.5">
                  {files.slice(0, 20).map(it => (
                    <button
                      key={it.file}
                      onClick={() => openItem(it)}
                      className="w-full text-left p-2 rounded hover:bg-accent/40 transition-colors flex items-center gap-3"
                    >
                      <FileText className="w-3.5 h-3.5 text-muted-foreground shrink-0" />
                      <div className="flex-1 min-w-0">
                        <div className="text-sm truncate">
                          {it.title_preview || it.file}
                        </div>
                        <div className="text-xs text-muted-foreground flex items-center gap-2 mt-0.5 min-w-0">
                          <span className="shrink-0">{formatDate(it.mtime_iso)}</span>
                          <span className="shrink-0">·</span>
                          <span className="shrink-0">{formatBytes(it.size)}</span>
                          <span className="text-muted-foreground/60 truncate min-w-0">{it.file}</span>
                        </div>
                      </div>
                      <ExternalLink className="w-3.5 h-3.5 text-muted-foreground/50 shrink-0" />
                    </button>
                  ))}
                  {files.length > 20 && (
                    <div className="text-xs text-muted-foreground text-center py-1">
                      还有 {files.length - 20} 份未显示...
                    </div>
                  )}
                </div>
              </div>
            )
          })}
        </div>
      )}

      {/* 报告详情 Dialog */}
      <Dialog open={!!selected} onOpenChange={open => !open && setSelected(null)}>
        <DialogContent className="max-w-4xl max-h-[85vh] overflow-y-auto">
          <DialogHeader>
            <DialogTitle className="text-base">
              {selected?.item.title_preview || selected?.item.file}
            </DialogTitle>
            <div className="text-xs text-muted-foreground flex items-center gap-3 mt-1">
              <span>{selected?.item.job_name}</span>
              <span>·</span>
              <span>{selected && formatDate(selected.item.mtime_iso)}</span>
              <span>·</span>
              <span>{selected && formatBytes(selected.item.size)}</span>
            </div>
          </DialogHeader>
          <div className="mt-3">
            {loadingContent ? (
              <div className="flex items-center justify-center py-12 text-muted-foreground">
                <Loader2 className="w-5 h-5 animate-spin mr-2" /> 加载中...
              </div>
            ) : (
              <div className="report-content overflow-x-auto prose prose-sm dark:prose-invert max-w-none prose-headings:font-semibold prose-h2:text-base prose-h3:text-sm prose-h3:mt-4 prose-h3:mb-2 prose-table:text-xs prose-th:bg-accent/30 prose-th:p-1.5 prose-td:p-1.5 prose-td:border-border prose-th:border-border prose-code:bg-accent/30 prose-code:px-1 prose-code:rounded prose-code:before:content-none prose-code:after:content-none">
                <ReactMarkdown remarkPlugins={[remarkGfm]}>{selected?.content || ''}</ReactMarkdown>
              </div>
            )}
          </div>
        </DialogContent>
      </Dialog>
    </div>
  )
}