/**
 * 参谋任务列表页（只读，2026-10-25）。
 *
 * 回答一个很具体的问题：「我刚才问的那些问题，每一轮到底跑了什么、花了多久、成没成？」
 * 数据全部来自既有接口（`/copilot/runs` 列表 + `/copilot/runs/{id}` 单轮回放），本页不写库。
 *
 * 口径铁律（与 ToolTimeline / 治理看板一致）：
 *  - 缺值一律 `--`，**绝不补 0**（没测到耗时 ≠ 0ms，没记工具数 ≠ 0 次调用）；
 *  - 四态分开且诚实：加载中 / 无数据 / 读取失败 / 有数据 —— 不用「空表格」冒充失败，也不用
 *    「加载中」掩盖失败；
 *  - 层级靠 hairline 边框与字号，不做卡片堆砌、不加阴影；
 *  - 展开即拉详情，同一行**只拉一次**（Map 缓存已加载的回放）。
 */
import { useNavigate } from 'react-router-dom'
import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { ChevronDown, ChevronRight, RefreshCw } from 'lucide-react'
import { safeFixed, safeInt, safeNum } from '@/lib/format'
import { Skeleton } from '@/components/Skeleton'
import { formatElapsed } from '@/components/copilot/ToolTimeline'
import { getCopilotRunDetail, getCopilotRuns } from '@/lib/copilot/api'
import type { CopilotRunDetail, CopilotRunRow } from '@/lib/copilot/api'

/** 每页条数；后端 limit 上限 100，所以最多能翻 5 页（再多就没有接口可拉了）。 */
const PAGE_SIZE = 20
const MAX_LIMIT = 100

/** 状态徽标：中文文案 + 语义色（灰=正常 / 橙=降级 / 绿=失败，与全站「涨红跌绿」不冲突：这里不是价格）。 */
const STATUS_META: Record<string, { label: string; cls: string }> = {
  ok: { label: '成功', cls: 'text-muted-foreground' },
  degraded: { label: '降级', cls: 'text-amber-500' },
  error: { label: '失败', cls: 'text-stock-down' },
  running: { label: '运行中', cls: 'text-muted-foreground' },
}

/** 未知状态按中性处理并原样显示，不编造语义。 */
function statusMeta(status: string | null | undefined): { label: string; cls: string } {
  const key = String(status ?? '').toLowerCase()
  const hit = STATUS_META[key]
  if (hit) return hit
  const raw = String(status ?? '').trim()
  return { label: raw || '--', cls: 'text-muted-foreground' }
}

/** 配方工程名 → 中文（英文原名留在 title 上，排障时仍能一一对应）。 */
const RECIPE_CN: Record<string, string> = {
  left_diagnosis: '左侧诊断',
  deep_diagnosis: '深度诊断',
  chip_analyst: '筹码分析',
  postmarket_reviewer: '盘后复盘',
  demon_diagnosis: '妖股诊断',
  intraday_watcher: '盘中监测',
}

function recipeText(recipe: string | null | undefined): string {
  const name = String(recipe ?? '').trim()
  if (!name) return '--'
  return RECIPE_CN[name] || name
}

/** 问题列：一眼能扫完，完整文本放 title。 */
function clip(text: string | null | undefined, n = 30): string {
  const t = String(text ?? '').replace(/\s+/g, ' ').trim()
  if (!t) return '--'
  return t.length > n ? `${t.slice(0, n)}…` : t
}

/** 时间列：只给「月-日 时:分」，完整时间放 title；拿不到就是 `--`。 */
function timeText(iso: string | null | undefined): string {
  const s = String(iso ?? '').trim()
  if (!s) return '--'
  return s.length >= 16 ? s.slice(5, 16) : s
}

/** 百分比：分母不存在 → `--`，不写 0%。 */
function rateText(v: number | null): string {
  return v == null ? '--' : `${safeFixed(v * 100, 0)}%`
}

/** 均值：样本为空 → `--`。 */
function avgText(v: number | null): string {
  return v == null ? '--' : safeFixed(v, 1)
}

export default function CopilotRunsPage() {
  const [page, setPage] = useState(1)
  const [rows, setRows] = useState<CopilotRunRow[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const navigate = useNavigate()
  const [expanded, setExpanded] = useState<string | null>(null)
  /** run_id → 单轮回放。同一行再次展开直接命中缓存，不再发请求。 */
  const [details, setDetails] = useState<Map<string, CopilotRunDetail>>(() => new Map())
  const [detailLoading, setDetailLoading] = useState<string | null>(null)
  const [detailError, setDetailError] = useState<string | null>(null)
  /** 竞态闸门：翻页途中回来的旧响应不许覆盖新页。 */
  const reqRef = useRef(0)

  const limit = Math.min(PAGE_SIZE * page, MAX_LIMIT)

  const load = useCallback(
    async (dropDetailCache: boolean) => {
      const reqId = reqRef.current + 1
      reqRef.current = reqId
      setLoading(true)
      setError(null)
      try {
        const list = await getCopilotRuns(limit)
        if (reqRef.current !== reqId) return
        setRows(Array.isArray(list) ? list : [])
        if (dropDetailCache) {
          setDetails(new Map())
          setExpanded(null)
          setDetailError(null)
        }
      } catch (e) {
        if (reqRef.current !== reqId) return
        setRows([])
        setError(e instanceof Error ? e.message : '读取失败')
      } finally {
        if (reqRef.current === reqId) setLoading(false)
      }
    },
    [limit],
  )

  useEffect(() => {
    void load(false)
  }, [load])

  const toggle = useCallback(
    async (runId: string) => {
      const willOpen = expanded !== runId
      setExpanded(willOpen ? runId : null)
      if (!willOpen) return
      // 已加载过 → 直接展开，不重复请求
      if (details.has(runId)) return
      setDetailLoading(runId)
      setDetailError(null)
      try {
        const detail = await getCopilotRunDetail(runId)
        setDetails((prev) => {
          const next = new Map(prev)
          next.set(runId, detail)
          return next
        })
      } catch {
        setDetailError(runId)
      } finally {
        setDetailLoading((cur) => (cur === runId ? null : cur))
      }
    },
    [details, expanded],
  )

  /** 汇总口径：只用**已载入**的行算；分母不存在给 `--`，不拿 0 顶替。 */
  const summary = useMemo(() => {
    const total = rows.length
    if (total === 0) return { total, okRate: null as number | null, avgTools: null as number | null }
    const okCount = rows.filter((r) => String(r.status ?? '').toLowerCase() === 'ok').length
    const toolNums = rows
      .map((r) => safeNum(r.tool_calls))
      .filter((n): n is number => n !== null)
    const avgTools = toolNums.length
      ? toolNums.reduce((a, b) => a + b, 0) / toolNums.length
      : null
    return { total, okRate: okCount / total, avgTools }
  }, [rows])

  const pageRows = useMemo(
    () => rows.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE),
    [rows, page],
  )
  /** 本页拉满且还没到接口上限 → 后面可能还有；否则就是最后一页。 */
  const canNext = rows.length >= limit && limit < MAX_LIMIT
  const totalPages = canNext
    ? page + 1
    : Math.max(1, Math.ceil(rows.length / PAGE_SIZE))

  return (
    <div className="space-y-3" data-testid="copilot-runs-page">
      <header className="flex flex-wrap items-center gap-2">
        <h1 className="text-[16px] font-medium text-foreground">任务</h1>
        <button
          type="button"
          onClick={() => void load(true)}
          disabled={loading}
          data-testid="runs-refresh"
          className="ml-auto flex items-center gap-1 rounded-md border border-border px-2 py-1 text-[12px] text-muted-foreground hover:text-foreground disabled:opacity-60"
        >
          <RefreshCw className="h-3 w-3" aria-hidden />
          刷新
        </button>
      </header>

      <div className="text-[12px] text-muted-foreground" data-testid="runs-summary">
        近 <span className="tabular-nums text-foreground">{loading || error ? '--' : summary.total}</span> 次运行 · 成功率{' '}
        <span className="tabular-nums text-foreground">
          {loading || error ? '--' : rateText(summary.okRate)}
        </span>{' '}
        · 平均工具数{' '}
        <span className="tabular-nums text-foreground">
          {loading || error ? '--' : avgText(summary.avgTools)}
        </span>
      </div>

      {loading ? (
        <div className="space-y-2 rounded-xl border border-border bg-s1 px-3 py-3" data-testid="runs-loading">
          <div className="text-[12px] text-muted-foreground">读取中…</div>
          <Skeleton shape="text" className="h-3 w-3/4" />
          <Skeleton shape="text" className="h-3 w-1/2" />
          <Skeleton shape="text" className="h-3 w-2/3" />
        </div>
      ) : error ? (
        <div
          className="rounded-xl border border-border bg-s1 px-3 py-6 text-[12px] text-muted-foreground"
          data-testid="runs-error"
        >
          读取失败（可点刷新重试）
        </div>
      ) : rows.length === 0 ? (
        <div
          className="rounded-xl border border-border bg-s1 px-3 py-6 text-[12px] text-muted-foreground"
          data-testid="runs-empty"
        >
          暂无运行记录
          <div className="mt-1 text-[12px]">在「数智参谋」里提一次问题，这里就会出现那一轮的轨迹。</div>
        </div>
      ) : (
        <section className="rounded-xl border border-border bg-s1" data-testid="runs-table-wrap">
          <div className="max-h-[70vh] overflow-y-auto">
            <table className="w-full text-[12px]">
              <thead className="sticky top-0 z-10 bg-s1 text-muted-foreground">
                <tr className="border-b border-border">
                  <th className="px-2 py-1.5 text-left font-normal">状态</th>
                  <th className="px-2 py-1.5 text-left font-normal">问题</th>
                  <th className="px-2 py-1.5 text-left font-normal">配方</th>
                  <th className="px-2 py-1.5 text-right font-normal">工具数</th>
                  <th className="px-2 py-1.5 text-right font-normal">耗时</th>
                  <th className="px-2 py-1.5 text-right font-normal">时间</th>
                </tr>
              </thead>
              <tbody>
                {pageRows.map((row) => {
                  const meta = statusMeta(row.status)
                  const open = expanded === row.run_id
                  const detail = details.get(row.run_id)
                  return (
                    <Fragment key={row.run_id}>
                      <tr
                        onClick={() => void toggle(row.run_id)}
                        aria-expanded={open}
                        data-testid={`run-row-${row.run_id}`}
                        className="cursor-pointer border-t border-border hover:bg-s2"
                      >
                        <td className={`whitespace-nowrap px-2 py-1.5 ${meta.cls}`}>
                          <span className="flex items-center gap-1">
                            {open ? (
                              <ChevronDown className="h-3 w-3 shrink-0 text-muted-foreground" aria-hidden />
                            ) : (
                              <ChevronRight className="h-3 w-3 shrink-0 text-muted-foreground" aria-hidden />
                            )}
                            <span data-testid={`run-status-${row.run_id}`}>{meta.label}</span>
                          </span>
                        </td>
                        <td
                          className="max-w-[360px] truncate px-2 py-1.5 text-foreground"
                          title={row.question || undefined}
                        >
                          {clip(row.question)}
                        </td>
                        <td
                          className="max-w-[140px] truncate px-2 py-1.5 text-muted-foreground"
                          title={row.recipe || undefined}
                        >
                          {recipeText(row.recipe)}
                        </td>
                        <td className="whitespace-nowrap px-2 py-1.5 text-right font-mono tabular-nums text-foreground">
                          {safeInt(row.tool_calls)}
                        </td>
                        <td className="whitespace-nowrap px-2 py-1.5 text-right font-mono tabular-nums text-muted-foreground">
                          {formatElapsed(row.elapsed_ms)}
                        </td>
                        <td
                          className="whitespace-nowrap px-2 py-1.5 text-right font-mono tabular-nums text-muted-foreground"
                          title={row.created_at || undefined}
                        >
                          {timeText(row.created_at)}
                        </td>
                      </tr>
                      {open ? (
                        <tr className="border-t border-border" data-testid={`run-steps-${row.run_id}`}>
                          <td colSpan={6} className="px-2 py-1.5">
                            {detail ? (
                              detail.steps.length === 0 ? (
                                <div className="text-[12px] text-muted-foreground">
                                  本轮没有工具调用（纯文本回答）
                                </div>
                              ) : (
                                <ol className="m-0 list-none space-y-0.5 p-0">
                                  {detail.steps.map((step) => (
                                    <li
                                      key={step.seq}
                                      className="flex items-baseline gap-2"
                                      title={step.args || undefined}
                                      data-testid={`run-step-${row.run_id}-${step.seq}`}
                                    >
                                      <span className="w-8 shrink-0 font-mono tabular-nums text-[12px] text-muted-foreground">
                                        #{step.seq}
                                      </span>
                                      <span className="min-w-0 flex-1 truncate font-mono text-[12px] text-foreground">
                                        {step.tool || '--'}
                                      </span>
                                      <span className="shrink-0 font-mono tabular-nums text-[12px] text-muted-foreground">
                                        {formatElapsed(step.elapsed_ms)}
                                      </span>
                                      <span
                                        className={`shrink-0 text-[12px] ${
                                          step.ok ? 'text-muted-foreground' : 'text-stock-down'
                                        }`}
                                      >
                                        {step.ok ? '成功' : '失败'}
                                      </span>
                                    </li>
                                  ))}
                                </ol>
                              )
                            ) : detailError === row.run_id ? (
                              <div className="text-[12px] text-muted-foreground">
                                工具步骤读取失败（收起后再展开可重试）
                              </div>
                            ) : detailLoading === row.run_id ? (
                              <div className="space-y-1" data-testid={`run-steps-loading-${row.run_id}`}>
                                <div className="text-[12px] text-muted-foreground">读取中…</div>
                                <Skeleton shape="text" className="h-2 w-1/2" />
                              </div>
                            ) : null}
                            {/* 2026-10-25: 展开只是速览; 要分享/细看走独立证据页 */}
                            <div className="mt-1 border-t border-border pt-1">
                              <button
                                type="button"
                                onClick={(e) => {
                                  e.stopPropagation()
                                  navigate(`/copilot/runs/${encodeURIComponent(row.run_id)}`)
                                }}
                                className="text-[12px] text-muted-foreground hover:text-foreground"
                                data-testid={`run-open-detail-${row.run_id}`}
                              >
                                打开证据卡 ↗
                              </button>
                            </div>
                          </td>
                        </tr>
                      ) : null}
                    </Fragment>
                  )
                })}
              </tbody>
            </table>
          </div>
          <div className="flex flex-wrap items-center gap-2 border-t border-border px-2 py-1.5 text-[12px] text-muted-foreground">
            <span>
              第 <span className="tabular-nums text-foreground">{page}</span> /{' '}
              <span className="tabular-nums text-foreground">{totalPages}</span> 页
            </span>
            <span>
              已载入 <span className="tabular-nums text-foreground">{rows.length}</span> 条
            </span>
            <div className="ml-auto flex items-center gap-1">
              <button
                type="button"
                data-testid="runs-prev"
                disabled={page <= 1}
                onClick={() => setPage((p) => Math.max(1, p - 1))}
                className="rounded-md border border-border px-2 py-0.5 text-[12px] text-muted-foreground hover:text-foreground disabled:opacity-50"
              >
                上一页
              </button>
              <button
                type="button"
                data-testid="runs-next"
                disabled={!canNext}
                onClick={() => setPage((p) => p + 1)}
                className="rounded-md border border-border px-2 py-0.5 text-[12px] text-muted-foreground hover:text-foreground disabled:opacity-50"
              >
                下一页
              </button>
            </div>
          </div>
        </section>
      )}
    </div>
  )
}
