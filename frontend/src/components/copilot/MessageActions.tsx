import { useEffect, useRef, useState } from 'react'
import { Check, Copy, Pencil, RefreshCw, Trash2 } from 'lucide-react'

/**
 * 消息级操作条（复制 / 重新生成 / 编辑重发 / 删除）。
 *
 * 铁律：
 *  - **hover 才显形，触屏常驻**：桌面端 `opacity-0 → hover/focus-visible 显形`，
 *    触屏（hover:none）永远可见 —— 触屏没有 hover，不常驻等于功能不存在。
 *    宿主（消息行）想整行 hover 显形，加 `group/actions` 即可（见类名注释）。
 *  - **无障碍**：每个按钮都有 `aria-label`（图标按钮对读屏不可见，必须有名字）。
 *  - **不用卡片/阴影**：就是一排图标按钮，靠 hairline 与留白分隔。
 *  - **彩色只表语义**：删除二次确认走 role-risk，其余是中性前景色。
 */
export interface MessageActionsProps {
  /** 消息角色：user 才能编辑重发，assistant 才能重新生成。 */
  role: 'user' | 'assistant' | 'system'
  /** 重新生成是否可用（通常只有 assistant 的**最后一条**为 true）。 */
  canRegenerate?: boolean
  /** 编辑重发是否可用（user 消息默认可用，传 false 关掉）。 */
  canEdit?: boolean
  /** 复制回调（组件已写完剪贴板后调用；宿主也可在此做自己的兜底复制）。 */
  onCopy: () => void
  onRegenerate?: () => void
  /** 不传 = 不显示「编辑」（例如还没拿到 message id 的会话）。 */
  onEdit?: () => void
  /** 不传 = 不显示「删除」（同上，避免删了又刷回来的假删除）。 */
  onDelete?: () => void
  /**
   * 要复制的正文。给了 → 组件自己写剪贴板并显示「已复制」；
   * 不给 → 只回调 onCopy（宿主自行复制），同样显示「已复制」提示。
   */
  copyText?: string
  /** 复制成功后的提示文案，默认「已复制」。 */
  copiedLabel?: string
}

export function MessageActions({
  role,
  canRegenerate,
  canEdit,
  onCopy,
  onRegenerate,
  onEdit,
  onDelete,
  copyText,
  copiedLabel = '已复制',
}: MessageActionsProps) {
  const [copied, setCopied] = useState(false)
  const [confirming, setConfirming] = useState(false)
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  useEffect(
    () => () => {
      if (timerRef.current !== null) clearTimeout(timerRef.current)
    },
    [],
  )

  const showRegenerate = role === 'assistant' && canRegenerate === true
  const showEdit = role === 'user' && canEdit !== false && typeof onEdit === 'function'

  const flashCopied = () => {
    setCopied(true)
    if (timerRef.current !== null) clearTimeout(timerRef.current)
    // 1.5s 后回落成复制图标（不打断阅读）
    timerRef.current = setTimeout(() => setCopied(false), 1500)
  }

  const handleCopy = async () => {
    if (copyText === undefined) {
      // 宿主自带复制逻辑 → 直接给反馈
      onCopy()
      flashCopied()
      return
    }
    try {
      await navigator.clipboard.writeText(copyText)
      flashCopied()
    } catch {
      // 剪贴板不可用（非 https / 权限拒绝）→ 静默，不打断阅读
    }
    onCopy()
  }

  const btn =
    'flex h-6 w-6 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-s2 hover:text-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring'

  return (
    <div
      data-testid="message-actions"
      role="group"
      aria-label="消息操作"
      className="group/actions flex items-center gap-0.5 opacity-0 transition-opacity hover:opacity-100 focus-within:opacity-100 group-hover/actions:opacity-100 [@media(hover:none)]:opacity-100"
    >
      <button
        type="button"
        aria-label="复制消息"
        title={copied ? copiedLabel : '复制'}
        data-testid="message-action-copy"
        onClick={() => void handleCopy()}
        className={btn}
      >
        {copied ? (
          <Check aria-hidden="true" className="h-3.5 w-3.5 text-role-watch" />
        ) : (
          <Copy aria-hidden="true" className="h-3.5 w-3.5" />
        )}
      </button>

      {showRegenerate ? (
        <button
          type="button"
          aria-label="重新生成"
          title="重新生成"
          data-testid="message-action-regenerate"
          onClick={() => onRegenerate?.()}
          className={btn}
        >
          <RefreshCw aria-hidden="true" className="h-3.5 w-3.5" />
        </button>
      ) : null}

      {showEdit ? (
        <button
          type="button"
          aria-label="编辑并重发"
          title="编辑并重发"
          data-testid="message-action-edit"
          onClick={() => onEdit?.()}
          className={btn}
        >
          <Pencil aria-hidden="true" className="h-3.5 w-3.5" />
        </button>
      ) : null}

      {confirming ? (
        <span className="flex items-center gap-1 pl-1" data-testid="message-delete-confirm-box">
          <span className="text-[11px] text-role-risk">删除？</span>
          <button
            type="button"
            aria-label="确认删除消息"
            data-testid="message-action-delete-confirm"
            onClick={() => {
              setConfirming(false)
              onDelete?.()
            }}
            className="flex h-6 items-center rounded-md px-1.5 text-[11px] text-role-risk transition-colors hover:bg-role-risk/10"
          >
            确认
          </button>
          <button
            type="button"
            aria-label="取消删除"
            data-testid="message-action-delete-cancel"
            onClick={() => setConfirming(false)}
            className="flex h-6 items-center rounded-md px-1.5 text-[11px] text-muted-foreground transition-colors hover:bg-s2 hover:text-foreground"
          >
            取消
          </button>
        </span>
      ) : (
        <button
          type="button"
          aria-label="删除消息"
          title="删除"
          data-testid="message-action-delete"
          onClick={() => setConfirming(true)}
          className={`${btn} hover:text-role-risk`}
        >
          <Trash2 aria-hidden="true" className="h-3.5 w-3.5" />
        </button>
      )}
    </div>
  )
}

export default MessageActions
