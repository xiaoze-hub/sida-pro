/**
 * Agent 目录（只读，2026-10-25 P2-2.1）。
 *
 * 回答一个问题：「平台到底有几个智能体、各自能干什么、挂哪些工具、现在是第几版？」
 * 版本是 YAML 内容的指纹（改一个字就变），不是手写版本号 —— 手写的必然忘记改。
 *
 * 口径铁律：缺值 `--`，不补 0；历史为空就是空，不编"上一版"。
 */
import { useCallback, useEffect, useState } from 'react'
import { RefreshCw } from 'lucide-react'
import { fetchAPI } from '@panwatch/api'
import { Skeleton } from '@/components/Skeleton'

type AgentRow = {
  id: string
  label: string
  icon?: string
  desc: string
  when: string[]
  tools: string[]
  output_sections: string[]
  version: string | null
  updated_at: string | null
}

type AgentCatalog = { available: boolean; reason?: string; items: AgentRow[]; count: number }
type AgentDetail = {
  available: boolean
  reason?: string
  item: AgentRow | null
  history: { fingerprint: string; created_at: string }[]
}

export default function AgentCatalogPage() {
  const [state, setState] = useState<'loading' | 'ok' | 'empty' | 'error'>('loading')
  const [items, setItems] = useState<AgentRow[]>([])
  const [error, setError] = useState('')
  const [openId, setOpenId] = useState<string | null>(null)
  const [detail, setDetail] = useState<AgentDetail | null>(null)
  const [detailError, setDetailError] = useState('')

  const load = useCallback(async () => {
    setState('loading')
    setError('')
    try {
      const res = await fetchAPI<AgentCatalog>('/copilot/agents')
      if (!res.available) {
        setState('error')
        setError(res.reason || '服务端未给出原因')
        return
      }
      setItems(res.items)
      setState(res.items.length ? 'ok' : 'empty')
    } catch (e) {
      setState('error')
      setError(e instanceof Error ? e.message : String(e))
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const toggle = useCallback(async (id: string) => {
    if (openId === id) {
      setOpenId(null)
      setDetail(null)
      return
    }
    setOpenId(id)
    setDetail(null)
    setDetailError('')
    try {
      const res = await fetchAPI<AgentDetail>(`/copilot/agents/${encodeURIComponent(id)}`)
      if (!res.available) {
        setDetailError(res.reason || '服务端未给出原因')
        return
      }
      setDetail(res)
    } catch (e) {
      setDetailError(e instanceof Error ? e.message : String(e))
    }
  }, [openId])

  return (
    <div className="space-y-3" data-testid="agent-catalog-page">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-[16px] font-semibold text-foreground">Agent 目录</h1>
          <p className="mt-1 text-[12px] text-muted-foreground">
            每个配方是一类问题的固定打法。版本号是 YAML 内容指纹，改一个字就变。
          </p>
        </div>
        <button
          type="button"
          onClick={() => void load()}
          className="flex items-center gap-1 rounded border border-border px-2 py-1 text-[12px] text-foreground hover:bg-s2"
          data-testid="agent-catalog-refresh"
        >
          <RefreshCw size={12} /> 刷新
        </button>
      </div>

      {state === 'loading' && (
        <div className="space-y-2" data-testid="agent-catalog-loading">
          <Skeleton className="h-9 w-full" />
          <Skeleton className="h-9 w-full" />
        </div>
      )}

      {state === 'error' && (
        <div className="card-surface rounded-xl bg-card p-3 text-[12px] text-stock-down" data-testid="agent-catalog-error">
          读取失败：{error}
        </div>
      )}

      {state === 'empty' && (
        <div className="card-surface rounded-xl bg-card p-3 text-[12px] text-muted-foreground" data-testid="agent-catalog-empty">
          没有配方。检查 <code>src/harness/recipes/*.yaml</code> 是否存在。
        </div>
      )}

      {state === 'ok' && (
        <div className="card-surface overflow-hidden rounded-xl bg-card">
          {items.map((a) => {
            const isOpen = openId === a.id
            return (
              <div key={a.id} className="border-b border-border last:border-0">
                <button
                  type="button"
                  onClick={() => void toggle(a.id)}
                  aria-expanded={isOpen}
                  className="flex min-h-11 w-full flex-wrap items-center gap-2 px-3 py-2 text-left hover:bg-s2"
                  data-testid={`agent-row-${a.id}`}
                >
                  <span className="text-[14px]">{a.icon || '🤖'}</span>
                  <span className="text-[12px] font-medium text-foreground">{a.label}</span>
                  <span className="text-[12px] text-muted-foreground">{a.id}</span>
                  <span className="ml-auto text-[12px] tabular-nums text-muted-foreground">
                    {a.tools?.length ?? 0} 工具 · {a.version || '--'}
                  </span>
                </button>
                {isOpen && (
                  <div className="bg-s1 px-3 py-2" data-testid={`agent-detail-${a.id}`}>
                    {detailError ? (
                      <div className="text-[12px] text-stock-down">详情读取失败：{detailError}</div>
                    ) : !detail ? (
                      <div className="text-[12px] text-muted-foreground">读取中…</div>
                    ) : (
                      <div className="space-y-1.5 text-[12px]">
                        <div className="text-muted-foreground">{detail.item?.desc || '--'}</div>
                        <div>
                          <span className="text-muted-foreground">适用问法：</span>
                          <span className="text-foreground">{(detail.item?.when ?? []).join('、') || '--'}</span>
                        </div>
                        <div>
                          <span className="text-muted-foreground">输出段落：</span>
                          <span className="text-foreground">{(detail.item?.output_sections ?? []).join(' / ') || '--'}</span>
                        </div>
                        <div>
                          <span className="text-muted-foreground">工具：</span>
                          <span className="text-foreground">{(detail.item?.tools ?? []).join('、') || '--'}</span>
                        </div>
                        <div>
                          <span className="text-muted-foreground">版本历史：</span>
                          {detail.history?.length ? (
                            <span className="text-foreground">
                              {detail.history.map((h, i) => (
                                <span key={h.fingerprint + i} className="mr-2 tabular-nums">
                                  {h.fingerprint}
                                  <span className="ml-1 text-muted-foreground">{h.created_at.slice(0, 10)}</span>
                                </span>
                              ))}
                            </span>
                          ) : (
                            <span className="text-muted-foreground">只有当前一次观察记录</span>
                          )}
                        </div>
                      </div>
                    )}
                  </div>
                )}
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}
