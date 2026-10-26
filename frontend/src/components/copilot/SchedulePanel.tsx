/**
 * 定时任务面板（2026-10-22 新增，AI 参谋工作台）。
 *
 * 为什么单独成面板：定时任务是**用户下指令、系统到点执行**的合约，
 * 必须能被用户看见「排了什么、什么时候、上次跑没跑」，并且能停能删。
 *
 * 设计口径（三条硬约束）：
 *  ① **只读展示跑过什么** —— 显示后端给的 `last_run_date`（YYYYMMDD 原样格式化），
 *     **不编造**「下次触发时间」：后端没这个字段，编出来就是骗人。
 *     「下次触发」只在**能真算出来**时给：即 `last_run_date` 已知 ⇒ 按工作日推进可推；
 *     推不出来（last_run_date 为空 / weekdays 不可解析）⇒ 显示 `--`。
 *  ② **空值不补 0** —— 时间/上次都没跑过就是 `--`，不是 `00:00` 也不是「今天」。
 *  ③ **不用卡片堆砌** —— 一条录入带 + hairline 分隔的列表行。
 *
 * 接口（走 fetchAPI，自动补 `/api` 前缀）：
 *  - GET    /copilot/schedules            列表
 *  - POST   /copilot/schedules            {kind, at_hhmm, weekdays}
 *  - PATCH  /copilot/schedules/{id}       {enabled}
 *  - DELETE /copilot/schedules/{id}
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { Loader2, Trash2 } from 'lucide-react'
import { fetchAPI } from '@panwatch/api'
import { Switch } from '@panwatch/base-ui/components/ui/switch'

interface ScheduleRow {
  id: number
  kind: string
  at_hhmm: string | null
  weekdays: string | null
  conversation_id: number | null
  enabled: boolean
  last_run_date: string | null
  created_at?: string | null
}

/** 类型下拉。后端只认 `auction_digest` / `daily_brief`（src/core/copilot_schedule.py）。 */
const KIND_OPTIONS: { value: string; label: string }[] = [
  { value: 'auction_digest', label: '竞价摘要' },
  { value: 'daily_brief', label: '收盘小结' },
]

/** 工作日多选的 7 个键 —— 与后端 `_WEEKDAY_KEYS` 同序同字面。
 *  刻意不加 `as const`：本组件按**字符串**处理工作日集合（后端存的就是串），
 *  收紧成字面量联合反而让 indexOf/filter 的入参类型对不上。 */
const WEEKDAY_KEYS: { key: string; label: string }[] = [
  { key: 'mon', label: '一' },
  { key: 'tue', label: '二' },
  { key: 'wed', label: '三' },
  { key: 'thu', label: '四' },
  { key: 'fri', label: '五' },
  { key: 'sat', label: '六' },
  { key: 'sun', label: '日' },
]

const kindLabel = (kind: string) =>
  KIND_OPTIONS.find((k) => k.value === kind)?.label || kind

/** HH:MM 校验（后端 create() 同样要求 HH:MM，0≤hh<24 / 0≤mm<60）。 */
const isValidHhmm = (v: string): boolean => {
  const m = /^(\d{1,2}):(\d{2})$/.exec(v.trim())
  if (!m) return false
  const hh = Number(m[1])
  const mm = Number(m[2])
  return Number.isInteger(hh) && Number.isInteger(mm) && hh >= 0 && hh < 24 && mm >= 0 && mm < 60
}

/** `mon-fri` / `mon,wed,fri` / `daily` → 展示用的中文/键集合。推不出就 null（→ `--`）。 */
export function parseWeekdays(spec: string | null | undefined): string[] | null {
  const raw = (spec || '').trim().toLowerCase()
  if (!raw) return null
  if (raw === 'daily' || raw === 'everyday' || raw === 'all') {
    return WEEKDAY_KEYS.map((w) => w.key)
  }
  if (raw.includes('-')) {
    const [a, b] = raw.split('-').map((x) => x.trim())
    const ai = WEEKDAY_KEYS.findIndex((w) => w.key === a)
    const bi = WEEKDAY_KEYS.findIndex((w) => w.key === b)
    if (ai < 0 || bi < 0 || ai > bi) return null
    return WEEKDAY_KEYS.slice(ai, bi + 1).map((w) => w.key)
  }
  const parts = raw.split(',').map((x) => x.trim()).filter(Boolean)
  const picked = parts.filter((p) => WEEKDAY_KEYS.some((w) => w.key === p))
  return picked.length ? picked : null
}

/** 工作日 → 「周一至周五」/「每天」/「周一、周三」。 */
export function formatWeekdays(spec: string | null | undefined): string {
  const raw = (spec || '').trim().toLowerCase()
  if (!raw) return '--'
  if (raw === 'daily' || raw === 'everyday' || raw === 'all') return '每天'
  const keys = parseWeekdays(raw)
  if (!keys) return raw || '--'
  const full = WEEKDAY_KEYS.map((w) => w.key)
  if (keys.length === full.length) return '每天'
  const contiguous =
    keys.length > 1 &&
    keys.every((k, i) => full.indexOf(k) === full.indexOf(keys[0]) + i)
  if (contiguous) {
    const names = ['一', '二', '三', '四', '五', '六', '日']
    const a = names[full.indexOf(keys[0])]
    const b = names[full.indexOf(keys[keys.length - 1])]
    return keys.length === 2 ? `周${a}、周${b}` : `周${a}至周${b}`
  }
  const names: Record<string, string> = {
    mon: '一', tue: '二', wed: '三', thu: '四', fri: '五', sat: '六', sun: '日',
  }
  return keys.map((k) => `周${names[k]}`).join('、')
}

/**
 * 下次触发时间 —— **只在真能推出来的时候**给。
 *
 * 推法：以 `last_run_date`（YYYYMMDD）为**必需锚点**，往后找第一个「星期命中 且 时间未过」的日期。
 * 推不出来（`last_run_date` 为空 / 格式不认识 / 工作日集合为空 / 时间不是 HH:MM）⇒ null ⇒ 上屏 `--`。
 *
 * 为什么锚点是必需的：后端**没有**「下次触发」这个字段，`last_run_date` 为空意味着
 * 「不知道上次什么时候跑的」，此时若拿「今天」硬推一个日期就是在**编造**运行历史 ——
 * 任务可能今天已经跑过。宁可显示 `--`，也不给一个看着像真的假时间。
 * 面板同时展示 `last_run_date` 原文，用户可自行对照判断。
 */
export function deriveNextRun(
  lastRunDate: string | null | undefined,
  atHhmm: string | null | undefined,
  weekdaysSpec: string | null | undefined,
  now: Date,
): string | null {
  const keys = parseWeekdays(weekdaysSpec)
  if (!keys || keys.length === 0) return null
  if (!atHhmm || !isValidHhmm(atHhmm)) return null
  const m = /^(\d{1,2}):(\d{2})$/.exec(atHhmm.trim())
  const hh = Number(m?.[1])
  const mm = Number(m?.[2])

  const anchorRaw = (lastRunDate || '').trim()
  if (!/^\d{8}$/.test(anchorRaw)) return null
  const anchor = new Date(
    Number(anchorRaw.slice(0, 4)),
    Number(anchorRaw.slice(4, 6)) - 1,
    Number(anchorRaw.slice(6, 8)),
  )
  if (Number.isNaN(anchor.getTime())) return null
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate())
  // 起点 = max(锚点次日, 今天)。
  // 锚点次日：锚点当天那次已经跑过了；
  // 今天：下次触发不可能是**过去**的时间（锚点很久没跑过时，硬推会算出历史日期）。
  const afterAnchor = new Date(anchor.getFullYear(), anchor.getMonth(), anchor.getDate() + 1)
  const start = afterAnchor.getTime() > today.getTime() ? afterAnchor : today

  for (let i = 0; i < 14; i++) {
    const day = new Date(start.getFullYear(), start.getMonth(), start.getDate() + i)
    const key = WEEKDAY_KEYS[(day.getDay() + 6) % 7].key
    if (!keys.includes(key)) continue
    const isToday = day.getTime() === today.getTime()
    if (isToday && (hh * 60 + mm) <= now.getHours() * 60 + now.getMinutes()) continue
    return `${day.getMonth() + 1}月${day.getDate()}日 ${atHhmm.trim()}`
  }
  return null
}

/** YYYYMMDD → YYYY-MM-DD（后端存的是紧凑串）；认不出就原样返回。 */
export function formatLastRun(raw: string | null | undefined): string {
  const s = (raw || '').trim()
  if (!s) return '--'
  if (/^\d{8}$/.test(s)) return `${s.slice(0, 4)}-${s.slice(4, 6)}-${s.slice(6, 8)}`
  return s
}

const jsonHeaders = { 'Content-Type': 'application/json' }

export function SchedulePanel({ className = '' }: { className?: string }) {
  const [rows, setRows] = useState<ScheduleRow[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  // 录入带
  const [kind, setKind] = useState('auction_digest')
  const [at, setAt] = useState('09:20')
  const [weekdays, setWeekdays] = useState<string[]>(['mon', 'tue', 'wed', 'thu', 'fri'])
  const [creating, setCreating] = useState(false)

  const [confirmId, setConfirmId] = useState<number | null>(null)
  const [pendingIds, setPendingIds] = useState<number[]>([])
  const aliveRef = useRef(true)

  useEffect(() => {
    aliveRef.current = true
    return () => {
      aliveRef.current = false
    }
  }, [])

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const data = await fetchAPI<ScheduleRow[]>('/copilot/schedules', { cacheMode: false })
      if (!aliveRef.current) return
      setRows(Array.isArray(data) ? data : [])
      setError(null)
    } catch {
      if (!aliveRef.current) return
      setRows([])
      setError('定时任务读取失败，稍后可重试')
    } finally {
      if (aliveRef.current) setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const markPending = useCallback((id: number, on: boolean) => {
    setPendingIds((prev) => (on ? [...prev, id] : prev.filter((x) => x !== id)))
  }, [])

  /** 工作日 → 后端 `weekdays` 串（连续段写成 `a-b`，其余逗号分隔）。 */
  const weekdaysSpec = useCallback((keys: string[]): string => {
    if (!keys.length) return 'mon-fri'
    if (keys.length === WEEKDAY_KEYS.length) return 'daily'
    const full = WEEKDAY_KEYS.map((w) => w.key)
    const idx = keys.map((k) => full.indexOf(k)).sort((a, b) => a - b)
    const contiguous = idx.every((v, i) => v === idx[0] + i)
    if (contiguous && idx.length > 1) return `${full[idx[0]]}-${full[idx[idx.length - 1]]}`
    return full.filter((k) => keys.includes(k)).join(',')
  }, [])

  const create = useCallback(async () => {
    if (creating) return
    if (!isValidHhmm(at)) {
      setError('时间格式应为 HH:MM，例如 09:20')
      return
    }
    if (weekdays.length === 0) {
      setError('至少选一个工作日')
      return
    }
    const spec = weekdaysSpec(weekdays)
    const hhmm = `${at.trim()}`
    setCreating(true)
    setError(null)
    try {
      const created = await fetchAPI<ScheduleRow>('/copilot/schedules', {
        method: 'POST',
        headers: jsonHeaders,
        body: JSON.stringify({ kind, at_hhmm: hhmm, weekdays: spec }),
      })
      if (!aliveRef.current) return
      setRows((prev) => [created, ...prev])
    } catch {
      if (!aliveRef.current) return
      setError('创建失败')
    } finally {
      if (aliveRef.current) setCreating(false)
    }
  }, [at, creating, kind, weekdays, weekdaysSpec])

  const toggle = useCallback(
    async (row: ScheduleRow, next: boolean) => {
      setRows((prev) => prev.map((r) => (r.id === row.id ? { ...r, enabled: next } : r)))
      markPending(row.id, true)
      try {
        await fetchAPI<{ ok: boolean }>(`/copilot/schedules/${row.id}`, {
          method: 'PATCH',
          headers: jsonHeaders,
          body: JSON.stringify({ enabled: next }),
        })
        setError(null)
      } catch {
        setRows((prev) => prev.map((r) => (r.id === row.id ? { ...r, enabled: !next } : r)))
        setError('启停失败，已还原')
      } finally {
        markPending(row.id, false)
      }
    },
    [markPending],
  )

  const remove = useCallback(
    async (row: ScheduleRow) => {
      setConfirmId(null)
      setRows((prev) => prev.filter((r) => r.id !== row.id))
      markPending(row.id, true)
      try {
        await fetchAPI<{ ok: boolean }>(`/copilot/schedules/${row.id}`, { method: 'DELETE' })
        setError(null)
      } catch {
        setRows((prev) => [...prev, row])
        setError('删除失败，已还原')
      } finally {
        markPending(row.id, false)
      }
    },
    [markPending],
  )

  const toggleWeekday = useCallback((key: string) => {
    setWeekdays((prev) =>
      prev.includes(key) ? prev.filter((k) => k !== key) : [...prev, key].sort(
        (a, b) =>
          WEEKDAY_KEYS.findIndex((w) => w.key === a) - WEEKDAY_KEYS.findIndex((w) => w.key === b),
      ),
    )
  }, [])

  // 「下次触发」按**渲染那一刻**推算；列表不变时不重算（避免每次渲染抖动）。
  const now = useRef(new Date())
  now.current = new Date()

  return (
    <section className={`flex flex-col gap-3 ${className}`} data-testid="copilot-schedule-panel">
      <p className="text-[12px] leading-relaxed text-muted-foreground">
        定时任务按工作日到点执行，结果写成一条消息进会话
      </p>

      {/* 录入带：类型 + 时间 + 工作日多选 + 创建 */}
      <div className="flex flex-wrap items-center gap-2 border-b border-border pb-2">
        <select
          aria-label="任务类型"
          data-testid="schedule-kind-select"
          value={kind}
          onChange={(e) => setKind(e.target.value)}
          className="rounded border border-border bg-transparent px-1 py-0.5 text-[12px] text-foreground"
        >
          {KIND_OPTIONS.map((k) => (
            <option key={k.value} value={k.value}>
              {k.label}
            </option>
          ))}
        </select>
        <input
          aria-label="触发时间"
          data-testid="schedule-time-input"
          type="time"
          step={60}
          className="rounded border border-border bg-transparent px-1 py-0.5 text-[12px] text-foreground"
          value={at}
          onChange={(e) => setAt(e.target.value)}
        />
        <div
          className="flex items-center gap-1"
          role="group"
          aria-label="工作日"
          data-testid="schedule-weekdays"
        >
          {WEEKDAY_KEYS.map((w) => {
            const on = weekdays.includes(w.key)
            return (
              <button
                key={w.key}
                type="button"
                aria-pressed={on}
                aria-label={`周${w.label}`}
                data-testid={`schedule-weekday-${w.key}`}
                onClick={() => toggleWeekday(w.key)}
                className={`h-5 w-5 rounded text-[12px] transition-colors ${
                  on
                    ? 'border border-border bg-s3 text-foreground'
                    : 'border border-border text-muted-foreground'
                }`}
              >
                {w.label}
              </button>
            )
          })}
        </div>
        <button
          type="button"
          data-testid="schedule-create-btn"
          disabled={creating || !isValidHhmm(at) || weekdays.length === 0}
          onClick={() => void create()}
          className="rounded border border-border px-2 py-0.5 text-[12px] text-muted-foreground transition-colors hover:text-foreground disabled:opacity-50"
        >
          {creating ? '创建中…' : '创建'}
        </button>
      </div>

      {error ? (
        <p role="alert" className="text-[12px] text-role-risk">
          {error}
        </p>
      ) : null}

      {loading ? (
        <div className="flex items-center gap-2 text-[12px] text-muted-foreground">
          <Loader2 className="h-3 w-3 animate-spin" />
          读取中…
        </div>
      ) : rows.length === 0 ? (
        <p data-testid="schedule-empty" className="py-3 text-[12px] text-muted-foreground">
          还没有定时任务。可以让我每天 9:20 给你发竞价摘要。
        </p>
      ) : (
        <ul className="flex flex-col">
          {rows.map((row) => {
            const confirming = confirmId === row.id
            const pending = pendingIds.includes(row.id)
            const nextRun = deriveNextRun(row.last_run_date, row.at_hhmm, row.weekdays, now.current)
            return (
              <li
                key={row.id}
                data-testid="schedule-row"
                data-schedule-id={row.id}
                className="flex items-start gap-2 border-b border-border py-2 transition-opacity duration-200"
              >
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-[12px] font-medium text-foreground">
                      {kindLabel(row.kind)}
                    </span>
                    <span
                      data-testid="schedule-time"
                      className="font-mono text-[12px] text-muted-foreground"
                    >
                      {row.at_hhmm || '--'}
                    </span>
                    <span data-testid="schedule-weekdays-text" className="text-[12px] text-muted-foreground">
                      {formatWeekdays(row.weekdays)}
                    </span>
                    {!row.enabled ? (
                      <span className="rounded bg-s2 px-1 text-[12px] text-muted-foreground">
                        已停用
                      </span>
                    ) : null}
                  </div>
                  <div className="mt-1 flex flex-wrap items-center gap-2 text-[12px] text-muted-foreground">
                    <span data-testid="schedule-last-run">
                      上次 {formatLastRun(row.last_run_date)}
                    </span>
                    <span data-testid="schedule-next-run">下次 {nextRun || '--'}</span>
                    {pending ? <span className="text-muted-foreground">处理中…</span> : null}
                  </div>
                </div>

                <div className="flex shrink-0 items-center gap-1">
                  {confirming ? (
                    <>
                      <span className="text-[12px] text-role-risk">确认删除？</span>
                      <button
                        type="button"
                        data-testid="schedule-delete-confirm"
                        onClick={() => void remove(row)}
                        className="rounded border border-role-risk/50 px-1.5 py-0.5 text-[12px] text-role-risk"
                      >
                        确认
                      </button>
                      <button
                        type="button"
                        data-testid="schedule-delete-cancel"
                        onClick={() => setConfirmId(null)}
                        className="rounded border border-border px-1.5 py-0.5 text-[12px] text-muted-foreground"
                      >
                        取消
                      </button>
                    </>
                  ) : (
                    <>
                      <button
                        type="button"
                        aria-label={`删除任务：${kindLabel(row.kind)}`}
                        data-testid="schedule-delete"
                        onClick={() => setConfirmId(row.id)}
                        className="rounded p-1 text-muted-foreground transition-colors hover:text-role-risk"
                      >
                        <Trash2 className="h-3 w-3" />
                      </button>
                      <Switch
                        aria-label={`启用任务：${kindLabel(row.kind)}`}
                        data-testid="schedule-toggle"
                        checked={row.enabled}
                        onCheckedChange={(next) => void toggle(row, next)}
                      />
                    </>
                  )}
                </div>
              </li>
            )
          })}
        </ul>
      )}
    </section>
  )
}

export default SchedulePanel
