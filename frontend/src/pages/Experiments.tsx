/**
 * 经验库 / 实验台（只读，2026-10-25 P2-2.4）。
 *
 * 回答一个问题：「我改了一句提示词，指标到底变好还是变差了？」
 * 数据来自 `harness_experiments`（只存事实：改动摘要 / 基准 / 候选 / verdict / 回滚原因）。
 *
 * 口径铁律：
 *  - 这里是**只读视图**，verdict 由写入时判定，页面不改判；
 *  - 缺值一律 `--`，**绝不补 0**（没测到 ≠ 0）；数字走 `safeFixed`；
 *  - 四态分开：加载中 / 无数据 / 读取失败（透出服务端原文）/ 有数据；
 *  - 证据文件不存在就写「证据文件缺失」，不做假下载入口。
 */
import { useCallback, useEffect, useState } from 'react'
import { RefreshCw } from 'lucide-react'
import { fetchAPI } from '@panwatch/api'
import { safeFixed } from '@/lib/format'
import { Skeleton } from '@/components/Skeleton'

export type ExperimentRow = {
  id: number
  recipe_id: string
  change_summary: string
  verdict: string
  created_at: string
}

export type ExperimentList = {
  available: boolean
  reason?: string
  items: ExperimentRow[]
  total: number
  stats?: Record<string, number>
}

export type ExperimentDetail = {
  available: boolean
  reason?: string
  item: (ExperimentRow & {
    baseline?: Record<string, unknown>
    candidate?: Record<string, unknown>
    reasons?: string[]
    note?: string | null
    evidence_path?: string | null
    evidence_exists?: boolean
  }) | null
}

const VERDICT_META: Record<string, { label: string; cls: string }> = {
  keep: { label: '保留', cls: 'text-stock-up' },
  rollback: { label: '回滚', cls: 'text-stock-down' },
}

function verdictMeta(v: string | null | undefined) {
  const hit = VERDICT_META[String(v ?? '').toLowerCase()]
  if (hit) return hit
  // 未知值原样显示，不编语义
  return { label: String(v ?? '--'), cls: 'text-muted-foreground' }
}

/** 指标对照：只显示 baseline/candidate 里都有的键，缺的就是 `--`。 */
function metricPairs(baseline?: Record<string, unknown>, candidate?: Record<string, unknown>) {
  const keys = Array.from(new Set([...Object.keys(baseline ?? {}), ...Object.keys(candidate ?? {})]))
  return keys.sort().map((k) => ({ key: k, before: baseline?.[k], after: candidate?.[k] }))
}

function cell(v: unknown): string {
  if (v === null || v === undefined) return '--'
  if (typeof v === 'number') return Number.isInteger(v) ? String(v) : safeFixed(v, 4)
  if (typeof v === 'boolean') return v ? '是' : '否'
  if (typeof v === 'object') return JSON.stringify(v)
  return String(v)
}

export default function ExperimentsPage() {
  const [state, setState] = useState<'loading' | 'ok' | 'empty' | 'error'>('loading')
  const [list, setList] = useState<ExperimentList | null>(null)
  const [error, setError] = useState('')
  const [detail, setDetail] = useState<ExperimentDetail['item']>(null)
  const [detailError, setDetailError] = useState('')
  const [openId, setOpenId] = useState<number | null>(null)

  const load = useCallback(async () => {
    setState('loading')
    setError('')
    try {
      const res = await fetchAPI<ExperimentList>('/copilot/experiments?limit=50')
      setList(res)
      if (!res.available) {
        setState('error')
        setError(res.reason || '服务端未给出原因')
        return
      }
      setState(res.items.length ? 'ok' : 'empty')
    } catch (e) {
      setState('error')
      setError(e instanceof Error ? e.message : String(e))
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const open = useCallback(async (id: number) => {
    if (openId === id) {
      setOpenId(null)
      setDetail(null)
      return
    }
    setOpenId(id)
    setDetailError('')
    setDetail(null)
    try {
      const res = await fetchAPI<ExperimentDetail>(`/copilot/experiments/${id}`)
      if (!res.available) {
        setDetailError(res.reason || '服务端未给出原因')
        return
      }
      setDetail(res.item)
    } catch (e) {
      setDetailError(e instanceof Error ? e.message : String(e))
    }
  }, [openId])

  const stats = list?.stats ?? {}

  return (
    <div className="space-y-3" data-testid="experiments-page">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-[16px] font-semibold text-foreground">经验库 · 实验台</h1>
          <p className="mt-1 text-[12px] text-muted-foreground">
            每条实验只记事实：改了什么、基准与候选的指标、结论（保留/回滚）。
          </p>
        </div>
        <button
          type="button"
          onClick={() => void load()}
          className="flex items-center gap-1 rounded border border-border px-2 py-1 text-[12px] text-foreground hover:bg-s2"
          data-testid="experiments-refresh"
        >
          <RefreshCw size={12} /> 刷新
        </button>
      </div>

      <div className="flex gap-4 text-[12px] text-muted-foreground" data-testid="experiments-stats">
        <span>共 {list?.total ?? 0} 条</span>
        <span>保留 {stats.keep ?? 0}</span>
        <span>回滚 {stats.rollback ?? 0}</span>
      </div>

      {state === 'loading' && (
        <div className="space-y-2" data-testid="experiments-loading">
          <Skeleton className="h-9 w-full" />
          <Skeleton className="h-9 w-full" />
        </div>
      )}

      {state === 'error' && (
        <div className="card-surface rounded-xl bg-card p-3 text-[12px] text-stock-down" data-testid="experiments-error">
          读取失败：{error}
        </div>
      )}

      {state === 'empty' && (
        <div className="card-surface rounded-xl bg-card p-3 text-[12px] text-muted-foreground" data-testid="experiments-empty">
          还没有实验记录。跑一次对照（改配方 → 同批问题对比 → 记 verdict）就会出现在这里。
        </div>
      )}

      {state === 'ok' && (
        <div className="card-surface overflow-hidden rounded-xl bg-card">
          <table className="w-full text-[12px] tabular-nums">
            <thead>
              <tr className="border-b border-border text-left text-muted-foreground">
                <th className="px-2 py-1.5 font-normal">改动</th>
                <th className="px-2 py-1.5 font-normal">配方</th>
                <th className="px-2 py-1.5 font-normal">结论</th>
                <th className="px-2 py-1.5 font-normal">时间</th>
              </tr>
            </thead>
            <tbody>
              {(list?.items ?? []).map((r) => {
                const m = verdictMeta(r.verdict)
                const isOpen = openId === r.id
                return (
                  <>
                    <tr
                      key={r.id}
                      onClick={() => void open(r.id)}
                      className="cursor-pointer border-b border-border last:border-0 hover:bg-s2"
                      data-testid={`experiment-row-${r.id}`}
                    >
                      <td className="px-2 py-1.5 text-foreground">{r.change_summary}</td>
                      <td className="px-2 py-1.5 text-muted-foreground">{r.recipe_id}</td>
                      <td className={`px-2 py-1.5 ${m.cls}`}>{m.label}</td>
                      <td className="px-2 py-1.5 text-muted-foreground">{r.created_at || '--'}</td>
                    </tr>
                    {isOpen && (
                      <tr key={`${r.id}-detail`} className="border-b border-border bg-s1">
                        <td colSpan={4} className="px-2 py-2" data-testid={`experiment-detail-${r.id}`}>
                          {detailError ? (
                            <span className="text-stock-down">详情读取失败：{detailError}</span>
                          ) : !detail ? (
                            <span className="text-muted-foreground">读取中…</span>
                          ) : (
                            <div className="space-y-2">
                              <div className="grid grid-cols-3 gap-2 text-[12px] text-muted-foreground">
                                <span>指标</span>
                                <span>基准</span>
                                <span>候选</span>
                              </div>
                              {metricPairs(detail.baseline, detail.candidate).map((p) => (
                                <div key={p.key} className="grid grid-cols-3 gap-2 text-[12px] tabular-nums">
                                  <span className="text-foreground">{p.key}</span>
                                  <span className="text-muted-foreground">{cell(p.before)}</span>
                                  <span className="text-foreground">{cell(p.after)}</span>
                                </div>
                              ))}
                              {(detail.reasons ?? []).length > 0 && (
                                <div className="text-[12px] text-stock-down">
                                  回滚原因：{(detail.reasons ?? []).join('；')}
                                </div>
                              )}
                              <div className="text-[12px] text-muted-foreground">
                                证据：
                                {detail.evidence_path
                                  ? detail.evidence_exists
                                    ? detail.evidence_path
                                    : `${detail.evidence_path}（证据文件缺失）`
                                  : '未记录'}
                              </div>
                            </div>
                          )}
                        </td>
                      </tr>
                    )}
                  </>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
