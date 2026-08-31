import { Skeleton } from '@panwatch/base-ui/components/ui/skeleton'

/**
 * 轻量骨架屏:列表行占位(animate-pulse 灰块),复用于 Dashboard / Notifications 首次加载期。
 * 仅首次加载展示(数据为空时),自动刷新/手动刷新不闪骨架。
 */
export default function SkeletonRows({ rows = 5 }: { rows?: number }) {
  return (
    <div className="space-y-2.5 py-1">
      {Array.from({ length: rows }).map((_, i) => (
        <div key={i} className="flex items-center gap-3">
          <Skeleton className="h-8 w-8 shrink-0 rounded-full" />
          <div className="min-w-0 flex-1 space-y-1.5">
            <Skeleton className="h-3 w-2/5" />
            <Skeleton className="h-2.5 w-4/5" />
          </div>
          <Skeleton className="h-5 w-12 shrink-0" />
        </div>
      ))}
    </div>
  )
}
