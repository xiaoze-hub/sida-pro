import type { ReactNode } from 'react'

export interface StreamingBeamProps {
  active?: boolean
  className?: string
  children?: ReactNode
}

/**
 * 实时推演/生成态流光边框 (React Bits 流光卡片模式)
 * 当 active 为 true 时，在卡片四周渲染柔和的微光与流动轮廓，
 * 避免死板的灰框与生硬的转圈。
 */
export function StreamingBeam({
  active = true,
  className = '',
  children,
}: StreamingBeamProps) {
  if (!active) return <>{children}</>

  return (
    <div className={`relative rounded-xl ${className}`}>
      {/* 柔和外发光层 */}
      <div
        aria-hidden="true"
        className="pointer-events-none absolute -inset-[1px] rounded-[inherit] opacity-60 blur-[2px] transition-opacity duration-500 animate-pulse bg-gradient-to-r from-role-watch/30 via-role-opp/20 to-role-watch/30"
      />
      {/* 边缘细线微流光 */}
      <div
        aria-hidden="true"
        className="pointer-events-none absolute -inset-[1px] rounded-[inherit] p-[1px] overflow-hidden"
      >
        <div className="absolute inset-[-100%] animate-[spin_4s_linear_infinite] bg-[conic-gradient(from_0deg_at_50%_50%,transparent_0deg,hsl(var(--role-watch)/0.6)_60deg,transparent_120deg,hsl(var(--role-watch)/0.3)_240deg,transparent_360deg)] opacity-70" />
      </div>
      {/* 内部原内容（保持高于流光层） */}
      <div className="relative rounded-[inherit] z-10 bg-inherit">
        {children}
      </div>
    </div>
  )
}
