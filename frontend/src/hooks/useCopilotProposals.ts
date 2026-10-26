/**
 * 提案面板取数（2026-10-22，AI 参谋工作台）。
 *
 * 职责边界（照 useCopilotRuns 的口径）：
 *  - 列表与 stats **同时**拉（汇总带要四个数，列表要行），互不阻塞；
 *  - stats 挂了不算整面板失败：列表照常上屏，只是**不显示汇总带**（不拿 0 冒充）；
 *  - 裁决成功后**局部刷新**（本 tab 列表 + stats），不整页重载、不闪骨架屏；
 *  - 接口失败 → `error` 非空，组件必须显式报错，**绝不降级成空态**（挂了和真没有是两件事）。
 *
 * 口径：`limit` 固定 50（后端夹 1..200；设计文档 §8）。
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { fetchAPI } from '@panwatch/api'
import type { ProposalAction, ProposalRow, ProposalStats, ProposalTab } from '@/lib/copilot/proposals'

/** 面板固定取 50 条（设计文档 §8）。 */
export const PROPOSAL_LIMIT = 50

export interface UseCopilotProposalsResult {
  rows: ProposalRow[]
  /** stats 拿不到就是 null —— 汇总带整条不显示，不用 0 顶替「不知道」 */
  stats: ProposalStats | null
  /** 首屏 / 切 tab：展示骨架 */
  loading: boolean
  /** 裁决后的静默刷新：保留旧列表，不闪骨架 */
  refreshing: boolean
  /** 列表读取失败（面板必须显式报错，不许伪装成空态） */
  error: string | null
  /** 裁决失败（列表还在，只在列表上方补一行提示） */
  decideError: string | null
  /** 正在裁决的提案 id（行内禁用三个按钮）—— **只做展示**，重入守卫是 ref（见 decidingRef） */
  decidingId: number | null
  decide: (id: number, action: ProposalAction) => Promise<boolean>
  reload: () => void
}

/** 列表路径：'all' 不带 status（后端语义：不传 = 全部）。 */
function listPath(tab: ProposalTab): string {
  const base = `/copilot/proposals?limit=${PROPOSAL_LIMIT}`
  return tab === 'all' ? base : `${base}&status=${tab}`
}

export function useCopilotProposals(tab: ProposalTab = 'pending'): UseCopilotProposalsResult {
  const [rows, setRows] = useState<ProposalRow[]>([])
  const [stats, setStats] = useState<ProposalStats | null>(null)
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [decideError, setDecideError] = useState<string | null>(null)
  const [decidingId, setDecidingId] = useState<number | null>(null)
  const [tick, setTick] = useState(0)

  const aliveRef = useRef(true)
  /** 在途请求：切 tab / 重试时取消上一轮，避免旧响应把新 tab 覆盖掉。 */
  const abortRef = useRef<AbortController | null>(null)
  /**
   * 裁决同步互斥（2026-10-22 审查 High）。
   *
   * 原来用 `decidingId !== null` 当重入锁 —— 那是 **React state**，要下一次渲染才可见：
   * 同一个事件循环里连点两次「采纳」，第二次调用闭包里读到的还是 `null` → 守卫被穿透 →
   * 两条 POST 都发出去，后端连记两次裁决、stats 减两次。锁必须是同步的 ref，state 只负责展示。
   */
  const decidingRef = useRef(false)

  useEffect(() => {
    aliveRef.current = true
    return () => {
      aliveRef.current = false
      abortRef.current?.abort()
    }
  }, [])

  /**
   * 取数。`silent` = 裁决后的局部刷新：不清空列表、不亮骨架，失败也只是**保留旧数据**
   * 并给一行错误（用户刚点了采纳，不能因为刷新失败把整屏变空）。
   */
  const load = useCallback(
    async (silent: boolean) => {
      abortRef.current?.abort()
      const ac = new AbortController()
      abortRef.current = ac

      if (silent) setRefreshing(true)
      else setLoading(true)

      const [listRes, statsRes] = await Promise.allSettled([
        fetchAPI<ProposalRow[]>(listPath(tab), { signal: ac.signal, cacheMode: false }),
        fetchAPI<ProposalStats>('/copilot/proposals/stats', { signal: ac.signal, cacheMode: false }),
      ])

      if (ac.signal.aborted || !aliveRef.current) return

      if (listRes.status === 'fulfilled') {
        setRows(Array.isArray(listRes.value) ? listRes.value : [])
        setError(null)
      } else if (!silent) {
        // 首次/切 tab 失败：清空列表 + 报错（错误态与空态必须能区分）
        setRows([])
        setError('加载失败')
      } else {
        setError('刷新失败，显示的是上一次的数据')
      }

      // stats 失败：置 null（汇总带整条隐藏），不写 0 —— 「不知道」不等于「没有」。
      setStats(statsRes.status === 'fulfilled' && statsRes.value ? statsRes.value : null)

      if (silent) setRefreshing(false)
      else setLoading(false)
    },
    [tab],
  )

  useEffect(() => {
    void load(false)
  }, [load, tick])

  const reload = useCallback(() => setTick((t) => t + 1), [])

  /**
   * 裁决一条提案。成功 → 静默刷新列表与 stats（本 tab 内该行的终态会随之消失）。
   *
   * ⚠ 只改数据库 `status`，**配方一个字都没动** —— 组件必须显示「仍需人工改配方」提示，
   * 否则用户会以为参谋下次就长出了这个能力（设计文档 §5 红线）。
   */
  const decide = useCallback(
    async (id: number, action: ProposalAction): Promise<boolean> => {
      // 同步守卫：ref 立即生效，不等渲染（state 版本在这里会被双击穿透）
      if (decidingRef.current) return false
      decidingRef.current = true
      setDecidingId(id)
      setDecideError(null)
      try {
        const res = await fetchAPI<{ ok: boolean; proposal?: ProposalRow }>(
          `/copilot/proposals/${id}/decide`,
          {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ action }),
          },
        )
        if (!aliveRef.current) return false
        // 服务端回的行优先（含 decided_at）；没回就本地标状态，刷新会纠正。
        const serverRow = res?.proposal
        setRows((prev) =>
          prev.map((r) => (r.id === id ? { ...r, ...(serverRow ?? {}), status: serverRow?.status || action } : r)),
        )
        setStats((prev) =>
          prev && prev.summary
            ? {
                ...prev,
                summary: {
                  ...prev.summary,
                  pending: Math.max(0, prev.summary.pending - 1),
                  [action]: prev.summary[action] + 1,
                },
              }
            : prev,
        )
        await load(true)
        return true
      } catch {
        if (!aliveRef.current) return false
        setDecideError('操作失败，这条提案没有被改动')
        return false
      } finally {
        // 锁无条件同步释放（卸载后也必须放 —— 释放时机不该取决于渲染/挂载状态）；
        // 只有「清展示态」才受 aliveRef 约束，避免卸载后 setState。
        decidingRef.current = false
        if (aliveRef.current) setDecidingId(null)
      }
    },
    [load],
  )

  return {
    rows,
    stats,
    loading,
    refreshing,
    error,
    decideError,
    decidingId,
    decide,
    reload,
  }
}

export default useCopilotProposals
