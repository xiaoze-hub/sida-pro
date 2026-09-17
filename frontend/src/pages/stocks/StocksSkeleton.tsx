import { Skeleton } from '@/components/Skeleton'

/**
 * 持仓/自选页首屏骨架（亮/暗双态 + shimmer）。
 * 覆盖 Header / 汇总卡片 / 账户列表 三段，避免加载期整页空白。
 */
export function StocksSkeleton() {
  return (
    <div aria-busy aria-live="polite">
      {/* Header Skeleton */}
      <div className="mb-6 flex items-center justify-between">
        <div>
          <Skeleton className="mb-2 h-6 w-16" />
          <Skeleton className="h-4 w-32" />
        </div>
        <div className="hidden items-center gap-3 md:flex">
          <Skeleton className="h-9 w-24" />
          <Skeleton className="h-9 w-24" />
          <Skeleton className="h-9 w-24" />
        </div>
      </div>
      {/* Summary Cards Skeleton */}
      <div className="mb-6 grid grid-cols-2 gap-3 md:grid-cols-4 md:gap-4">
        {[...Array(4)].map((_, i) => (
          <div key={i} className="rounded-lg border border-border/40 p-3">
            <Skeleton className="mb-2 h-4 w-16" />
            <Skeleton className="h-6 w-24" />
          </div>
        ))}
      </div>
      {/* K 线占位（行情 tab 加载时可见，账户列表先出） */}
      <div className="mb-6">
        <Skeleton shape="chart" height={220} />
      </div>
      {/* Account List Skeleton */}
      <div className="space-y-4">
        {[...Array(2)].map((_, i) => (
          <div key={i}>
            <div className="border-b border-border/50 px-4 py-3">
              <Skeleton className="h-5 w-32" />
            </div>
            <div className="divide-y divide-border/50">
              {[...Array(3)].map((_, j) => (
                <div key={j} className="flex items-center gap-4 px-4 py-3">
                  <Skeleton className="h-4 w-16" />
                  <Skeleton className="h-4 w-24" />
                  <Skeleton className="ml-auto h-4 w-16" />
                </div>
              ))}
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}
