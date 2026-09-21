import { useCallback, useEffect, useState } from 'react'

/**
 * 列表键盘协议(2026-09-20 设计系统 P0: J/K 行移动、Enter 进工作台、/ 搜索、Esc 关闭)。
 *
 * 依据: 列表页此前只有 `g d/g p/g o` 这种"跳页"序列键, **行级**键盘操作完全没有 ——
 * 复盘时只能鼠标逐行点。行情终端的专业感一半来自"手不离键盘"。
 *
 * 边界(诚实): 焦点在输入框/可编辑区时不劫持键盘; 不吞掉浏览器原生快捷键(带 meta/ctrl/alt 的一律放行)。
 */
export interface RowNavOptions {
  count: number
  onEnter: (index: number) => void
  enabled?: boolean
  onSlash?: () => void
  onEscape?: () => void
}

export function useRowNav({ count, onEnter, enabled = true, onSlash, onEscape }: RowNavOptions) {
  const [index, setIndex] = useState(-1)

  useEffect(() => {
    // 行数变少时把游标夹回范围内(否则 Enter 会打到不存在的行)
    setIndex((i) => (i >= count ? count - 1 : i))
  }, [count])

  useEffect(() => {
    if (!enabled) return
    const handler = (e: KeyboardEvent) => {
      if (e.metaKey || e.ctrlKey || e.altKey) return
      const t = e.target as HTMLElement | null
      const tag = t?.tagName?.toLowerCase()
      if (tag === 'input' || tag === 'textarea' || tag === 'select' || t?.isContentEditable) return
      const k = e.key.toLowerCase()
      if (k === 'j' || e.key === 'ArrowDown') {
        e.preventDefault()
        setIndex((i) => Math.min(i + 1, count - 1))
      } else if (k === 'k' || e.key === 'ArrowUp') {
        e.preventDefault()
        setIndex((i) => Math.max(i - 1, 0))
      } else if (e.key === 'Enter') {
        if (index >= 0 && index < count) {
          e.preventDefault()
          onEnter(index)
        }
      } else if (e.key === '/') {
        if (onSlash) {
          e.preventDefault()
          onSlash()
        }
      } else if (e.key === 'Escape') {
        if (onEscape) onEscape()
        else setIndex(-1)
      }
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [count, enabled, index, onEnter, onSlash, onEscape])

  /** 行的可访问性/三态 class: 键盘焦点 + 选中态 + 热区下限, 一处给全。 */
  const rowProps = useCallback(
    (i: number) => ({
      tabIndex: 0,
      'aria-selected': i === index,
      'data-row-index': i,
      className: `row-hit row-hover row-focusable${i === index ? ' row-selected' : ''}`,
      onFocus: () => setIndex(i),
    }),
    [index],
  )

  return { index, setIndex, rowProps }
}
