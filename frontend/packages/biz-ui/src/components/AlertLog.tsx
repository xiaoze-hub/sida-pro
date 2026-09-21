/**
 * 会话消息流(2026-09-20 设计系统 P2)。
 *
 * 依据: 现以 toast 为主 —— toast 是**一次性**的, 错过即永久错过; 而盯盘恰恰需要"回头看一眼
 * 刚才那条预警是什么时候来的"。这里在右栏累积本次会话的消息(带时间戳、级别、口径), toast 只留
 * 给"一次性确认"(如保存成功)。
 */
import { useCallback, useMemo, useRef, useState } from 'react'
import { cn } from '@panwatch/base-ui'
import { formatClock } from './FreshnessBadge'

export type AlertLevel = 'info' | 'warn' | 'risk'

export interface AlertItem {
  id: string
  at: string | number | Date
  level: AlertLevel
  text: string
  /** 该消息的数据口径(有则显示徽章, 无则不显示 —— 不补造) */
  caliber?: string
}

const LEVEL_DOT: Record<AlertLevel, string> = {
  info: 'text-muted-foreground',
  warn: 'text-amber-500',
  risk: 'text-[hsl(var(--role-risk))]',
}

/**
 * 会话级消息流(只保留最近 `cap` 条; 累计而不弹窗)。
 * 用法: `const log = useAlertLog()` → `log.push({...})`, 面板里 `<AlertLog items={log.items} />`。
 */
export function useAlertLog(cap = 200) {
  const [items, setItems] = useState<AlertItem[]>([])
  const seq = useRef(0)
  const push = useCallback(
    (item: Omit<AlertItem, 'id' | 'at'> & { at?: string | number | Date }) => {
      seq.current += 1
      const id = `a${seq.current}`
      setItems((prev) => [{ id, at: item.at ?? new Date(), level: item.level, text: item.text, caliber: item.caliber }, ...prev].slice(0, cap))
    },
    [cap],
  )
  const clear = useCallback(() => setItems([]), [])
  return { items, push, clear }
}

export default function AlertLog({
  items,
  className,
  emptyHint = '本次会话暂无消息',
  onClear,
}: {
  items: AlertItem[]
  className?: string
  emptyHint?: string
  onClear?: () => void
}) {
  const counts = useMemo(() => items.filter((i) => i.level !== 'info').length, [items])
  return (
    <div className={cn('flex flex-col', className)} data-alert-log={items.length}>
      <div className="flex items-center gap-2 border-b border-border/60 px-2 py-1.5">
        <span className="text-[11px] font-medium text-muted-foreground">会话消息</span>
        {counts > 0 && <span className="font-mono text-[10px] text-amber-500">{counts} 条非信息</span>}
        {items.length > 0 && onClear && (
          <button
            type="button"
            onClick={onClear}
            className="ml-auto rounded px-1 text-[10px] text-muted-foreground transition-colors duration-fast hover:bg-s2 hover:text-foreground"
          >
            清空
          </button>
        )}
      </div>
      {items.length === 0 ? (
        <div className="px-2 py-3 text-[11px] text-muted-foreground/70">{emptyHint}</div>
      ) : (
        <ul className="list-window max-h-64 overflow-y-auto">
          {items.map((it) => (
            <li key={it.id} className="flex items-start gap-1.5 border-b border-border/40 px-2 py-1 last:border-0">
              <span className={cn('mt-1 session-dot', LEVEL_DOT[it.level])} />
              <span className="font-mono text-[10px] text-muted-foreground/70">{formatClock(it.at)}</span>
              <span className="flex-1 text-[11px] leading-4 text-foreground/90">{it.text}</span>
              {it.caliber && (
                <span className="mt-0.5 rounded border border-border px-1 font-mono text-[10px] text-muted-foreground">{it.caliber}</span>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
