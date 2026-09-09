import { Search } from 'lucide-react'
import { useSettings } from './context'

export function SearchEmptyState() {
  const {
    globalQuery,
    setGlobalQuery,
    hasGlobalQuery,
    matchCount,
  } = useSettings()
  return (
    <>
  {/* 2026-08-17 全局搜索空态: 有搜索但 0 命中(闭环修正 P0-1) */}
  {hasGlobalQuery && matchCount === 0 && (
    <div className="col-span-full p-8 md:p-10 text-center">
      <Search className="mx-auto h-7 w-7 text-muted-foreground/50 mb-3" />
      <p className="text-[13px] text-muted-foreground">
        未找到匹配 &ldquo;<span className="text-foreground font-medium">{globalQuery}</span>&rdquo; 的设置项
      </p>
      <p className="text-[11px] text-muted-foreground/70 mt-2">
        试试搜索关键词,如 openai / 微信 / 凭证 / 主题
      </p>
      <button
        type="button"
        onClick={() => setGlobalQuery('')}
        className="mt-3 text-[11px] text-primary hover:text-primary/80 transition-colors"
      >
        清空搜索
      </button>
    </div>
  )}
    </>
  )
}
