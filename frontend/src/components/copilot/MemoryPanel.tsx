/**
 * 长期记忆面板（2026-10-22 新增，AI 参谋工作台）。
 *
 * 为什么单独成面板：记忆是**跨会话**的长期资产，必须能被用户逐条看见/改/停/删，
 * 而不是藏在对话里 —— 否则「它为什么这么答」无从查证。
 *
 * 设计口径（三条硬约束）：
 *  ① **只记用户明确说过的** —— 面板只做「增删改查+启停」，不做任何自动推断展示；
 *  ② **空值不补 0** —— 缺失显示 `--`；命中次数 0 是**真实 0**（新记忆确实没被召回过），
 *     所以显示 0 而不是 `--`；
 *  ③ **不用卡片堆砌** —— 一行说明 + 一条录入带 + hairline 分隔的列表行。
 *
 * 接口（走 fetchAPI，自动补 `/api` 前缀）：
 *  - GET    /copilot/memory?include_disabled=1   列表（带停用的，否则开关是单向门）
 *  - POST   /copilot/memory                       {content, kind, weight}
 *  - PATCH  /copilot/memory/{id}                  {content?, weight?, enabled?}
 *  - DELETE /copilot/memory/{id}
 *
 * 注（后端现状）：`PATCH /copilot/memory/{id}` 当前实现只落 `enabled`，
 * `content`/`weight` 需要后端补 `update()` 才真正持久化（见交付说明）。
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { Loader2, Trash2 } from 'lucide-react'
import { fetchAPI } from '@panwatch/api'
import { Switch } from '@panwatch/base-ui/components/ui/switch'
import { safeInt } from '@/lib/format'

interface MemoryRow {
  id: number
  kind: string
  content: string
  weight: number | null
  source: string | null
  enabled: boolean
  hits: number | null
  created_at?: string | null
}

/** 徽标文案：与后端 `format_block` 的标签口径一致（preference→偏好 / rule→规则 / …）。 */
const KIND_LABEL: Record<string, string> = {
  preference: '偏好',
  rule: '规则',
  fact: '事实',
  skill: '流程',
}
/** 录入下拉的顺序：先用户最常用的两类，再其余。 */
const KIND_ORDER = ['preference', 'rule', 'fact', 'skill']

const kindLabel = (kind: string) => KIND_LABEL[kind] || kind

/** 权重上限（后端 remember() 也是 0~10 夹取；面板收窄到 1~10 便于用户理解）。 */
const clampWeight = (raw: string): number => {
  const n = Number.parseInt(raw, 10)
  if (!Number.isFinite(n)) return 5
  return Math.min(10, Math.max(1, n))
}

const jsonHeaders = { 'Content-Type': 'application/json' }

export function MemoryPanel({ className = '' }: { className?: string }) {
  const [rows, setRows] = useState<MemoryRow[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  // 录入带
  const [draft, setDraft] = useState({ content: '', kind: 'preference', weight: '5' })
  const [saving, setSaving] = useState(false)

  // 行内状态
  const [editingId, setEditingId] = useState<number | null>(null)
  const [editDraft, setEditDraft] = useState('')
  const [confirmId, setConfirmId] = useState<number | null>(null)
  const [pendingIds, setPendingIds] = useState<number[]>([])

  // Esc 取消编辑后 input 卸载会触发 blur；用它挡住「Esc 却把编辑存回去」。
  const skipBlurRef = useRef(false)
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
      // include_disabled=1：停用过的记忆要**留在列表里**（开关可再打开），
      // 否则「停用」=「消失」，用户没法反悔。
      const data = await fetchAPI<MemoryRow[]>('/copilot/memory?include_disabled=1', {
        cacheMode: false,
      })
      if (!aliveRef.current) return
      setRows(Array.isArray(data) ? data : [])
      setError(null)
    } catch {
      if (!aliveRef.current) return
      setRows([])
      setError('记忆读取失败，稍后可重试')
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

  /** 新增：**乐观插入** —— 先上屏（临时负 id），服务端回来后换成真 id；失败则撤掉。 */
  const add = useCallback(async () => {
    const content = draft.content.trim()
    if (content.length < 2 || saving) return
    const weight = clampWeight(draft.weight)
    const tempId = -Date.now()
    const optimistic: MemoryRow = {
      id: tempId,
      kind: draft.kind,
      content,
      weight,
      source: 'user',
      enabled: true,
      hits: 0,
      created_at: null,
    }
    setRows((prev) => [optimistic, ...prev])
    setDraft((prev) => ({ ...prev, content: '' }))
    setSaving(true)
    setError(null)
    try {
      const created = await fetchAPI<MemoryRow>('/copilot/memory', {
        method: 'POST',
        headers: jsonHeaders,
        body: JSON.stringify({ content, kind: draft.kind, weight }),
      })
      if (!aliveRef.current) return
      setRows((prev) => prev.map((r) => (r.id === tempId ? created : r)))
    } catch {
      if (!aliveRef.current) return
      setRows((prev) => prev.filter((r) => r.id !== tempId))
      setDraft((prev) => ({ ...prev, content }))
      setError('记忆保存失败')
    } finally {
      if (aliveRef.current) setSaving(false)
    }
  }, [draft, saving])

  /** 启停：乐观翻转 + 失败回滚。 */
  const toggle = useCallback(
    async (row: MemoryRow, next: boolean) => {
      setRows((prev) => prev.map((r) => (r.id === row.id ? { ...r, enabled: next } : r)))
      markPending(row.id, true)
      try {
        await fetchAPI<{ ok: boolean }>(`/copilot/memory/${row.id}`, {
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

  /** 编辑：双击内容 → 输入框；Enter 保存 / Esc 取消。 */
  const startEdit = useCallback((row: MemoryRow) => {
    // 新一轮编辑开始 ⇒ 上一轮遗留的 blur 抑制标记作废（否则会吞掉本次的正常 blur）。
    skipBlurRef.current = false
    setEditingId(row.id)
    setEditDraft(row.content)
    setConfirmId(null)
  }, [])

  const cancelEdit = useCallback(() => {
    skipBlurRef.current = true
    setEditingId(null)
    setEditDraft('')
  }, [])

  const saveEdit = useCallback(
    async (row: MemoryRow) => {
      const content = editDraft.trim()
      // 已经关掉了（Enter/Esc 走的就是这条路）就别再发一次 —— 否则 blur 会补一发重复 PATCH。
      if (editingId !== row.id) return
      if (content.length < 2) {
        setEditingId(null)
        setError('内容太短，未保存')
        return
      }
      skipBlurRef.current = true
      setRows((prev) => prev.map((r) => (r.id === row.id ? { ...r, content } : r)))
      setEditingId(null)
      markPending(row.id, true)
      try {
        await fetchAPI<{ ok: boolean }>(`/copilot/memory/${row.id}`, {
          method: 'PATCH',
          headers: jsonHeaders,
          body: JSON.stringify({ content, weight: row.weight ?? 5, enabled: row.enabled }),
        })
        setError(null)
      } catch {
        setRows((prev) => prev.map((r) => (r.id === row.id ? { ...r, content: row.content } : r)))
        setError('修改失败，已还原')
      } finally {
        markPending(row.id, false)
      }
    },
    [editDraft, editingId, markPending],
  )

  /** 删除：**二次确认**（行内），确认后才打 DELETE。 */
  const remove = useCallback(
    async (row: MemoryRow) => {
      setConfirmId(null)
      setRows((prev) => prev.filter((r) => r.id !== row.id))
      markPending(row.id, true)
      try {
        await fetchAPI<{ ok: boolean }>(`/copilot/memory/${row.id}`, { method: 'DELETE' })
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

  return (
    <section className={`flex flex-col gap-3 ${className}`} data-testid="copilot-memory-panel">
      <p className="text-[11px] leading-relaxed text-muted-foreground">
        记忆会在每次提问时注入，只记你明确说过的偏好与规则
      </p>

      {/* 录入带：一行输入 + 类型 + 权重 + 记住 */}
      <div className="flex flex-wrap items-center gap-2 border-b border-border/40 pb-2">
        <select
          aria-label="记忆类型"
          data-testid="memory-kind-select"
          value={draft.kind}
          onChange={(e) => setDraft((prev) => ({ ...prev, kind: e.target.value }))}
          className="rounded border border-border/60 bg-transparent px-1 py-0.5 text-[11px] text-foreground"
        >
          {KIND_ORDER.map((k) => (
            <option key={k} value={k}>
              {kindLabel(k)}
            </option>
          ))}
        </select>
        <input
          aria-label="记忆内容"
          data-testid="memory-content-input"
          className="min-w-[220px] flex-1 rounded border border-border/60 bg-transparent px-2 py-0.5 text-[11px] text-foreground"
          placeholder="例如：我只做主板，不碰北交所"
          value={draft.content}
          onChange={(e) => setDraft((prev) => ({ ...prev, content: e.target.value }))}
          onKeyDown={(e) => {
            if (e.key === 'Enter') void add()
          }}
        />
        <label className="flex items-center gap-1 text-[11px] text-muted-foreground">
          权重
          <input
            aria-label="权重"
            data-testid="memory-weight-input"
            type="number"
            min={1}
            max={10}
            className="w-10 rounded border border-border/60 bg-transparent px-1 py-0.5 text-[11px] text-foreground"
            value={draft.weight}
            onChange={(e) => setDraft((prev) => ({ ...prev, weight: e.target.value }))}
          />
        </label>
        <button
          type="button"
          data-testid="memory-add-btn"
          disabled={saving || draft.content.trim().length < 2}
          onClick={() => void add()}
          className="rounded border border-border/60 px-2 py-0.5 text-[11px] text-muted-foreground transition-colors hover:text-foreground disabled:opacity-50"
        >
          {saving ? '保存中…' : '记住'}
        </button>
      </div>

      {error ? (
        <p role="alert" className="text-[11px] text-role-risk">
          {error}
        </p>
      ) : null}

      {loading ? (
        <div className="flex items-center gap-2 text-[11px] text-muted-foreground">
          <Loader2 className="h-3 w-3 animate-spin" />
          读取中…
        </div>
      ) : rows.length === 0 ? (
        <p data-testid="memory-empty" className="py-3 text-[11px] text-muted-foreground">
          还没有记忆。对参谋说「记住：…」或在这里手动添加。
        </p>
      ) : (
        <ul className="flex flex-col">
          {rows.map((row) => {
            const editing = editingId === row.id
            const confirming = confirmId === row.id
            const pending = pendingIds.includes(row.id)
            return (
              <li
                key={row.id}
                data-testid="memory-row"
                data-memory-id={row.id}
                className="flex items-start gap-2 border-b border-border/30 py-2 transition-opacity duration-200"
              >
                <span
                  data-testid="memory-kind-badge"
                  className="mt-0.5 shrink-0 rounded bg-s2 px-1 text-[10px] text-muted-foreground"
                >
                  {kindLabel(row.kind)}
                </span>

                <div className="min-w-0 flex-1">
                  {editing ? (
                    <input
                      autoFocus
                      aria-label="编辑记忆内容"
                      data-testid="memory-edit-input"
                      className="w-full rounded border border-border/60 bg-transparent px-1 py-0.5 text-[11px] text-foreground"
                      value={editDraft}
                      onChange={(e) => setEditDraft(e.target.value)}
                      onBlur={() => {
                        if (skipBlurRef.current) {
                          skipBlurRef.current = false
                          return
                        }
                        void saveEdit(row)
                      }}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter') {
                          e.preventDefault()
                          void saveEdit(row)
                        } else if (e.key === 'Escape') {
                          e.preventDefault()
                          cancelEdit()
                        }
                      }}
                    />
                  ) : (
                    <p
                      data-testid="memory-content"
                      title="双击可编辑"
                      onDoubleClick={() => startEdit(row)}
                      className={`cursor-text text-[11px] leading-snug line-clamp-2 ${
                        row.enabled ? 'text-foreground' : 'text-muted-foreground'
                      }`}
                    >
                      {row.content}
                    </p>
                  )}
                  <div className="mt-1 flex items-center gap-2 text-[10px] text-muted-foreground">
                    <span data-testid="memory-weight">权重 {safeInt(row.weight, '--')}</span>
                    <span data-testid="memory-hits">命中 {safeInt(row.hits, '--')} 次</span>
                    {row.source ? <span className="truncate">来源 {row.source}</span> : null}
                    {pending ? <span className="text-muted-foreground/80">处理中…</span> : null}
                  </div>
                </div>

                <div className="flex shrink-0 items-center gap-1">
                  {confirming ? (
                    <>
                      <span className="text-[10px] text-role-risk">确认删除？</span>
                      <button
                        type="button"
                        data-testid="memory-delete-confirm"
                        onClick={() => void remove(row)}
                        className="rounded border border-role-risk/50 px-1.5 py-0.5 text-[10px] text-role-risk"
                      >
                        确认
                      </button>
                      <button
                        type="button"
                        data-testid="memory-delete-cancel"
                        onClick={() => setConfirmId(null)}
                        className="rounded border border-border/60 px-1.5 py-0.5 text-[10px] text-muted-foreground"
                      >
                        取消
                      </button>
                    </>
                  ) : (
                    <>
                      <button
                        type="button"
                        aria-label={`删除记忆：${row.content.slice(0, 12)}`}
                        data-testid="memory-delete"
                        onClick={() => setConfirmId(row.id)}
                        className="rounded p-1 text-muted-foreground transition-colors hover:text-role-risk"
                      >
                        <Trash2 className="h-3 w-3" />
                      </button>
                      <Switch
                        aria-label={`启用记忆：${row.content.slice(0, 12)}`}
                        data-testid="memory-toggle"
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

export default MemoryPanel
