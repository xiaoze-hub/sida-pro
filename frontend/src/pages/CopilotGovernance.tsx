/**
 * 数智参谋治理看板（P3，2026-10-20）。
 *
 * 三个 tab：
 * 1. 轨迹回放 —— 每轮问答的骨架 + 逐步骤工具调用（回答"这句话是怎么来的"）
 * 2. 成本     —— token / 估算费用按日、按模型、按用户；没配单价的模型显式标"未定价"
 * 3. 配额     —— 每人每日轮数/token 上限；没有行 = 不限；超限网关层 429
 *
 * 口径红线：
 * - 费用算不出来就是 `--`，不拿 0 冒充；
 * - 统计缺失就是 `--`，不写成 0。
 */
import { useCallback, useEffect, useState } from 'react'
import { safeFixed } from '@/lib/format'
import { Loader2, Plus, RefreshCw } from 'lucide-react'
import { authApi } from '@panwatch/api'
import {
  deleteMemory,
  deleteSchedule,
  getCopilotCost,
  getCopilotMetrics,
  getCopilotQuotas,
  getMemories,
  getSchedules,
  patchMemory,
  patchSchedule,
  postMemory,
  postSchedule,
  getCopilotRunDetail,
  getCopilotRuns,
  putCopilotQuota,
  type CopilotCost,
  type CopilotMemoryRow,
  type CopilotMetrics,
  type CopilotQuotaRow,
  type CopilotScheduleRow,
  type CopilotRunDetail,
  type CopilotRunRow,
} from '@/lib/copilot/api'

type Tab = 'traces' | 'cost' | 'quota' | 'memory' | 'schedule'

const TABS: { id: Tab; label: string; hint: string }[] = [
  { id: 'traces', label: '轨迹回放', hint: '每轮问答用了哪些工具、花了多久' },
  { id: 'cost', label: '成本', hint: 'token 与估算费用（未配单价显示 --）' },
  { id: 'quota', label: '配额', hint: '按人限额；没有记录=不限' },
  { id: 'memory', label: '记忆', hint: '它记住的偏好/规则（来自你明确说过的）' },
  { id: 'schedule', label: '定时', hint: '到点把内容推回会话' },
]

function ms(v: number | null | undefined) {
  return v == null ? '--' : `${safeFixed(v / 1000, 1)}s`
}

function num(v: number | null | undefined) {
  return v == null ? '--' : v.toLocaleString('zh-CN')
}

/** 0~1 的比率 → 百分比；null（分母不存在）→ `--`，不写 0%。 */
function rate(v: number | null | undefined) {
  return v == null ? '--' : `${safeFixed(v * 100, 0)}%`
}

/** 状态 → 颜色：只有 ok/degraded/error 三种有语义，其它（running）保持中性。 */
function statusClass(status: string | null | undefined) {
  switch (String(status || '').toLowerCase()) {
    case 'ok':
      return 'text-role-up'
    case 'degraded':
      return 'text-role-watch'
    case 'error':
      return 'text-role-risk'
    default:
      return 'text-muted-foreground'
  }
}

/**
 * 配方名 → 中文（2026-10-24 L3-F）
 * 之前列表直接显示 left_diagnosis / postmarket_reviewer 这类工程名，非研发读不懂。
 * 英文原名保留在 title 与括号副标题里，排障时仍能一一对应。
 */
const RECIPE_CN: Record<string, string> = {
  left_diagnosis: '左侧诊断',
  deep_diagnosis: '深度诊断',
  chip_analyst: '筹码分析',
  postmarket_reviewer: '盘后复盘',
  demon_diagnosis: '妖股诊断',
  intraday_watcher: '盘中监测',
}

function recipeLabel(name: string | null | undefined): string {
  if (!name) return '默认配方'
  const cn = RECIPE_CN[name]
  return cn ? `${cn}（${name}）` : name
}

export default function CopilotGovernancePage() {
  const [tab, setTab] = useState<Tab>('traces')

  return (
    <div className="space-y-3" data-testid="copilot-governance-page">
      <header className="flex flex-wrap items-center gap-2">
        <h1 className="text-[16px] font-medium">数智参谋 · 治理</h1>
        <span className="text-[12px] text-muted-foreground">轨迹 / 成本 / 配额（客观证据 · 不构成买卖指令）</span>
        <div className="ml-auto flex items-center gap-1">
          {TABS.map((t) => (
            <button
              key={t.id}
              type="button"
              title={t.hint}
              onClick={() => setTab(t.id)}
              className={`rounded-md border px-2.5 py-1 text-[12px] ${
                tab === t.id
                  ? 'border-role-watch/60 bg-role-watch/10 text-foreground'
                  : 'border-border text-muted-foreground hover:text-foreground'
              }`}
            >
              {t.label}
            </button>
          ))}
        </div>
      </header>

      {tab === 'traces' ? <TracesTab /> : null}
      {tab === 'cost' ? <CostTab /> : null}
      {tab === 'quota' ? <QuotaTab /> : null}
      {tab === 'memory' ? <MemoryTab /> : null}
      {tab === 'schedule' ? <ScheduleTab /> : null}
    </div>
  )
}

function TracesTab() {
  const [rows, setRows] = useState<CopilotRunRow[]>([])
  const [metrics, setMetrics] = useState<CopilotMetrics | null>(null)
  const [loading, setLoading] = useState(true)
  const [detail, setDetail] = useState<CopilotRunDetail | null>(null)
  const [detailLoading, setDetailLoading] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      setRows(await getCopilotRuns(50))
    } catch {
      setRows([])
    } finally {
      setLoading(false)
    }
    // 质量拆解单独一段取：它挂了不该把轨迹列表一起抹掉（两件事的可用性互不绑定）。
    try {
      setMetrics(await getCopilotMetrics(7))
    } catch {
      setMetrics(null)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const open = useCallback(async (runId: string) => {
    setDetailLoading(true)
    try {
      setDetail(await getCopilotRunDetail(runId))
    } catch {
      setDetail(null)
    } finally {
      setDetailLoading(false)
    }
  }, [])

  return (
    <div className="space-y-3">
      {/* P3：按配方质量拆解。没有这层，就只能看到「总 ok 率」，看不出是哪份配方在坏。 */}
      {metrics && metrics.by_recipe.length > 0 ? (
        <section className="rounded-xl border border-border bg-s1" data-testid="recipe-health">
          <div className="flex items-baseline justify-between gap-2 border-b border-border px-3 py-2">
            <span className="text-[12px]">按配方质量 · 近 7 天</span>
            <span className="text-[12px] text-muted-foreground">
              总计 <span className="font-mono tabular-nums text-foreground">{num(metrics.runs)}</span> 轮 · 降级率{' '}
              <span className="font-mono tabular-nums text-foreground">{rate(metrics.degraded_rate)}</span> · 零工具{' '}
              <span className="font-mono tabular-nums text-foreground">{num(metrics.zero_tool_runs)}</span> 轮
            </span>
          </div>
          <table className="w-full text-[12px]">
            <thead className="text-muted-foreground">
              <tr>
                <th className="px-2 py-1.5 text-left font-normal">配方</th>
                <th className="px-2 py-1.5 text-right font-normal">运行</th>
                <th className="px-2 py-1.5 text-right font-normal">ok</th>
                <th className="px-2 py-1.5 text-right font-normal">降级</th>
                <th className="px-2 py-1.5 text-right font-normal">错误</th>
                <th className="px-2 py-1.5 text-right font-normal">零工具</th>
                <th className="px-2 py-1.5 text-right font-normal">均耗时</th>
              </tr>
            </thead>
            <tbody>
              {metrics.by_recipe.map((r) => (
                <tr key={r.recipe} className="border-t border-border" data-testid="recipe-health-row">
                  <td
                    className="max-w-[200px] truncate px-2 py-1.5 font-mono text-foreground"
                    title={r.recipe}
                  >
                    {recipeLabel(r.recipe)}
                  </td>
                  <td className="px-2 py-1.5 text-right font-mono tabular-nums">{num(r.runs)}</td>
                  <td className="px-2 py-1.5 text-right font-mono tabular-nums">{rate(r.ok_rate)}</td>
                  <td
                    className={`px-2 py-1.5 text-right font-mono tabular-nums ${
                      r.degraded_runs > 0 ? 'text-role-watch' : 'text-muted-foreground'
                    }`}
                  >
                    {num(r.degraded_runs)}
                  </td>
                  <td
                    className={`px-2 py-1.5 text-right font-mono tabular-nums ${
                      r.error_runs > 0 ? 'text-role-risk' : 'text-muted-foreground'
                    }`}
                  >
                    {num(r.error_runs)}
                  </td>
                  <td
                    className={`px-2 py-1.5 text-right font-mono tabular-nums ${
                      r.zero_tool_runs > 0 ? 'text-role-watch' : 'text-muted-foreground'
                    }`}
                  >
                    {num(r.zero_tool_runs)}
                  </td>
                  <td className="px-2 py-1.5 text-right font-mono tabular-nums text-muted-foreground">
                    {ms(r.avg_elapsed_ms)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      ) : null}

      <div className="grid gap-3 lg:grid-cols-[1.1fr_1fr]">
          <section className="rounded-xl border border-border bg-s1">
            <div className="flex items-center justify-between border-b border-border px-3 py-2">
              <span className="text-[12px]">最近 50 轮</span>
              <button
                type="button"
                className="flex items-center gap-1 text-[12px] text-muted-foreground"
                onClick={() => void load()}
              >
                <RefreshCw className="h-3 w-3" /> 刷新
              </button>
          </div>
          {loading ? (
            <div className="flex items-center gap-2 px-3 py-6 text-[12px] text-muted-foreground">
              <Loader2 className="h-3 w-3 animate-spin" /> 读取中…
            </div>
          ) : rows.length === 0 ? (
            <div className="px-3 py-6 text-[12px] text-muted-foreground">还没有轨迹（提问一次就会产生）</div>
          ) : (
            <div className="max-h-[62vh] overflow-y-auto">
              <table className="w-full text-[12px]">
                <thead className="sticky top-0 bg-s1 text-muted-foreground">
                  <tr>
                    <th className="px-2 py-1.5 text-left font-normal">时间</th>
                    <th className="px-2 py-1.5 text-left font-normal">配方</th>
                    <th className="px-2 py-1.5 text-left font-normal">状态</th>
                    <th className="px-2 py-1.5 text-left font-normal">问题</th>
                    <th className="px-2 py-1.5 text-right font-normal">工具</th>
                    <th className="px-2 py-1.5 text-right font-normal">耗时</th>
                    <th className="px-2 py-1.5 text-right font-normal">token</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((r) => (
                    <tr
                      key={r.run_id}
                      className="cursor-pointer border-t border-border hover:bg-s2"
                      onClick={() => void open(r.run_id)}
                    >
                      <td className="whitespace-nowrap px-2 py-1.5 font-mono text-muted-foreground">
                        {String(r.created_at || '').slice(5, 16)}
                      </td>
                      <td
                        className="max-w-[110px] truncate px-2 py-1.5 font-mono text-muted-foreground"
                        title={r.recipe || '(默认)'}
                      >
                        {recipeLabel(r.recipe)}
                      </td>
                      <td className={`px-2 py-1.5 font-mono ${statusClass(r.status)}`}>{r.status || '--'}</td>
                      <td className="max-w-[240px] truncate px-2 py-1.5">{r.question || '—'}</td>
                      <td className="px-2 py-1.5 text-right font-mono">{r.tool_calls ?? '--'}</td>
                      <td className="px-2 py-1.5 text-right font-mono">{ms(r.elapsed_ms)}</td>
                      <td className="px-2 py-1.5 text-right font-mono">{num(r.tokens)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>

        <section className="rounded-xl border border-border bg-s1">
          <div className="border-b border-border px-3 py-2 text-[12px]">回放</div>
          {detailLoading ? (
            <div className="flex items-center gap-2 px-3 py-6 text-[12px] text-muted-foreground">
              <Loader2 className="h-3 w-3 animate-spin" /> 读取中…
            </div>
          ) : !detail ? (
            <div className="px-3 py-6 text-[12px] text-muted-foreground">点左侧任意一行看这轮怎么跑的</div>
          ) : (
            <div className="max-h-[62vh] space-y-2 overflow-y-auto px-3 py-2">
              <div className="space-y-0.5 text-[12px]">
                <div className="text-muted-foreground">
                  {detail.run.created_at} · 状态 {detail.run.status} · {detail.run.tool_calls} 个工具 · {ms(detail.run.elapsed_ms)}
                </div>
                <div className="text-foreground">{detail.run.question || '（无问题文本）'}</div>
              </div>
              {detail.steps.length === 0 ? (
                <div className="text-[12px] text-muted-foreground">这轮没有工具调用（纯文本回答）</div>
              ) : (
                <ol className="space-y-1.5">
                  {detail.steps.map((s) => (
                    <li key={s.seq} className="rounded-md border border-border px-2 py-1.5">
                      <div className="flex items-center gap-1.5 text-[12px]">
                        <span className="font-mono text-muted-foreground">#{s.seq}</span>
                        <span className="font-medium">{s.tool}</span>
                        <span className={s.ok ? 'text-role-up' : 'text-role-risk'}>{s.ok ? 'ok' : '失败'}</span>
                        <span className="ml-auto font-mono text-muted-foreground">{ms(s.elapsed_ms)}</span>
                      </div>
                      {s.args ? (
                        <div className="mt-1 truncate font-mono text-[12px] text-muted-foreground" title={s.args}>
                          args: {s.args}
                        </div>
                      ) : null}
                      {s.result_digest ? (
                        <div className="mt-1 whitespace-pre-wrap break-words text-[12px] text-muted-foreground">
                          {s.result_digest}
                        </div>
                      ) : null}
                    </li>
                  ))}
                </ol>
              )}
            </div>
          )}
        </section>
      </div>
    </div>
  )
}

function CostTab() {
  const [days, setDays] = useState(7)
  const [data, setData] = useState<CopilotCost | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let alive = true
    setLoading(true)
    getCopilotCost(days)
      .then((d) => alive && setData(d))
      .catch(() => alive && setData(null))
      .finally(() => alive && setLoading(false))
    return () => {
      alive = false
    }
  }, [days])

  const maxTokens = Math.max(1, ...(data?.by_day || []).map((d) => d.tokens))

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2">
        {[7, 30].map((d) => (
          <button
            key={d}
            type="button"
            onClick={() => setDays(d)}
            className={`rounded-md border px-2 py-0.5 text-[12px] ${
              days === d ? 'border-role-watch/60 text-foreground' : 'border-border text-muted-foreground'
            }`}
          >
            近 {d} 天
          </button>
        ))}
        <span className="ml-auto text-[12px] text-muted-foreground">
          合计 token <span className="font-mono text-foreground">{num(data?.total_tokens)}</span> · 调用{' '}
          <span className="font-mono text-foreground">{num(data?.total_calls)}</span>
        </span>
      </div>

      {loading ? (
        <div className="flex items-center gap-2 text-[12px] text-muted-foreground">
          <Loader2 className="h-3 w-3 animate-spin" /> 读取中…
        </div>
      ) : !data ? (
        <div className="text-[12px] text-muted-foreground">成本数据读取失败</div>
      ) : (
        <>
          {data.unpriced_models.length > 0 ? (
            <div className="rounded-md border border-border px-2 py-1.5 text-[12px] text-muted-foreground">
              未配置单价的模型：{data.unpriced_models.join('、')} —— 这些的金额显示 `--`，不估算
            </div>
          ) : null}

          <section className="rounded-xl border border-border bg-s1 p-3">
            <div className="mb-2 text-[12px]">按日 token</div>
            <div className="flex h-24 items-end gap-1">
              {(data.by_day || []).map((d) => (
                <div key={d.date} className="flex-1" title={`${d.date}: ${num(d.tokens)} tokens`}>
                  <div
                    className="w-full rounded-t bg-role-watch/60"
                    style={{ height: `${Math.max(2, (d.tokens / maxTokens) * 90)}px` }}
                  />
                  <div className="mt-1 text-center text-[12px] text-muted-foreground">{String(d.date).slice(5)}</div>
                </div>
              ))}
            </div>
          </section>

          <div className="grid gap-3 md:grid-cols-2">
            <section className="rounded-xl border border-border bg-s1">
              <div className="border-b border-border px-3 py-2 text-[12px]">按模型</div>
              <table className="w-full text-[12px]">
                <tbody>
                  {(data.by_model || []).map((m) => (
                    <tr key={m.model} className="border-t border-border">
                      <td className="px-2 py-1.5 font-mono">{m.model}</td>
                      <td className="px-2 py-1.5 text-right font-mono">{num(m.tokens)}</td>
                      <td className="px-2 py-1.5 text-right font-mono text-muted-foreground">
                        {m.cost == null ? '--' : `¥${safeFixed(m.cost, 2)}`}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </section>

            <section className="rounded-xl border border-border bg-s1">
              <div className="border-b border-border px-3 py-2 text-[12px]">
                按用户{data.scope === 'self' ? '（仅自己）' : ''}
              </div>
              <table className="w-full text-[12px]">
                <tbody>
                  {(data.by_user || []).map((u) => (
                    <tr key={u.user_id} className="border-t border-border">
                      <td className="max-w-[160px] truncate px-2 py-1.5 font-mono">{u.user_id}</td>
                      <td className="px-2 py-1.5 text-right font-mono">{num(u.tokens)}</td>
                      <td className="px-2 py-1.5 text-right font-mono text-muted-foreground">
                        {u.cost == null ? '--' : `¥${safeFixed(u.cost, 2)}`}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </section>
          </div>
        </>
      )}
    </div>
  )
}

function QuotaTab() {
  const [rows, setRows] = useState<CopilotQuotaRow[]>([])
  const [scope, setScope] = useState('')
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  // 没有配额行的用户是「不限」，管理页必须能给他新增一行，否则这个功能等于没有入口
  const [users, setUsers] = useState<{ id: string; username: string }[]>([])
  const [pick, setPick] = useState('')
  const [newRuns, setNewRuns] = useState('')
  const [newTokens, setNewTokens] = useState('')

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const res = await getCopilotQuotas()
      setRows(res.items || [])
      setScope(res.scope || '')
    } catch {
      setRows([])
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  useEffect(() => {
    if (scope !== 'all') return
    authApi
      .listUsers()
      .then((res) => {
        const list = (res?.users || []).map((u: { id: string; username: string }) => ({
          id: u.id,
          username: u.username,
        }))
        setUsers(list)
        setPick((prev) => prev || list[0]?.id || '')
      })
      .catch(() => setUsers([]))
  }, [scope])

  const nameOf = useCallback(
    (uid: string) => users.find((u) => u.id === uid)?.username || uid.slice(0, 8),
    [users],
  )

  const save = useCallback(
    async (row: CopilotQuotaRow, patch: Partial<CopilotQuotaRow>) => {
      setSaving(row.user_id)
      setError(null)
      const next = { ...row, ...patch }
      try {
        const saved = await putCopilotQuota(row.user_id, {
          daily_runs: next.daily_runs ?? null,
          daily_tokens: next.daily_tokens ?? null,
          enabled: next.enabled,
          note: next.note || undefined,
        })
        setRows((prev) => prev.map((r) => (r.user_id === row.user_id ? { ...r, ...saved } : r)))
      } catch (e) {
        setError(e instanceof Error ? e.message : '保存失败')
      } finally {
        setSaving(null)
      }
    },
    [],
  )

  if (loading) {
    return (
      <div className="flex items-center gap-2 text-[12px] text-muted-foreground">
        <Loader2 className="h-3 w-3 animate-spin" /> 读取中…
      </div>
    )
  }

  return (
    <div className="space-y-2">
      <div className="text-[12px] text-muted-foreground">
        没有记录 = 不限。填 0 表示该维度禁止。超限时接口直接 429，不靠提示词提醒。
        {scope === 'self' ? '（普通账号只能看自己）' : ''}
      </div>
      {error ? <div className="text-[12px] text-role-risk">{error}</div> : null}
      {scope === 'all' && users.length > 0 ? (
        <div className="flex flex-wrap items-center gap-2 rounded-md border border-border px-2 py-1.5">
          <span className="text-[12px] text-muted-foreground">新增/覆盖配额</span>
          <select
            className="rounded border border-border bg-transparent px-1 py-0.5 text-[12px]"
            value={pick}
            onChange={(e) => setPick(e.target.value)}
          >
            {users.map((u) => (
              <option key={u.id} value={u.id}>
                {u.username}
              </option>
            ))}
          </select>
          <input
            className="w-20 rounded border border-border bg-transparent px-1 py-0.5 text-[12px]"
            placeholder="轮数/天"
            value={newRuns}
            onChange={(e) => setNewRuns(e.target.value.replace(/[^0-9]/g, ''))}
          />
          <input
            className="w-28 rounded border border-border bg-transparent px-1 py-0.5 text-[12px]"
            placeholder="tokens/天"
            value={newTokens}
            onChange={(e) => setNewTokens(e.target.value.replace(/[^0-9]/g, ''))}
          />
          <button
            type="button"
            className="flex items-center gap-1 rounded border border-border px-2 py-0.5 text-[12px] text-muted-foreground hover:text-foreground"
            disabled={!pick || saving !== null}
            onClick={() =>
              void save(
                { user_id: pick, daily_runs: null, daily_tokens: null, enabled: true },
                {
                  daily_runs: newRuns === '' ? null : Number(newRuns),
                  daily_tokens: newTokens === '' ? null : Number(newTokens),
                },
              )
            }
          >
            <Plus className="h-3 w-3" /> 保存
          </button>
        </div>
      ) : null}

      {rows.length === 0 ? (
        <div className="text-[12px] text-muted-foreground">还没有任何配额记录（全员不限）</div>
      ) : (
        <table className="w-full text-[12px]">
          <thead className="text-muted-foreground">
            <tr>
              <th className="px-2 py-1.5 text-left font-normal">用户</th>
              <th className="px-2 py-1.5 text-right font-normal">今日轮数 / 上限</th>
              <th className="px-2 py-1.5 text-right font-normal">今日 token / 上限</th>
              <th className="px-2 py-1.5 text-center font-normal">启用</th>
              <th className="px-2 py-1.5 text-right font-normal">操作</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <QuotaRow
                key={r.user_id}
                row={r}
                saving={saving === r.user_id}
                onSave={save}
                nameOf={nameOf}
              />
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}

function QuotaRow({
  row,
  saving,
  onSave,
  nameOf,
}: {
  row: CopilotQuotaRow
  saving: boolean
  onSave: (row: CopilotQuotaRow, patch: Partial<CopilotQuotaRow>) => Promise<void>
  nameOf: (uid: string) => string
}) {
  const [runs, setRuns] = useState(row.daily_runs == null ? '' : String(row.daily_runs))
  const [tokens, setTokens] = useState(row.daily_tokens == null ? '' : String(row.daily_tokens))

  return (
    <tr className="border-t border-border">
      <td className="max-w-[180px] truncate px-2 py-1.5 font-mono" title={row.user_id}>
        {nameOf(row.user_id)}
      </td>
      <td className="px-2 py-1.5 text-right font-mono">
        {num(row.used_runs)} /{' '}
        <input
          className="w-16 rounded border border-border bg-transparent px-1 text-right font-mono"
          value={runs}
          placeholder="不限"
          onChange={(e) => setRuns(e.target.value.replace(/[^0-9]/g, ''))}
        />
      </td>
      <td className="px-2 py-1.5 text-right font-mono">
        {num(row.used_tokens)} /{' '}
        <input
          className="w-24 rounded border border-border bg-transparent px-1 text-right font-mono"
          value={tokens}
          placeholder="不限"
          onChange={(e) => setTokens(e.target.value.replace(/[^0-9]/g, ''))}
        />
      </td>
      <td className="px-2 py-1.5 text-center">
        <input
          type="checkbox"
          checked={row.enabled}
          onChange={(e) => void onSave(row, { enabled: e.target.checked })}
        />
      </td>
      <td className="px-2 py-1.5 text-right">
        <button
          type="button"
          className="rounded border border-border px-2 py-0.5 text-[12px] text-muted-foreground hover:text-foreground"
          disabled={saving}
          onClick={() =>
            void onSave(row, {
              daily_runs: runs === '' ? null : Number(runs),
              daily_tokens: tokens === '' ? null : Number(tokens),
            })
          }
        >
          {saving ? '保存中…' : '保存'}
        </button>
      </td>
    </tr>
  )
}

const KIND_LABEL: Record<string, string> = {
  preference: '偏好',
  rule: '规则',
  fact: '事实',
  skill: '流程',
}

function MemoryTab() {
  const [rows, setRows] = useState<CopilotMemoryRow[]>([])
  const [loading, setLoading] = useState(true)
  const [text, setText] = useState('')
  const [kind, setKind] = useState('preference')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      setRows(await getMemories())
    } catch {
      setRows([])
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const add = useCallback(async () => {
    const content = text.trim()
    if (content.length < 2) return
    setBusy(true)
    setError(null)
    try {
      const row = await postMemory(content, kind, 7)
      setRows((prev) => [row, ...prev])
      setText('')
    } catch (e) {
      setError(e instanceof Error ? e.message : '保存失败')
    } finally {
      setBusy(false)
    }
  }, [text, kind])

  return (
    <div className="space-y-2">
      <div className="text-[12px] text-muted-foreground">
        只记你**明确说过**的（「记住…」「以后都…」「我的口径是…」）。删掉就不再注入；权重 ≥9 的每条都会带上。
      </div>
      <div className="flex flex-wrap items-center gap-2 rounded-md border border-border px-2 py-1.5">
        <select
          className="rounded border border-border bg-transparent px-1 py-0.5 text-[12px]"
          value={kind}
          onChange={(e) => setKind(e.target.value)}
        >
          {Object.entries(KIND_LABEL).map(([k, v]) => (
            <option key={k} value={k}>{v}</option>
          ))}
        </select>
        <input
          className="min-w-[240px] flex-1 rounded border border-border bg-transparent px-2 py-0.5 text-[12px]"
          placeholder="例如：我只做主板，不碰北交所"
          value={text}
          onChange={(e) => setText(e.target.value)}
        />
        <button
          type="button"
          className="rounded border border-border px-2 py-0.5 text-[12px] text-muted-foreground hover:text-foreground"
          disabled={busy || text.trim().length < 2}
          onClick={() => void add()}
        >
          {busy ? '保存中…' : '记住'}
        </button>
      </div>
      {error ? <div className="text-[12px] text-role-risk">{error}</div> : null}
      {loading ? (
        <div className="flex items-center gap-2 text-[12px] text-muted-foreground">
          <Loader2 className="h-3 w-3 animate-spin" /> 读取中…
        </div>
      ) : rows.length === 0 ? (
        <div className="text-[12px] text-muted-foreground">还没有记忆。下次直接说「记住…」就会自动记下。</div>
      ) : (
        <ul className="space-y-1">
          {rows.map((r) => (
            <li key={r.id} className="flex items-start gap-2 rounded-md border border-border px-2 py-1.5">
              <span className="mt-0.5 rounded bg-s2 px-1 text-[12px] text-muted-foreground">
                {KIND_LABEL[r.kind] || r.kind}
              </span>
              <span className="min-w-0 flex-1 text-[12px]">{r.content}</span>
              <span className="font-mono text-[12px] text-muted-foreground">w{r.weight} · 用{r.hits}次</span>
              <button
                type="button"
                className="text-[12px] text-muted-foreground hover:text-foreground"
                onClick={() => void patchMemory(r.id, false).then(() => setRows((p) => p.filter((x) => x.id !== r.id)))}
              >
                停用
              </button>
              <button
                type="button"
                className="text-[12px] text-muted-foreground hover:text-role-risk"
                onClick={() => void deleteMemory(r.id).then(() => setRows((p) => p.filter((x) => x.id !== r.id)))}
              >
                删
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

function ScheduleTab() {
  const [rows, setRows] = useState<CopilotScheduleRow[]>([])
  const [loading, setLoading] = useState(true)
  const [kind, setKind] = useState('auction_digest')
  const [at, setAt] = useState('09:25')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      setRows(await getSchedules())
    } catch {
      setRows([])
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const add = useCallback(async () => {
    setBusy(true)
    setError(null)
    try {
      const row = await postSchedule(kind, at, 'mon-fri')
      setRows((prev) => [row, ...prev])
    } catch (e) {
      setError(e instanceof Error ? e.message : '创建失败')
    } finally {
      setBusy(false)
    }
  }, [kind, at])

  return (
    <div className="space-y-2">
      <div className="text-[12px] text-muted-foreground">
        只在交易日执行；结果写成一条消息进会话。到点时若取不到数据，不写空内容、也不标记为已跑。
      </div>
      <div className="flex flex-wrap items-center gap-2 rounded-md border border-border px-2 py-1.5">
        <select
          className="rounded border border-border bg-transparent px-1 py-0.5 text-[12px]"
          value={kind}
          onChange={(e) => setKind(e.target.value)}
        >
          <option value="auction_digest">竞价摘要</option>
          <option value="daily_brief">收盘小结</option>
        </select>
        <input
          className="w-20 rounded border border-border bg-transparent px-1 py-0.5 text-[12px]"
          value={at}
          onChange={(e) => setAt(e.target.value)}
          placeholder="09:25"
        />
        <span className="text-[12px] text-muted-foreground">周一至周五</span>
        <button
          type="button"
          className="rounded border border-border px-2 py-0.5 text-[12px] text-muted-foreground hover:text-foreground"
          disabled={busy}
          onClick={() => void add()}
        >
          {busy ? '创建中…' : '新建'}
        </button>
      </div>
      {error ? <div className="text-[12px] text-role-risk">{error}</div> : null}
      {loading ? (
        <div className="flex items-center gap-2 text-[12px] text-muted-foreground">
          <Loader2 className="h-3 w-3 animate-spin" /> 读取中…
        </div>
      ) : rows.length === 0 ? (
        <div className="text-[12px] text-muted-foreground">还没有定时任务。也可以在对话里说「每天 9:25 帮我看竞价」。</div>
      ) : (
        <table className="w-full text-[12px]">
          <thead className="text-muted-foreground">
            <tr>
              <th className="px-2 py-1.5 text-left font-normal">类型</th>
              <th className="px-2 py-1.5 text-left font-normal">时间</th>
              <th className="px-2 py-1.5 text-left font-normal">星期</th>
              <th className="px-2 py-1.5 text-left font-normal">上次</th>
              <th className="px-2 py-1.5 text-right font-normal">操作</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.id} className="border-t border-border">
                <td className="px-2 py-1.5">{r.kind === 'auction_digest' ? '竞价摘要' : '收盘小结'}</td>
                <td className="px-2 py-1.5 font-mono">{r.at_hhmm}</td>
                <td className="px-2 py-1.5 text-muted-foreground">{r.weekdays}</td>
                <td className="px-2 py-1.5 font-mono text-muted-foreground">{r.last_run_date || '--'}</td>
                <td className="px-2 py-1.5 text-right">
                  <button
                    type="button"
                    className="mr-2 text-[12px] text-muted-foreground hover:text-foreground"
                    onClick={() =>
                      void patchSchedule(r.id, !r.enabled).then(() =>
                        setRows((p) => p.map((x) => (x.id === r.id ? { ...x, enabled: !x.enabled } : x))),
                      )
                    }
                  >
                    {r.enabled ? '停用' : '启用'}
                  </button>
                  <button
                    type="button"
                    className="text-[12px] text-muted-foreground hover:text-role-risk"
                    onClick={() => void deleteSchedule(r.id).then(() => setRows((p) => p.filter((x) => x.id !== r.id)))}
                  >
                    删
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}
