import type { CSSProperties } from 'react'
import { cn } from '@/lib/utils'

/**
 * 通用骨架屏组件（亮/暗双态）。
 * 内置 shimmer 动画（CSS @keyframes sida-shimmer），比纯 animate-pulse 更接近最终内容闪烁节奏。
 *
 * 支持形状:
 * - text   : 单行文本条（可配 width / 多行）
 * - avatar : 圆形头像
 * - card   : 卡片块（标题 + 正文占位）
 * - table  : 表格行占位
 * - chart  : 图表区占位（K 线等）
 * - custom : 直接用 className 控制尺寸的原始块
 *
 * Usage:
 *   <Skeleton shape="text" lines={3} />
 *   <Skeleton shape="card" />
 *   <Skeleton shape="table" rows={5} />
 */

export type SkeletonShape = 'text' | 'avatar' | 'card' | 'table' | 'chart' | 'custom'

export interface SkeletonProps {
  shape?: SkeletonShape
  /** text 行数（shape='text'） */
  lines?: number
  /** table 行数（shape='table'） */
  rows?: number
  /** chart 高度（shape='chart'） */
  height?: number
  /** avatar 直径（shape='avatar'） */
  size?: number
  className?: string
  style?: CSSProperties
}

/* ── 基础闪烁块 ── */
function Shimmer({ className, style }: { className?: string; style?: CSSProperties }) {
  return (
    <div
      aria-hidden
      className={cn('sida-shimmer rounded-md bg-muted', className)}
      style={style}
    />
  )
}

/** 单行文本条（默认宽度随机感：70%~95%） */
export function SkeletonText({ lines = 1, className }: { lines?: number; className?: string }) {
  if (lines <= 1) {
    return <Shimmer className={cn('h-3 w-3/4', className)} />
  }
  return (
    <div className={cn('space-y-2', className)} aria-hidden>
      {Array.from({ length: lines }).map((_, i) => (
        <Shimmer
          key={i}
          className="h-3"
          style={{ width: i === lines - 1 ? '55%' : `${95 - i * 8}%` }}
        />
      ))}
    </div>
  )
}

/** 圆形头像 */
export function SkeletonAvatar({ size = 32, className }: { size?: number; className?: string }) {
  return (
    <Shimmer
      className={cn('rounded-full', className)}
      style={{ width: size, height: size }}
    />
  )
}

/** 卡片：顶部条 + 正文两行 + 底部操作条 */
export function SkeletonCard({ className }: { className?: string }) {
  return (
    <div
      aria-hidden
      className={cn('rounded-xl border border-border/50 bg-card p-4 space-y-3', className)}
    >
      <div className="flex items-center gap-3">
        <SkeletonAvatar size={32} />
        <div className="flex-1 space-y-2">
          <Shimmer className="h-3.5 w-1/3" />
          <Shimmer className="h-2.5 w-1/2" />
        </div>
        <Shimmer className="h-5 w-14 shrink-0" />
      </div>
      <SkeletonText lines={2} />
      <div className="flex gap-2 pt-1">
        <Shimmer className="h-7 w-16" />
        <Shimmer className="h-7 w-16" />
      </div>
    </div>
  )
}

/** 表格行：头像 + 两列文本 + 右侧数值 */
export function SkeletonTable({ rows = 5, className }: { rows?: number; className?: string }) {
  return (
    <div className={cn('space-y-2.5 py-1', className)} aria-hidden>
      {Array.from({ length: rows }).map((_, i) => (
        <div key={i} className="flex items-center gap-3">
          <SkeletonAvatar size={28} />
          <div className="min-w-0 flex-1 space-y-1.5">
            <Shimmer className="h-3 w-2/5" />
            <Shimmer className="h-2.5 w-3/5" />
          </div>
          <Shimmer className="h-3 w-16 shrink-0" />
          <Shimmer className="h-5 w-12 shrink-0" />
        </div>
      ))}
    </div>
  )
}

/** 图表区（K 线 / 走势图加载占位） */
export function SkeletonChart({ height = 280, className }: { height?: number; className?: string }) {
  return (
    <div
      aria-hidden
      className={cn('relative overflow-hidden rounded-xl border border-border/50 bg-card', className)}
      style={{ height }}
    >
      {/* 顶部标签 */}
      <div className="flex items-center gap-2 p-3">
        <Shimmer className="h-4 w-20" />
        <Shimmer className="h-4 w-14" />
        <Shimmer className="ml-auto h-4 w-16" />
      </div>
      {/* 主图区：竖条模拟 K 线 */}
      <div className="absolute inset-x-3 bottom-3 top-12 flex items-end gap-1.5">
        {Array.from({ length: 36 }).map((_, i) => {
          const h = 18 + ((i * 37) % 62) // 确定性伪随机高度，避免 hydration 抖动
          return (
            <div
              key={i}
              className="sida-shimmer flex-1 rounded-sm bg-muted"
              style={{ height: `${h}%`, animationDelay: `${(i % 8) * 0.08}s` }}
            />
          )
        })}
      </div>
    </div>
  )
}

export function Skeleton({ shape = 'custom', lines, rows, height, size, className, style }: SkeletonProps) {
  switch (shape) {
    case 'text':
      return <SkeletonText lines={lines ?? 1} className={className} />
    case 'avatar':
      return <SkeletonAvatar size={size ?? 32} className={className} />
    case 'card':
      return <SkeletonCard className={className} />
    case 'table':
      return <SkeletonTable rows={rows ?? 5} className={className} />
    case 'chart':
      return <SkeletonChart height={height ?? 280} className={className} />
    case 'custom':
    default:
      return <Shimmer className={className} style={style} />
  }
}

export default Skeleton
