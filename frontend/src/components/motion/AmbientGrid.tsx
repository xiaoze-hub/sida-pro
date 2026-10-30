export interface AmbientGridProps {
  className?: string
  gridSize?: number
}

/**
 * 科技暗黑微质感网格背景 (React Bits Dot/Grid Pattern 模式)
 * 为数智参谋空态/Hero 区域提供沉浸式量化终端质感，
 * 采用原生 SVG pattern，0 体积开销，自适应亮/暗模式。
 */
export function AmbientGrid({
  className = '',
  gridSize = 24,
}: AmbientGridProps) {
  return (
    <div
      aria-hidden="true"
      className={`pointer-events-none absolute inset-0 overflow-hidden select-none [mask-image:radial-gradient(ellipse_60%_50%_at_50%_40%,#000_70%,transparent_100%)] ${className}`}
    >
      <svg
        className="absolute inset-0 h-full w-full stroke-foreground/[0.04] [stroke-dasharray:2_2]"
        xmlns="http://www.w3.org/2000/svg"
      >
        <defs>
          <pattern
            id="ambient-copilot-grid"
            width={gridSize}
            height={gridSize}
            patternUnits="userSpaceOnUse"
          >
            <path d={`M.5 ${gridSize}V.5H${gridSize}`} fill="none" />
            <circle cx="0.5" cy="0.5" r="0.75" className="fill-foreground/[0.08]" />
          </pattern>
        </defs>
        <rect width="100%" height="100%" strokeWidth={0} fill="url(#ambient-copilot-grid)" />
      </svg>
    </div>
  )
}
