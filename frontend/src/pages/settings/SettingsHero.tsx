import { Cpu, Download, Search, X } from 'lucide-react'
import { Button } from '@sida/base-ui/components/ui/button'
import { Input } from '@sida/base-ui/components/ui/input'
import { StatusChip } from '@sida/base-ui/components/ui/status-chip'
import { useSettings } from './context'
import { useI18n } from '@/hooks/useI18n'

export function SettingsHero() {
  const { t } = useI18n()
  const { services, allModels, enabledChannels, channels, globalQuery, setGlobalQuery, isOwner, exporting, exportTemplate, scrollTo } = useSettings()
  return <div className="card flex flex-wrap items-center justify-between gap-4 p-4">
    <div className="relative w-full sm:max-w-sm">
      <Search className="pointer-events-none absolute left-3 top-3 h-4 w-4 text-muted-foreground" />
      <Input value={globalQuery} onChange={e => setGlobalQuery(e.target.value)} placeholder={t('settings.searchPlaceholder')} aria-label={t('settings.searchPlaceholder')} className="pl-9 pr-10" />
      {globalQuery && <button type="button" onClick={() => setGlobalQuery('')} className="ui-icon-button absolute right-0.5 top-0.5" aria-label="清空设置搜索"><X className="h-4 w-4" /></button>}
    </div>
    <div className="flex flex-wrap items-center gap-2">
      {isOwner && <StatusChip>{services.length} 服务商 · {allModels.length} 模型</StatusChip>}
      <StatusChip>{enabledChannels.length}/{channels.length} 通知启用</StatusChip>
      {isOwner && <><Button variant="ghost" size="sm" onClick={exportTemplate} disabled={exporting}><Download className="h-3.5 w-3.5" />导出配置</Button><Button size="sm" onClick={() => scrollTo('sec-ai')}><Cpu className="h-3.5 w-3.5" />配置 AI</Button></>}
    </div>
  </div>
}
