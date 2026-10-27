import PageHeader from '@/components/PageHeader'
import MarketStatusPills from '@/components/MarketStatusPills'
import { StatusChip } from '@sida/base-ui/components/ui/status-chip'
import { Bot } from 'lucide-react'
import { Building2 } from 'lucide-react'
import { Button } from '@sida/base-ui/components/ui/button'
import { Download } from 'lucide-react'
import { Plus } from 'lucide-react'
import { RefreshCw } from 'lucide-react'
import { Select } from '@sida/base-ui/components/ui/select'
import { SelectContent } from '@sida/base-ui/components/ui/select'
import { SelectItem } from '@sida/base-ui/components/ui/select'
import { SelectTrigger } from '@sida/base-ui/components/ui/select'
import { SelectValue } from '@sida/base-ui/components/ui/select'
import { Switch } from '@sida/base-ui/components/ui/switch'
import { emptyStockForm } from './shared'
import { useStocks } from './context'

export function StocksHeader() {
  const {
    quotesLoading,
    autoRefresh,
    setAutoRefresh,
    refreshInterval,
    setRefreshInterval,
    lastRefreshTime,
    scanning,
    poolSuggestions,
    poolSuggestionsLoading,
    marketStatus,
    setShowStockForm,
    setStockForm,
    setSearchQuery,
    handleRefresh,
    scanAndReload,
    exportPortfolio,
    openAccountDialog,
  } = useStocks()
  return (
    <PageHeader title="持仓" actions={<>
      <Button variant="ghost" size="sm" onClick={exportPortfolio}><Download className="mr-1.5 h-4 w-4" />导出</Button>
      <Button variant="outline" size="sm" onClick={handleRefresh} disabled={quotesLoading}><RefreshCw className={`mr-1.5 h-4 w-4 ${quotesLoading ? 'animate-spin' : ''}`} />刷新</Button>
      <Button variant="ghost" size="sm" onClick={() => void scanAndReload()} disabled={scanning}><Bot className="mr-1.5 h-4 w-4" />扫描</Button>
      <Button variant="ghost" size="sm" onClick={() => openAccountDialog()}><Building2 className="mr-1.5 h-4 w-4" />添加账户</Button>
      <Button size="sm" onClick={() => { setStockForm(emptyStockForm); setSearchQuery(''); setShowStockForm(true) }}><Plus className="mr-1.5 h-4 w-4" />添加股票</Button>
    </>} metadata={<>
      <MarketStatusPills items={marketStatus} />
      <div className="flex min-h-9 items-center gap-2 rounded-full bg-s2 px-3 py-1 text-[12px]">
        <Switch checked={autoRefresh} onCheckedChange={setAutoRefresh} aria-label="自动刷新行情" />
        <span className="text-muted-foreground">自动刷新</span>
        {autoRefresh && <Select value={refreshInterval.toString()} onValueChange={v => setRefreshInterval(parseInt(v))}>
          <SelectTrigger className="h-8 w-20 text-[12px]" aria-label="刷新间隔"><SelectValue /></SelectTrigger>
          <SelectContent><SelectItem value="10">10秒</SelectItem><SelectItem value="30">30秒</SelectItem><SelectItem value="60">1分钟</SelectItem><SelectItem value="120">2分钟</SelectItem></SelectContent>
        </Select>}
      </div>
      {lastRefreshTime && <StatusChip>更新于 <time className="font-mono">{lastRefreshTime.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' })}</time></StatusChip>}
      {(poolSuggestionsLoading || Object.keys(poolSuggestions).length > 0) && <StatusChip tone="info">{poolSuggestionsLoading ? '分析中…' : `${Object.keys(poolSuggestions).length} 项技术读数`}</StatusChip>}
    </>} />
  )
}
