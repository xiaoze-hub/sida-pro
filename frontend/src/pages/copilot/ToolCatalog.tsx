/**
 * 工具目录页（=插件面板，只读反射，2026-10-25）。
 *
 * 回答一个问题：「平台到底有哪些能力、各自什么口径、挂在哪些配方里、有没有人在用？」
 * 数据来自 `GET /copilot/tools`（注册表反射），**本页不写库、不改注册表**。
 *
 * 口径铁律：
 *  - 每个工具的口径**逐条**展示，不汇总（五个数据源口径不可换算）；
 *  - 未挂任何配方的工具要显性标出来，而不是藏起来 —— 「没人用」本身是重要信息
 *    （Vercel 砍掉 80% 工具后成功率 80%→100%，可见性先于增删）；
 *  - 四态分开：加载中 / 无数据 / 读取失败（透出服务端原文）/ 有数据；
 *  - 缺值 `--`，绝不补 0。
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import { RefreshCw, Search } from 'lucide-react'
import { Skeleton } from '@/components/Skeleton'
import { getChatToolCatalog } from '@/lib/copilot/tools'
import type { ChatToolCatalog, ChatToolRow } from '@/lib/copilot/tools'

type Filter = 'all' | 'mounted' | 'orphan'

const FILTERS: { key: Filter; label: string }[] = [
  { key: 'all', label: '全部' },
  { key: 'mounted', label: '已挂配方' },
  { key: 'orphan', label: '未挂配方' },
]

/** 配方 id → 中文名。拿不到就原样显示 id，不编造中文名。 */
const RECIPE_LABEL: Record<string, string> = {
  auction_hunter: '竞价猎手',
  chip_analyst: '筹码分析',
  deep_diagnosis: '深度诊断',
  demon_diagnosis: '妖股诊断',
  devil_advocate: '反方质证',
  intraday_analyst: '盘中分析',
  intraday_watcher: '盘中盯盘',
  left_diagnosis: '左侧诊断',
  postmarket_reviewer: '盘后复盘',
  premarket_strategist: '盘前策略',
  risk_judge: '风险裁决',
  theme_leader: '题材龙头',
}

function recipeLabel(id: string): string {
  return RECIPE_LABEL[id] ?? id
}

export default function ToolCatalog() {
  const [data, setData] = useState<ChatToolCatalog | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [q, setQ] = useState('')
  const [filter, setFilter] = useState<Filter>('all')

  const load = useCallback(async (signal?: AbortSignal) => {
    setLoading(true)
    setError(null)
    try {
      const body = await getChatToolCatalog(signal)
      setData(body)
    } catch (exc) {
      const raw = exc instanceof Error ? exc.message : String(exc)
      setError(raw.trim() || '读取工具目录失败')
      setData(null)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    const ctrl = new AbortController()
    void load(ctrl.signal)
    return () => ctrl.abort()
  }, [load])

  const rows = useMemo<ChatToolRow[]>(() => {
    const all = data?.tools ?? []
    const kw = q.trim().toLowerCase()
    return all.filter((t) => {
      if (filter === 'mounted' && t.recipes.length === 0) return false
      if (filter === 'orphan' && t.recipes.length > 0) return false
      if (!kw) return true
      return (
        t.name.toLowerCase().includes(kw) ||
        t.caliber.toLowerCase().includes(kw) ||
        t.description.toLowerCase().includes(kw) ||
        t.recipes.some((r) => r.toLowerCase().includes(kw))
      )
    })
  }, [data, q, filter])

  const orphanCount = useMemo(
    () => (data?.tools ?? []).filter((t) => t.recipes.length === 0).length,
    [data],
  )

  return (
    <div className="mx-auto flex w-full max-w-[1280px] flex-col gap-4 p-4">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-[20px] font-semibold text-foreground">工具目录</h1>
          <p className="mt-1 text-[12px] text-muted-foreground">
            注册表全量反射，只读。口径逐条展示，不可换算。
          </p>
        </div>
        <div className="flex items-center gap-3">
          {data ? (
            <div className="flex items-center gap-2 text-[12px] text-muted-foreground">
              <span className="tabular-nums">{data.total} 个工具</span>
              <span aria-hidden>·</span>
              <span className="tabular-nums">{data.mounted} 已挂配方</span>
              <span aria-hidden>·</span>
              <span className="tabular-nums">{orphanCount} 未挂配方</span>
            </div>
          ) : null}
          <button
            type="button"
            onClick={() => void load()}
            className="inline-flex items-center gap-1 rounded-lg border border-border px-2 py-1 text-[12px] text-foreground hover:bg-muted"
          >
            <RefreshCw className="h-3 w-3" />
            刷新
          </button>
        </div>
      </header>

      <div className="flex flex-wrap items-center gap-2">
        <label className="relative flex-1 min-w-[220px]">
          <Search className="pointer-events-none absolute left-2 top-1/2 h-3 w-3 -translate-y-1/2 text-muted-foreground" />
          <input
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="按工具名 / 口径 / 配方过滤"
            aria-label="搜索工具"
            className="w-full rounded-lg border border-border bg-card py-1 pl-7 pr-2 text-[12px] text-foreground placeholder:text-muted-foreground"
          />
        </label>
        <div className="flex items-center gap-1">
          {FILTERS.map((f) => (
            <button
              key={f.key}
              type="button"
              onClick={() => setFilter(f.key)}
              className={
                filter === f.key
                  ? 'rounded-lg border border-border bg-muted px-2 py-1 text-[12px] text-foreground'
                  : 'rounded-lg border border-transparent px-2 py-1 text-[12px] text-muted-foreground hover:bg-muted'
              }
            >
              {f.label}
            </button>
          ))}
        </div>
      </div>

      {loading ? (
        <div className="flex flex-col gap-2" aria-label="加载中">
          <Skeleton className="h-8 w-full" />
          <Skeleton className="h-8 w-full" />
          <Skeleton className="h-8 w-full" />
        </div>
      ) : error ? (
        <div className="rounded-xl border border-border bg-card p-4 text-[14px] text-stock-down">
          读取工具目录失败：{error}
        </div>
      ) : !data || data.tools.length === 0 ? (
        <div className="rounded-xl border border-border bg-card p-6 text-center text-[14px] text-muted-foreground">
          注册表里没有工具
        </div>
      ) : rows.length === 0 ? (
        <div className="rounded-xl border border-border bg-card p-6 text-center text-[14px] text-muted-foreground">
          当前过滤条件下没有工具
        </div>
      ) : (
        <div className="overflow-x-auto rounded-xl border border-border bg-card">
          <table className="w-full border-collapse text-[12px]">
            <thead>
              <tr className="border-b border-border text-left text-muted-foreground">
                <th className="px-3 py-2 font-medium">工具</th>
                <th className="px-3 py-2 font-medium">口径</th>
                <th className="px-3 py-2 font-medium">挂载配方</th>
                <th className="px-3 py-2 font-medium text-right">计预算</th>
                <th className="px-3 py-2 font-medium text-right">降级</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((t) => (
                <tr key={t.name} className="border-b border-border last:border-0">
                  <td className="px-3 py-2 align-top">
                    <div className="font-medium text-foreground">{t.name}</div>
                    <div className="mt-0.5 text-[12px] text-muted-foreground">
                      {t.description || '--'}
                    </div>
                  </td>
                  <td className="px-3 py-2 align-top text-foreground">{t.caliber || '--'}</td>
                  <td className="px-3 py-2 align-top">
                    {t.recipes.length === 0 ? (
                      <span className="text-amber-500">未挂配方</span>
                    ) : (
                      <span className="flex flex-wrap gap-1">
                        {t.recipes.map((r) => (
                          <span
                            key={r}
                            title={r}
                            className="rounded-md border border-border px-1.5 py-0.5 text-[12px] text-foreground"
                          >
                            {recipeLabel(r)}
                          </span>
                        ))}
                      </span>
                    )}
                  </td>
                  <td className="px-3 py-2 text-right align-top tabular-nums text-foreground">
                    {t.charges_budget ? '是' : '否'}
                  </td>
                  <td className="px-3 py-2 text-right align-top text-foreground">
                    {t.has_fallback ? '有' : '--'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
