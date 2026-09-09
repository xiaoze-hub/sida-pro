import { useInsight } from './context'
import { ExternalLink } from 'lucide-react'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@panwatch/base-ui/components/ui/select'
import { formatTime } from './helpers'

export function AnnouncementsTab() {
  const {
    announcementHours,
    setAnnouncementHours,
    announcements,
  } = useInsight()
  return (
    <div className="space-y-3">
      <div className="flex items-center justify-end">
        <Select value={announcementHours} onValueChange={setAnnouncementHours}>
          <SelectTrigger className="h-8 w-[110px] text-[12px]">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="168">近7天</SelectItem>
            <SelectItem value="336">近14天</SelectItem>
            <SelectItem value="720">近30天</SelectItem>
            <SelectItem value="2160">近90天</SelectItem>
            <SelectItem value="4320">近180天</SelectItem>
            <SelectItem value="24">近24小时</SelectItem>
            <SelectItem value="48">近48小时</SelectItem>
            <SelectItem value="72">近72小时</SelectItem>
          </SelectContent>
        </Select>
      </div>
      {announcements.length === 0 ? (
        <div className="card p-6 text-[12px] text-muted-foreground text-center">暂无公告</div>
      ) : (
        announcements.map((item, idx) => (
          <a
            key={`${item.publish_time || 'a'}-${idx}`}
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
