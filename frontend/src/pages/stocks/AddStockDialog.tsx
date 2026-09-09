import { Badge } from '@panwatch/base-ui/components/ui/badge'
import { Button } from '@panwatch/base-ui/components/ui/button'
import { Dialog } from '@panwatch/base-ui/components/ui/dialog'
import { DialogContent } from '@panwatch/base-ui/components/ui/dialog'
import { DialogDescription } from '@panwatch/base-ui/components/ui/dialog'
import { DialogHeader } from '@panwatch/base-ui/components/ui/dialog'
import { DialogTitle } from '@panwatch/base-ui/components/ui/dialog'
import { Input } from '@panwatch/base-ui/components/ui/input'
import { Label } from '@panwatch/base-ui/components/ui/label'
import { RefreshCw } from 'lucide-react'
import { Search } from 'lucide-react'
import { useStocks } from './context'

export function AddStockDialog() {
  const {
    showStockForm,
    setShowStockForm,
    stockForm,
    searchQuery,
    setSearchQuery,
    searchMarket,
    setSearchMarket,
    searchResults,
    showDropdown,
    setShowDropdown,
    searching,
    refreshingStockList,
    dropdownRef,
    handleSearchInput,
    handleSearchMarketChange,
    refreshStockListCache,
    selectStock,
    handleStockSubmit,
    marketLabel,
  } = useStocks()
  return (
    <>
{/* Add Stock Dialog */}
<Dialog open={showStockForm} onOpenChange={(open) => { setShowStockForm(open); if (!open) { setSearchQuery(''); setSearchMarket('') } }}>
  <DialogContent className="max-w-lg">
    <DialogHeader>
      <DialogTitle>添加股票到自选</DialogTitle>
      <DialogDescription>搜索并添加到自选股列表</DialogDescription>
    </DialogHeader>
    <form onSubmit={handleStockSubmit}>
      <div className="relative" ref={dropdownRef}>
        <div className="flex items-center gap-2 mb-2">
          <Label className="mb-0">搜索股票</Label>
          <div className="flex items-center gap-1">
            {[
              { value: '', label: '全部' },
              { value: 'CN', label: 'A股' },
              { value: 'HK', label: '港股' },
              { value: 'US', label: '美股' },
            ].map(opt => (
              <button
                key={opt.value}
                type="button"
                onClick={() => handleSearchMarketChange(opt.value)}
                className={`text-[11px] px-2 py-0.5 rounded transition-colors ${
                  searchMarket === opt.value
                    ? 'bg-primary text-primary-foreground'
                    : 'bg-accent/50 text-muted-foreground hover:bg-accent'
                }`}
              >
                {opt.label}
              </button>
            ))}
          </div>
          <button
            type="button"
            onClick={refreshStockListCache}
            disabled={refreshingStockList}
            className="text-[10px] text-muted-foreground hover:text-foreground transition-colors ml-2"
            title="搜索不到？点击刷新股票列表"
          >
            {refreshingStockList ? (
              <span className="flex items-center gap-1">
                <RefreshCw className="w-3 h-3 animate-spin" /> 刷新中...
              </span>
            ) : (
              <span className="flex items-center gap-1">
                <RefreshCw className="w-3 h-3" /> 刷新列表
              </span>
            )}
          </button>
        </div>
        <div className="relative">
          <Search className="absolute left-3.5 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground/50" />
          <Input
            value={searchQuery}
            onChange={e => handleSearchInput(e.target.value)}
            onFocus={() => searchResults.length > 0 && setShowDropdown(true)}
            placeholder={searchMarket === 'HK' ? '代码或名称，如 00700 或 腾讯' : searchMarket === 'US' ? '代码或名称，如 AAPL 或 苹果' : '代码或名称，如 600519 或 茅台'}
            className="pl-10"
            autoComplete="off"
          />
          {searching && <span className="absolute right-3.5 top-1/2 -translate-y-1/2 w-4 h-4 border-2 border-primary/30 border-t-primary rounded-full animate-spin" />}
        </div>
        {showDropdown && (
          <div className="absolute z-50 w-full mt-2 max-h-64 overflow-auto scrollbar card shadow-lg">
            {searchResults.map(item => (
              <button
                key={`${item.market}-${item.symbol}`}
                type="button"
                onClick={() => selectStock(item)}
                className="w-full flex items-center gap-3 px-4 py-3 text-[13px] hover:bg-accent/50 text-left transition-colors"
              >
                <span className="font-mono text-muted-foreground text-[12px] w-14">{item.symbol}</span>
                <span className="flex-1 font-medium text-foreground">{item.name}</span>
                <Badge variant="secondary">{marketLabel(item.market)}</Badge>
              </button>
            ))}
          </div>
        )}
        {stockForm.symbol && (
          <div className="mt-2.5 flex items-center gap-2">
            <Badge><span className="font-mono">{stockForm.symbol}</span> {stockForm.name}</Badge>
            <Badge variant="secondary">{marketLabel(stockForm.market)}</Badge>
          </div>
        )}
      </div>
      <div className="mt-6 flex items-center gap-3 justify-end">
        <Button type="button" variant="ghost" onClick={() => { setShowStockForm(false); setSearchQuery('') }}>取消</Button>
        <Button type="submit" disabled={!stockForm.symbol}>确认添加</Button>
      </div>
    </form>
  </DialogContent>
</Dialog>
    </>
  )
}
