import { useEffect, useRef, useState } from 'react'
import {
  Check,
  MoreHorizontal,
  Pencil,
  Pin,
  PinOff,
  Plus,
  Search,
  Trash2,
  X,
} from 'lucide-react'
import type { ConversationItem } from '@/hooks/useConversations'

/**
 * 会话侧边栏（纯展示 + 回调，**自己不请求**）。
 *
 * 为什么是纯展示：会话数据由 `useConversations` 管，侧边栏只负责
 * 「长什么样 + 用户点了什么」。这样页面（AiCopilot）可以自己决定
 * 何时拉列表、何时乐观更新，组件永远不发请求、不持有会话真值。
 *
 * 视觉铁律（与设计稿一致）：
 *  - **不做卡片堆砌**：整栏是平铺列表，行间用 hairline 分隔，不用卡片/阴影。
 *  - **当前会话高亮 = 左侧 2px 竖条 + 背景微亮**，不用边框盒子。
 *  - **彩色只表语义**：置顶/激活走 role-watch，删除确认走 role-risk；
 *    不引入涨跌色，也不手调背景透明度（走 bg-s0..s4 阶梯）。
 */
export interface ConversationSidebarProps {
  conversations: ConversationItem[]
  /** 当前会话 id；null = 还没有选中（新建对话态）。 */
  activeId: number | null
  onSelect: (id: number) => void
  /** 双击标题改完提交；传空串 = 用户清空了标题（父层决定是否允许）。 */
  onRename: (id: number, title: string) => void
  /** pinned=true 表示置顶，false 表示取消置顶。 */
  onPin: (id: number, pinned: boolean) => void
  onDelete: (id: number) => void
  onCreate: () => void
  /** 搜索词（受控，父层持有 —— 搜索要能打到后端 q 参数）。 */
  query: string
  onQueryChange: (q: string) => void
}

const UNTITLED = '未命名会话'

export function ConversationSidebar({
  conversations,
  activeId,
  onSelect,
  onRename,
  onPin,
  onDelete,
  onCreate,
  query,
  onQueryChange,
}: ConversationSidebarProps) {
  /** 正在改名中的会话 id（双击标题进入编辑）。 */
  const [editingId, setEditingId] = useState<number | null>(null)
  /** 展开「···」菜单的会话 id。 */
  const [menuId, setMenuId] = useState<number | null>(null)
  /** 进入「删除二次确认」的会话 id（inline，不用 modal）。 */
  const [confirmDeleteId, setConfirmDeleteId] = useState<number | null>(null)
  /** 键盘游标（↑/↓ 移动，Enter 选中）。初值对齐当前选中会话。 */
  const [cursor, setCursor] = useState(() => {
    const idx = conversations.findIndex((c) => c.id === activeId)
    return idx >= 0 ? idx : 0
  })
  const [draftTitle, setDraftTitle] = useState('')
  const editInputRef = useRef<HTMLInputElement | null>(null)
  /** 上次已把游标对齐到的 activeId（只认变化，避免父层每次传新数组就重置游标）。 */
  const syncedActiveRef = useRef<number | null>(activeId)
  /**
   * 本轮改名是否已提交。
   * Enter 与 blur 都会调 commitRename，两者可能连着触发（Enter 改完输入框随即卸载，
   * 若浏览器补一次 blur，闭包里的 editingId 还是旧值 → onRename 会被调两次）。
   * 用 ref 一次性闩住，重复提交直接忽略。
   */
  const renameCommittedRef = useRef(false)

  // 列表变短时把游标夹回范围内，避免 ArrowUp 越界选不中任何会话
  useEffect(() => {
    setCursor((c) => (conversations.length === 0 ? 0 : Math.min(c, conversations.length - 1)))
  }, [conversations.length])

  // 外部切换会话（父层改了 activeId）→ 游标跟着走，↑↓ 从当前会话开始
  useEffect(() => {
    if (activeId === syncedActiveRef.current) return
    syncedActiveRef.current = activeId
    const idx = conversations.findIndex((c) => c.id === activeId)
    if (idx >= 0) setCursor(idx)
  }, [activeId, conversations])

  useEffect(() => {
    if (editingId !== null) editInputRef.current?.focus()
  }, [editingId])

  const startRename = (c: ConversationItem) => {
    setMenuId(null)
    setConfirmDeleteId(null)
    setEditingId(c.id)
    setDraftTitle(c.title ?? '')
    renameCommittedRef.current = false
  }

  const commitRename = () => {
    if (editingId === null) return
    if (renameCommittedRef.current) return
    renameCommittedRef.current = true
    const next = draftTitle.trim()
    // 空标题不提交（避免出现一行空白会话）
    if (next) onRename(editingId, next)
    setEditingId(null)
  }

  const cancelRename = () => {
    renameCommittedRef.current = true
    setEditingId(null)
    setDraftTitle('')
  }

  const closeMenus = () => {
    setMenuId(null)
    setConfirmDeleteId(null)
  }

  /**
   * 键盘：↑/↓ 切换游标并同步选中，Enter 确认选中。
   * 编辑框里的按键**不**冒泡到列表（改标题时按 Enter 是保存，不是切会话）。
   */
  const onListKeyDown = (e: React.KeyboardEvent<HTMLUListElement>) => {
    const target = e.target as HTMLElement | null
    if (target && (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA')) return
    if (conversations.length === 0) return

    if (e.key === 'ArrowDown') {
      e.preventDefault()
      const next = Math.min(cursor + 1, conversations.length - 1)
      setCursor(next)
      onSelect(conversations[next].id)
      return
    }
    if (e.key === 'ArrowUp') {
      e.preventDefault()
      const next = Math.max(cursor - 1, 0)
      setCursor(next)
      onSelect(conversations[next].id)
      return
    }
    if (e.key === 'Enter') {
      e.preventDefault()
      const target2 = conversations[cursor]
      if (target2) onSelect(target2.id)
    }
  }

  return (
    <aside
      data-testid="copilot-conv-sidebar"
      className="flex h-full min-h-0 w-64 shrink-0 flex-col border-r border-border/40 bg-s1"
      aria-label="会话列表"
    >
      {/* 顶部：新建 + 搜索 */}
      <div className="flex items-center gap-1.5 border-b border-border/40 px-2 py-2">
        <button
          type="button"
          onClick={onCreate}
          data-testid="copilot-conv-new"
          aria-label="新建对话"
          className="flex items-center gap-1 rounded-md px-1.5 py-1 text-[12px] text-muted-foreground transition-colors hover:bg-s2 hover:text-foreground"
        >
          <Plus className="h-3.5 w-3.5" />
          新对话
        </button>
        <div className="relative ml-auto flex min-w-0 flex-1 items-center">
          <Search
            aria-hidden="true"
            className="pointer-events-none absolute left-1.5 h-3 w-3 text-muted-foreground"
          />
          <input
            type="text"
            value={query}
            onChange={(e) => onQueryChange(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Escape') onQueryChange('')
            }}
            placeholder="搜索对话"
            aria-label="搜索对话"
            data-testid="copilot-conv-search"
            className="h-6 w-full min-w-0 rounded-md border border-border/50 bg-s0 pl-6 pr-2 text-[12px] text-foreground outline-none transition-colors focus:border-role-watch/60"
          />
        </div>
      </div>

      {/* 列表 */}
      {conversations.length === 0 ? (
        <div className="px-3 py-4" data-testid="copilot-conv-empty">
          <div className="text-[12px] text-foreground/80">还没有对话</div>
          <div className="mt-1 text-[11px] leading-relaxed text-muted-foreground">
            在右侧提问会自动开一个新对话，之后可以在这里随时翻回。
          </div>
        </div>
      ) : (
        <ul
          tabIndex={0}
          onKeyDown={onListKeyDown}
          data-testid="copilot-conv-list"
          aria-label="会话列表（↑↓ 切换，Enter 选中）"
          className="min-h-0 flex-1 overflow-y-auto py-1 outline-none focus-visible:ring-1 focus-visible:ring-ring"
        >
          {conversations.map((c, i) => {
            const isActive = c.id === activeId
            const isEditing = editingId === c.id
            const isMenuOpen = menuId === c.id
            const isConfirming = confirmDeleteId === c.id
            return (
              <li
                key={c.id}
                data-testid={`copilot-conv-item-${c.id}`}
                className={`group relative flex items-center gap-1 px-2 py-1.5 transition-colors hover:bg-s2 ${
                  isActive ? 'bg-s2' : ''
                }`}
              >
                {/* 当前会话：左侧 2px 竖条（不是边框盒子） */}
                <span
                  aria-hidden="true"
                  data-testid={`copilot-conv-active-bar-${c.id}`}
                  className={`absolute inset-y-0 left-0 w-0.5 ${
                    isActive ? 'bg-role-watch' : 'bg-transparent'
                  }`}
                />

                {isEditing ? (
                  <input
                    ref={editInputRef}
                    value={draftTitle}
                    onChange={(e) => setDraftTitle(e.target.value)}
                    onBlur={commitRename}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter') {
                        e.preventDefault()
                        e.stopPropagation()
                        commitRename()
                      } else if (e.key === 'Escape') {
                        e.preventDefault()
                        e.stopPropagation()
                        cancelRename()
                      }
                    }}
                    aria-label="重命名对话"
                    data-testid={`copilot-conv-rename-input-${c.id}`}
                    className="min-w-0 flex-1 rounded-md border border-role-watch/50 bg-s0 px-1 py-0.5 text-[12px] text-foreground outline-none"
                  />
                ) : (
                  <button
                    type="button"
                    onClick={() => {
                      setCursor(i)
                      closeMenus()
                      onSelect(c.id)
                    }}
                    onDoubleClick={() => startRename(c)}
                    title={c.title || UNTITLED}
                    data-testid={`copilot-conv-title-${c.id}`}
                    aria-current={isActive ? 'true' : undefined}
                    className="flex min-w-0 flex-1 items-center gap-1 text-left"
                  >
                    <span className="truncate text-[12px] text-foreground">
                      {c.title || UNTITLED}
                    </span>
                    {c.pinned ? (
                      <span
                        title="已置顶"
                        data-testid={`copilot-conv-pin-${c.id}`}
                        className="shrink-0 text-role-watch"
                      >
                        <Pin aria-hidden="true" className="h-3 w-3" />
                        <span className="sr-only">已置顶</span>
                      </span>
                    ) : null}
                    {c.stock_symbol ? (
                      <span className="shrink-0 font-mono text-[10px] text-muted-foreground">
                        {c.stock_symbol}
                      </span>
                    ) : null}
                  </button>
                )}

                {!isEditing ? (
                  <button
                    type="button"
                    aria-label={`会话操作：${c.title || UNTITLED}`}
                    aria-haspopup="menu"
                    aria-expanded={isMenuOpen}
                    data-testid={`copilot-conv-menu-btn-${c.id}`}
                    onClick={() => {
                      setConfirmDeleteId(null)
                      setMenuId(isMenuOpen ? null : c.id)
                    }}
                    className="shrink-0 rounded-md p-1 text-muted-foreground opacity-0 transition-opacity hover:text-foreground focus-visible:opacity-100 group-hover:opacity-100 [@media(hover:none)]:opacity-100"
                  >
                    <MoreHorizontal aria-hidden="true" className="h-3.5 w-3.5" />
                  </button>
                ) : null}

                {/* 「···」菜单 / 删除二次确认（inline，不是 modal） */}
                {isMenuOpen ? (
                  <div
                    role="menu"
                    data-testid={`copilot-conv-menu-${c.id}`}
                    className="absolute right-2 top-8 z-20 w-36 overflow-hidden rounded-xl border border-border/60 bg-s1 shadow-float"
                  >
                    {isConfirming ? (
                      <div className="px-2 py-1.5" data-testid={`copilot-conv-confirm-box-${c.id}`}>
                        <div className="text-[11px] text-foreground">删除这条对话？</div>
                        <div className="mt-1.5 flex items-center gap-1.5">
                          <button
                            type="button"
                            data-testid={`copilot-conv-confirm-${c.id}`}
                            onClick={() => {
                              closeMenus()
                              onDelete(c.id)
                            }}
                            className="flex items-center gap-1 rounded-md px-1.5 py-0.5 text-[11px] text-role-risk transition-colors hover:bg-role-risk/10"
                          >
                            <Trash2 aria-hidden="true" className="h-3 w-3" />
                            确认删除
                          </button>
                          <button
                            type="button"
                            data-testid={`copilot-conv-cancel-${c.id}`}
                            onClick={() => setConfirmDeleteId(null)}
                            className="flex items-center gap-1 rounded-md px-1.5 py-0.5 text-[11px] text-muted-foreground transition-colors hover:bg-s2 hover:text-foreground"
                          >
                            <X aria-hidden="true" className="h-3 w-3" />
                            取消
                          </button>
                        </div>
                      </div>
                    ) : (
                      <>
                        <button
                          type="button"
                          role="menuitem"
                          data-testid={`copilot-conv-menu-rename-${c.id}`}
                          onClick={() => startRename(c)}
                          className="flex w-full items-center gap-1.5 px-2 py-1.5 text-left text-[12px] text-foreground transition-colors hover:bg-s2"
                        >
                          <Pencil aria-hidden="true" className="h-3 w-3 text-muted-foreground" />
                          重命名
                        </button>
                        <button
                          type="button"
                          role="menuitem"
                          data-testid={`copilot-conv-menu-pin-${c.id}`}
                          onClick={() => {
                            closeMenus()
                            onPin(c.id, !c.pinned)
                          }}
                          className="flex w-full items-center gap-1.5 px-2 py-1.5 text-left text-[12px] text-foreground transition-colors hover:bg-s2"
                        >
                          {c.pinned ? (
                            <PinOff aria-hidden="true" className="h-3 w-3 text-muted-foreground" />
                          ) : (
                            <Pin aria-hidden="true" className="h-3 w-3 text-muted-foreground" />
                          )}
                          {c.pinned ? '取消置顶' : '置顶'}
                        </button>
                        <button
                          type="button"
                          role="menuitem"
                          data-testid={`copilot-conv-menu-delete-${c.id}`}
                          onClick={() => setConfirmDeleteId(c.id)}
                          className="flex w-full items-center gap-1.5 px-2 py-1.5 text-left text-[12px] text-role-risk transition-colors hover:bg-role-risk/10"
                        >
                          <Trash2 aria-hidden="true" className="h-3 w-3" />
                          删除
                        </button>
                      </>
                    )}
                  </div>
                ) : null}
              </li>
            )
          })}
        </ul>
      )}

      {/* 提示条：键盘操作说明（不是卡片，是一行 hairline 分隔的脚注） */}
      <div className="border-t border-border/40 px-2.5 py-1.5 text-[10px] text-muted-foreground/70">
        <span className="inline-flex items-center gap-1">
          <Check aria-hidden="true" className="h-2.5 w-2.5" />
          双击标题改名 · ↑↓ 切换 · Enter 选中
        </span>
      </div>
    </aside>
  )
}

export default ConversationSidebar
