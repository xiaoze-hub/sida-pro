import { Button } from '@panwatch/base-ui/components/ui/button'
import { Dialog } from '@panwatch/base-ui/components/ui/dialog'
import { DialogContent } from '@panwatch/base-ui/components/ui/dialog'
import { DialogDescription } from '@panwatch/base-ui/components/ui/dialog'
import { DialogHeader } from '@panwatch/base-ui/components/ui/dialog'
import { DialogTitle } from '@panwatch/base-ui/components/ui/dialog'
import { Input } from '@panwatch/base-ui/components/ui/input'
import { Label } from '@panwatch/base-ui/components/ui/label'
import { Search } from 'lucide-react'
import { Select } from '@panwatch/base-ui/components/ui/select'
import { SelectContent } from '@panwatch/base-ui/components/ui/select'
import { SelectItem } from '@panwatch/base-ui/components/ui/select'
import { SelectTrigger } from '@panwatch/base-ui/components/ui/select'
import { SelectValue } from '@panwatch/base-ui/components/ui/select'
import { X } from 'lucide-react'
import { useStocks } from './context'

export function PositionDialog() {
  const {
    accounts,
    positionDialogOpen,
    setPositionDialogOpen,
    positionForm,
    setPositionForm,
    editPositionId,
    positionDialogAccountId,
    positionSearchQuery,
    setPositionSearchQuery,
    positionSearchMarket,
    setPositionSearchMarket,
    positionSearchResults,
    setPositionSearchResults,
    positionSearching,
    showPositionDropdown,
    setShowPositionDropdown,
    positionDropdownRef,
    handlePositionSearchInput,
    handlePositionSearchMarketChange,
    selectPositionStock,
    handlePositionSubmit,
    marketBadge,
  } = useStocks()
  return (
    <>
{/* Position Dialog */}
<Dialog
  open={positionDialogOpen}
  onOpenChange={(open) => {
    setPositionDialogOpen(open)
    if (!open) {
      setPositionSearchQuery('')
      setPositionSearchResults([])
      setShowPositionDropdown(false)
      setPositionSearchMarket('')
    }
  }}
>
  <DialogContent>
    <DialogHeader>
      <DialogTitle>{editPositionId ? '编辑持仓' : '添加持仓'}</DialogTitle>
      <DialogDescription>
        {accounts.find(a => a.id === positionDialogAccountId)?.name} 账户持仓
      </DialogDescription>
    </DialogHeader>
    <div className="space-y-4 mt-2">
      {editPositionId ? (
        <div className="flex items-center gap-2 px-3 py-2 rounded-md bg-accent/30">
          <span className={`text-[9px] px-1.5 py-0.5 rounded ${marketBadge(positionForm.stock_market).style}`}>
            {marketBadge(positionForm.stock_market).label}
          </span>
          <span className="font-mono text-[12px] text-muted-foreground">{positionForm.stock_symbol}</span>
          <span className="text-[13px] text-foreground">{positionForm.stock_name}</span>
        </div>
      ) : (
        <div>
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
                  onClick={() => handlePositionSearchMarketChange(opt.value)}
                  className={`text-[11px] px-2 py-0.5 rounded transition-colors ${
                    positionSearchMarket === opt.value
                      ? 'bg-primary text-primary-foreground'
                      : 'bg-accent/50 text-muted-foreground hover:bg-accent'
                  }`}
                >
                  {opt.label}
                </button>
              ))}
            </div>
          </div>
          <div className="relative" ref={positionDropdownRef}>
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground/50" />
            <Input
              value={positionSearchQuery}
              onChange={e => handlePositionSearchInput(e.target.value)}
              onFocus={() => positionSearchResults.length > 0 && setShowPositionDropdown(true)}
              placeholder={positionSearchMarket === 'HK' ? '代码或名称，如 00700 或 腾讯' : positionSearchMarket === 'US' ? '代码或名称，如 LI 或 理想汽车' : positionSearchMarket === 'CN' ? '代码或名称，如 600519 或 茅台' : '代码或名称，如 600519 / 00700 / AAPL'}
              className="pl-9"
              autoComplete="off"
            />
            {positionSearching && <span className="absolute right-3 top-1/2 -translate-y-1/2 w-4 h-4 border-2 border-primary/30 border-t-primary rounded-full animate-spin" />}
            {showPositionDropdown && positionSearchResults.length > 0 && (
              <div className="absolute z-50 w-full mt-1 max-h-48 overflow-auto scrollbar card shadow-lg">
                {positionSearchResults.map(item => (
                  <button
                    key={`${item.market}-${item.symbol}`}
                    type="button"
                    onClick={() => selectPositionStock(item)}
                    className="w-full flex items-center gap-2 px-3 py-2 text-[13px] hover:bg-accent/50 text-left transition-colors"
                  >
                    <span className={`text-[9px] px-1 py-0.5 rounded ${marketBadge(item.market).style}`}>
                      {marketBadge(item.market).label}
                    </span>
                    <span className="font-mono text-muted-foreground text-[12px]">{item.symbol}</span>
                    <span className="flex-1 text-foreground">{item.name}</span>
                  </button>
                ))}
              </div>
            )}
          </div>
          {positionForm.stock_symbol && (
            <div className="mt-2 flex items-center gap-2">
              <span className={`text-[9px] px-1.5 py-0.5 rounded ${marketBadge(positionForm.stock_market).style}`}>
                {marketBadge(positionForm.stock_market).label}
              </span>
              <span className="font-mono text-[12px] text-muted-foreground">{positionForm.stock_symbol}</span>
              <span className="text-[13px] text-foreground">{positionForm.stock_name}</span>
              <button
                type="button"
                onClick={() => {
                  setPositionForm({ ...positionForm, stock_id: 0, stock_symbol: '', stock_name: '', stock_market: '' })
                  setPositionSearchQuery('')
                }}
                className="ml-1 text-muted-foreground hover:text-destructive"
              >
                <X className="w-3.5 h-3.5" />
              </button>
            </div>
          )}
        </div>
      )}
      <div className="grid grid-cols-2 gap-4">
        <div>
          <Label>成本价</Label>
          <Input
            value={positionForm.cost_price}
            onChange={e => setPositionForm({ ...positionForm, cost_price: e.target.value })}
            placeholder="0.00"
            className="font-mono"
            inputMode="decimal"
          />
        </div>
        <div>
          <Label>持仓数量</Label>
          <Input
            value={positionForm.quantity}
            onChange={e => setPositionForm({ ...positionForm, quantity: e.target.value })}
            placeholder="0"
            className="font-mono"
            inputMode="numeric"
          />
        </div>
      </div>
      <div className="grid grid-cols-2 gap-4">
        <div>
          <Label>投入资金 <span className="text-muted-foreground/60 text-[11px]">(选填)</span></Label>
          <Input
            value={positionForm.invested_amount}
            onChange={e => setPositionForm({ ...positionForm, invested_amount: e.target.value })}
            placeholder="选填"
            className="font-mono"
            inputMode="decimal"
          />
        </div>
        <div>
          <Label>交易风格 <span className="text-muted-foreground font-normal">(选填)</span></Label>
          <Select
            value={positionForm.trading_style}
            onValueChange={val => setPositionForm({ ...positionForm, trading_style: val === '__none__' ? '' : val })}
          >
            <SelectTrigger>
              <SelectValue placeholder="不设置" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="__none__">不设置</SelectItem>
              <SelectItem value="short">短线 (1-5天)</SelectItem>
              <SelectItem value="swing">波段 (1-4周)</SelectItem>
              <SelectItem value="long">长线 (数月)</SelectItem>
            </SelectContent>
          </Select>
        </div>
      </div>
      <div className="flex justify-end gap-2 pt-2">
        <Button variant="ghost" onClick={() => setPositionDialogOpen(false)}>取消</Button>
        <Button
          onClick={handlePositionSubmit}
          disabled={!positionForm.cost_price || !positionForm.quantity || (!editPositionId && !positionForm.stock_id && !positionForm.stock_symbol)}
        >
          {editPositionId ? '保存' : '添加'}
        </Button>
      </div>
    </div>
  </DialogContent>
</Dialog>
    </>
  )
}
