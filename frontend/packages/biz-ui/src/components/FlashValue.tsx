import { useEffect, useRef, useState } from 'react'
import { cn } from '@panwatch/base-ui'

/**
 * 涨跌闪(2026-09-05 UI 全量改造 Wave1)。
 * value 变大闪红 / 变小闪绿 / 首挂载不闪。颜色走 --stock-up/down 令牌(买红卖绿)。
 * 用 key 切换重触发 CSS 动画, 比 setTimeout 稳。
 */
export default function FlashValue({
  value,
  children,
  className,
  pctRef,
}: {
  value: number | null | undefined
  children: React.ReactNode
  className?: string
  /** 视为"满强度"的涨跌幅(%); 缺省 3 —— 与热力图/涨跌色夹紧口径一致 */
  pctRef?: number
}) {
  const [flash, setFlash] = useState<'up' | 'down' | null>(null)
  const [dur, setDur] = useState<string | null>(null)
  const prevRef = useRef<number | null>(null)
  const timerRef = useRef<number>(0)
  const keyRef = useRef(0)

  useEffect(() => {
    const prev = prevRef.current
    prevRef.current = value ?? null
    if (value == null || prev == null || value === prev) return
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return
    setFlash(value > prev ? 'up' : 'down')
    keyRef.current += 1
    // 强度随 |Δ| 映射并**夹顶**(2026-09-20): 大单要看得见, 小单不许满屏闪。
    // |Δ| 相对 pctRef(默认 3% 视为满强度)线性映射到 --dur-flash..--dur-flash-max,
    // 超过就封顶 —— 不再"越大越闪"。
    const span = Math.abs(value - prev)
    const ref = pctRef ?? 3
    const ratio = Math.min(Math.max(span / (ref * 0.01 * Math.max(Math.abs(value), 1)), 0), 1)
    const durMs = 420 + (700 - 420) * ratio
    setDur(`${Math.round(durMs)}ms`)
    window.clearTimeout(timerRef.current)
    timerRef.current = window.setTimeout(() => setFlash(null), durMs + 40)
    return () => window.clearTimeout(timerRef.current)
  }, [value, pctRef])

  if (!flash) return <span className={className}>{children}</span>
  return (
    <span
      key={keyRef.current}
      style={dur ? ({ ['--flash-dur' as string]: dur } as React.CSSProperties) : undefined}
      className={cn(
        'sida-flash',
        flash === 'up' ? 'sida-flash-up' : 'sida-flash-down',
        className,
      )}
    >
      {children}
    </span>
  )
}
