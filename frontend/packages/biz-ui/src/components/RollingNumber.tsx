/**
 * 大数字逐位滚动(2026-09-20 设计系统 P1)。
 *
 * 依据: 现只有整块背景闪(FlashValue) —— 顶栏/带1 的大数字每次轮询都"整块闪一下", 读盘时是干扰。
 * 逐位滚动只让**变化的位**动, 其余数字钉住不动, 视觉连续得多。
 *
 * 实现: 按字符切分, 只给"与上一帧不同"的字符挂入场动画; 等宽数字字体保证不跳版。
 * prefers-reduced-motion 由 token 层把 --dur-* 归零 ⇒ 自动降级为瞬间替换。
 */
import { useEffect, useRef } from 'react'
import { cn } from '@panwatch/base-ui'

export default function RollingNumber({
  text,
  className,
  title,
}: {
  /** 已格式化好的字符串(格式化不在这里做 —— 数字口径由调用方负责) */
  text: string
  className?: string
  title?: string
}) {
  const prev = useRef<string[]>([])
  const chars = Array.from(text)
  const changed = chars.map((c, i) => prev.current[i] !== c)
  useEffect(() => {
    prev.current = chars
  })

  return (
    <span className={cn('inline-flex font-mono tabular-nums', className)} title={title}>
      {chars.map((c, i) => (
        <span
          // 变化的位换 key ⇒ 重放一次入场动画; 未变化的位 key 稳定 ⇒ 不动
          key={changed[i] ? `${i}-${c}-${prev.current[i] ?? ''}` : `${i}-${c}`}
          className={changed[i] ? 'sida-digit-in' : undefined}
          aria-hidden={!changed[i] && i > 0 ? undefined : undefined}
        >
          {c}
        </span>
      ))}
      <span className="sr-only">{text}</span>
    </span>
  )
}
