/**
 * 会话管理 hook（AI 参谋工作台）。
 *
 * 设计取舍（三条）：
 *  1. **乐观更新 + 失败回滚**：改名/置顶/删除都是"点了就该立刻变"的小动作，
 *     等接口回来再动会让人觉得界面卡死。所以先把本地列表改成目标态，
 *     请求失败则**回滚到操作前的快照**并把失败原样返回。
 *  2. **错误静默**：本 hook 不弹任何全局错误（弹窗会把用户从阅读里拽出来）。
 *     统一返回 `{ ok: false, error }`，调用方要提示就提示，不提示也不阻塞。
 *  3. **不引入新依赖**：只用 `fetchAPI`（自带 `/api` 前缀，路径里不要再写 `/api/`）。
 *
 * 后端契约（`src/web/api/chat_admin.py` + `chat.py`，prefix `/api/chat`）：
 *  - `GET    /chat/conversations?q=`      列表/搜索（q 为空 = 全部，按 pinned desc 排序）
 *  - `PATCH  /chat/conversations/{id}`    改名 `{title}` / 置顶 `{pinned}`（可只传其一）
 *  - `DELETE /chat/conversations/{id}`    删除会话
 *  - `POST   /chat/conversations`         新建会话
 *
 * 搜索端点是 chat_admin 的**超集**版本（q 多一个查询参数，字段多了 pinned/updated_at），
 * 与本 hook 的 `list(q)` 契约一致。
 */
import { useCallback, useRef, useState } from 'react'
import { fetchAPI } from '@sida/api'
import { describeApiError } from '@/lib/api-error'

/** 会话行（后端 chat_conversations 的前端投影，字段缺失一律 null，不猜）。 */
export interface ConversationItem {
  id: number
  title: string
  stock_symbol?: string | null
  stock_market?: string | null
  created_at?: string | null
  updated_at?: string | null
  /** 置顶标记；后端未返回时按 false 处理。 */
  pinned?: boolean
}

/**
 * 所有动作统一返回这个形状。
 * `ok=false` 时 `error` 是**可展示的**短句（已按超时/网络/4xx/5xx 分类）。
 */
export interface ConversationActionResult<T = undefined> {
  ok: boolean
  error?: string
  data?: T
}

const BASE = '/chat/conversations'

function isConversationItem(v: unknown): v is ConversationItem {
  return !!v && typeof v === 'object' && typeof (v as ConversationItem).id === 'number'
}

export function useConversations() {
  const [conversations, setConversations] = useState<ConversationItem[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  /**
   * 列表真值放在 ref 里：乐观更新与回滚都要读"当前"列表，
   * 用 state 闭包会在连续操作时读到过期快照。
   */
  const listRef = useRef<ConversationItem[]>([])

  const commit = useCallback((next: ConversationItem[]) => {
    listRef.current = next
    setConversations(next)
  }, [])

  /** 列表：GET `/chat/conversations?q=`（q 为空则不带查询串）。 */
  const list = useCallback(
    async (q?: string): Promise<ConversationActionResult<ConversationItem[]>> => {
      setLoading(true)
      try {
        const query = (q ?? '').trim()
        const path = query ? `${BASE}?q=${encodeURIComponent(query)}` : BASE
        // cacheMode:false —— 会话列表必须每次都是最新的（改名/删除后不能吃 30s 缓存）
        const data = await fetchAPI<ConversationItem[]>(path, { cacheMode: false })
        const items = Array.isArray(data) ? data.filter(isConversationItem) : []
        commit(items)
        setError(null)
        return { ok: true, data: items }
      } catch (e) {
        const msg = describeApiError(e)
        // 列表拉取失败**保留**已有列表（不清空），否则一次抖动就把侧栏洗掉
        setError(msg)
        return { ok: false, error: msg }
      } finally {
        setLoading(false)
      }
    },
    [commit],
  )

  /** 改名：PATCH `/chat/conversations/{id}`。 */
  const rename = useCallback(
    async (id: number, title: string): Promise<ConversationActionResult> => {
      const prev = listRef.current
      const nextTitle = title.trim()
      commit(prev.map((c) => (c.id === id ? { ...c, title: nextTitle } : c)))
      try {
        const res = await fetchAPI<Partial<ConversationItem>>(`${BASE}/${id}`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ title: nextTitle }),
        })
        // 后端回了完整行就以它为准；只回 {ok:true} 时保留乐观值
        if (isConversationItem(res)) {
          commit(listRef.current.map((c) => (c.id === id ? { ...c, ...res } : c)))
        }
        return { ok: true }
      } catch (e) {
        commit(prev)
        return { ok: false, error: describeApiError(e) }
      }
    },
    [commit],
  )

  /** 置顶/取消置顶：PATCH `/chat/conversations/{id}`。 */
  const pin = useCallback(
    async (id: number, pinned: boolean): Promise<ConversationActionResult> => {
      const prev = listRef.current
      commit(prev.map((c) => (c.id === id ? { ...c, pinned } : c)))
      try {
        const res = await fetchAPI<Partial<ConversationItem>>(`${BASE}/${id}`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ pinned }),
        })
        if (isConversationItem(res)) {
          commit(listRef.current.map((c) => (c.id === id ? { ...c, ...res } : c)))
        }
        return { ok: true }
      } catch (e) {
        commit(prev)
        return { ok: false, error: describeApiError(e) }
      }
    },
    [commit],
  )

  /** 删除：DELETE `/chat/conversations/{id}`。 */
  const remove = useCallback(
    async (id: number): Promise<ConversationActionResult> => {
      const prev = listRef.current
      commit(prev.filter((c) => c.id !== id))
      try {
        await fetchAPI<{ ok: boolean }>(`${BASE}/${id}`, { method: 'DELETE' })
        return { ok: true }
      } catch (e) {
        commit(prev)
        return { ok: false, error: describeApiError(e) }
      }
    },
    [commit],
  )

  /** 新建：POST `/chat/conversations`（新建项插到列表最前）。 */
  const create = useCallback(
    async (params?: {
      stock_symbol?: string
      stock_market?: string
      initial_context?: string
    }): Promise<ConversationActionResult<ConversationItem>> => {
      try {
        const res = await fetchAPI<ConversationItem>(BASE, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(params ?? {}),
        })
        if (!isConversationItem(res)) {
          return { ok: false, error: '返回格式异常' }
        }
        commit([res, ...listRef.current])
        return { ok: true, data: res }
      } catch (e) {
        return { ok: false, error: describeApiError(e) }
      }
    },
    [commit],
  )

  return { conversations, loading, error, list, rename, pin, remove, create }
}

export type UseConversations = ReturnType<typeof useConversations>
