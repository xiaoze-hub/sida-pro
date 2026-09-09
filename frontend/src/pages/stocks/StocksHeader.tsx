import { Bot } from 'lucide-react'
import { Building2 } from 'lucide-react'
import { Button } from '@panwatch/base-ui/components/ui/button'
import { Download } from 'lucide-react'
import { Plus } from 'lucide-react'
import { RefreshCw } from 'lucide-react'
import { Select } from '@panwatch/base-ui/components/ui/select'
import { SelectContent } from '@panwatch/base-ui/components/ui/select'
import { SelectItem } from '@panwatch/base-ui/components/ui/select'
import { SelectTrigger } from '@panwatch/base-ui/components/ui/select'
import { SelectValue } from '@panwatch/base-ui/components/ui/select'
import { Switch } from '@panwatch/base-ui/components/ui/switch'
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
    <>
{/* Header */}
<div className="flex flex-col gap-2 md:gap-3 mb-5 md:mb-6">
  <div className="flex items-center justify-between gap-2">
    <h1 className="text-[18px] md:text-[22px] font-bold text-foreground tracking-tight shrink-0">持仓</h1>
    {/* Desktop buttons + controls */}
    <div className="hidden md:flex items-center gap-3">
      {/* Controls */}
      <div className="flex items-center gap-2 px-2.5 py-1.5 rounded-md bg-accent/30">
        <div className="flex items-center gap-1.5">
          <Switch checked={autoRefresh} onCheckedChange={setAutoRefresh} className="scale-90" />
          <span className="text-[11px] text-muted-foreground">自动刷新</span>
          {autoRefresh && (
            <Select value={refreshInterval.toString()} onValueChange={v => setRefreshInterval(parseInt(v))}>
              <SelectTrigger className="h-6 w-14 text-[10px] px-1.5">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="10">10s</SelectItem>
                <SelectItem value="30">30s</SelectItem>
                <SelectItem value="60">1分钟</SelectItem>
                <SelectItem value="120">2分钟</SelectItem>
              </SelectContent>
            </Select>
          )}
        </div>
        {(poolSuggestionsLoading || Object.keys(poolSuggestions).length > 0) && (
          <>
            <div className="w-px h-4 bg-border" />
            <div className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
              {poolSuggestionsLoading && (
                <span className="w-3 h-3 border-2 border-primary/30 border-t-primary rounded-full animate-spin" />
              )}
              {!poolSuggestionsLoading && Object.keys(poolSuggestions).length > 0 && (
                <span className="text-[10px] text-primary">
                  {Object.keys(poolSuggestions).length}
                </span>
              )}
            </div>
          </>
        )}
        {lastRefreshTime && (
          <>
            <div className="w-px h-4 bg-border" />
            <span className="text-[10px] text-muted-foreground/60">
              {lastRefreshTime.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' })}
            </span>
          </>
        )}
      </div>
      {/* Buttons */}
      <Button variant="secondary" onClick={exportPortfolio}>
        <Download className="w-4 h-4" /> 导出
      </Button>
      <Button variant="secondary" onClick={handleRefresh} disabled={quotesLoading}>
        <RefreshCw className={`w-4 h-4 ${quotesLoading ? 'animate-spin' : ''}`} />
        刷新
      </Button>
      <Button variant="secondary" onClick={() => void scanAndReload()} disabled={scanning}>
        <Bot className="w-4 h-4" /> 扫描
      </Button>
      <Button variant="secondary" onClick={() => openAccountDialog()}>
        <Building2 className="w-4 h-4" /> 添加账户
      </Button>
      <Button onClick={() => { setStockForm(emptyStockForm); setSearchQuery(''); setShowStockForm(true) }}>
        <Plus className="w-4 h-4" /> 添加股票
      </Button>
    </div>
    {/* Mobile buttons */}
    <div className="flex md:hidden items-center gap-1.5">
      <Button variant="secondary" size="sm" className="h-8 w-8 p-0" onClick={exportPortfolio}>
        <Download className="w-4 h-4" />
      </Button>
      <Button variant="secondary" size="sm" className="h-8 w-8 p-0" onClick={handleRefresh} disabled={quotesLoading}>
        <RefreshCw className={`w-4 h-4 ${quotesLoading ? 'animate-spin' : ''}`} />
      </Button>
      <Button variant="secondary" size="sm" className="h-8 w-8 p-0" onClick={() => void scanAndReload()} disabled={scanning}>
        <Bot className="w-4 h-4" />
      </Button>
      <Button variant="secondary" size="sm" className="h-8 w-8 p-0" onClick={() => openAccountDialog()}>
        <Building2 className="w-4 h-4" />
      </Button>
      <Button size="sm" className="h-8 w-8 p-0" onClick={() => { setStockForm(emptyStockForm); setSearchQuery(''); setShowStockForm(true) }}>
        <Plus className="w-4 h-4" />
      </Button>
    </div>
  </div>

  {/* 移动端 row 2：市场状态 + 自动刷新 + 时间戳合并到同一行,横向滚动避免换行；桌面端只展示市场 pills (auto-refresh 在桌面顶部已展示) */}
  <div className="flex items-center gap-1.5 overflow-x-auto scrollbar-none -mx-1 px-1 md:flex-wrap md:overflow-visible">
    {marketStatus.map(m => {
      const statusColors: Record<string, string> = {
        trading: 'bg-emerald-500',
        pre_market: 'bg-amber-500',
        break: 'bg-amber-500',
        after_hours: 'bg-slate-400',
        closed: 'bg-slate-400',
      }
      return (
        <div
          key={m.code}
          className="shrink-0 flex items-center gap-1 md:gap-1.5"
          title={`${m.sessions.join(', ')} (${m.local_time}) · ${m.status_text}`}
        >
          <span className={`w-1.5 h-1.5 rounded-full ${statusColors[m.status] || 'bg-slate-400'}`} />
          <span className="text-[11px] text-muted-foreground">{m.name}</span>
          <span className={`text-[10px] ${m.is_trading ? 'text-emerald-600' : 'text-muted-foreground/60'} hidden sm:inline`}>
            {m.status_text}
          </span>
        </div>
      )
    })}
    {/* 移动端紧凑型自动刷新控件 */}
    <div className="flex md:hidden shrink-0 items-center gap-1 px-2 py-0.5 rounded-full bg-accent/30 ml-1">
      <Switch checked={autoRefresh} onCheckedChange={setAutoRefresh} className="scale-75" />
      {autoRefresh ? (
        <Select value={refreshInterval.toString()} onValueChange={v => setRefreshInterval(parseInt(v))}>
          <SelectTrigger className="h-5 w-12 text-[10px] px-1 border-0 bg-transparent">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="10">10s</SelectItem>
            <SelectItem value="30">30s</SelectItem>
            <SelectItem value="60">1分钟</SelectItem>
            <SelectItem value="120">2分钟</SelectItem>
          </SelectContent>
        </Select>
      ) : (
        <span className="text-[10px] text-muted-foreground">自动刷新</span>
      )}
      {poolSuggestionsLoading && (
        <span className="w-2.5 h-2.5 border-2 border-primary/30 border-t-primary rounded-full animate-spin" />
      )}
    </div>
    {lastRefreshTime && (
      <span className="md:hidden shrink-0 text-[10px] text-muted-foreground/60 font-mono ml-1">
        {lastRefreshTime.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' })}
      </span>
    )}
  </div>
</div>
    </>
  )
}
