import { Button } from '@panwatch/base-ui/components/ui/button'
import { Dialog } from '@panwatch/base-ui/components/ui/dialog'
import { DialogContent } from '@panwatch/base-ui/components/ui/dialog'
import { DialogDescription } from '@panwatch/base-ui/components/ui/dialog'
import { DialogHeader } from '@panwatch/base-ui/components/ui/dialog'
import { DialogTitle } from '@panwatch/base-ui/components/ui/dialog'
import { Input } from '@panwatch/base-ui/components/ui/input'
import { Label } from '@panwatch/base-ui/components/ui/label'
import { useStocks } from './context'

export function WatchlistDialogs() {
  const {
    accountDialogOpen,
    setAccountDialogOpen,
    accountForm,
    setAccountForm,
    editAccountId,
    removeWatchStock,
    setRemoveWatchStock,
    removingWatchStock,
    hasAnyPositionForStockId,
    removeFromWatchlist,
    handleAccountSubmit,
  } = useStocks()
  return (
    <>
{/* Remove Watchlist Dialog */}
<Dialog open={!!removeWatchStock} onOpenChange={(open) => { if (!open) setRemoveWatchStock(null) }}>
  <DialogContent className="max-w-md">
    <DialogHeader>
      <DialogTitle>删除股票</DialogTitle>
      <DialogDescription>删除后将从系统中移除该股票及其关注配置</DialogDescription>
    </DialogHeader>
    {removeWatchStock && (
      <div className="space-y-4 mt-2">
        <div className="rounded-md border border-border/40 bg-accent/20 p-3">
          <div className="text-[13px] font-semibold text-foreground">
            {removeWatchStock.name}
            <span className="ml-2 font-mono text-[12px] text-muted-foreground">{removeWatchStock.symbol}</span>
          </div>
          <div className="mt-1 text-[12px] text-muted-foreground">
            {hasAnyPositionForStockId(removeWatchStock.id)
              ? '该股票存在持仓，不能直接删除。请先在“持仓”Tab 删除持仓记录。'
              : '删除后将不再出现在关注列表，同时会清理该股票关联的价格提醒。'}
          </div>
        </div>

        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={() => setRemoveWatchStock(null)} disabled={removingWatchStock}>取消</Button>
          <Button
            variant="destructive"
            onClick={() => removeFromWatchlist(removeWatchStock)}
            disabled={removingWatchStock || hasAnyPositionForStockId(removeWatchStock.id)}
          >
            {hasAnyPositionForStockId(removeWatchStock.id) ? '请先删除持仓' : (removingWatchStock ? '处理中…' : '删除股票')}
          </Button>
        </div>
      </div>
    )}
  </DialogContent>
</Dialog>

{/* Account Dialog */}
<Dialog open={accountDialogOpen} onOpenChange={setAccountDialogOpen}>
  <DialogContent>
    <DialogHeader>
      <DialogTitle>{editAccountId ? '编辑账户' : '添加账户'}</DialogTitle>
      <DialogDescription>设置交易账户信息</DialogDescription>
    </DialogHeader>
    <div className="space-y-4 mt-2">
      <div>
        <Label>账户名称</Label>
        <Input
          value={accountForm.name}
          onChange={e => setAccountForm({ ...accountForm, name: e.target.value })}
          placeholder="如：招商证券、华泰证券"
        />
      </div>
      <div>
        <Label>可用资金（元）</Label>
        <Input
          value={accountForm.available_funds}
          onChange={e => setAccountForm({ ...accountForm, available_funds: e.target.value })}
          placeholder="0"
          className="font-mono"
          inputMode="decimal"
        />
      </div>
      <div className="flex justify-end gap-2 pt-2">
        <Button variant="ghost" onClick={() => setAccountDialogOpen(false)}>取消</Button>
        <Button onClick={handleAccountSubmit} disabled={!accountForm.name}>
          {editAccountId ? '保存' : '创建'}
        </Button>
      </div>
    </div>
  </DialogContent>
</Dialog>
    </>
  )
}
