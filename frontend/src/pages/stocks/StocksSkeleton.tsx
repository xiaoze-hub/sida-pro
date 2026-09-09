import { Skeleton } from '@panwatch/base-ui/components/ui/skeleton'
export function StocksSkeleton() {
return (
  <div>
    {/* Header Skeleton */}
    <div className="flex items-center justify-between mb-6">
      <div>
        <Skeleton className="h-6 w-16 mb-2" />
        <Skeleton className="h-4 w-32" />
      </div>
      <div className="hidden md:flex items-center gap-3">
        <Skeleton className="h-9 w-24" />
        <Skeleton className="h-9 w-24" />
        <Skeleton className="h-9 w-24" />
      </div>
    </div>
    {/* Summary Cards Skeleton */}
    <div className="grid grid-cols-2 md:grid-cols-4 gap-3 md:gap-4 mb-6">
      {[...Array(4)].map((_, i) => (
        <div key={i}>
          <Skeleton className="h-4 w-16 mb-2" />
          <Skeleton className="h-6 w-24" />
        </div>
      ))}
    </div>
    {/* Account List Skeleton */}
    <div className="space-y-4">
      {[...Array(2)].map((_, i) => (
        <div key={i}>
          <div className="px-4 py-3 border-b border-border/50">
            <Skeleton className="h-5 w-32" />
          </div>
          <div className="divide-y divide-border/50">
            {[...Array(3)].map((_, j) => (
              <div key={j} className="px-4 py-3 flex items-center gap-4">
                <Skeleton className="h-4 w-16" />
                <Skeleton className="h-4 w-24" />
                <Skeleton className="h-4 w-16 ml-auto" />
              </div>
            ))}
          </div>
        </div>
      ))}
    </div>
  </div>
)
}
