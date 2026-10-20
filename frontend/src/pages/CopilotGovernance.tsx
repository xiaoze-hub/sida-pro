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
  getCopilotCost,
  getCopilotQuotas,
  getCopilotRunDetail,
  getCopilotRuns,
  putCopilotQuota,
  type CopilotCost,
  type CopilotQuotaRow,
  type CopilotRunDetail,
  type CopilotRunRow,
} from '@/lib/copilot/api'

type Tab = 'traces' | 'cost' | 'quota'

const TABS: { id: Tab; label: string; hint: string }[] = [
  { id: 'traces', label: '轨迹回放', hint: '每轮问答用了哪些工具、花了多久' },
  { id: 'cost', label: '成本', hint: 'token 与估算费用（未配单价显示 --）' },
  { id: 'quota', label: '配额', hint: '按人限额；没有记录=不限' },
]

function ms(v: number | null | undefined) {
  return v == null ? '--' : `${safeFixed(v / 1000, 1)}s`
}

function num(v: number | null | undefined) {
  return v == null ? '--' : v.toLocaleString('zh-CN')
}

export default function CopilotGovernancePage() {
  const [tab, setTab] = useState<Tab>('traces')

  return (
    <div className="space-y-3" data-testid="copilot-governance-page">
      <header className="flex flex-wrap items-center gap-2">
        <h1 className="text-[16px] font-medium">数智参谋 · 治理</h1>
        <span className="text-[11px] text-muted-foreground">轨迹 / 成本 / 配额（客观证据 · 不构成买卖指令）</span>
        <div className="ml-auto flex items-center gap-1">
          {TABS.map((t) => (
            <button
              key={t.id}
              type="button"
              title={t.hint}
              onClick={() => setTab(t.id)}
              className={`rounded-md border px-2.5 py-1 text-[11px] ${
                tab === t.id
                  ? 'border-role-watch/60 bg-role-watch/10 text-foreground'
                  : 'border-border/60 text-muted-foreground hover:text-foreground'
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
    </div>
  )
}

function TracesTab() {
  const [rows, setRows] = useState<CopilotRunRow[]>([])
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
    <div className="grid gap-3 lg:grid-cols-[1.1fr_1fr]">
      <section className="rounded-xl border border-border/60 bg-s1">
        <div className="flex items-center justify-between border-b border-border/40 px-3 py-2">
          <span className="text-[12px]">最近 50 轮</span>
          <button type="button" className="flex items-center gap-1 text-[11px] text-muted-foreground" onClick={() => void load()}>
            <RefreshCw className="h-3 w-3" /> 刷新
          </button>
        </div>
        {loading ? (
          <div className="flex items-center gap-2 px-3 py-6 text-[11px] text-muted-foreground">
            <Loader2 className="h-3 w-3 animate-spin" /> 读取中…
          </div>
        ) : rows.length === 0 ? (
          <div className="px-3 py-6 text-[11px] text-muted-foreground">还没有轨迹（提问一次就会产生）</div>
        ) : (
          <div className="max-h-[62vh] overflow-y-auto">
            <table className="w-full text-[11px]">
              <thead className="sticky top-0 bg-s1 text-muted-foreground">
                <tr>
                  <th className="px-2 py-1.5 text-left font-normal">时间</th>
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
                    className="cursor-pointer border-t border-border/30 hover:bg-s2"
                    onClick={() => void open(r.run_id)}
                  >
                    <td className="whitespace-nowrap px-2 py-1.5 font-mono text-muted-foreground">
                      {String(r.created_at || '').slice(5, 16)}
                    </td>
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

      <section className="rounded-xl border border-border/60 bg-s1">
        <div className="border-b border-border/40 px-3 py-2 text-[12px]">回放</div>
        {detailLoading ? (
          <div className="flex items-center gap-2 px-3 py-6 text-[11px] text-muted-foreground">
            <Loader2 className="h-3 w-3 animate-spin" /> 读取中…
          </div>
        ) : !detail ? (
          <div className="px-3 py-6 text-[11px] text-muted-foreground">点左侧任意一行看这轮怎么跑的</div>
        ) : (
          <div className="max-h-[62vh] space-y-2 overflow-y-auto px-3 py-2">
            <div className="space-y-0.5 text-[11px]">
              <div className="text-muted-foreground">
                {detail.run.created_at} · 状态 {detail.run.status} · {detail.run.tool_calls} 个工具 · {ms(detail.run.elapsed_ms)}
              </div>
              <div className="text-foreground">{detail.run.question || '（无问题文本）'}</div>
            </div>
            {detail.steps.length === 0 ? (
              <div className="text-[11px] text-muted-foreground">这轮没有工具调用（纯文本回答）</div>
            ) : (
              <ol className="space-y-1.5">
                {detail.steps.map((s) => (
                  <li key={s.seq} className="rounded-md border border-border/50 px-2 py-1.5">
                    <div className="flex items-center gap-1.5 text-[11px]">
                      <span className="font-mono text-muted-foreground">#{s.seq}</span>
                      <span className="font-medium">{s.tool}</span>
                      <span className={s.ok ? 'text-role-up' : 'text-role-risk'}>{s.ok ? 'ok' : '失败'}</span>
                      <span className="ml-auto font-mono text-muted-foreground">{ms(s.elapsed_ms)}</span>
                    </div>
                    {s.args ? (
                      <div className="mt-1 truncate font-mono text-[10px] text-muted-foreground" title={s.args}>
                        args: {s.args}
                      </div>
                    ) : null}
                    {s.result_digest ? (
                      <div className="mt-1 whitespace-pre-wrap break-words text-[10px] text-muted-foreground/90">
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
            className={`rounded-md border px-2 py-0.5 text-[11px] ${
              days === d ? 'border-role-watch/60 text-foreground' : 'border-border/60 text-muted-foreground'
            }`}
          >
            近 {d} 天
          </button>
        ))}
        <span className="ml-auto text-[11px] text-muted-foreground">
          合计 token <span className="font-mono text-foreground">{num(data?.total_tokens)}</span> · 调用{' '}
          <span className="font-mono text-foreground">{num(data?.total_calls)}</span>
        </span>
      </div>

      {loading ? (
        <div className="flex items-center gap-2 text-[11px] text-muted-foreground">
          <Loader2 className="h-3 w-3 animate-spin" /> 读取中…
        </div>
      ) : !data ? (
        <div className="text-[11px] text-muted-foreground">成本数据读取失败</div>
      ) : (
        <>
          {data.unpriced_models.length > 0 ? (
            <div className="rounded-md border border-border/50 px-2 py-1.5 text-[11px] text-muted-foreground">
              未配置单价的模型：{data.unpriced_models.join('、')} —— 这些的金额显示 `--`，不估算
            </div>
          ) : null}

          <section className="rounded-xl border border-border/60 bg-s1 p-3">
            <div className="mb-2 text-[12px]">按日 token</div>
            <div className="flex h-24 items-end gap-1">
              {(data.by_day || []).map((d) => (
                <div key={d.date} className="flex-1" title={`${d.date}: ${num(d.tokens)} tokens`}>
                  <div
                    className="w-full rounded-t bg-role-watch/60"
                    style={{ height: `${Math.max(2, (d.tokens / maxTokens) * 90)}px` }}
                  />
                  <div className="mt-1 text-center text-[10px] text-muted-foreground">{String(d.date).slice(5)}</div>
                </div>
              ))}
            </div>
          </section>

          <div className="grid gap-3 md:grid-cols-2">
            <section className="rounded-xl border border-border/60 bg-s1">
              <div className="border-b border-border/40 px-3 py-2 text-[12px]">按模型</div>
              <table className="w-full text-[11px]">
                <tbody>
                  {(data.by_model || []).map((m) => (
                    <tr key={m.model} className="border-t border-border/30">
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

            <section className="rounded-xl border border-border/60 bg-s1">
              <div className="border-b border-border/40 px-3 py-2 text-[12px]">
                按用户{data.scope === 'self' ? '（仅自己）' : ''}
              </div>
              <table className="w-full text-[11px]">
                <tbody>
                  {(data.by_user || []).map((u) => (
                    <tr key={u.user_id} className="border-t border-border/30">
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
      <div className="flex items-center gap-2 text-[11px] text-muted-foreground">
        <Loader2 className="h-3 w-3 animate-spin" /> 读取中…
      </div>
    )
  }

  return (
    <div className="space-y-2">
      <div className="text-[11px] text-muted-foreground">
        没有记录 = 不限。填 0 表示该维度禁止。超限时接口直接 429，不靠提示词提醒。
        {scope === 'self' ? '（普通账号只能看自己）' : ''}
      </div>
      {error ? <div className="text-[11px] text-role-risk">{error}</div> : null}
      {scope === 'all' && users.length > 0 ? (
        <div className="flex flex-wrap items-center gap-2 rounded-md border border-border/50 px-2 py-1.5">
          <span className="text-[11px] text-muted-foreground">新增/覆盖配额</span>
          <select
            className="rounded border border-border/60 bg-transparent px-1 py-0.5 text-[11px]"
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
            className="w-20 rounded border border-border/60 bg-transparent px-1 py-0.5 text-[11px]"
            placeholder="轮数/天"
            value={newRuns}
            onChange={(e) => setNewRuns(e.target.value.replace(/[^0-9]/g, ''))}
          />
          <input
            className="w-28 rounded border border-border/60 bg-transparent px-1 py-0.5 text-[11px]"
            placeholder="tokens/天"
            value={newTokens}
            onChange={(e) => setNewTokens(e.target.value.replace(/[^0-9]/g, ''))}
          />
          <button
            type="button"
            className="flex items-center gap-1 rounded border border-border/60 px-2 py-0.5 text-[11px] text-muted-foreground hover:text-foreground"
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
        <div className="text-[11px] text-muted-foreground">还没有任何配额记录（全员不限）</div>
      ) : (
        <table className="w-full text-[11px]">
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
    <tr className="border-t border-border/30">
      <td className="max-w-[180px] truncate px-2 py-1.5 font-mono" title={row.user_id}>
        {nameOf(row.user_id)}
      </td>
      <td className="px-2 py-1.5 text-right font-mono">
        {num(row.used_runs)} /{' '}
        <input
          className="w-16 rounded border border-border/60 bg-transparent px-1 text-right font-mono"
          value={runs}
          placeholder="不限"
          onChange={(e) => setRuns(e.target.value.replace(/[^0-9]/g, ''))}
        />
      </td>
      <td className="px-2 py-1.5 text-right font-mono">
        {num(row.used_tokens)} /{' '}
        <input
          className="w-24 rounded border border-border/60 bg-transparent px-1 text-right font-mono"
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
          className="rounded border border-border/60 px-2 py-0.5 text-[11px] text-muted-foreground hover:text-foreground"
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
