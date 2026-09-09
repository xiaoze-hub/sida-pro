import { useInsight } from './context'
import { ExternalLink } from 'lucide-react'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@panwatch/base-ui/components/ui/select'
import { formatTime } from './helpers'

export function NewsTab() {
  const {
    newsHours,
    setNewsHours,
    news,
  } = useInsight()
  return (
    <div className="space-y-3">
      <div className="flex items-center justify-end">
        <Select value={newsHours} onValueChange={setNewsHours}>
          <SelectTrigger className="h-8 w-[110px] text-[12px]">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="6">近6小时</SelectItem>
            <SelectItem value="12">近12小时</SelectItem>
            <SelectItem value="24">近24小时</SelectItem>
            <SelectItem value="48">近48小时</SelectItem>
            <SelectItem value="168">近7天</SelectItem>
          </SelectContent>
        </Select>
      </div>
      {news.length === 0 ? (
        <div className="card p-6 text-[12px] text-muted-foreground text-center">暂无相关新闻</div>
      ) : (
        news.map((item, idx) => (
          <a
            key={`${item.publish_time || 'n'}-${idx}`}
            href={item.url}
            target="_blank"
            rel="noreferrer"
            className="card block p-4 hover:bg-accent/20 transition-colors"
          >
            <div className="flex items-center justify-between gap-2">
              <div className="text-[13px] font-medium text-foreground line-clamp-2">{item.title}</div>
              <ExternalLink className="w-3.5 h-3.5 text-muted-foreground shrink-0" />
            </div>
            <div className="mt-2 text-[11px] text-muted-foreground">{item.source_label || item.source} · {formatTime(item.publish_time)}</div>
          </a>
        ))
      )}
    </div>
  )
}
